"""Main application window."""
from __future__ import annotations

import contextlib
import io
import json
import logging
import re
import sys
import traceback
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from . import accounts as acctmod
from . import alerts as alertmod
from . import backtest as bt
from . import data as datamod
from . import icons
from . import scriptmode
from . import theme
from .chart import INDICATOR_KEYS, STYLES, DateAxis
from .drawings import TOOL_LABELS
from .editor import CodeEditor
from .panel import ChartPanel
from .watchlist import DEFAULT_WATCHLIST, WatchlistWidget

log = logging.getLogger("chartlab")

FALLBACK_STRATEGY = '''def strategy(df):
    fast = ta.sma(df["Close"], 20)
    slow = ta.sma(df["Close"], 50)
    return {"buy": ta.crossover(fast, slow), "sell": ta.crossunder(fast, slow),
            "plot_SMA 20": fast, "plot_SMA 50": slow}
'''

INTERVALS = [("1m", "1m"), ("3m", "3m"), ("5m", "5m"), ("15m", "15m"), ("30m", "30m"), ("1h", "1h"),
             ("1d", "1D"), ("1wk", "1W"), ("1mo", "1M")]
RANGES = ["5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "max"]
from . import studies as STUDY
INDICATORS = {k: STUDY.STUDIES[k].legend(STUDY.defaults(k)) + ("  —  separate pane" if STUDY.STUDIES[k].pane else "")
              for k in STUDY.KEYS}
LAYOUTS = {"1": "Single chart", "2h": "Two charts — side by side", "2v": "Two charts — stacked",
           "4": "Four charts"}
LAYOUT_COUNT = {"1": 1, "2h": 2, "2v": 2, "4": 4}
DRAW_BUTTONS = [("trend", "╱"), ("ray", "↗"), ("hline", "—"), ("vline", "|"),
                ("rect", "▭"), ("fib", "φ"), ("measure", "m"), ("text", "T"),
                ("hray", "→"), ("crossline", "+")]
STYLE_NAMES = {"candles": "Candles", "hollow": "Hollow candles", "bars": "Bars", "heikin": "Heikin Ashi", "line": "Line",
               "linemark": "Line with markers", "step": "Step line", "area": "Area", "baseline": "Baseline"}


def _safe_key(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text)


class BacktestWorker(QtCore.QThread):
    """Runs the user's strategy + the backtest off the GUI thread."""
    finished_ok = QtCore.Signal(object, object, str, float)
    failed = QtCore.Signal(str, str)

    def __init__(self, code, df, cfg, parent=None):
        super().__init__(parent)
        self.code, self.df, self.cfg = code, df, cfg

    def run(self):
        import time as _t
        buf = io.StringIO()
        t0 = _t.perf_counter()
        try:
            sig = bt.run_user_strategy(self.code, self.df, out=buf)
            res = bt.run_backtest(self.df, sig, self.cfg)
        except Exception:  # noqa: BLE001 - user code
            self.failed.emit(traceback.format_exc(), buf.getvalue())
            return
        self.finished_ok.emit(sig, res, buf.getvalue(), _t.perf_counter() - t0)


class MainWindow(QtWidgets.QMainWindow):
    updProgress = QtCore.Signal(int, int)

    def __init__(self, settings: QtCore.QSettings | None = None):
        super().__init__()
        from . import __version__
        self.version = __version__
        self.setWindowTitle(f"ChartLab {__version__}")      # so it is always clear which build is running
        self.setUnifiedTitleAndToolBarOnMac(True)
        self.settings = settings or QtCore.QSettings("ChartLab", "ChartLab")
        theme.check_icon_path()                       # tick image used by checkboxes in the style sheet
        from . import settings as _cs
        _cs.load(self.settings)
        self.layout_name = "1"
        self.panels: list[ChartPanel] = []
        self.active: ChartPanel | None = None
        self.api = None
        self._syncing = False

        self.alerts = alertmod.AlertManager(self.settings)
        self.poller = alertmod.Poller(self.alerts)
        self.alerts.triggered.connect(self._alert_fired)
        self.alerts.changed.connect(self._refresh_alert_lines)
        self._build_central()
        self._build_toolbar()
        self._build_draw_toolbar()
        self._build_watchlist_dock()
        self._build_strategy_dock()
        self._build_alerts_dock()
        self._build_data_dock()
        self._build_chain_dock()
        self._build_results_dock()
        self._build_trading()
        self._build_right_rail()
        self._build_menu()
        hm = QtWidgets.QMenu(self)
        for act in self.menuBar().actions():
            if act.menu():
                hm.addMenu(act.menu())
        self.menu_btn.setMenu(hm)
        # TradingView has no status bar: messages show as a small toast over the chart instead, and the live-feed /
        # Claude-link labels sit in the bottom bar next to the clock
        self.setStatusBar(QtWidgets.QStatusBar())
        self.statusBar().hide()
        self.toast = QtWidgets.QLabel(self)
        self.toast.setObjectName("toast")
        self.toast.setStyleSheet("QLabel#toast { background: #1e1e1e; color: #dbdbdb; border: 1px solid #2e2e2e;"
                                 " border-radius: 6px; padding: 7px 14px; font-size: 13px; }")
        self.toast.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.toast.hide()
        self.statusBar().messageChanged.connect(self._show_toast)
        self._toast_timer = QtCore.QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self.toast.hide)
        about = QtGui.QAction("About ChartLab", self)
        about.setMenuRole(QtGui.QAction.MenuRole.AboutRole)
        about.triggered.connect(self.show_about)
        self.addAction(about)
        self._about_action = about
        self._shortcuts()
        QtWidgets.QApplication.instance().installEventFilter(self)
        self.apply_chart_settings()
        self._restore()

    # ------------------------------------------------------------------ construction
    def _build_central(self):
        self.central = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(self.central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.area = QtWidgets.QWidget()
        self.area_lay = QtWidgets.QVBoxLayout(self.area)
        self.area_lay.setContentsMargins(0, 0, 0, 0)
        self.area_lay.setSpacing(0)
        self.split_root = None
        outer.addWidget(self.area, 1)
        outer.addWidget(self._build_strip())
        self.setCentralWidget(self.central)
        for i in range(4):
            p = ChartPanel(i)
            p.activated.connect(self.set_active)
            p.loadStarted.connect(self._on_load_started)
            p.loadFinished.connect(self._on_loaded)
            p.loadFailed.connect(self._on_load_failed)
            p.refreshed.connect(self._on_refreshed)
            p.drawingsChanged.connect(self._save_drawings)
            p.toolChanged.connect(lambda panel, tool: self._tool_changed(tool))
            p.chart.replayChanged.connect(lambda: self.replay_btn.setChecked(self.active.chart.replay['active']))
            p.chart.alertRequested.connect(lambda price, pp=p: self.create_alert(price, pp))
            p.chart.tradeRequested.connect(self._on_trade)
            p.chart.orderCancelRequested.connect(lambda o: o is not None and self.trader.cancel(o, self))
            p.chart.orderMoveRequested.connect(self._order_moved)
            p.chart.orderAtPrice.connect(self._order_at_price)
            p.chart.positionCloseRequested.connect(lambda x: x is not None and self.trader.close_position(x, self))
            p.chart.settingsRequested.connect(self.open_settings)
            p.chart.gotoDateRequested.connect(self._goto_date)
            p.chart.indicatorsChanged.connect(lambda pp=p: self._ind_params_changed(pp))
            p.chart.indicatorToggled.connect(lambda key, on, pp=p: self._ind_toggled_on_chart(pp, key, on))
            try:
                saved = json.loads(self.settings.value("ind_params", "") or "{}")
                for k, v in saved.items():
                    if k in p.chart.ind_params and isinstance(v, dict):
                        p.chart.ind_params[k].update(v)
            except Exception:  # noqa: BLE001 - a broken saved value only means: defaults
                pass
            p.chart.typed.connect(self._chart_typed)
            p.chart.barHovered.connect(lambda i, p=p: self._update_data_window(p, i))
            self.panels.append(p)
        self.active = self.panels[0]
        self.active.set_active(True)

    def _quiet_status(self, text: str):
        """A routine status line (loading, bar count): kept for the API / tests, never shown as a toast."""
        self._quiet = True
        try:
            self.statusBar().showMessage(text)
        finally:
            self._quiet = False

    def _show_toast(self, text: str):
        t = self.toast
        if not text or getattr(self, "_quiet", False):
            t.hide()
            return
        t.setText(text)
        t.adjustSize()
        t.setMaximumWidth(max(200, self.width() - 160))
        y = self.height() - t.height() - 56                   # just above the bottom bar, centred
        t.move(max(8, (self.width() - t.width()) // 2), max(8, y))
        t.show()
        t.raise_()
        self._toast_timer.start(4000)                         # a toast never stays on screen

    def _build_strip(self):
        """TradingView's bottom bar: 1D 5D 1M 3M 6M YTD 1Y 5Y All  ...  clock."""
        bar = QtWidgets.QFrame()
        bar.setObjectName("stripbar")
        h = QtWidgets.QHBoxLayout(bar)
        h.setContentsMargins(8, 2, 10, 2)
        h.setSpacing(0)
        for key, text in (("1d", "1D"), ("5d", "5D"), ("1mo", "1M"), ("3mo", "3M"), ("6mo", "6M"),
                          ("ytd", "YTD"), ("1y", "1Y"), ("5y", "5Y"), ("all", "All")):
            b = QtWidgets.QToolButton()
            b.setText(text)
            b.setToolTip(f"Show the last {text}" if key != "all" else "Show all data")
            b.clicked.connect(lambda _=False, k=key: self.active.chart.set_range_preset(k))
            h.addWidget(b)
        gd = QtWidgets.QToolButton()
        gd.setIcon(icons.get("calendar"))
        gd.setToolTip("Go to date")
        gd.clicked.connect(self._goto_date)
        h.addWidget(gd)
        h.addStretch(1)
        self.live_label = QtWidgets.QLabel("")                # live feed status, next to the clock
        self.live_label.hide()
        self.api_label = QtWidgets.QLabel("")                 # Claude link: TradingView has no such text here -
        self.api_label.hide()                                 # its state is in the clock's tooltip instead
        for lab in (self.live_label, self.api_label):
            lab.setStyleSheet("color:#b8b8b8; font-size:12px; padding-right:10px;")
            h.addWidget(lab)
        self.clock = QtWidgets.QLabel("")
        self.clock.setStyleSheet("color:#b8b8b8; font-size:13px; padding-right:10px;")
        h.addWidget(self.clock)
        sep = QtWidgets.QFrame()
        sep.setObjectName("vsep")
        sep.setFixedSize(1, 18)
        h.addWidget(sep)
        adj = QtWidgets.QLabel("  ADJ")
        adj.setToolTip("Prices are adjusted for splits / dividends")
        adj.setStyleSheet("color:#b8b8b8; font-size:13px;")
        h.addWidget(adj)
        self._clock_timer = QtCore.QTimer(self)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start(1000)
        self._tick_clock()
        return bar

    @staticmethod
    def _tv_interval(text: str) -> str:
        return {"1D": "D", "1W": "W", "1M": "M"}.get(text, text[:-1] if text.endswith("m") else text)

    def leftbar_mode(self, action):
        """Popup mode of a drawing-toolbar button (TradingView: icon = tool, narrow strip on its right = list)."""
        for tb in self.findChildren(QtWidgets.QToolBar):
            if tb.objectName() == "leftrail":
                w_ = tb.widgetForAction(action)
                return w_.popupMode() if w_ is not None else None
        return None

    def _sync_interval_label(self):
        """TradingView: with favourite intervals the toolbar shows them plus a chevron that opens the full list;
        without any (its default) one button with the current interval ('D', '5m') opens the list."""
        if getattr(self, "fav_intervals", None):
            self.interval_btn.setText("\u2304")
            self.interval_btn.setMinimumWidth(0)
        else:
            cur = next((v for v, b in self.interval_btns.items() if b.isChecked()), "1d")
            self.interval_btn.setText(self.interval_btns[cur].text())
            f = QtGui.QFont(self.interval_btn.font())
            f.setPixelSize(14)
            self.interval_btn.setMinimumWidth(max(37, QtGui.QFontMetrics(f).horizontalAdvance(
                self.interval_btn.text()) + 22))            # TradingView: 'D' is a 37 px button

    # ---- favourite intervals (TradingView: a star in the interval list puts it on the toolbar)
    DEFAULT_FAV_INTERVALS: list = []                     # TradingView's default: none, only the current interval

    # ---- any interval (TradingView's "Add custom interval"): 45m, 2h, 4h, 3D, 2W, 3M ...
    def _load_custom_intervals(self) -> list:
        import json as _json
        from . import timeframes as TF
        try:
            raw = _json.loads(self.settings.value("custom_intervals", "") or "[]")
        except Exception:  # noqa: BLE001
            raw = []
        base = [v for v, _ in INTERVALS]
        return sorted({str(v) for v in raw if TF.valid(str(v)) and str(v) not in base}, key=TF.sort_key)

    def _all_intervals(self) -> list:
        from . import timeframes as TF
        return sorted([v for v, _ in INTERVALS] + list(getattr(self, "custom_intervals", [])), key=TF.sort_key)

    def _make_interval_button(self, val: str):
        from . import timeframes as TF
        if val in self.interval_btns:
            return self.interval_btns[val]
        b = QtWidgets.QToolButton(self)
        b.setText(TF.short(val))                              # favourites row: 1m 5m 45m 2h D W 3M
        b.setCheckable(True)
        self.interval_group.addButton(b)
        self.interval_btns[val] = b
        b.clicked.connect(lambda _=False, v=val: self._interval_clicked(v))
        b.setObjectName("ivbtn")
        f = b.font()
        f.setPixelSize(14)
        f.setWeight(QtGui.QFont.Weight.DemiBold)
        b.setMinimumWidth(QtGui.QFontMetrics(f).horizontalAdvance(b.text()) + 13)    # never elide to "..."
        b.setToolTip(TF.name(val))
        if self._iv_anchor is None:
            self.interval_btn_actions[val] = self._top_tb.addWidget(b)
        else:                                                 # added later: next to the other intervals
            self.interval_btn_actions[val] = self._top_tb.insertWidget(self._iv_anchor, b)
            self.interval_btn_actions[val].setVisible(False)
        return b

    def _build_interval_menu(self):
        from . import timeframes as TF
        im = self.interval_menu
        im.clear()
        self.interval_menu_actions = {}
        self.interval_stars = {}
        add = im.addAction(icons.get("plus") if "plus" in getattr(icons, "_DRAWERS", {}) else QtGui.QIcon(),
                           "Add custom interval\u2026")
        add.triggered.connect(self.add_custom_interval)
        codes = self._all_intervals()
        for title in ("MINUTES", "HOURS", "DAYS"):
            items = [c for c in codes if TF.group(c) == title]
            if not items:
                continue
            im.addSeparator()
            theme.menu_section(im, title)
            for val in items:
                self.interval_menu_actions[val] = self._interval_row(im, val, TF.name(val),
                                                                     removable=val in self.custom_intervals)

    def ensure_interval(self, code: str, save: bool = True) -> bool:
        """Make `code` (any valid interval) pickable: a custom one is added to the list, like TradingView."""
        import json as _json
        from . import timeframes as TF
        if code in self.interval_btns:
            return True
        if not TF.valid(code):
            return False
        self.custom_intervals = sorted(set(self.custom_intervals) | {code}, key=TF.sort_key)
        if save:
            self.settings.setValue("custom_intervals", _json.dumps(self.custom_intervals))
        self._make_interval_button(code)
        if hasattr(self, "interval_menu"):
            self._build_interval_menu()
        return True

    def remove_custom_interval(self, code: str):
        import json as _json
        if code not in self.custom_intervals:
            return
        if self.interval_btns[code].isChecked():
            self.interval_btns["1d"].click()
        self.custom_intervals = [c for c in self.custom_intervals if c != code]
        self.settings.setValue("custom_intervals", _json.dumps(self.custom_intervals))
        if code in self.fav_intervals:
            self.toggle_fav_interval(code, False)
        b = self.interval_btns.pop(code)
        act = self.interval_btn_actions.pop(code)
        self.interval_group.removeButton(b)
        self._top_tb.removeAction(act)
        b.deleteLater()
        self._build_interval_menu()
        self._apply_fav_intervals()

    def add_custom_interval(self):
        from .tvui import CustomInterval
        d = CustomInterval(self)
        if d.exec() and d.code and self.ensure_interval(d.code):
            self.interval_btns[d.code].click()
            self._sync_interval_label()
        self.focus_chart()

    def _load_fav_intervals(self) -> list:
        import json as _json
        try:
            raw = self.settings.value("fav_intervals", "")
            favs = _json.loads(raw) if raw else list(self.DEFAULT_FAV_INTERVALS)
        except Exception:  # noqa: BLE001
            favs = list(self.DEFAULT_FAV_INTERVALS)
        return [v for v in self._all_intervals() if v in favs]

    def _apply_fav_intervals(self):
        """Show the favourite intervals and, like TradingView, the current one even when it is not a favourite."""
        acts = getattr(self, "interval_btn_actions", {})
        favs = self.fav_intervals
        for v, a in acts.items():
            a.setVisible(v in favs or (bool(favs) and self.interval_btns[v].isChecked()))
        if hasattr(self, "interval_btn"):
            self._sync_interval_label()
        if hasattr(self, "run_btn"):
            self._fit_topbar()

    def toggle_fav_interval(self, val: str, on: bool | None = None):
        import json as _json
        on = (val not in self.fav_intervals) if on is None else bool(on)
        favs = set(self.fav_intervals)
        favs.add(val) if on else favs.discard(val)
        self.fav_intervals = [v for v in self._all_intervals() if v in favs]
        self.settings.setValue("fav_intervals", _json.dumps(self.fav_intervals))
        self._apply_fav_intervals()
        self._sync_interval_menu()

    def _interval_row(self, menu, val: str, text: str, removable: bool = False):
        """One line of the interval list: the name picks the interval, the star on the right adds it to / removes
        it from the toolbar (TradingView's favourites)."""
        row = QtWidgets.QWidget()
        row.setObjectName("ivrow")
        row.setStyleSheet("QWidget#ivrow { background: transparent; border-radius: 0; }"
                          " QWidget#ivrow:hover { background: #2e2e2e; }"
                          " QPushButton { background: transparent; border: 0; color: #dbdbdb; text-align: left;"
                          " padding: 7px 8px 7px 16px; font-size: 13px; }"
                          " QPushButton:hover { background: transparent; }"
                          " QToolButton { background: transparent; border: 0; color: #6a6a6a; font-size: 15px;"
                          " padding: 0 12px 0 6px; } QToolButton:hover { color: #dbdbdb; background: transparent; }"
                          " QToolButton:checked { color: #f5a623; }")
        row.setAttribute(QtCore.Qt.WidgetAttribute.WA_Hover)
        h = QtWidgets.QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        name = QtWidgets.QPushButton(text)
        name.setMinimumWidth(150)
        name.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        star = QtWidgets.QToolButton()
        star.setCheckable(True)
        star.setToolTip("Add to favourites (shown on the toolbar)")
        star.setText("\u2605")
        h.addWidget(name, 1)
        if removable:                                         # a custom interval can be taken off the list again
            rm = QtWidgets.QToolButton()
            rm.setText("\u2715")
            rm.setToolTip("Remove this custom interval")
            rm.setStyleSheet("font-size: 12px; padding: 0 4px;")
            rm.clicked.connect(lambda _=False, v=val: (menu.close(), self.remove_custom_interval(v)))
            h.addWidget(rm)
        h.addWidget(star)
        act = QtWidgets.QWidgetAction(menu)
        act.setDefaultWidget(row)
        menu.addAction(act)

        def pick():
            menu.close()
            self.interval_btns[val].click()
        name.clicked.connect(pick)
        star.clicked.connect(lambda on, v=val: self.toggle_fav_interval(v, on))
        act.name_btn, act.star = name, star
        self.interval_stars[val] = star
        return act

    def _sync_interval_menu(self):
        for v, act in getattr(self, "interval_menu_actions", {}).items():
            cur = self.interval_btns[v].isChecked()
            f = act.name_btn.font()
            f.setWeight(QtGui.QFont.Weight.DemiBold if cur else QtGui.QFont.Weight.Normal)
            act.name_btn.setFont(f)
            act.name_btn.setStyleSheet("color: #ffffff;" if cur else "")
            act.star.setChecked(v in self.fav_intervals)

    def _zoom_x(self, f):
        vb = self.active.chart.price.vb
        a, b = vb.viewRange()[0]
        c = b - (b - a) * 0.15
        vb.setXRange(c - (c - a) * f, c + (b - c) * f, padding=0)

    def _on_trade(self, side, price):
        """Buy / Sell on the chart: the order panel, ready for that side at the current price."""
        self.open_order_ticket(side, price)

    # ------------------------------------------------------------------ trading
    def _build_trading(self):
        from .trading import TradingManager, TradingPanel
        self.trader = TradingManager(self)
        self.trade_panel = TradingPanel(self.trader)
        self.trade_dock = QtWidgets.QDockWidget("Trading Panel", self)
        self.trade_dock.setObjectName("trade_dock")
        self.trade_dock.setWidget(self.trade_panel)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, self.trade_dock)
        self.tabifyDockWidget(self.results_dock, self.trade_dock)
        self.trade_dock.hide()
        self.trade_dock.visibilityChanged.connect(lambda on: on and self.trader.refresh())
        self.live.tickPrice.connect(self.trader.on_price)
        self.live.tickPrice.connect(self._ticket_price)
        self.live.feed.order.connect(lambda _d: self.trader.refresh())
        self.live.wanted_symbols = self._trade_symbols
        self.trader.updated.connect(self._update_trade_lines)
        self.trader.instrumentReady.connect(lambda _s: self._update_trade_lines())
        self.trader.notify.connect(self._trade_note)
        self.trader.accountChanged.connect(self.live_sync)

    def show_trading_panel(self):
        self.trade_dock.show()
        self.trade_dock.raise_()
        self.trader.refresh()

    def open_order_ticket(self, side: str = "BUY", price: float | None = None, order_type: str = "MARKET",
                          symbol: str | None = None, at_price: float | None = None):
        from .trading import OrderTicket
        p = self.active
        sym = symbol or self.live._norm(p.params.get("symbol", "")) if p is not None else (symbol or "")
        if not sym or (p.df is None and not symbol):
            self.statusBar().showMessage("Open a chart first: the order is for the symbol on the chart", 5000)
            return
        last = float(price or 0) or (float(p.df["Close"].iloc[-1]) if p.df is not None and len(p.df) else 0.0)
        if last:
            self.trader.on_price(sym, last)
        dlg = OrderTicket(self.trader, sym, side, last, order_type, self)
        if at_price:
            dlg.price.setValue(at_price)
            dlg.trig.setValue(at_price)
        self._ticket = dlg
        try:
            dlg.exec()
        finally:
            self._ticket = None

    def _ticket_price(self, sym, px):
        t = getattr(self, "_ticket", None)
        if t is not None and t.symbol == sym:
            t.set_last(px)

    def _trade_symbols(self):
        """Symbols the live feed should follow for trading even without a chart (paper orders need prices)."""
        s = self.trader.snap
        out = set()
        if self.trader.broker().id == "paper":
            out |= {o.token for o in s["orders"] if o.is_open}
            out |= {p.token for p in s["positions"] if p.qty}
        return sorted(out)

    def _update_trade_lines(self):
        if not hasattr(self, "trader"):
            return
        s = self.trader.snap
        b = self.trader.broker()
        movable = "modify" in b.caps
        show = self.settings.value("trade_lines", "true") == "true"
        for p in getattr(self, "panels", []):
            items = []
            sym = self.live._norm(p.params.get("symbol", "")) if p.df is not None else ""
            tok = self.trader.token_of(sym) if (sym and show) else ""
            if tok:
                for o in s["orders"]:
                    if o.is_open and str(o.token) == tok:
                        px = o.price if (o.order_type in ("LIMIT", "SL") and o.price) else o.trigger
                        if px:
                            items.append({"kind": "order", "id": o.id, "price": px, "qty": o.pending or o.qty,
                                          "side": o.side, "type": o.order_type, "obj": o, "movable": movable})
                for po in s["positions"]:
                    if po.qty and str(po.token) == tok and po.avg:
                        items.append({"kind": "position", "id": "pos:" + po.product, "price": po.avg, "qty": po.qty,
                                      "pnl": po.unrealised, "obj": po})
            p.chart.set_trade_lines(items)

    def _order_at_cursor(self, side: str, direct: bool = True):
        """Alt+Shift+B / Alt+Shift+S / Shift+T: an order at the price under the crosshair (TradingView)."""
        ch = self.active.chart
        if ch.df is None or not len(ch.df):
            return
        hl = ch.price.hline
        px = float(ch.iy(hl.value())) if hl.isVisible() else float(ch.df["Close"].iloc[-1])
        last = float(ch.df["Close"].iloc[-1])
        below = px < last
        typ = ("LIMIT" if below else "SL-M") if side == "BUY" else ("SL-M" if below else "LIMIT")
        self._order_at_price(side, typ, px, direct)

    def _order_at_price(self, side: str, order_type: str, price: float, direct: bool):
        """The chart menu's Buy / Sell at a price: one share (one lot for F&O) at once, like TradingView; 'Add order'
        opens the order panel at that price instead."""
        from .brokers import OrderRequest
        p = self.active
        sym = self.live._norm(p.params.get("symbol", "")) if p is not None else ""
        if not sym or p.df is None:
            return
        last = float(p.df["Close"].iloc[-1])
        self.trader.on_price(sym, last)
        if not direct:
            self.open_order_ticket(side, last, order_type, at_price=price)
            return
        inst = self.trader.instrument(sym)
        lots = 1 if inst is not None and inst.derivative and inst.lot > 1 else 0
        req = OrderRequest(sym, side, qty=0 if lots else 1, lots=lots, order_type=order_type,
                           price=round(price, 2) if order_type in ("LIMIT", "SL") else 0.0,
                           trigger=round(price, 2) if order_type in ("SL", "SL-M") else 0.0, instrument=inst)
        self.trader.place(req, self, inst)

    def _order_moved(self, order, price):
        if order is None:
            return
        if order.order_type in ("LIMIT", "SL") and order.price:
            self.trader.modify(order, self, price=round(price, 2))
        else:
            self.trader.modify(order, self, trigger=round(price, 2))

    def _trade_note(self, text, level):
        if level in ("ok", "error", "warn"):
            col = {"ok": theme.UP, "error": theme.DOWN, "warn": "#ff9800"}[level]
            title = {"ok": "Order", "error": "Order problem", "warn": "Check your order book"}[level]
            alertmod.Toast(self, title, text, col)

    def _goto_date(self):
        from .tvui import GoTo
        import pandas as _pd
        ch = self.active.chart
        if not ch.has_data():
            return
        d = GoTo(self)
        if not d.exec():
            return

        def idx(qd, qt=None):
            t = _pd.Timestamp(qd.year(), qd.month(), qd.day())
            if qt is not None:
                t += _pd.Timedelta(hours=qt.hour(), minutes=qt.minute())
            if ch.df.index.tz is not None:
                t = t.tz_localize(ch.df.index.tz)
            return max(0, min(int(ch.df.index.searchsorted(t)), len(ch.df) - 1))
        if d.tabs.currentIndex() == 0:
            i = idx(d.date.date(), d.time.time())
            ch.price.vb.setXRange(i - 60, i + 60, padding=0)
        else:
            i0, i1 = idx(d.d0.date()), idx(d.d1.date())
            ch.price.vb.setXRange(i0 - 2, max(i1, i0 + 5) + 2, padding=0)

    def open_symbol_search(self, text: str = ""):
        from .tvui import SymbolSearch
        d = SymbolSearch(self, self.watchlist.symbols() + list(self._symbol_model.stringList()), text)
        if d.exec() and d.chosen:
            self.symbol.setText(d.chosen)
            self.on_load()
        self.focus_chart()

    def open_interval_popup(self, first: str):
        from .tvui import IntervalPopup
        d = IntervalPopup(self, first)
        if d.exec() and d.code and self.ensure_interval(d.code):
            self.interval_btns[d.code].click()
            self._sync_interval_label()
        self.focus_chart()

    def _tick_clock(self):
        import datetime as _dt
        now = _dt.datetime.now().astimezone()
        off = now.utcoffset().total_seconds() / 3600
        sign = "+" if off >= 0 else "-"
        hh, mm = int(abs(off)), int(round((abs(off) % 1) * 60))
        zone = "UTC" if not (hh or mm) else f"UTC{sign}{hh}" + (f":{mm:02d}" if mm else "")   # TradingView: "UTC"
        self.clock.setText(f"{now:%H:%M:%S} {zone}")

    def _build_toolbar(self):
        tb = self.addToolBar("Data")
        tb.setObjectName("topbar")
        tb.setMovable(False)
        tb.setIconSize(QtCore.QSize(28, 28))                  # TradingView: 28 px icons in 38 px buttons
        self.topbar = tb

        tb.setContentsMargins(0, 0, 0, 0)
        if tb.layout() is not None:
            tb.layout().setContentsMargins(0, 0, 0, 0)
            tb.layout().setSpacing(0)                         # TradingView's buttons touch (gaps are explicit)

        def vsep():                                           # TradingView: 9 px, a 1 x 22 line in the middle
            s = QtWidgets.QFrame()
            s.setObjectName("vsep")
            s.setFixedSize(1, 22)
            w = QtWidgets.QWidget()
            lay = QtWidgets.QHBoxLayout(w)
            lay.setContentsMargins(4, 0, 4, 0)
            lay.addWidget(s)
            tb.addWidget(w)

        # ☰ main menu (TradingView keeps every app menu behind this button, over the 52 px drawing toolbar)
        self.menu_btn = QtWidgets.QToolButton()
        self.menu_btn.setIcon(icons.get("menu"))
        self.menu_btn.setToolTip("Main menu")
        self.menu_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu_btn.setProperty("tvkind", "menu")
        tb.addWidget(self.menu_btn)
        tb.addWidget(self._gap(4))

        # symbol pill: a button that opens Symbol Search (typing on the chart does the same)
        self.symbol = QtWidgets.QLineEdit()                 # state holder (also used by the API / tests)
        self.symbol.setObjectName("symbolbox")
        self.symbol.returnPressed.connect(self.on_load)
        self.symbol.setVisible(False)
        self._symbol_model = QtCore.QStringListModel(self._suggestions())
        # TradingView's pill (measured): 128 x 28, #303030, radius 14; the symbol 12 px from the left in 14 px / 600
        # #b8b8b8; inside it on the right a 32 x 28 'Switch data type' button with an 18 px icon
        pill = QtWidgets.QFrame()
        pill.setObjectName("symbolpill")
        pill.setFixedSize(128, 28)
        ph = QtWidgets.QHBoxLayout(pill)
        ph.setContentsMargins(0, 0, 0, 0)
        ph.setSpacing(0)
        self.sym_btn = QtWidgets.QPushButton()
        self.sym_btn.setObjectName("symbtn")
        self.sym_btn.setFixedHeight(28)
        self.sym_btn.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.sym_btn.setToolTip("Symbol Search  (just start typing on the chart)")
        self.sym_btn.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.sym_btn.clicked.connect(lambda: self.open_symbol_search(""))
        ph.addWidget(self.sym_btn, 1)
        ph.addWidget(self.symbol)
        self.src_menu = QtWidgets.QMenu(self)                # data source + history (TradingView: data type)
        self.src_menu.aboutToShow.connect(self._fill_src_menu)
        self.dtype_btn = QtWidgets.QToolButton()
        self.dtype_btn.setObjectName("symact")
        self.dtype_btn.setFixedSize(32, 28)
        self.dtype_btn.setIcon(icons.get("datatype", size=18))
        self.dtype_btn.setIconSize(QtCore.QSize(18, 18))
        self.dtype_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.dtype_btn.setMenu(self.src_menu)
        self.dtype_btn.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        ph.addWidget(self.dtype_btn)
        self.symbol.textChanged.connect(lambda t: self._set_pill_text(t.strip().upper() or "Symbol"))
        self._set_pill_text("Symbol")
        tb.addWidget(pill)
        tb.addWidget(self._gap(4))
        cmp_ = QtWidgets.QToolButton()
        cmp_.setIcon(icons.get("compare"))
        cmp_.setToolTip("Add symbol to the watchlist")
        cmp_.clicked.connect(self._add_current_to_watchlist)
        tb.addWidget(cmp_)
        vsep()

        # hidden state holders for the data source and history length (shown via the source button)
        self.source = QtWidgets.QComboBox()
        self.source.addItem("Auto", "auto")           # my data folder -> connected API -> Yahoo
        self.source.addItem("Yahoo", "yahoo")
        self.source.addItem("Local file", "file")
        for name in datamod.list_providers():
            self.source.addItem(f"Provider: {name}", f"provider:{name}")
        self._fill_api_sources()
        self.source.currentIndexChanged.connect(self._source_changed)
        self.range = QtWidgets.QComboBox()
        self.range.addItems(RANGES)
        self.range.setCurrentText("2y")
        for w_ in (self.source, self.range):
            w_.setParent(self)
            w_.hide()

        self.interval_group = QtWidgets.QButtonGroup(self)
        self.interval_btns = {}
        self.interval_btn_actions = {}                        # TradingView: favourite intervals + the current one
        self.fav_intervals = self._load_fav_intervals()
        self._top_tb, self._iv_anchor = tb, None
        self.custom_intervals = self._load_custom_intervals()
        for val in self._all_intervals():
            self._make_interval_button(val)
        self.interval_btns["1d"].setChecked(True)
        self.interval_btn = QtWidgets.QToolButton()
        self.interval_btn.setToolTip("Time interval  (type a number on the chart)")
        self.interval_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        im = QtWidgets.QMenu(self)
        self.interval_menu = im
        self._build_interval_menu()
        im.aboutToShow.connect(self._sync_interval_menu)
        self.interval_btn.setMenu(im)
        self._iv_anchor = tb.addWidget(self.interval_btn)
        self.interval_group.buttonToggled.connect(lambda *_: (self._sync_interval_label(), self._apply_fav_intervals()))
        self._sync_interval_label()
        self._apply_fav_intervals()
        vsep()

        style_menu = QtWidgets.QMenu(self)
        self.style_group = QtGui.QActionGroup(self)
        self.style_actions = {}
        for key in STYLES:
            a = style_menu.addAction(icons.get("st_" + key), STYLE_NAMES[key])
            a.setCheckable(True)
            self.style_group.addAction(a)
            a.triggered.connect(lambda _=False, k=key: self._style_chosen(k))
            self.style_actions[key] = a
            if key in ("heikin", "step"):
                style_menu.addSeparator()
        self.style_actions["candles"].setChecked(True)
        self.style_btn = self._menu_button(tb, "", style_menu, icon="st_candles", tip="Chart type")
        vsep()

        self.ind_menu = QtWidgets.QMenu(self)                # (state holder; the button opens the dialog)
        self.ind_actions = {}
        for key, text in INDICATORS.items():
            a = self.ind_menu.addAction(text)
            a.setCheckable(True)
            a.toggled.connect(lambda on, k=key: self._indicator_toggled(k, on))
            self.ind_actions[key] = a
        self.ind_btn = QtWidgets.QToolButton()
        self.ind_btn.setIcon(icons.get("fx"))
        self.ind_btn.setText("Indicators")
        self.ind_btn.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.ind_btn.setToolTip("Indicators, metrics & strategies  ( / )")
        self.ind_btn.clicked.connect(lambda: self.open_indicators())
        tb.addWidget(self.ind_btn)
        self.ind_fav_btn = QtWidgets.QToolButton()           # TradingView: a 24 px chevron with the quick list
        self.ind_fav_btn.setObjectName("chevbtn")
        self.ind_fav_btn.setIcon(icons.get("chev", size=24))
        self.ind_fav_btn.setIconSize(QtCore.QSize(24, 24))
        self.ind_fav_btn.setToolTip("Indicators on this chart: switch on / off")
        self.ind_fav_btn.setMenu(self.ind_menu)
        self.ind_fav_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        tb.addWidget(self.ind_fav_btn)

        layout_menu = QtWidgets.QMenu(self)
        self.layout_group = QtGui.QActionGroup(self)
        self.layout_actions = {}
        for key, text in LAYOUTS.items():
            a = layout_menu.addAction(icons.get({"1": "layout_icon", "2h": "layout_2h", "2v": "layout_2v",
                                                 "4": "layout_4"}[key]), text)
            a.setCheckable(True)
            self.layout_group.addAction(a)
            a.triggered.connect(lambda _=False, k=key: self.set_layout(k))
            self.layout_actions[key] = a
        self.layout_actions["1"].setChecked(True)
        self._layout_menu = layout_menu                      # (its button sits on the right, like TradingView)
        vsep()

        self.alert_btn = QtWidgets.QToolButton()
        self.alert_btn.setIcon(icons.get("bell"))
        self.alert_btn.setText("Alert")
        self.alert_btn.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.alert_btn.setToolTip(f"Create alert  ({theme.sc('Alt+A')})")
        self.alert_btn.clicked.connect(lambda: self.create_alert())
        tb.addWidget(self.alert_btn)

        self.replay_btn = QtWidgets.QToolButton()
        self.replay_btn.setIcon(icons.get("replay_icon"))
        self.replay_btn.setText("Replay")
        self.replay_btn.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.replay_btn.setCheckable(True)
        self.replay_btn.setToolTip("Bar replay: pick a start bar, then play the market bar by bar")
        self.replay_btn.clicked.connect(self.toggle_replay)
        tb.addWidget(self.replay_btn)
        vsep()

        def tbtn(icon, tip, fn):
            b = QtWidgets.QToolButton()
            b.setIcon(icons.get(icon))
            b.setToolTip(tip)
            b.clicked.connect(fn)
            tb.addWidget(b)
            return b
        tbtn("undo", f"Undo  ({theme.sc('Ctrl+Z')})", lambda: self.active.chart.layer.undo())
        tbtn("redo", f"Redo  ({theme.sc('Ctrl+Shift+Z')})", lambda: self.active.chart.layer.redo())

        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        # data source + history live in the symbol pill's own button (TradingView: 'Switch data type'); this one
        # only keeps the current choice as text (tooltips, older callers)
        self.src_btn = QtWidgets.QToolButton(self)
        self.src_btn.setObjectName("srcbtn")
        self.src_btn.hide()
        self.source.currentIndexChanged.connect(lambda *_: self._sync_src_btn())
        self.range.currentTextChanged.connect(lambda *_: self._sync_src_btn())
        self._sync_src_btn()

        # TradingView's right side: Layout setup | Quick search, Settings, Fullscreen, Snapshot, then the Trade and
        # Upgrade pills (here: Accounts and Run)
        self.layout_btn = self._menu_button(tb, "", self._layout_menu, icon="layout_icon", tip="Chart layout")
        vsep()
        tbtn("search", f"Quick search  ({theme.sc('Ctrl+K')})", self.quick_search)
        tbtn("gear", "Settings", self.open_settings)
        tbtn("fullscreen", f"Full screen  ({theme.sc('Shift+F')})", self.toggle_fullscreen)
        tbtn("camera", "Save chart image", self.save_chart_image)
        tb.addWidget(self._gap(4))

        self.acct_btn = QtWidgets.QToolButton()
        self.acct_btn.setObjectName("tradepill")              # styled like TradingView's "Trade" pill
        self.acct_btn.setFixedHeight(28)
        self.acct_btn.setIconSize(QtCore.QSize(10, 10))        # just the status dot (not a 28 px toolbar icon)
        self.acct_btn.setText("Accounts")
        self.acct_btn.setToolTip("Broker accounts: API data + run one script on many accounts")
        m_ = QtWidgets.QMenu(self.acct_btn)
        m_.addAction("Trading Panel", lambda: self.show_trading_panel())
        m_.addAction("New order\u2026", lambda: self.open_order_ticket("BUY"))
        self.trade_with_menu = m_.addMenu("Trade with")
        self.trade_with_menu.aboutToShow.connect(self._fill_trade_with)
        m_.addSeparator()
        m_.addAction("Manage broker accounts…", self.manage_accounts)
        m_.addAction("All accounts: status, balance, P&&L…", self.accounts_overview)
        ar = m_.addAction("Keep broker logins fresh automatically")
        ar.setCheckable(True)
        ar.setChecked(self.settings.value("auto_renew", "true") == "true")
        ar.setToolTip("Before a login runs out (and after it has), ChartLab gets a fresh one by itself: the broker's own "
                      "renewal, or PIN + the saved TOTP key (Dhan, Angel One). Zerodha and Upstox want a browser login daily.")
        self._auto_renew_action = ar
        ar.triggered.connect(self.set_auto_renew)
        m_.setToolTipsVisible(True)
        m_.addSeparator()
        m_.addAction("Run script on multiple accounts…", self.run_on_accounts)
        self.auto_act = m_.addAction("Auto-trade strategy on accounts…", self.toggle_auto_trade)
        m_.addAction("Stop all runs", self.stop_script)
        self.acct_btn.setMenu(m_)
        self.acct_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.acct_btn.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        holder = QtWidgets.QWidget()                          # (inside a holder: the toolbar would force its 28 px
        hl = QtWidgets.QHBoxLayout(holder)                    # icon size on it and the pill would grow)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(self.acct_btn)
        tb.addWidget(holder)
        self.acct_poller = acctmod.StatusPoller(self)
        self.acct_poller.updated.connect(self._acct_status_changed)
        self._acct_timer = QtCore.QTimer(self)
        self._acct_timer.timeout.connect(lambda: self.acct_poller.refresh())
        self._acct_timer.start(10 * 60 * 1000)
        QtCore.QTimer.singleShot(1500, lambda: self.acct_poller.refresh())
        self._update_acct_dot()
        tb.addWidget(self._gap(8))
        self.load_btn = QtWidgets.QPushButton("Load", self)   # kept for state; loading happens on Enter / search
        self.load_btn.hide()
        self.upd_btn = QtWidgets.QPushButton("Update")       # appears only when a newer build is published
        self.upd_btn.setObjectName("updpill")
        self.upd_btn.setToolTip("Download the new ChartLab and restart into it")
        self.upd_btn.clicked.connect(self.start_update)
        self._upd_action = tb.addWidget(self.upd_btn)
        self._upd_action.setVisible(False)
        self.run_btn = QtWidgets.QPushButton("\u25b6  Run")
        self.run_btn.setObjectName("pill")
        self.run_btn.setFixedHeight(28)                       # TradingView: 28 px pills (Upgrade / Trade)
        self.run_btn.setToolTip("Run the strategy / script  (Cmd+R)")
        self.run_btn.clicked.connect(self.run_backtest)
        tb.addWidget(self.run_btn)
        tb.addWidget(self._gap(4))

    def _set_pill_text(self, text: str):
        """The symbol in the pill, cut with '\u2026' when it does not fit (the pill keeps TradingView's 128 px)."""
        b = getattr(self, "sym_btn", None)
        if b is None:
            return
        f = QtGui.QFont(b.font())
        f.setPixelSize(14)
        f.setWeight(QtGui.QFont.Weight.DemiBold)
        room = 128 - 12 - 32 - 4
        b.setText(QtGui.QFontMetrics(f).elidedText(text, QtCore.Qt.TextElideMode.ElideRight, room))
        b.setToolTip(f"{text} \u2014 Symbol Search  (just start typing on the chart)")

    def _sync_src_btn(self):
        if hasattr(self, "src_btn"):
            name = self.source.currentText().replace("Provider: ", "")
            self._src_full = f"{name} \u00b7 {self.range.currentText()}  \u25be"
            self._src_short = f"{name}  \u25be"
            self.src_btn.setText(self._src_full)
            if hasattr(self, "dtype_btn"):
                self.dtype_btn.setToolTip(f"Data: {name} \u00b7 {self.range.currentText()} history \u2014 change source")
            self._fit_topbar()

    def _fit_topbar(self):
        """Keep every top-bar button on screen on a 13" laptop: when the window is narrow, labels fold away one by
        one (Replay, Alert, history length, Indicators, Run) and only their icons stay - TradingView does the same."""
        tb = getattr(self, "topbar", None)
        if tb is None or not hasattr(self, "run_btn") or getattr(self, "_fitting", False):
            return
        self._fitting = True
        try:
            TB = QtCore.Qt.ToolButtonStyle
            on = self.auto.active if hasattr(self, "auto") else False
            steps = [(self.replay_btn, "Replay"), (self.alert_btn, "Alert"),
                     (self.ind_btn, "Indicators"), ("favs", None), (self.run_btn, "\u25b6  Run"), (self.acct_btn, None)]
            ivs = getattr(self, "interval_btn_actions", {})
            favs = getattr(self, "fav_intervals", [])
            for b, full in steps:                                   # everything at full size first
                if b == "favs":
                    for v, a in ivs.items():
                        a.setVisible(v in favs or (bool(favs) and self.interval_btns[v].isChecked()))
                elif b is self.acct_btn:
                    b.setToolButtonStyle(TB.ToolButtonTextBesideIcon)
                    self._set_folded(b, False)
                elif b is self.run_btn:
                    b.setText(full)
                else:
                    b.setText(full)
                    b.setToolButtonStyle(TB.ToolButtonTextBesideIcon)
            self._tv_sizes()
            for b, _full in steps:
                tb.layout().invalidate()
                if tb.sizeHint().width() <= self.width() - 4:
                    break
                if b == "favs":                                     # then only the current interval (TradingView)
                    for v, a in ivs.items():
                        a.setVisible(bool(favs) and self.interval_btns[v].isChecked())
                elif b is self.run_btn:
                    b.setText("\u25b6")
                else:
                    b.setToolButtonStyle(TB.ToolButtonIconOnly)
                    if b is self.acct_btn:                          # just the status dot, without the pill outline
                        self._set_folded(b, True)
                self._tv_sizes()
            del on
        finally:
            self._fitting = False

    def _tv_sizes(self):
        """TradingView's top-bar buttons, measured on its page: an icon button is 38 x 38 (28 px icon, 5 px each
        side); icon + text = 5 + 28 + 4 + text + 10 (Alert 77.8, Replay 90.2, Indicators 110.8); a text button =
        13.5 + text + 13.5 ('D' is 37); the pills are 28 tall, 12 px left / right of the text."""
        tb = getattr(self, "topbar", None)
        if tb is None:
            return
        TB = QtCore.Qt.ToolButtonStyle
        f = QtGui.QFont(tb.font())
        f.setPixelSize(14)
        fm = QtGui.QFontMetricsF(f)
        for b in tb.findChildren(QtWidgets.QToolButton):
            name = b.objectName()
            if name.startswith("qt_") or (b.parentWidget() is not None
                                          and b.parentWidget().objectName() == "symbolpill"):
                continue                                            # Qt's own overflow arrow / the pill's button
            txt = b.text().strip()
            if name == "chevbtn":
                kind, w, h = "chev", 24, 38
            elif name == "tradepill":
                if b.toolButtonStyle() == TB.ToolButtonIconOnly:
                    kind, w, h = "icon", 38, 38
                else:                                           # (Qt keeps room for its menu arrow: ask it)
                    b.setMinimumWidth(0)
                    b.setMaximumWidth(16777215)
                    kind, w, h = "pill", max(int(round(12 + 10 + 4 + fm.horizontalAdvance(txt) + 12)),
                                             b.sizeHint().width()), 28
            elif b.property("tvkind") == "menu":
                kind, w, h = "menu", 52, 38
            elif b.toolButtonStyle() == TB.ToolButtonIconOnly or not txt:
                kind, w, h = "icon", 38, 38
            elif b.icon().isNull() or b.toolButtonStyle() == TB.ToolButtonTextOnly:
                kind, w, h = "text", int(round(fm.horizontalAdvance(txt) + 27)), 38
            else:
                kind, w, h = "both", int(round(47 + fm.horizontalAdvance(txt))), 38
            if b.property("tvkind") not in (kind, "menu"):
                b.setProperty("tvkind", kind)
                b.style().unpolish(b)
                b.style().polish(b)
            if b.minimumWidth() != w or b.maximumWidth() != w or b.height() != h:
                b.setFixedSize(w, h)

    @staticmethod
    def _set_folded(b, on: bool):
        if b.property("folded") != on:
            b.setProperty("folded", on)
            b.style().unpolish(b)
            b.style().polish(b)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._fit_topbar()
        if hasattr(self, "toast") and not self.toast.isHidden():    # keep a showing toast above the bottom bar
            t = self.toast
            t.move(max(8, (self.width() - t.width()) // 2), max(8, self.height() - t.height() - 56))

    def _fill_src_menu(self):
        m = self.src_menu
        m.clear()
        theme.menu_section(m, "Data source")
        g = QtGui.QActionGroup(m)
        for i in range(self.source.count()):
            a = m.addAction(self.source.itemText(i))
            a.setCheckable(True)
            a.setChecked(i == self.source.currentIndex())
            g.addAction(a)
            a.triggered.connect(lambda _=False, i=i: (self.source.setCurrentIndex(i), self.on_load()))
        m.addAction("Connect a broker account…", self.manage_accounts)
        m.addSeparator()
        theme.menu_section(m, "Auto update")
        g3 = QtGui.QActionGroup(m)
        cur = int(self.settings.value("auto_refresh", 60))
        for sec, lab in ((0, "Off"), (10, "Every 10 seconds"), (30, "Every 30 seconds"), (60, "Every minute"),
                         (300, "Every 5 minutes")):
            a = m.addAction("Live refresh: " + lab)
            a.setCheckable(True)
            a.setChecked(sec == cur)
            g3.addAction(a)
            a.triggered.connect(lambda _=False, s=sec: self.set_auto_refresh(s))
        a = m.addAction("Real-time prices from Dhan (ticks)")
        a.setCheckable(True)
        a.setChecked(self.settings.value("live_prices", "true") == "true")
        a.triggered.connect(self.set_live_prices)
        a = m.addAction("Download watchlist every day")
        a.setCheckable(True)
        a.setChecked(self.settings.value("daily_download", "true") == "true")
        a.triggered.connect(lambda on: (self.settings.setValue("daily_download", "true" if on else "false"),
                                        self._daily_download() if on else None))
        m.addAction("Refresh now", self.refresh_now)
        m.addAction("Open My data folder\u2026", self.open_my_data)
        m.addSeparator()
        theme.menu_section(m, "History to load")
        g2 = QtGui.QActionGroup(m)
        for r in RANGES:
            a = m.addAction(r)
            a.setCheckable(True)
            a.setChecked(r == self.range.currentText())
            g2.addAction(a)
            a.triggered.connect(lambda _=False, r=r: (self.range.setCurrentText(r), self.on_load()))

    def _watchlist_add_search(self):
        from .tvui import SymbolSearch
        d = SymbolSearch(self, list(self._symbol_model.stringList()), "")
        d.setWindowTitle("Add symbol to watchlist")
        if d.exec() and d.chosen:
            self.watchlist.add(d.chosen)

    def _add_current_to_watchlist(self):
        sym = self.symbol.text().strip().upper()
        if sym and self.watchlist.add(sym):
            self.statusBar().showMessage(f"{sym} added to the watchlist", 3000)
        else:
            self.open_symbol_search("")

    @staticmethod
    def _suggestions() -> list[str]:
        base = ["NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN",
                "ITC", "LT", "BHARTIARTL", "TMPV", "TMCV", "TATASTEEL", "AXISBANK", "KOTAKBANK", "HINDUNILVR",
                "BAJFINANCE", "MARUTI", "SUNPHARMA", "WIPRO", "HCLTECH", "ADANIENT", "ADANIPORTS", "ONGC",
                "NTPC", "POWERGRID", "COALINDIA", "ASIANPAINT", "ULTRACEMCO", "TITAN", "M&M", "JSWSTEEL",
                "TECHM", "NESTLEIND", "DRREDDY", "CIPLA", "EICHERMOT", "HEROMOTOCO", "BPCL", "GRASIM",
                "US:AAPL", "US:MSFT", "US:NVDA", "US:TSLA", "BTC-USD", "ETH-USD", "BSE:TCS", "BSE:SBIN"]
        return base

    @staticmethod
    def _gap(w: int):
        g = QtWidgets.QWidget()
        g.setFixedWidth(w)
        return g

    def _menu_button(self, tb, text, menu, icon=None, tip=""):
        b = QtWidgets.QToolButton()
        if icon:
            b.setIcon(icons.get(icon))
        b.setText(text)
        if text:
            b.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        b.setToolTip(tip)
        b.setMenu(menu)
        b.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        tb.addWidget(b)
        return b

    def _build_draw_toolbar(self):
        tb = QtWidgets.QToolBar("Drawing tools")
        tb.setObjectName("leftrail")
        tb.setOrientation(QtCore.Qt.Orientation.Vertical)
        tb.setMovable(False)
        tb.setFixedWidth(56)                                  # TradingView: 52 px of buttons + the 4 px gutter
        tb.setIconSize(QtCore.QSize(28, 28))                  # TradingView: 52 x 38 buttons, 28 px icons
        self.addToolBar(QtCore.Qt.ToolBarArea.LeftToolBarArea, tb)
        self.draw_group = QtGui.QActionGroup(self)
        self.draw_actions = {}
        cur = tb.addAction(icons.get("cross"), "")
        cur.setCheckable(True)
        cur.setChecked(True)
        cur.setToolTip("Cursor / select  (Esc)")
        self.draw_group.addAction(cur)
        cur.triggered.connect(lambda: self._tool_clicked(None))
        self.draw_actions[None] = cur
        ICON = {"hray": "ray", "crossline": "cross", "ext": "t_ext", "arrow": "t_arrow", "ellipse": "t_ellipse",
                "pricerange": "t_pricerange", "daterange": "t_daterange", "long": "t_long", "short": "t_short",
                "channel": "t_channel", "brush": "t_brush"}
        SC = {"trend": "Alt+T", "hline": "Alt+H", "hray": "Alt+J", "vline": "Alt+V", "crossline": "Alt+C",
              "fib": "Alt+F", "rect": "Shift+Alt+R"}
        # TradingView's order: cursor, trend tools, Gann & Fibonacci, forecasting, shapes (brush first), text |
        # measure, zoom | magnet, keep drawing, lock, hide | remove
        GROUPS = [("Trend lines", ["trend", "ray", "ext", "hray", "hline", "vline", "crossline", "channel"]),
                  ("Gann and Fibonacci", ["fib"]),
                  ("Forecasting and measurement", ["long", "short", "pricerange", "daterange"]),
                  ("Geometric shapes  (the brush draws freehand)", ["brush", "rect", "ellipse", "arrow"]),
                  ("Text and notes", ["text"]),
                  (None, None),
                  ("Measure", ["measure"])]
        self.tool_groups = {}
        self.tool_actions = {}                                # one action per tool (the group lists)
        for gname, keys in GROUPS:
            if gname is None:
                tb.addSeparator()
                continue
            ga = tb.addAction(icons.get(ICON.get(keys[0], keys[0])), "")
            ga.setCheckable(True)
            ga.setToolTip(gname)
            self.draw_group.addAction(ga)
            for k in keys:
                self.draw_actions[k] = ga
            self.tool_groups[ga] = keys
            ga.setData(keys[0])
            ga.triggered.connect(lambda _=False, ga=ga: self._tool_clicked(ga.data()))
            if len(keys) == 1:
                self.tool_actions[keys[0]] = ga
                continue
            menu = QtWidgets.QMenu(self)
            for k in keys:
                act = menu.addAction(icons.get(ICON.get(k, k)), TOOL_LABELS[k] + (f"\t{theme.sc(SC[k])}" if k in SC else ""))
                act.triggered.connect(lambda _=False, k=k, ga=ga: (ga.setIcon(icons.get(ICON.get(k, k))), ga.setData(k), self._tool_clicked(k)))
                self.tool_actions[k] = act
            ga.setMenu(menu)
            btn = tb.widgetForAction(ga)                      # TradingView: the icon picks the tool, the narrow
            btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.MenuButtonPopup)   # strip on its right lists it
        self._tool_icon = ICON
        zi = tb.addAction(icons.get("zoom"), "")
        zi.setToolTip("Zoom in")
        zi.triggered.connect(lambda: self._zoom_x(0.7))
        tb.addSeparator()
        self.magnet_action = tb.addAction(icons.get("magnet"), "")
        self.magnet_action.setCheckable(True)
        self.magnet_action.setChecked(True)
        self.magnet_action.setToolTip("Magnet: snap drawings to candle open/high/low/close")
        self.magnet_action.toggled.connect(self._magnet_toggled)
        st = tb.addAction(icons.get("t_stay"), "")
        st.setCheckable(True)
        st.setToolTip("Stay in drawing mode: the tool stays selected after each drawing")
        st.toggled.connect(lambda on: [setattr(p.chart, "stay_in_drawing", on) for p in self.panels])
        lk = tb.addAction(icons.get("lock"), "")
        lk.setCheckable(True)
        lk.setToolTip("Lock all drawings")
        lk.toggled.connect(lambda on: [setattr(p.chart.layer, "locked", on) for p in self.panels])
        hd = tb.addAction(icons.get("eye"), "")
        hd.setCheckable(True)
        hd.setToolTip("Hide all drawings")
        hd.toggled.connect(lambda on: [p.chart.layer.set_hidden(on) for p in self.panels])
        tb.addSeparator()
        c = tb.addAction(icons.get("trash"), "")             # TradingView: one Remove button with a list
        c.setToolTip("Remove all drawings on this chart")
        c.triggered.connect(lambda: self.active.chart.layer.clear())
        rm = QtWidgets.QMenu(self)
        rm.addAction(icons.get("eraser"), f"Remove selected drawing\t{theme.sc('Del')}",
                     lambda: self.active.chart.layer.delete_selected())
        rm.addAction(icons.get("trash"), "Remove all drawings", lambda: self.active.chart.layer.clear())
        c.setMenu(rm)
        tb.widgetForAction(c).setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.MenuButtonPopup)

    def _build_right_rail(self):
        tb = QtWidgets.QToolBar("Panels")
        tb.setObjectName("rightrail")
        tb.setOrientation(QtCore.Qt.Orientation.Vertical)
        tb.setMovable(False)
        tb.setFixedWidth(49)                                  # TradingView: 45 px + the 4 px gutter
        tb.setIconSize(QtCore.QSize(28, 28))
        self.addToolBar(QtCore.Qt.ToolBarArea.RightToolBarArea, tb)
        self.right_docks = [self.watch_dock, self.alerts_dock, self.strategy_dock, self.data_dock, self.chain_dock]
        self.rail_actions = {}
        for dock, ico, tip in ((self.watch_dock, "watchlist", "Watchlist"),
                               (self.alerts_dock, "bell", "Alerts"),
                               (self.strategy_dock, "code", "Python strategy editor"),
                               (self.data_dock, "datawin", f"Data window  ({theme.sc('Alt+D')})"),
                               (self.chain_dock, "chain", "Option chain")):
            act = tb.addAction(icons.get(ico), "")
            act.setCheckable(True)
            act.setToolTip(tip)
            act.triggered.connect(lambda _=False, d=dock: self.toggle_right_panel(d))
            self.rail_actions[dock] = act
        tb.addSeparator()
        res = self.results_dock.toggleViewAction()
        res.setIcon(icons.get("table"))
        res.setToolTip("Strategy Tester")
        tb.addAction(res)

    def toggle_right_panel(self, dock):
        """Click an icon: show that panel alone; click the active one again: hide the side panel."""
        self.show_right_panel(None if dock.isVisible() else dock)

    def show_right_panel(self, dock):
        for d in self.right_docks:
            d.setVisible(d is dock)
        for d, a in self.rail_actions.items():
            a.setChecked(d is dock)
        if dock is getattr(self, "chain_dock", None) and getattr(self, "active", None) is not None:
            QtCore.QTimer.singleShot(0, lambda: self.chain.follow(str(self.active.params.get("symbol", ""))))
        self.live_sync()
        if dock is getattr(self, "data_dock", None):          # open on the bar the legend shows, not "Hover a bar"
            ch = self.active.chart
            if ch.df is not None and ch._n():
                self._update_data_window(self.active, min(getattr(ch, "_hdr_i", ch._n() - 1), ch._n() - 1))

    def _build_data_dock(self):
        self.data_lbl = QtWidgets.QLabel("Hover a bar")
        self.data_lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.data_lbl.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.data_lbl.setWordWrap(True)                      # lets the table span the whole panel width
        self.data_lbl.setStyleSheet("padding:8px 12px; font-size:13px;")
        self.data_dock = QtWidgets.QDockWidget("Data window", self)
        self.data_dock.setObjectName("data_dock")
        self.data_dock.setWidget(self.data_lbl)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.data_dock)
        self.data_dock.hide()

    def _build_chain_dock(self):
        from .live import LiveController
        from .optionchain import OptionChain
        self.live = LiveController(self)
        self.live.enabled = self.settings.value("live_prices", "true") == "true"
        self.chain = OptionChain(account_fn=self.live.dhan_account)
        self.chain_dock = QtWidgets.QDockWidget("Option chain", self)
        self.chain_dock.setObjectName("chain_dock")
        self.chain_dock.setWidget(self.chain)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.chain_dock)
        self.chain_dock.hide()
        self.live.extra = self.chain.instruments
        self._live_timer = QtCore.QTimer(self)
        self._live_timer.setSingleShot(True)
        self._live_timer.setInterval(150)
        self._live_timer.timeout.connect(self.live.sync)
        self.chain.changed.connect(self.live_sync)
        self.chain.picked.connect(self.open_symbol)
        self.chain.needAccount.connect(lambda: self.manage_accounts())
        self.live.status.connect(self._live_status)

    def live_sync(self):
        """Tell the live feed what is on screen now (debounced: layout changes come in bursts)."""
        if hasattr(self, "_live_timer"):
            self._live_timer.start()

    def open_symbol(self, sym: str):
        self.symbol.setText(sym)
        self.on_load()

    def set_live_prices(self, on: bool):
        self.settings.setValue("live_prices", "true" if on else "false")
        self.live.enabled = bool(on)
        self.live.sync()
        if not on:
            self.live_label.hide()

    def _live_status(self, mode: str, detail: str):
        lab = self.live_label
        text, col = {"live": ("\u25cf Live", theme.UP), "polling": ("\u25cf Live (1.5 s)", "#f7a600"),
                     "yahoo": ("\u25cf Live (3 s)", "#f7a600"),
                     "connecting": ("\u25cb Connecting\u2026", theme.MUTED),
                     "error": ("\u25cf Live prices off", theme.DOWN)}.get(mode, ("", theme.MUTED))
        lab.setVisible(bool(text))
        lab.setText(text + "  ")
        lab.setStyleSheet(f"color:{col};")
        bn = self.live.feed.broker_name if hasattr(self, "live") else "the broker"
        tip = {"live": f"Real-time prices from {bn}'s live market feed: every trade moves the chart.",
               "polling": f"Prices are fetched from {bn} every 1.5 seconds (the live socket is not connected).",
               "yahoo": "The candle follows Yahoo's newest price, checked every 3 seconds. Yahoo can be delayed for "
                        "some exchanges and is not tick by tick: log in a broker account (Accounts) for real-time prices.",
               "connecting": f"Connecting to {bn}'s live market feed\u2026",
               "error": "Live prices are not running: " + (detail or "")}.get(mode, "")
        lab.setToolTip(tip)
        if mode == "error" and detail and getattr(self, "_live_err", "") != detail:
            self.statusBar().showMessage("Live prices: " + detail, 9000)
        self._live_err = detail if mode == "error" else ""

    def on_live_price(self, panel, ltp: float, new_bar: bool):
        """A live trade reached a chart: alerts fire at once; a freshly opened bar is checked against the broker's
        own candle a moment later."""
        self.alerts.check(self._alert_key(panel), ltp, ltp, ltp)
        if new_bar:
            QtCore.QTimer.singleShot(2500, panel.refresh)

    def _update_data_window(self, panel, i):
        if panel is self.active and self.data_dock.isVisible():
            self.data_lbl.setText(panel.chart.data_window_html(i))

    def _build_alerts_dock(self):
        self.alerts_panel = alertmod.AlertsPanel(self.alerts)
        self.alerts_panel.newRequested.connect(lambda: self.create_alert())
        self.alerts_dock = QtWidgets.QDockWidget("Alerts", self)
        self.alerts_dock.setObjectName("alerts_dock")
        self.alerts_dock.setWidget(self.alerts_panel)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.alerts_dock)

    @staticmethod
    def _alert_key(panel) -> str:
        return panel.symbol_key

    def create_alert(self, price: float | None = None, panel=None):
        panel = panel or self.active
        ch = panel.chart
        if ch.df is None:
            self.statusBar().showMessage("Load a symbol first")
            return
        last = float(ch.df["Close"].iloc[-1])
        dlg = alertmod.AlertDialog(self, ch.label or panel.label, last, price)
        if dlg.exec():
            v = dlg.result_values()
            a = self.alerts.add(self._alert_key(panel), ch.label or panel.label, v["cond"], v["value"], last,
                                v["message"], v["once"], v["expires"])
            self.statusBar().showMessage("Alert created: " + alertmod.describe(a))
            self.show_right_panel(self.alerts_dock)
            self.poller.poll()

    def _refresh_alert_lines(self):
        for p in self.panels:
            if p.chart.df is None:
                continue
            key = self._alert_key(p)
            p.chart.set_alert_lines([(a["value"], f"{alertmod.COND_TEXT[a['cond']]} {a['value']:,.2f}")
                                     for a in self.alerts.for_symbol(key)])

    def _alert_fired(self, a, price):
        for p in self.panels:   # alerts stay silent during bar replay, like TradingView
            if p.chart.replay["active"] and self._alert_key(p) == a["symbol"]:
                return
        msg = alertmod.describe(a) + f"  (price {price:,.2f})" + (f"\n{a['message']}" if a["message"] else "")
        alertmod.Toast(self, "Alert triggered", msg.replace("\n", "<br>"))
        QtWidgets.QApplication.beep()
        alertmod.notify_os("ChartLab alert", msg.replace("\n", " - "))
        self.add_log("[alert] " + msg.replace("\n", " - "))

    def _build_watchlist_dock(self):
        self.watchlist = WatchlistWidget()
        self.watchlist.symbolChosen.connect(self._watchlist_chosen)
        self.watchlist.changed.connect(self._save_watchlist)
        self.watchlist.changed.connect(lambda: self.live_sync())
        self.watchlist.addRequested.connect(self._watchlist_add_search)
        self.watch_dock = QtWidgets.QDockWidget("Watchlist", self)
        self.watch_dock.setWidget(self.watchlist)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.watch_dock)

    def _build_strategy_dock(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)
        row = QtWidgets.QHBoxLayout()
        self.templates = QtWidgets.QComboBox()
        self.templates.addItem("Load a template…", None)
        sdir = datamod.ROOT / "strategies"
        for f in sorted(sdir.glob("*.py")) if sdir.exists() else []:
            self.templates.addItem(f.stem.replace("_", " "), str(f))
        self.templates.activated.connect(self._template_chosen)
        row.addWidget(self.templates, 1)
        b_open, b_save = QtWidgets.QPushButton("Open…"), QtWidgets.QPushButton("Save…")
        b_open.clicked.connect(self.open_strategy)
        b_save.clicked.connect(self.save_strategy)
        row.addWidget(b_open)
        row.addWidget(b_save)
        v.addLayout(row)

        self.editor = CodeEditor()
        self.editor.runRequested.connect(self.run_backtest)
        v.addWidget(self.editor, 1)
        suggest = "\u2303Space" if sys.platform == "darwin" else "Ctrl+Space"
        dot = "  \u00b7  "
        self.strat_status = QtWidgets.QLabel(
            theme.sc("Ctrl+Return") + " run" + dot + theme.sc("Ctrl+/") + " comment" + dot + suggest
            + " suggestions" + dot + "type ta. or df[\" to complete")
        self.strat_status.setWordWrap(True)
        self.strat_status.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.strat_status.setStyleSheet(f"color:{theme.MUTED}; font-size:12px; padding:2px 2px;")
        v.addWidget(self.strat_status)

        row2 = QtWidgets.QHBoxLayout()
        row2.addWidget(QtWidgets.QLabel("Capital"))
        self.capital = QtWidgets.QDoubleSpinBox()
        self.capital.setRange(1000, 1e10)
        self.capital.setDecimals(0)
        self.capital.setSingleStep(10000)
        self.capital.setValue(100000)
        row2.addWidget(self.capital, 1)
        row2.addWidget(QtWidgets.QLabel("Fee % / side"))
        self.fee = QtWidgets.QDoubleSpinBox()
        self.fee.setRange(0, 5)
        self.fee.setDecimals(3)
        self.fee.setSingleStep(0.01)
        self.fee.setValue(0.05)
        row2.addWidget(self.fee)
        v.addLayout(row2)

        run2 = QtWidgets.QPushButton("▶  Run backtest")
        run2.setObjectName("primary")
        run2.clicked.connect(self.run_backtest)
        v.addWidget(run2)
        self.run2_btn = run2

        self.strategy_dock = QtWidgets.QDockWidget("Strategy (Python)", self)
        self.strategy_dock.setWidget(w)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.strategy_dock)

    def _build_results_dock(self):
        self.tabs = QtWidgets.QTabWidget()
        # Overview: TradingView's headline numbers + equity curve
        ov = QtWidgets.QWidget()
        ovl = QtWidgets.QVBoxLayout(ov)
        ovl.setContentsMargins(12, 10, 12, 6)
        ovl.setSpacing(8)
        cards = QtWidgets.QHBoxLayout()
        cards.setSpacing(28)
        self.ov_cards = {}
        for key, title in (("net", "Net Profit"), ("trades", "Total Closed Trades"), ("win", "Percent Profitable"),
                           ("pf", "Profit Factor"), ("dd", "Max Drawdown"), ("avg", "Avg Trade"),
                           ("bars", "Avg # Bars in Trades")):
            lab = QtWidgets.QLabel("")
            lab.setTextFormat(QtCore.Qt.TextFormat.RichText)
            lab._title = title
            cards.addWidget(lab)
            self.ov_cards[key] = lab
        cards.addStretch(1)
        ovl.addLayout(cards)
        self.equity_axis = DateAxis("bottom")
        self.equity_plot = pg.PlotWidget(axisItems={"bottom": self.equity_axis})
        self.equity_plot.setBackground(theme.BG)
        self.equity_plot.showGrid(x=False, y=True, alpha=0.15)
        self.equity_plot.hideButtons()
        self.equity_plot.setMenuEnabled(False)
        self.equity_plot.showAxis("right")
        self.equity_plot.hideAxis("left")
        for a in ("right", "bottom"):
            ax = self.equity_plot.getAxis(a)
            ax.setTextPen(pg.mkPen("#b2b5be"))
            ax.setPen(pg.mkPen("#2a2a2a"))
        self.equity_plot.addLegend(offset=(10, 6), labelTextColor="#b2b5be", brush=pg.mkBrush(0, 0, 0, 0),
                                   pen=pg.mkPen(None))
        ovl.addWidget(self.equity_plot, 1)
        self.tabs.addTab(ov, "Overview")
        self._clear_overview()

        self.summary = QtWidgets.QTableWidget(0, 4)
        self.summary.horizontalHeader().setVisible(False)
        self.summary.verticalHeader().setVisible(False)
        self.summary.verticalHeader().setDefaultSectionSize(30)
        self.summary.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.summary.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.summary.setShowGrid(False)
        self.tabs.addTab(self.summary, "Performance Summary")

        cols = ["#", "Type", "Signal", "Entry time", "Exit time", "Entry", "Exit", "Qty", "P&L", "P&L %",
                "Cum. P&L", "Bars"]
        self.trades_tbl = QtWidgets.QTableWidget(0, len(cols))
        self.trades_tbl.setHorizontalHeaderLabels(cols)
        self.trades_tbl.verticalHeader().setVisible(False)
        self.trades_tbl.verticalHeader().setDefaultSectionSize(30)
        hh = self.trades_tbl.horizontalHeader()
        hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        for c in (0, 1, 2, 3, 4):
            hh.setSectionResizeMode(c, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        AL = QtCore.Qt.AlignmentFlag
        for c in range(len(cols)):                          # numbers right-aligned under right-aligned headings
            self.trades_tbl.horizontalHeaderItem(c).setTextAlignment(
                (AL.AlignRight if c >= 5 else AL.AlignLeft) | AL.AlignVCenter)
        self.trades_tbl.setShowGrid(False)
        self.trades_tbl.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.trades_tbl.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.trades_tbl.setAlternatingRowColors(True)
        self.trades_tbl.cellClicked.connect(self._trade_clicked)
        self.tabs.addTab(self.trades_tbl, "List of Trades")

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("print() output of your strategy and run messages appear here.")
        self.tabs.addTab(self.log_view, "Log")

        self.results_dock = QtWidgets.QDockWidget("Strategy Tester", self)
        self.results_dock.setWidget(self.tabs)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, self.results_dock)
        # TradingView: one side panel at a time, chosen from the right icon rail; no tabs
        self.watch_dock.setTitleBarWidget(QtWidgets.QWidget())          # the watchlist draws its own header
        for d, title in ((self.strategy_dock, "Strategy (Python)"), (self.alerts_dock, "Alerts"),
                         (self.data_dock, "Data window"), (self.chain_dock, "Option chain")):
            lab = QtWidgets.QLabel(title)
            lab.setObjectName("panelTitle")
            d.setTitleBarWidget(lab)
        for d in (self.watch_dock, self.strategy_dock, self.alerts_dock, self.data_dock, self.chain_dock):
            d.setFeatures(QtWidgets.QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
            d.setMinimumWidth(340)
        self.chain_dock.setMinimumWidth(430)                 # seven columns need a little more room
        self.results_dock.hide()
        self.watch_dock.setObjectName("dock_watchlist")
        self.strategy_dock.setObjectName("dock_strategy")
        self.alerts_dock.setObjectName("dock_alerts")
        self.results_dock.setObjectName("dock_results")
        for tb_ in self.findChildren(QtWidgets.QToolBar):     # saveState needs names; keep the styled ones
            if not tb_.objectName():
                tb_.setObjectName("tb_" + (tb_.windowTitle() or "main").replace(" ", "_"))
        self._default_state = self.saveState(3)
        self.resizeDocks([self.strategy_dock], [480], QtCore.Qt.Orientation.Horizontal)
        self.resizeDocks([self.watch_dock], [350], QtCore.Qt.Orientation.Horizontal)
        self.resizeDocks([self.results_dock], [260], QtCore.Qt.Orientation.Vertical)

    def _build_menu(self):
        mb = self.menuBar()
        f = mb.addMenu("File")
        f.addAction("Check for updates\u2026", self.open_updates)
        f.addAction("Open data file…", self.open_data_file, QtGui.QKeySequence("Ctrl+O"))
        f.addAction("Open data folder…", self.open_data_folder)
        f.addAction("Open strategy…", self.open_strategy)
        f.addAction("Save strategy…", self.save_strategy, QtGui.QKeySequence("Ctrl+S"))
        f.addAction("Export trades to CSV…", self.export_trades)
        f.addAction("Save chart image…", self.save_chart_image)
        f.addSeparator()
        f.addAction("Quit", self.close, QtGui.QKeySequence("Ctrl+Q"))
        s = mb.addMenu("Strategy")
        run = QtGui.QAction("Run backtest", self)
        run.setShortcuts([QtGui.QKeySequence("Ctrl+R"), QtGui.QKeySequence("Ctrl+Return")])
        run.triggered.connect(self.run_backtest)
        s.addAction(run)
        s.addAction("Backtest settings…", self.show_bt_settings)
        s.addAction("Remove strategy from chart", self.remove_strategy)
        s.addAction("Stop script", self.stop_script, QtGui.QKeySequence("Ctrl+."))
        s.addAction("Choose Python for scripts…", self.choose_python)
        s.addSeparator()
        s.addAction("Manage broker accounts…", self.manage_accounts)
        s.addAction("Run script on multiple accounts…", self.run_on_accounts)
        s.addAction("Auto-trade strategy on accounts…", self.toggle_auto_trade)
        v = mb.addMenu("View")
        v.addAction("Performance test", self.perf_test)
        v.addAction("Reset layout", self.reset_layout)
        v.addAction("Watchlist", lambda: self.show_right_panel(self.watch_dock))
        v.addAction("Alerts", lambda: self.show_right_panel(self.alerts_dock))
        v.addAction("Strategy editor", lambda: self.show_right_panel(self.strategy_dock))
        v.addAction("Data window\t" + theme.sc("Alt+D"), lambda: self.show_right_panel(self.data_dock))
        v.addAction("Option chain", lambda: self.show_right_panel(self.chain_dock))
        v.addAction("Hide side panel", lambda: self.show_right_panel(None))
        v.addSeparator()
        v.addAction(self.results_dock.toggleViewAction())
        c = mb.addMenu("Claude")
        c.addAction("Connect to Claude…", self.show_connect_dialog)

    def _shortcuts(self):
        QtGui.QShortcut(QtGui.QKeySequence("Escape"), self, activated=self._escape)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Z"), self, activated=lambda: self.active.chart.layer.undo())
        for k in ("Ctrl+Shift+Z", "Ctrl+Y"):
            QtGui.QShortcut(QtGui.QKeySequence(k), self, activated=lambda: self.active.chart.layer.redo())
        QtGui.QShortcut(QtGui.QKeySequence("Alt+A"), self, activated=lambda: self.create_alert())
        QtGui.QShortcut(QtGui.QKeySequence("Alt+Shift+B"), self, activated=lambda: self._order_at_cursor("BUY"))
        QtGui.QShortcut(QtGui.QKeySequence("Alt+Shift+S"), self, activated=lambda: self._order_at_cursor("SELL"))
        QtGui.QShortcut(QtGui.QKeySequence("Shift+T"), self, activated=lambda: self._order_at_cursor("BUY", False))
        QtGui.QShortcut(QtGui.QKeySequence("Alt+R"), self, activated=lambda: self.active.chart.go_latest())
        for key, tool in (("Alt+T", "trend"), ("Alt+H", "hline"), ("Alt+J", "hray"), ("Alt+V", "vline"),
                          ("Alt+C", "crossline"), ("Alt+F", "fib"), ("Shift+Alt+R", "rect")):
            QtGui.QShortcut(QtGui.QKeySequence(key), self, activated=lambda t=tool: self._tool_clicked(t))
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+K"), self, activated=self.quick_search)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Alt+H"), self, activated=lambda: [p.chart.layer.set_hidden(not p.chart.layer.hidden) for p in self.panels])
        QtGui.QShortcut(QtGui.QKeySequence("Alt+Return"), self, activated=lambda: self.statusBar().showMessage("Maximise chart: use the 1-chart layout", 3000))

        QtGui.QShortcut(QtGui.QKeySequence("Alt+G"), self, activated=lambda: self._goto_date())
        QtGui.QShortcut(QtGui.QKeySequence("Alt+I"), self, activated=lambda: self.active.chart.price.vb.invertY(not self.active.chart.price.vb.state["yInverted"]))
        QtGui.QShortcut(QtGui.QKeySequence("Alt+L"), self, activated=lambda: self._toggle_scale("log"))
        QtGui.QShortcut(QtGui.QKeySequence("Alt+P"), self, activated=lambda: self._toggle_scale("percent"))
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Shift+S"), self, activated=self.copy_chart_image)
        QtGui.QShortcut(QtGui.QKeySequence("Alt+S"), self, activated=self.copy_chart_image)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Alt+S"), self, activated=self.save_chart_image)
        QtGui.QShortcut(QtGui.QKeySequence("Alt+D"), self, activated=lambda: self.toggle_right_panel(self.data_dock))
        QtGui.QShortcut(QtGui.QKeySequence("Shift+F"), self, activated=self.toggle_fullscreen)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+,"), self, activated=self.open_settings)

    def open_settings(self, tab: int = 0):
        from . import settings as cs
        dlg = cs.SettingsDialog(self, tab if isinstance(tab, int) else 0)
        dlg.applied.connect(lambda _s: (cs.save(self.settings), self.apply_chart_settings()))
        dlg.exec()

    def open_updates(self):
        """Settings -> Updates, checking right away."""
        from . import settings as cs
        QtCore.QTimer.singleShot(0, lambda: self.check_updates(True))
        self.open_settings(cs.SettingsDialog.UPDATES_TAB)

    def apply_chart_settings(self):
        for p in self.panels:
            p.chart.apply_settings()

    # ------------------------------------------------------------------ TradingView keyboard model
    _TEXT_INPUTS = (QtWidgets.QLineEdit, QtWidgets.QTextEdit, QtWidgets.QPlainTextEdit, QtWidgets.QAbstractSpinBox,
                    QtWidgets.QKeySequenceEdit)

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QtCore.QEvent.Type.KeyPress and self._route_key(ev, obj):
            return True
        if t == QtCore.QEvent.Type.Polish and isinstance(obj, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
            le = obj.findChild(QtWidgets.QLineEdit)        # Qt drops a spin box's left padding when its arrow
            if le is not None and le.textMargins().left() == 0:   # buttons are hidden: indent the text like
                le.setTextMargins(7, 0, 0, 0)                     # every other input field
        return super().eventFilter(obj, ev)

    def _route_key(self, ev, target=None) -> bool:
        """Keys typed anywhere in the main window that is not a text field go to the chart, like TradingView:
        a letter opens Symbol Search, a digit or ',' opens Change interval, '/' opens Indicators,
        arrows / + / - / Home / End move and zoom the active chart."""
        app = QtWidgets.QApplication
        if app.activeModalWidget() is not None or app.activePopupWidget() is not None:
            return False
        fw = app.focusWidget() or (target if isinstance(target, QtWidgets.QWidget) else None)
        if fw is None or fw.window() is not self:
            return False
        if isinstance(fw, self._TEXT_INPUTS) or (isinstance(fw, QtWidgets.QComboBox) and fw.isEditable()):
            return False
        M = QtCore.Qt.KeyboardModifier
        mods = ev.modifiers()
        txt = ev.text()
        plain = not (mods & (M.ControlModifier | M.AltModifier | M.MetaModifier))
        if plain and len(txt) == 1 and txt.isprintable():
            if txt.isalpha():
                QtCore.QTimer.singleShot(0, lambda t=txt.upper(): self.open_symbol_search(t))
                return True
            if txt.isdigit() or txt == ",":
                QtCore.QTimer.singleShot(0, lambda t=("" if txt == "," else txt): self.open_interval_popup(t))
                return True
            if txt == "/":
                QtCore.QTimer.singleShot(0, self.open_indicators)
                return True
        if isinstance(fw, QtWidgets.QAbstractItemView):      # lists keep their own arrow / delete keys
            return False
        if self.active is None:
            return False
        if ev.key() in (QtCore.Qt.Key.Key_Delete, QtCore.Qt.Key.Key_Backspace) and plain:
            self.active.chart.layer.delete_selected()
            return True
        cmd = bool(mods & (M.ControlModifier | M.MetaModifier)) and not (mods & (M.AltModifier | M.ShiftModifier))
        lay = self.active.chart.layer
        if cmd and ev.key() == QtCore.Qt.Key.Key_C and lay.selected is not None:
            if lay.copy_selected():
                self.statusBar().showMessage("Drawing copied  \u2014  " + theme.sc("Ctrl+V") + " pastes it (on any chart)", 4000)
            return True
        if cmd and ev.key() == QtCore.Qt.Key.Key_V:
            from .drawings import _CLIP
            if _CLIP and lay.paste():
                return True
        if cmd and ev.key() == QtCore.Qt.Key.Key_D and lay.selected is not None:
            lay.duplicate_selected()
            return True
        return bool(self.active.chart.handle_key(ev))

    def focus_chart(self):
        if self.active is not None:
            self.active.chart.glw.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)

    def _chart_typed(self, ch: str):
        if ch.isalpha():
            self.open_symbol_search(ch.upper())
        else:
            self.open_interval_popup(ch)

    def _toggle_scale(self, mode):
        ch = self.active.chart
        ch.set_scale_mode("regular" if ch.scale_mode == mode else mode)

    def quick_actions(self) -> list:
        """Everything the app can do, for Cmd/Ctrl+K (title, category, shortcut, callable, checked)."""
        ch = self.active.chart
        A = []
        add = lambda title, cat, fn, sc="", checked=False: A.append((title, cat, sc, fn, checked))  # noqa: E731
        add("Symbol search", "Chart", self.open_symbol_search, "type a letter")
        add("Change interval", "Chart", lambda: self.open_interval_popup(""), "type a number")
        add("Go to date", "Chart", self._goto_date, "Alt+G")
        add("Reset chart view", "Chart", lambda: ch.go_latest(), "Alt+R")
        add("Invert scale", "Price scale", lambda: ch.price.vb.invertY(not ch.price.vb.state["yInverted"]), "Alt+I",
            bool(ch.price.vb.state["yInverted"]))
        add("Logarithmic scale", "Price scale", lambda: self._toggle_scale("log"), "Alt+L", ch.scale_mode == "log")
        add("Percent scale", "Price scale", lambda: self._toggle_scale("percent"), "Alt+P", ch.scale_mode == "percent")
        add("Regular scale", "Price scale", lambda: ch.set_scale_mode("regular"), "", ch.scale_mode == "regular")
        add("Settings", "Chart", self.open_settings, "Ctrl+,")
        add("Save chart image", "Chart", self.save_chart_image)
        add("Full screen", "View", self.toggle_fullscreen, "Shift+F")
        add("Create alert", "Alerts", lambda: self.create_alert(), "Alt+A")
        add("Bar replay", "Replay", self.toggle_replay, "", bool(ch.replay["active"]))
        for key in STYLES:
            add(STYLE_NAMES[key], "Chart type", lambda k=key: self._style_chosen(k), "", ch.style == key)
        cur = self._current_interval()
        from . import timeframes as _TF
        for val, text in [(v, _TF.legend(v)) for v in self._all_intervals()]:
            add(f"Interval {text}", "Interval", lambda v=val: self.interval_btns[v].click(), "", val == cur)
        for key, text in INDICATORS.items():
            on = key in ch._enabled
            add(text, "Indicator", lambda k=key, o=on: self.ind_actions[k].setChecked(not o), "", on)
        for key, text in LAYOUTS.items():
            add(text, "Layout", lambda k=key: self.set_layout(k), "", self.layout_name == key)
        sc = {"trend": "Alt+T", "hline": "Alt+H", "hray": "Alt+J", "vline": "Alt+V", "crossline": "Alt+C",
              "fib": "Alt+F", "rect": "Shift+Alt+R"}
        for key, text in TOOL_LABELS.items():
            add(text, "Drawing tool", lambda k=key: self._tool_clicked(k), sc.get(key, ""))
        add("Stay in drawing mode", "Drawing tool",
            lambda: [setattr(p.chart, "stay_in_drawing", not ch.stay_in_drawing) for p in self.panels], "",
            ch.stay_in_drawing)
        add("Copy chart image", "Chart", self.copy_chart_image, "Alt+S")
        add("Save chart image", "Chart", self.save_chart_image, "Ctrl+Alt+S")
        add("Remove all drawings", "Drawing tool", lambda: ch.layer.clear())
        add("Hide / show drawings", "Drawing tool", lambda: ch.layer.set_hidden(not ch.layer.hidden), "Ctrl+Alt+H")
        for dock, text in ((self.watch_dock, "Watchlist"), (self.alerts_dock, "Alerts panel"),
                           (self.strategy_dock, "Strategy editor"), (self.data_dock, "Data window"),
                           (self.chain_dock, "Option chain")):
            add(text, "Panel", lambda d=dock: self.show_right_panel(d), "Alt+D" if dock is self.data_dock else "",
                dock.isVisible())
        add("Strategy Tester", "Panel", lambda: self.results_dock.setVisible(not self.results_dock.isVisible()), "",
            self.results_dock.isVisible())
        add("Run strategy / backtest", "Strategy", self.run_backtest, "Cmd+Enter")
        add("Remove strategy from chart", "Strategy", self.remove_strategy)
        add("Backtest settings", "Strategy", self.show_bt_settings)
        add("Open strategy file", "Strategy", self.open_strategy)
        add("Save strategy file", "Strategy", self.save_strategy, "Cmd+S")
        add("Manage broker accounts", "Accounts", self.manage_accounts)
        add("Run script on multiple accounts", "Accounts", self.run_on_accounts)
        add("Open data file", "Data", self.open_data_file, "Cmd+O")
        for lbl, fn in getattr(self, "_extra_actions", {}).items():
            add(lbl, "Other", fn)
        return A

    def quick_search(self):
        """Ctrl+K: 'Search tool or function' over every action in the app."""
        from .tvui import QuickSearch
        QuickSearch(self, self.quick_actions()).exec()
        self.focus_chart()

    def toggle_fullscreen(self):
        self.setWindowState(self.windowState() ^ QtCore.Qt.WindowState.WindowFullScreen)

    def copy_chart_image(self):
        """The chart as a picture on the clipboard, ready to paste into a chat or a note."""
        QtWidgets.QApplication.clipboard().setImage(self.active.chart.glw.grab().toImage())
        self.statusBar().showMessage("Chart image copied to the clipboard", 4000)

    def toggle_replay(self):
        ch = self.active.chart
        if ch.replay["active"]:
            ch.replay_exit()
        else:
            ch.replay_begin()
        self.replay_btn.setChecked(ch.replay["active"])

    # ------------------------------------------------------------------ self-update
    def check_updates(self, manual: bool = False):
        from .autotrade import _OrderWorker
        from . import updater
        if getattr(self, "_upd_busy", False):
            return
        if not manual and self.settings.value("auto_update", "true") == "false":
            return                                           # Settings -> Updates: automatic checks are off
        self._upd_busy, self._upd_manual = True, bool(manual)
        if manual:
            self.statusBar().showMessage("Checking for a newer ChartLab\u2026")
        w = self._upd_worker = _OrderWorker(lambda: updater.check(self.version), self)
        w.done.connect(self._update_checked)
        w.start()

    def update_state(self) -> tuple:
        """(what the update is doing right now, is a new version ready to restart into) - shown in Settings."""
        info = getattr(self, "_upd_info", None) or {}
        ready = getattr(self, "_upd_ready", None)
        if ready:
            return f"ChartLab {ready[0]} is downloaded and checked. Restart to use it.", True
        if getattr(self, "_upd_busy", False):
            if info and getattr(self, "_upd_fetching", False):
                pct = getattr(self, "_upd_pct", -1)
                return f"Downloading ChartLab {info.get('version', '')}\u2026" + (f" {pct}%" if pct >= 0 else ""), False
            return "Checking for a newer version\u2026", False
        return getattr(self, "_upd_last", "") or "Not checked yet in this session.", False

    def _update_checked(self, info, err):
        import datetime as _dt
        self._upd_busy = False
        manual = getattr(self, "_upd_manual", False)
        when = _dt.datetime.now().strftime("%H:%M")
        if err:
            self._upd_last = f"Could not check for updates ({when}): {err}"
        elif not info:
            self._upd_last = f"ChartLab {self.version} is the newest version (checked {when})."
        if err:
            if manual:
                self.statusBar().showMessage(f"Could not check for updates: {err}", 8000)
            return
        if not info:
            if manual:
                self.statusBar().showMessage(f"ChartLab {self.version} is the newest version", 6000)
            return
        ready = getattr(self, "_upd_ready", None)
        if ready and ready[0] == info["version"]:              # already fetched: the pill is waiting
            return
        from . import updater as _u
        if sys.platform == "win32":                            # the Windows app cannot replace itself: say where to get it
            self._upd_last = f"ChartLab {info['version']} is available. " + _u.why_not()
            if manual:
                self.statusBar().showMessage(self._upd_last, 15000)
            return
        self._upd_info, self._upd_ready = info, None
        # TradingView: the new version is fetched quietly in the background; the button appears once it is ready
        self._fetch_update(info, quiet=not manual)
        if manual:
            self.statusBar().showMessage(f"ChartLab {info['version']} found \u2014 fetching it in the background\u2026",
                                         6000)

    def _fetch_update(self, info, quiet: bool):
        from .autotrade import _OrderWorker
        from . import updater
        if getattr(self, "_upd_busy", False):
            return
        self._upd_busy, self._upd_quiet, self._upd_fetching, self._upd_pct = True, quiet, True, -1
        if not quiet:
            self.upd_btn.setEnabled(False)
            self.upd_btn.setText("Downloading 0%")
            self._upd_action.setVisible(True)
            self._fit_topbar()
        self.updProgress.connect(self._update_progress, QtCore.Qt.ConnectionType.UniqueConnection)

        def job():
            z = updater.download(info, progress=lambda got, total: self.updProgress.emit(int(got), int(total)))
            self.updProgress.emit(-1, 0)
            return updater.unpack(z, info["version"])
        w = self._upd_worker = _OrderWorker(job, self)
        w.done.connect(self._update_downloaded)
        w.start()

    def start_update(self):
        """The 'Restart to update' button (TradingView): put the fetched version in place and open it."""
        from . import updater
        info = getattr(self, "_upd_info", None)
        if not info or getattr(self, "_upd_busy", False):
            return
        reason = updater.why_not()
        if reason:
            QtWidgets.QMessageBox.information(self, "Update", reason)
            return
        if hasattr(self, "auto") and self.auto.active:
            QtWidgets.QMessageBox.information(self, "Update", "Auto-trade is running. Stop it first, then update.")
            return
        ready = getattr(self, "_upd_ready", None)
        if ready and ready[0] == info["version"] and Path(ready[1]).exists():
            self._restart_into(ready[1])
            return
        self._upd_restart_after = True                          # fetch first (with progress), then restart
        self._fetch_update(info, quiet=False)

    def _update_progress(self, got: int, total: int):
        if total > 0 and got >= 0:
            self._upd_pct_any = int(got * 100 / total)
        if getattr(self, "_upd_quiet", False):
            self._upd_pct = getattr(self, "_upd_pct_any", -1)
            return
        if got < 0:
            self.upd_btn.setText("Checking\u2026")
        elif total > 0:
            pct = int(got * 100 / total)
            if pct != getattr(self, "_upd_pct", -1):
                self._upd_pct = pct
                self.upd_btn.setText(f"Downloading {pct}%")

    def _update_downloaded(self, new_app, err):
        self._upd_busy = self._upd_fetching = False
        if err or new_app is None:
            self._upd_last = f"The update could not be downloaded, nothing was changed: {err}"
        info = getattr(self, "_upd_info", {}) or {}
        if err or new_app is None:
            self.upd_btn.setEnabled(True)
            self.upd_btn.setText(f"Update to {info.get('version', '')}")
            self._upd_action.setVisible(True)
            self._fit_topbar()
            if not getattr(self, "_upd_quiet", False):
                self.statusBar().showMessage(f"Update failed, nothing was changed: {err}", 15000)
            logging.error("update failed: %s", err)
            self._upd_restart_after = False
            return
        self._upd_ready = (info.get("version", ""), str(new_app))
        if getattr(self, "_upd_restart_after", False):
            self._upd_restart_after = False
            self._restart_into(str(new_app))
            return
        self.upd_btn.setEnabled(True)
        self.upd_btn.setText("Restart to update")
        self.upd_btn.setToolTip(f"ChartLab {info.get('version', '')} is downloaded and checked \u2014 "
                                "click to restart into it (your charts and settings stay)")
        self._upd_action.setVisible(True)
        self._fit_topbar()
        self.statusBar().showMessage(f"ChartLab {info.get('version', '')} is ready \u2014 click 'Restart to update' "
                                     "at the top right", 15000)

    def _restart_into(self, new_app: str):
        from . import updater
        try:
            updater.swap_and_restart(Path(new_app), self.version, (self._upd_info or {}).get("version", ""))
        except Exception as e:  # noqa: BLE001
            self.upd_btn.setEnabled(True)
            self.statusBar().showMessage(f"Update failed, nothing was changed: {e}", 15000)
            return
        self.upd_btn.setEnabled(False)
        self.upd_btn.setText("Restarting\u2026")
        self.statusBar().showMessage("Restarting into the new ChartLab\u2026")
        QtCore.QTimer.singleShot(300, lambda: (self.close(), QtWidgets.QApplication.quit()))

    def report_last_update(self):
        """After a restart: say 'Updated to \u2026', or - when macOS kept the old app - why, and what to do."""
        from . import updater
        res = updater.take_pending(self.version)
        if not res:
            return
        if res.get("ok"):
            self.statusBar().showMessage(f"Updated to ChartLab {self.version}", 8000)
            return
        why = res.get("reason") or "the new app did not replace this one"
        new_app = res.get("new_app", "")
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Update")
        box.setText(f"ChartLab {res.get('to', '')} was downloaded and checked, but macOS did not let it replace this "
                    f"copy, so ChartLab {self.version} opened again.")
        box.setInformativeText(f"macOS said: {why}\n\nIf macOS asked about letting ChartLab modify apps, allow it in "
                               "System Settings \u2192 Privacy & Security \u2192 App Management and try the update "
                               "again. Or drag the new ChartLab (shown in Finder) into Applications yourself.")
        show = box.addButton("Show the new app", QtWidgets.QMessageBox.ButtonRole.ActionRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is show and new_app and Path(new_app).exists():
            import subprocess
            subprocess.Popen(["open", "-R", new_app])

    def start_update_checks(self):
        """The Mac app looks for a newer build 5 s after it opens and every 2 hours (TradingView does it too)."""
        from . import updater
        QtCore.QTimer.singleShot(1500, self.report_last_update)
        if updater.app_bundle() is None:
            return
        QtCore.QTimer.singleShot(5000, self.check_updates)
        self._upd_timer = QtCore.QTimer(self)
        self._upd_timer.timeout.connect(self.check_updates)
        self._upd_timer.start(2 * 60 * 60 * 1000)

    def perf_test(self):
        """View > Performance test: how long this Mac needs to draw one chart frame while panning / zooming."""
        import time
        ch = self.active.chart
        if ch.df is None:
            self.statusBar().showMessage("Load a chart first")
            return
        vp, vb = ch.glw.viewport(), ch.price.vb
        (x0, x1) = vb.viewRange()[0]

        def run(fn, n=40):
            vp.repaint()
            t0 = time.perf_counter()
            for k in range(n):
                fn(k)
                vp.repaint()
            return (time.perf_counter() - t0) / n * 1000
        pan = run(lambda k: vb.pan_x(1.5 if k % 2 else -1.5))
        zoom = run(lambda k: vb.zoom_x(0.97 if k % 2 else 1 / 0.97))
        ch.price.setXRange(x0, x1, padding=0)
        msg = (f"Performance on this Mac: pan {pan:.1f} ms per frame, zoom {zoom:.1f} ms per frame "
               f"({int(x1 - x0)} bars on screen, {vp.width()}x{vp.height()} at {vp.devicePixelRatioF():g}x). "
               f"Under 8 ms is perfectly smooth at 120 Hz, under 16 ms at 60 Hz.")
        self.statusBar().showMessage(msg)
        self.add_log("[performance] " + msg)

    def show_about(self):
        QtWidgets.QMessageBox.about(
            self, "About ChartLab",
            f"<b style='font-size:16px'>ChartLab {self.version}</b><br><br>TradingView-style charts, Python strategies, "
            "multi-account orders.<br><br>If this number is lower than the build you downloaded, an older copy of the "
            "app is still being opened: delete the old ChartLab from Applications and put the new one there.")

    def _escape(self):
        pop = QtWidgets.QApplication.activePopupWidget()     # Esc first closes an open menu / autocomplete list
        if pop is not None:
            pop.close()
            return
        if self.active.chart.replay["active"]:
            self.active.chart.replay_exit()
            self.replay_btn.setChecked(False)
            return
        self.active.chart.layer.cancel()
        self._tool_changed(None)

    # ------------------------------------------------------------------ settings
    def _restore(self):
        st = self.settings
        self.watchlist.set_symbols(WatchlistWidget.loads(st.value("watchlist", "")))
        self.symbol.setText(st.value("symbol", "RELIANCE"))
        self.range.setCurrentText(st.value("range", "2y"))
        iv = st.value("interval", "1d")
        if self.ensure_interval(str(iv)):
            self.interval_btns[iv].setChecked(True)
        src = st.value("source", "auto")
        if src == "yahoo" and st.value("auto_source_offered", "") != "1":
            src = "auto"                    # one time: older versions only knew Yahoo; Auto = my data -> API -> Yahoo
        st.setValue("auto_source_offered", "1")
        i = self.source.findData(src)
        self.source.setCurrentIndex(max(i, 0))
        code = st.value("strategy", "")
        if not code:
            f = datamod.ROOT / "strategies" / "sma_cross.py"
            code = f.read_text() if f.exists() else FALLBACK_STRATEGY
        self.editor.setPlainText(code)
        self.set_layout(st.value("layout", "1"))
        geo = st.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        state = st.value("dock_state_v4")
        if state:
            self.restoreState(state, 3)
        vis = [d for d in self.right_docks if d.isVisible()]
        self.show_right_panel(vis[0] if vis else (None if state else self.watch_dock))
        self._restore_panel_state()
        self._source_changed()

    def _restore_panel_state(self):
        """Each chart comes back with the indicators (and their order / hidden ones) and the chart type it had."""
        try:
            data = json.loads(self.settings.value("panel_state", "") or "[]")
        except (TypeError, ValueError):
            return
        for p, d in zip(self.panels, data if isinstance(data, list) else []):
            if not isinstance(d, dict):
                continue
            for k in d.get("on", []):
                if k in STUDY.STUDIES:
                    p.chart.set_indicator(k, True)
            p.chart._ind_hidden = {k for k in d.get("hidden", []) if k in STUDY.STUDIES}
            if d.get("style") in STYLES:
                p.chart.set_style(d["style"])
        self._sync_toolbar()

    def _save_panel_state(self):
        data = [{"on": p.chart._ordered_enabled(), "hidden": sorted(p.chart._ind_hidden), "style": p.chart.style}
                for p in self.panels]
        self.settings.setValue("panel_state", json.dumps(data))

    def _panel_state_changed(self):
        if not hasattr(self, "_pstate_timer"):
            self._pstate_timer = QtCore.QTimer(self)
            self._pstate_timer.setSingleShot(True)
            self._pstate_timer.setInterval(400)
            self._pstate_timer.timeout.connect(self._save_panel_state)
        self._pstate_timer.start()

    def reset_layout(self):
        self.restoreState(self._default_state, 3)
        self.show_right_panel(self.watch_dock)

    def closeEvent(self, e):
        st = self.settings
        st.setValue("symbol", self.symbol.text())
        st.setValue("interval", self._current_interval())
        st.setValue("range", self.range.currentText())
        st.setValue("source", self.source.currentData())
        st.setValue("strategy", self.editor.toPlainText())
        st.setValue("layout", self.layout_name)
        st.setValue("geometry", self.saveGeometry())
        st.setValue("dock_state_v4", self.saveState(3))
        self._save_watchlist()
        self._save_panel_state()
        if self.api is not None:
            self.api.stop()
        self.live.stop()
        self.chain.stop()
        ow = getattr(self, "_opt_worker", None)
        if ow is not None and ow.isRunning():                 # the option archive stops between two contracts
            ow.requestInterruption()
            ow.wait(4000)
        super().closeEvent(e)

    def _save_watchlist(self):
        self.settings.setValue("watchlist", WatchlistWidget.dumps(self.watchlist.symbols()))

    def _drawings_key(self, panel):
        return "drawings/" + _safe_key(panel.symbol_key)

    def _save_drawings(self, panel):
        self.settings.setValue(self._drawings_key(panel), json.dumps(panel.chart.layer.to_json()))

    def _load_drawings(self, panel):
        raw = self.settings.value(self._drawings_key(panel), "")
        try:
            panel.chart.layer.load_json(json.loads(raw) if raw else [])
        except Exception:  # noqa: BLE001
            panel.chart.layer.load_json([])

    # ------------------------------------------------------------------ layout & active panel
    def set_layout(self, name: str):
        if name not in LAYOUTS:
            name = "1"
        self.layout_name = name
        self.layout_actions[name].setChecked(True)
        if hasattr(self, "layout_btn"):                       # the button shows the current layout's shape
            self.layout_btn.setIcon(icons.get({"1": "layout_icon", "2h": "layout_2h", "2v": "layout_2v",
                                               "4": "layout_4"}[name]))
        n = LAYOUT_COUNT[name]
        H, V = QtCore.Qt.Orientation.Horizontal, QtCore.Qt.Orientation.Vertical
        sizes = getattr(self, "_split_sizes", {})
        if self.split_root is not None:                       # remember where the user dragged the dividers
            sizes[self._split_key] = [sp.sizes() for sp in [self.split_root] + self.split_root.findChildren(QtWidgets.QSplitter)]
        for p in self.panels:
            p.setParent(None)
        if self.split_root is not None:
            self.area_lay.removeWidget(self.split_root)
            self.split_root.deleteLater()

        def mk(orient, widgets):
            sp = QtWidgets.QSplitter(orient)
            sp.setChildrenCollapsible(False)
            sp.setHandleWidth(3)
            for w_ in widgets:
                sp.addWidget(w_)
                sp.setStretchFactor(sp.count() - 1, 1)
            return sp
        P = self.panels
        if name == "1":
            root = mk(H, [P[0]])
        elif name == "2h":
            root = mk(H, [P[0], P[1]])
        elif name == "2v":
            root = mk(V, [P[0], P[1]])
        else:
            root = mk(V, [mk(H, [P[0], P[1]]), mk(H, [P[2], P[3]])])
        root.setStyleSheet(f"QSplitter::handle {{ background: {theme.BORDER}; }} "
                           f"QSplitter::handle:hover {{ background: {theme.ACCENT}; }}")
        self.split_root, self._split_key, self._split_sizes = root, name, sizes
        self.area_lay.addWidget(root)
        for i, p in enumerate(self.panels):
            if i < n:
                p.show()
                if p.df is None and not p.loading and i > 0 and self.isVisible():
                    self._autoload_extra(p, i)
            else:
                p.hide()
        saved = sizes.get(name)
        if saved:
            for sp, sz in zip([root] + root.findChildren(QtWidgets.QSplitter), saved):
                if sum(sz) > 0 and len(sz) == sp.count():
                    sp.setSizes(sz)
        for p in self.panels:
            p.multi = n > 1
            p.set_active(p is self.active)
        if self.active not in self.panels[:n]:
            self.set_active(self.panels[0])
        self.live_sync()

    def _autoload_extra(self, panel, idx):
        syms = self.watchlist.symbols()
        sym = syms[idx % len(syms)] if syms else "NIFTY"
        a = self.active.params
        src = a["source"] if a["source"] != "file" else "yahoo"
        panel.load(src, sym, a["interval"], a["range"])

    def set_active(self, panel):
        if panel is self.active and panel.isVisible():
            self._sync_toolbar()
            return
        if self.active is not None:
            self.active.set_active(False)
        self.active = panel
        panel.set_active(True)
        self._sync_toolbar()
        self.focus_chart()
        self._show_result(panel)

    def _current_interval(self) -> str:
        for v, b in self.interval_btns.items():
            if b.isChecked():
                return v
        return "1d"

    def _sync_toolbar(self):
        p = self.active
        self._syncing = True
        try:
            prm = p.params
            self.symbol.setText(prm["symbol"] if prm["source"] != "file" else self.symbol.text())
            i = self.source.findData(prm["source"])
            if i >= 0:
                self.source.setCurrentIndex(i)
            self.range.setCurrentText(prm["range"])
            if self.ensure_interval(str(prm["interval"])):
                self.interval_btns[prm["interval"]].setChecked(True)
            for k, a in self.ind_actions.items():
                a.setChecked(k in p.chart._enabled)
            self.style_actions[p.chart.style].setChecked(True)
            self.style_btn.setIcon(icons.get("st_" + p.chart.style))
            self.magnet_action.setChecked(p.chart.magnet)
            tool = p.chart.layer.tool
            act = self.draw_actions.get(tool, self.draw_actions[None])
            act.setChecked(True)
            if tool and act in getattr(self, 'tool_groups', {}):
                act.setIcon(icons.get(self._tool_icon.get(tool, tool)))
        finally:
            self._syncing = False
        self._source_changed()

    # ------------------------------------------------------------------ toolbar handlers
    def _fill_api_sources(self):
        cur = self.source.currentData()
        for i in reversed(range(self.source.count())):
            if str(self.source.itemData(i)).startswith("api:"):
                self.source.removeItem(i)
        for a in acctmod.load_accounts():
            self.source.addItem(f"API: {a['name']}", f"api:{a['name']}")
        if cur is not None and self.source.findData(cur) >= 0:
            self.source.setCurrentIndex(self.source.findData(cur))

    def _update_acct_dot(self):
        state, tip = acctmod.overall_state()
        pm = QtGui.QPixmap(20, 20)
        pm.setDevicePixelRatio(2.0)
        pm.fill(QtCore.Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setBrush(QtGui.QColor(acctmod.STATE_COLOR.get(state)))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.drawEllipse(QtCore.QRectF(1, 1, 8, 8))
        p.end()
        self.acct_btn.setIcon(QtGui.QIcon(pm))
        self.acct_btn.setText("Accounts")
        self.acct_btn.setToolTip(tip)
        ready = any(a.get("access_token") for a in acctmod.load_accounts())
        for pn in getattr(self, "panels", []):              # Buy / Sell on the chart only once a broker is connected
            if pn.chart.trading_ready != ready:
                pn.chart.trading_ready = ready
                pn.chart._place_overlays()

    def _fill_trade_with(self):
        from .trading import OrderTicket
        m = self.trade_with_menu
        m.clear()
        g = QtGui.QActionGroup(m)
        for acc in self.trader.accounts():
            act = m.addAction(OrderTicket._acct_text(acc))
            act.setCheckable(True)
            act.setChecked(acc["name"] == self.trader.name)
            g.addAction(act)
            act.triggered.connect(lambda _c=False, n=acc["name"]: self.trader.set_account(n))
        m.addSeparator()
        cl = m.addAction("Confirm every LIVE order")
        cl.setCheckable(True)
        cl.setChecked(self.settings.value("confirm_live", "true") == "true")
        cl.triggered.connect(lambda on: self.settings.setValue("confirm_live", "true" if on else "false"))
        tl = m.addAction("Show orders and positions on the chart")
        tl.setCheckable(True)
        tl.setChecked(self.settings.value("trade_lines", "true") == "true")
        tl.triggered.connect(lambda on: (self.settings.setValue("trade_lines", "true" if on else "false"),
                                         self._update_trade_lines()))
        m.addAction("Reset paper account\u2026", self._reset_paper)

    def _reset_paper(self):
        b = self.trader.broker()
        if b.id != "paper":
            self.statusBar().showMessage("Choose a paper account first (Trade with)", 5000)
            return
        if QtWidgets.QMessageBox.question(self, "Reset paper account",
                                          f"Clear every paper order and position of '{b.acct.get('name')}' and start "
                                          "again with fresh cash?") == QtWidgets.QMessageBox.StandardButton.Yes:
            b.reset()
            self.trader.refresh()

    def accounts_overview(self):
        acctmod.OverviewDialog(self).exec()
        self._update_acct_dot()
        self.live_sync()

    def set_auto_renew(self, on: bool):
        self.settings.setValue("auto_renew", "true" if on else "false")
        if hasattr(self, "_auto_renew_action"):
            self._auto_renew_action.setChecked(bool(on))
        if on:
            self.acct_poller.refresh()

    def _auto_renew(self, name, st):
        """Called after each status check: a login that is close to its end, or already over, is replaced by a fresh
        one without the user (the broker's renewal, or PIN + saved TOTP key). Not more than once in 30 minutes."""
        if self.settings.value("auto_renew", "true") != "true" or not st:
            return
        state = st.get("state")
        d = st.get("days_left")
        import time as _t
        tried = getattr(self, "_renew_tried", {})
        soon = state in ("ok", "expiring") and d is not None and 0 < d * 24 < acctmod.EXPIRING_HOURS
        over = state in ("expired", "auth")
        if not (soon or over) or _t.time() - tried.get(name, 0) < 1800:
            return
        a = next((x for x in acctmod.load_accounts() if x["name"] == name), None)
        way = acctmod.auto_login_way(a) if a else ""
        if not way or (over and way != "totp"):
            return                                           # this one needs the user: the status line says so
        tried[name] = _t.time()
        self._renew_tried = tried
        job = acctmod._Job(lambda: acctmod.auto_login(name, expired=over), self)
        self._renew_jobs = [j for j in getattr(self, "_renew_jobs", []) if j.isRunning()] + [job]

        def done(msg):
            self.statusBar().showMessage(msg, 8000)
            self.add_log(f"[accounts] {msg}")
            self._warned = getattr(self, "_warned", set()) - {name}
            self.acct_poller.refresh([a for a in acctmod.load_accounts() if a["name"] == name], force=True)
            self.live_sync()
            QtCore.QTimer.singleShot(4000, self._options_archive)
        job.done.connect(done)
        job.failed.connect(lambda e: self.statusBar().showMessage(
            f"Account '{name}': a fresh login could not be made ({e[:120]}). Log in again in Accounts.", 15000))
        job.start()

    def _acct_status_changed(self, name):
        self._update_acct_dot()
        st = acctmod.STATUS.get(name)
        self._auto_renew(name, st)
        if st and st["state"] in ("expired", "auth", "expiring") and name not in getattr(self, "_warned", set()):
            self._warned = getattr(self, "_warned", set()) | {name}
            self.statusBar().showMessage(f"Account '{name}': {st['msg']}  (Accounts -> Manage broker accounts)", 10000)

    def manage_accounts(self, select: str = ""):
        dlg = acctmod.AccountsDialog(self)
        dlg.poller.updated.connect(self._acct_status_changed)
        dlg.exec()
        self._fill_api_sources()
        self._update_acct_dot()
        self.live_sync()
        if self.chain.isVisible():
            self.chain.reload()
        added = getattr(dlg, "saved", "")
        if added and self.source.findData(f"api:{added}") >= 0 and not any(
                str(self.source.itemData(i)).startswith("api:") and self.source.currentData() == self.source.itemData(i)
                for i in range(self.source.count())):
            self.source.setCurrentIndex(self.source.findData(f"api:{added}"))
            self.statusBar().showMessage(f"Data source set to API: {added}", 5000)

    # ------------------------------------------------------------------ auto-trade on all accounts
    def _auto_trader(self):
        if not hasattr(self, "auto"):
            from .autotrade import AutoTrader
            self.auto = AutoTrader(self)
            self.auto.log.connect(self.add_log)
            self.auto.changed.connect(self._auto_changed)
            self.auto.traded.connect(lambda t, x: alertmod.Toast(self, t, x, "#2962ff"))
        return self.auto

    def _auto_changed(self):
        on = self.auto.active
        self.auto_act.setText("Stop auto-trade" if on else "Auto-trade strategy on accounts\u2026")
        self.acct_btn.setText(f"AUTO \u00b7 {self.auto.symbol} \u00b7 {len(self.auto.names)}" if on else "Accounts")
        self.acct_btn.setStyleSheet("QToolButton { color: #f23645; font-weight: 700; }" if on else "")
        self.statusBar().showMessage(
            f"Auto-trade is ON for {self.auto.symbol} on {len(self.auto.names)} accounts" if on else "Auto-trade is off")

    def toggle_auto_trade(self):
        auto = self._auto_trader()
        if auto.active:
            auto.stop()
            return
        panel = self.active
        code = self.strategy_code()
        if panel.df is None or panel.params.get("source") == "file":
            self.statusBar().showMessage("Auto-trade needs a live chart (source Auto / API / Yahoo), not a local file")
            return
        if not re.search(r"^def\s+strategy\s*\(", code, re.M):
            self.statusBar().showMessage("Auto-trade needs a strategy with  def strategy(df):  in the editor")
            return
        accts = [a for a in acctmod.load_accounts() if a.get("broker") in acctmod.BROKERS]
        if not accts:
            self.statusBar().showMessage("Connect at least one broker account first")
            self.manage_accounts()
            return
        from .autotrade import AutoTradeDialog
        dlg = AutoTradeDialog(self, accts, str(panel.params.get("symbol", "")).upper(), self._current_interval())
        if not dlg.exec() or not dlg.chosen():
            return
        if int(self.settings.value("auto_refresh", 60)) <= 0:
            self.set_auto_refresh(60)                         # it needs fresh bars to act on
        self.tabs.setCurrentWidget(self.log_view)
        self.results_dock.show()
        auto.start(panel, code, dlg.chosen(), dlg.qty.value(), dlg.product.currentData(), dlg.exchange.currentText())

    def run_on_accounts(self):
        """Run the editor's script once per chosen account, all at the same time."""
        accts = acctmod.load_accounts()
        if not accts:
            self.statusBar().showMessage("Add accounts first (Strategy -> Manage broker accounts)")
            self.manage_accounts()
            return
        dlg = acctmod.PickAccountsDialog(self, accts)
        if dlg.exec():
            names = dlg.chosen()
            if names:
                self.run_multi(names, broadcast=dlg.broadcast())

    def run_multi(self, names: list[str], broadcast: bool = False) -> dict:
        self.stop_script()
        self.stop_multi()
        try:
            py, _ = scriptmode.find_python(str(self.settings.value("script_python", "") or ""))
            by = {a["name"]: a for a in acctmod.load_accounts()}
            chosen = [by[n] for n in names]
        except Exception as e:  # noqa: BLE001
            self.add_log(str(e))
            return {"ok": False, "error": str(e)}
        data = self.active.chart.effective_df()
        code = self.strategy_code()
        self._multi = []
        self.tabs.setCurrentWidget(self.log_view)
        if broadcast:
            return self._run_broadcast(py, chosen, data, code)
        self.add_log(f"--- running on {len(chosen)} accounts at once: " + ", ".join(names))
        for a in chosen:
            work = scriptmode.prepare(data)
            script = work / "user_script.py"
            script.write_text(code)
            env = QtCore.QProcessEnvironment.systemEnvironment()
            for k, v in {**scriptmode.environment(work, data), **acctmod.account_env(a)}.items():
                env.insert(k, v)
            proc = QtCore.QProcess(self)
            proc.setProcessEnvironment(env)
            proc.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)
            tag = a["name"]
            proc.readyReadStandardOutput.connect(
                lambda p=proc, t=tag: [self.add_log(f"[{t}] {ln}") for ln in
                                       bytes(p.readAllStandardOutput()).decode("utf8", "replace").splitlines()])
            proc.finished.connect(lambda code_, _s, t=tag: self._multi_done(t, code_))
            self._multi.append(proc)
            proc.start(py, [str(work / "_runner.py"), str(script)])
            proc.closeWriteChannel()
        self.statusBar().showMessage(f"Running on {len(chosen)} accounts… (Strategy -> Stop script to stop all)")
        return {"ok": True, "accounts": names}

    def _run_broadcast(self, py, chosen, data, code) -> dict:
        """ONE script process; chartlab.order() fans each order out to all accounts in parallel threads."""
        import os
        work = scriptmode.prepare(data)
        script = work / "user_script.py"
        script.write_text(code)
        af = work / "accounts.json"
        fd = os.open(af, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(chosen, f)
        env = QtCore.QProcessEnvironment.systemEnvironment()
        for k, v in {**scriptmode.environment(work, data), "CHARTLAB_ACCOUNTS_FILE": str(af)}.items():
            env.insert(k, v)
        proc = QtCore.QProcess(self)
        proc.setProcessEnvironment(env)
        proc.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)
        proc.readyReadStandardOutput.connect(lambda: self.add_log(bytes(proc.readAllStandardOutput()).decode("utf8", "replace")))
        proc.finished.connect(lambda c, _s: (af.unlink(missing_ok=True), self._multi_done("all accounts", c)))
        self._multi = [proc]
        live = [a["name"] for a in chosen if a.get("live")]
        self.add_log(f"--- ONE script, orders to {len(chosen)} accounts at once (LIVE: {', '.join(live) or 'none, paper only'})")
        proc.start(py, [str(work / "_runner.py"), str(script)])
        proc.closeWriteChannel()
        self.statusBar().showMessage("Running on all accounts… (Strategy -> Stop script to stop)")
        return {"ok": True, "accounts": [a["name"] for a in chosen], "mode": "broadcast"}

    def _multi_done(self, tag, code):
        self.add_log(f"[{tag}] finished" + ("" if code == 0 else f" with errors (exit {code})"))
        if all(p.state() == QtCore.QProcess.ProcessState.NotRunning for p in getattr(self, "_multi", [])):
            self.statusBar().showMessage("All account runs finished")

    def stop_multi(self):
        for p in getattr(self, "_multi", []):
            if p.state() != QtCore.QProcess.ProcessState.NotRunning:
                p.kill()
                self.add_log("[stopped by you]")

    def _source_changed(self, *_):
        is_file = self.source.currentData() == "file"
        for w in [self.symbol, self.range]:
            w.setEnabled(not is_file)
        for w in self.interval_btns.values():
            w.setEnabled(True)
        self.load_btn.setText("Open file…" if is_file else "Load")

    def _interval_clicked(self, val: str):
        if self._syncing:
            return
        if self.source.currentData() != "file":
            self.on_load()
        else:                                           # local data: resample what is already loaded
            path = self.active.params.get("path", "")
            if path:
                self.load_into(self.active, "file", self.active.params.get("symbol", ""), val, "max", path)

    # what the Indicators dialog shows: (title, category, extra search words)
    IND_INFO = {k: (STUDY.STUDIES[k].title + ("" if not STUDY.STUDIES[k].inputs else
                                               " (" + ", ".join(f"{STUDY.defaults(k)[n]:g}" for n, *_ in STUDY.STUDIES[k].inputs
                                                                if not n.startswith("ma_")) + ")"),
                    STUDY.STUDIES[k].cat, STUDY.STUDIES[k].search) for k in STUDY.KEYS}

    def open_indicators(self, text: str = ""):
        """TradingView's Indicators dialog ( / ): click to add or remove, strategies load into the editor and run."""
        from .tvui import IndicatorsDialog
        items = []
        for key, (title, cat, extra) in self.IND_INFO.items():
            if key in self.ind_actions:
                items.append({"key": key, "title": title, "category": cat, "kind": "indicator",
                              "on": self.ind_actions[key].isChecked(), "search": f"{extra} {INDICATORS.get(key, '')}"})
        sdir = datamod.ROOT / "strategies"
        for f in sorted(sdir.glob("*.py")) if sdir.exists() else []:
            words = [w.upper() if w in ("rsi", "sma", "ema", "macd", "vwap", "atr", "bb", "orb") else w
                     for w in f.stem.lower().split("_")]
            items.append({"key": "strategy:" + str(f), "title": " ".join(words)[:1].upper() + " ".join(words)[1:],
                          "category": "Strategies (Python)", "kind": "strategy", "on": False,
                          "search": "strategy backtest python"})

        def toggle(key):
            a = self.ind_actions[key]
            a.setChecked(not a.isChecked())
            return a.isChecked()

        def run(key):
            self.set_strategy_code(Path(key.split(":", 1)[1]).read_text())
            self.show_right_panel(self.strategy_dock)
            self.run_backtest()

        IndicatorsDialog(self, items, toggle, run, text).exec()
        self.focus_chart()

    def _ind_params_changed(self, panel):
        """Inputs changed on one pane (legend -> gear): every pane uses them, and they are remembered."""
        self.settings.setValue("ind_params", json.dumps(panel.chart.ind_params))
        self._panel_state_changed()
        for p in self.panels:
            if p is not panel:
                for k, v in panel.chart.ind_params.items():
                    if p.chart.ind_params.get(k) != v:
                        p.chart.blockSignals(True)
                        p.chart.set_indicator_params(k, **v)
                        p.chart.blockSignals(False)

    def _ind_toggled_on_chart(self, panel, key, on):
        self._panel_state_changed()
        if panel is self.active and key in self.ind_actions:
            self._syncing = True
            try:
                self.ind_actions[key].setChecked(on)
            finally:
                self._syncing = False

    def _indicator_toggled(self, key, on):
        if not self._syncing:
            self.active.chart.set_indicator(key, on)
            self._panel_state_changed()

    def _style_chosen(self, key):
        if hasattr(self, "style_btn"):
            self.style_btn.setIcon(icons.get("st_" + key))
        if key in self.style_actions:
            self.style_actions[key].setChecked(True)
        if not self._syncing:
            self.active.chart.set_style(key)
            self._panel_state_changed()

    def _magnet_toggled(self, on):
        for p in self.panels:
            p.chart.magnet = on

    def _tool_clicked(self, tool):
        if self._syncing:
            return
        for p in self.panels:
            if p is not self.active:
                p.chart.layer.set_tool(None)
        self.active.chart.layer.set_tool(tool)

    def _tool_changed(self, tool):
        self._syncing = True
        try:
            ga = self.draw_actions.get(tool, self.draw_actions[None])
            ga.setChecked(True)
            if tool is not None and ga in getattr(self, "tool_groups", {}) and ga.data() != tool:
                ga.setData(tool)                              # TradingView: a group shows the tool used last
                ga.setIcon(icons.get(self._tool_icon.get(tool, tool)))
        finally:
            self._syncing = False

    def _watchlist_chosen(self, sym: str):
        self.symbol.setText(sym)
        if self.source.currentData() == "file":
            self.source.setCurrentIndex(self.source.findData("auto"))
        self.on_load()

    # ------------------------------------------------------------------ data loading
    def on_load(self):
        src = self.source.currentData()
        if src == "file":
            self.open_data_file()
            return
        sym = self.symbol.text().strip()
        if not sym:
            self.statusBar().showMessage("Type a symbol first")
            return
        self.load_into(self.active, src, sym, self._current_interval(), self.range.currentText())

    def load_into(self, panel, source, symbol, interval, rng, path=""):
        panel.load(source, symbol, interval, rng, path)

    def open_data_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open price data", str(Path.home()), "Price data (*.csv *.tsv *.txt *.parquet);;All files (*)")
        if not path:
            return
        self.source.setCurrentIndex(self.source.findData("file"))
        self.load_into(self.active, "file", Path(path).stem, "auto", "max", path)

    def open_data_folder(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Open a folder of price files (e.g. one parquet per day)", str(Path.home()))
        if not path:
            return
        self.source.setCurrentIndex(self.source.findData("file"))
        self.load_into(self.active, "file", Path(path).name, "auto", "max", path)

    def _on_load_started(self, panel):
        if panel is self.active:
            self.load_btn.setEnabled(False)
        self._quiet_status(f"Loading {panel.params['symbol']}…")

    def _on_loaded(self, panel):
        self.load_btn.setEnabled(True)
        self._load_drawings(panel)
        self._watch_path(panel)
        df = panel.df
        self._refresh_alert_lines()
        if len(df):   # catch moves since last session; crossing alerts only arm on this first look
            last = float(df["Close"].iloc[-1])
            self.alerts.check(self._alert_key(panel), last, last, last)
        self._load_status(panel)
        self._sync_watch_price(panel)
        self.live_sync()
        self._feed_trade_price(panel)
        if panel is self.active:
            self.chain.follow(str(panel.params.get("symbol", "")))
            self._sync_toolbar()
            self._show_result(panel)
            self.watchlist.select(str(panel.params.get("symbol", "")).upper())
            if getattr(panel, "_strategy_on", False) and re.search(r"^def\s+strategy\s*\(", self.strategy_code(), re.M):
                QtCore.QTimer.singleShot(0, self.run_backtest)
            if QtWidgets.QApplication.activeModalWidget() is None:
                self.focus_chart()

    # ------------------------------------------------------------------ auto update
    def start_auto_update(self):
        """Live refresh of the charts on screen, reload when a local file / My data changes, and a once-a-day
        download of the whole watchlist into ~/ChartLab/data/downloads."""
        self._auto_timer = QtCore.QTimer(self)
        self._auto_timer.timeout.connect(self._auto_tick)
        self.set_auto_refresh(int(self.settings.value("auto_refresh", 60)))
        datamod.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._fs = QtCore.QFileSystemWatcher(self)
        self._fs.addPath(str(datamod.DATA_DIR))
        self._fs_debounce = QtCore.QTimer(self)
        self._fs_debounce.setSingleShot(True)
        self._fs_debounce.setInterval(1200)
        self._fs_debounce.timeout.connect(self._files_changed)
        self._fs.fileChanged.connect(lambda *_: self._fs_debounce.start())
        self._fs.directoryChanged.connect(lambda *_: self._fs_debounce.start())
        self._daily_timer = QtCore.QTimer(self)
        self._daily_timer.timeout.connect(self._daily_download)
        self._daily_timer.start(30 * 60 * 1000)
        QtCore.QTimer.singleShot(20000, self._daily_download)
        self._daily_timer.timeout.connect(self._options_archive)
        QtCore.QTimer.singleShot(45000, self._options_archive)
        # the watchlist keeps itself current like TradingView's: every minute in market hours, else every 10
        self._watch_timer = QtCore.QTimer(self)
        self._watch_timer.timeout.connect(self._watch_tick)
        self._watch_timer.start(30 * 1000)
        QtCore.QTimer.singleShot(2500, lambda: self._watch_tick(force=True))

    def _watch_tick(self, force: bool = False):
        import time as _t
        from .live import market_open
        if not force and (QtWidgets.QApplication.activeModalWidget() is not None or not self.isVisible()):
            return
        gap = 55 if market_open() else 600
        if force or _t.time() - getattr(self, "_watch_last", 0.0) >= gap:
            self._watch_last = _t.time()
            self.watchlist.refresh()

    def _sync_watch_price(self, panel):
        """Chart and watchlist row of the same symbol never disagree: the row takes the chart's newest close."""
        df = panel.df
        if df is None or len(df) < 2 or panel.params.get("source") == "file":
            return
        sym = str(panel.params.get("symbol", "")).strip().upper()
        sym = datamod.RENAMED.get(sym, sym)
        if sym not in self.watchlist.symbols():
            return
        idx = df.index
        last = float(df["Close"].iloc[-1])
        if panel.chart.axes[0].intraday:
            prev = df["Close"][idx.normalize() < idx[-1].normalize()]
            pc = float(prev.iloc[-1]) if len(prev) else None
        else:
            pc = float(df["Close"].iloc[-2])
        self.watchlist.chart_price(sym, last, pc)

    def set_auto_refresh(self, seconds: int):
        self.settings.setValue("auto_refresh", int(seconds))
        if seconds > 0:
            self._auto_timer.start(int(seconds) * 1000)
        else:
            self._auto_timer.stop()

    def _visible_panels(self):
        return [p for p in self.panels if p.isVisible()]

    def _auto_tick(self):
        if QtWidgets.QApplication.activeModalWidget() is not None:
            return
        for p in self._visible_panels():
            if p.params.get("source") != "file":
                p.refresh()

    def refresh_now(self):
        n = sum(bool(p.refresh()) for p in self._visible_panels())
        self.statusBar().showMessage("Refreshing\u2026" if n else "Nothing to refresh right now", 2500)
        self.watchlist.refresh()

    def _watch_path(self, panel):
        path = panel.params.get("path") if panel.params.get("source") == "file" else ""
        if path and hasattr(self, "_fs") and path not in self._fs.files() + self._fs.directories():
            self._fs.addPath(path)

    def _files_changed(self):
        for p in self._visible_panels():
            src = p.params.get("source")
            if src in ("file", "auto"):
                if src == "file":
                    self._watch_path(p)                   # editors replace files: watch the new one again
                p.refresh()

    def open_my_data(self):
        datamod.DATA_DIR.mkdir(parents=True, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(datamod.DATA_DIR)))
        self.statusBar().showMessage(
            "Put RELIANCE.csv / NIFTY.parquet here: with source Auto, your own file is used first", 8000)

    def _daily_download(self):
        import datetime as _dt
        today = _dt.date.today().isoformat()
        if (self.settings.value("daily_download", "true") != "true"
                or self.settings.value("daily_done", "") == today or getattr(self, "_daily_busy", False)):
            return
        syms = list(self.watchlist.symbols())
        if not syms:
            return
        self._daily_busy = True
        from .panel import ChartPanel, LoadWorker

        def job():
            ok = 0
            for s in syms:
                if QtCore.QThread.currentThread().isInterruptionRequested():
                    break
                try:
                    df, _lab = ChartPanel._load_auto(s, "1d", "2y")
                    datamod.save_daily(s, df, "1d")
                    ok += 1
                except Exception:  # noqa: BLE001 - one bad symbol must not stop the rest
                    logging.exception("daily download %s", s)
            return ok, str(len(syms))
        w = self._daily_worker = LoadWorker(job, self)

        def done(ok, total):
            self._daily_busy = False
            if ok:
                self.settings.setValue("daily_done", today)
            self.add_log(f"[daily download] saved {ok} of {total} watchlist symbols to {datamod.DATA_DIR / 'downloads'}")
            self.statusBar().showMessage(f"Daily data saved for {ok} of {total} watchlist symbols", 5000)
        w.done.connect(done)
        w.failed.connect(lambda *_: setattr(self, "_daily_busy", False))
        w.start()

    # ------------------------------------------------------------------ the option archive (Settings -> Data)
    def option_archive_settings(self) -> dict:
        from . import optstore
        st = self.settings
        unds = [u.strip().upper() for u in str(st.value("opt_underlyings", ",".join(optstore.DEFAULT_UNDERLYINGS))).split(",")
                if u.strip()]
        try:
            width = max(1, min(60, int(st.value("opt_width", optstore.DEFAULT_WIDTH))))
            n_exp = max(1, min(6, int(st.value("opt_expiries", optstore.DEFAULT_EXPIRIES))))
        except (TypeError, ValueError):
            width, n_exp = optstore.DEFAULT_WIDTH, optstore.DEFAULT_EXPIRIES
        return {"on": st.value("opt_archive", "true") == "true", "underlyings": unds, "width": width, "expiries": n_exp,
                "folder": str(optstore.root(str(st.value("opt_folder", "") or ""))),
                "last": str(st.value("opt_last", "") or "")}

    def _options_archive(self, force: bool = False):
        """Once a day (and again after the close), while a Dhan account is connected: save the 1-minute history of
        the option contracts around the money. Runs in the background; never places anything."""
        import datetime as _dt
        from . import optstore
        cfg = self.option_archive_settings()
        if getattr(self, "_opt_busy", False) or not (cfg["on"] or force):
            return
        now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=5, minutes=30)))
        after_close = now.weekday() < 5 and now.time() >= _dt.time(15, 35)
        stamp = now.date().isoformat() + ("-close" if after_close else "")
        done_stamp = str(self.settings.value("opt_done", "") or "")
        if not force and (done_stamp == stamp or done_stamp == now.date().isoformat() + "-close"):
            return
        acct = next((a for a in acctmod.load_accounts() if a.get("broker") == "dhan" and a.get("access_token")
                     and (acctmod.STATUS.get(a["name"]) or {}).get("state") in ("ok", "expiring")), None)
        if acct is None:
            if force:
                self.statusBar().showMessage("The option archive needs a connected Dhan account (Accounts)", 8000)
            return
        self._opt_busy = True
        base = Path(cfg["folder"])
        from .panel import LoadWorker

        def job():
            th = QtCore.QThread.currentThread()
            res = optstore.run(acct, base, cfg["underlyings"], cfg["width"], cfg["expiries"],
                               stop=th.isInterruptionRequested)
            return res, stamp
        w = self._opt_worker = LoadWorker(job, self)

        def done(res, st_):
            self._opt_busy = False
            when = _dt.datetime.now().strftime("%d %b %H:%M")
            text = (f"{when}: {res['contracts']} contracts followed, {res['updated']} files grew by {res['rows']} rows"
                    + (f", {len(res['errors'])} could not be read ({res['errors'][0]})" if res["errors"] else ""))
            self.settings.setValue("opt_last", text)
            if res["updated"] or not res["errors"]:
                self.settings.setValue("opt_done", st_)
            self.add_log(f"[option archive] {text}  ->  {res['folder']}")
            self.statusBar().showMessage(f"Option archive: {res['updated']} contracts updated", 6000)

        def failed(msg):
            self._opt_busy = False
            self.settings.setValue("opt_last", f"failed: {str(msg)[:160]}")
            self.add_log(f"[option archive] failed: {msg}")
        w.done.connect(done)
        w.failed.connect(failed)
        w.start()

    def _load_status(self, panel):
        df = panel.df
        if df is None or not len(df):
            return
        ms = getattr(panel, "load_ms", None)
        took = ""
        if ms is not None:
            took = f"  |  shown in {ms:,.0f} ms" if ms < 1000 else f"  |  shown in {ms / 1000:.1f} s"
            if getattr(panel, "from_cache", False):
                took += " from the saved copy, fetching fresh bars\u2026"
            elif getattr(panel, "fresh_ms", None):
                took += f", fresh bars after {panel.fresh_ms / 1000:.1f} s"
        self._quiet_status(                                     # (TradingView shows nothing when a chart loads)
            f"{panel.chart.ticker()}: {len(df):,} bars  |  {df.index[0]:%d %b %Y} \u2192 {df.index[-1]:%d %b %Y}{took}")

    def _feed_trade_price(self, panel):
        if not hasattr(self, "trader") or panel.df is None or not len(panel.df):
            return
        sym = self.live._norm(panel.params.get("symbol", ""))
        if sym:
            self.trader.on_price(sym, float(panel.df["Close"].iloc[-1]))
        self._update_trade_lines()

    def _on_refreshed(self, panel):
        """New bars arrived from a quiet auto refresh: keep alerts, strategy and labels in step."""
        df = panel.df
        self._feed_trade_price(panel)
        self._sync_watch_price(panel)
        if panel is self.active and getattr(panel, "fresh_ms", None):
            self._load_status(panel)
            panel.fresh_ms = None
        if not getattr(panel, "changed", True):
            return                                           # same bars as before: nothing else to do
        if hasattr(self, "auto"):
            self.auto.on_bars(panel)
        self._refresh_alert_lines()
        if len(df):
            last = float(df["Close"].iloc[-1])
            self.alerts.check(self._alert_key(panel), last, last, last)
        if panel is self.active and getattr(panel, "_strategy_on", False) \
                and re.search(r"^def\s+strategy\s*\(", self.strategy_code(), re.M) \
                and QtWidgets.QApplication.activeModalWidget() is None:
            QtCore.QTimer.singleShot(0, self.run_backtest)

    def _on_load_failed(self, panel, msg):
        self.load_btn.setEnabled(True)
        # the chart still shows the old symbol: so does the pill (TradingView never leaves a symbol it could not open
        # in the toolbar)
        if panel is self.active and panel.df is not None and len(panel.df):
            shown = str(panel.params.get("symbol", "")).strip()
            if shown and self.symbol.text().strip().upper() != shown.upper():
                self.symbol.setText(shown)
        src = str(panel.params.get("source", ""))
        if src.startswith("api:") and any(k in msg for k in ("401", "403", "DH-90", "nvalid", "xpired")):
            acct = src.split(":", 1)[1]
            msg = f"API token for '{acct}' is invalid or expired. Opening Accounts so you can paste a fresh one…"
            self.statusBar().showMessage(msg)
            self.add_log(f"[load {panel.params['symbol']}] {msg}")
            QtCore.QTimer.singleShot(300, lambda: self.manage_accounts())
            return
        self.statusBar().showMessage(msg)
        self.add_log(f"[load {panel.params['symbol']}] {msg}")  # never a modal dialog: it would freeze the app

    # ------------------------------------------------------------------ strategy files
    def _template_chosen(self, idx):
        path = self.templates.itemData(idx)
        if path:
            self.editor.setPlainText(Path(path).read_text())
        self.templates.setCurrentIndex(0)

    def open_strategy(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open strategy", str(Path.home()), "Python (*.py);;All files (*)")
        if path:
            self.script_path = path
            self.editor.setPlainText(Path(path).read_text())
            self.statusBar().showMessage(f"Opened {path}")

    def save_strategy(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save strategy", str(Path.home() / "my_strategy.py"), "Python (*.py)")
        if path:
            Path(path).write_text(self.editor.toPlainText())
            self.statusBar().showMessage(f"Saved {path}")

    def export_trades(self):
        res = self.active.result
        if res is None or res.trades.empty:
            self.statusBar().showMessage("Run a backtest with at least one trade first")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export trades", str(Path.home() / "trades.csv"), "CSV (*.csv)")
        if path:
            res.trades.drop(columns=["entry_idx", "exit_idx"]).to_csv(path, index=False)
            self.statusBar().showMessage(f"Exported {len(res.trades)} trades to {path}")

    def save_chart_image(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save chart image", str(Path.home() / "chart.png"), "PNG (*.png)")
        if path:
            self.active.grab().save(path)

    def strategy_code(self) -> str:
        return self.editor.toPlainText()

    def set_strategy_code(self, code: str):
        self.editor.setPlainText(code)

    # ------------------------------------------------------------------ backtest
    def add_log(self, text: str):
        if text:
            self.log_view.appendPlainText(text.rstrip())

    # ---- script mode: run the user's own script with their own Python
    def choose_python(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Choose the python you run your scripts with", "/usr/local/bin", "All files (*)")
        if path:
            self.settings.setValue("script_python", path)
            self.statusBar().showMessage(f"Scripts will run with {path}")

    def stop_script(self):
        self.stop_multi()
        proc = getattr(self, "_proc", None)
        if proc is not None and proc.state() != QtCore.QProcess.ProcessState.NotRunning:
            proc.kill()
            self.add_log("[stopped by you]")

    def run_script_mode(self, panel, capital=None) -> dict:
        import os
        import tempfile
        if getattr(self, "_proc", None) is not None and self._proc.state() != QtCore.QProcess.ProcessState.NotRunning:
            return {"ok": False, "error": "A script is already running (Strategy -> Stop script)", "log": ""}
        try:
            py, _ = scriptmode.find_python(str(self.settings.value("script_python", "") or ""))
        except Exception as e:  # noqa: BLE001
            self.add_log(str(e))
            return {"ok": False, "error": str(e), "log": ""}
        data = panel.chart.effective_df()
        work = scriptmode.prepare(data)
        link = getattr(self, "script_path", "") or ""
        if link and Path(link).parent.exists():
            tmp = Path(link).parent / f".chartlab_run_{Path(link).name}"
        else:
            tmp = work / "user_script.py"
        tmp.write_text(self.strategy_code())
        self._script_ctx = (panel, work, tmp, capital if capital is not None else self.capital.value(), tmp != work / "user_script.py")
        proc = QtCore.QProcess(self)
        proc.setProcessEnvironment(QtCore.QProcessEnvironment.systemEnvironment())
        env = QtCore.QProcessEnvironment.systemEnvironment()
        for k, v in scriptmode.environment(work, data).items():
            env.insert(k, v)
        proc.setProcessEnvironment(env)
        proc.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)
        proc.readyReadStandardOutput.connect(lambda: self.add_log(bytes(proc.readAllStandardOutput()).decode("utf8", "replace")))
        proc.finished.connect(self._script_finished)
        self._proc = proc
        self.add_log(f"--- running script with {py}")
        self.statusBar().showMessage("Running script… (Cmd+. to stop)")
        self.tabs.setCurrentWidget(self.log_view)
        proc.start(py, [str(work / "_runner.py"), str(tmp)])
        proc.closeWriteChannel()      # scripts that call input() get EOF instead of hanging
        return {"ok": True, "async": True, "log": ""}

    def _script_finished(self, code, _status):
        panel, work, tmp, capital, linked = self._script_ctx
        if linked:
            tmp.unlink(missing_ok=True)
        if code != 0:
            self.statusBar().showMessage("Script failed (see Log)")
            return
        try:
            res, plots = scriptmode.load_results(work, panel.chart.effective_df(), capital)
        except Exception:  # noqa: BLE001
            self.add_log(traceback.format_exc())
            self.statusBar().showMessage("Could not read the script's results (see Log)")
            return
        if res is None:
            self.add_log("Script finished but reported nothing. Call chartlab.report(trades=..., equity=...) or "
                         "keep a trades DataFrame (entry_time, exit_time, pnl) / 'equity' Series in a variable.")
            self.statusBar().showMessage("Script finished - no trades reported")
            return
        panel.result, panel.signals = res, None
        panel.chart.set_strategy_overlays(plots)
        panel.chart.set_trades(res.trades)
        if panel is self.active:
            self._show_result(panel)
            self.results_dock.show()
            self.tabs.setCurrentIndex(0)
        m = res.metrics
        self.statusBar().showMessage(f"Script done: {m['trades']} trades, net {m['total_return'] * 100:+.2f}%")

    def bt_config(self, capital=None, fee=None) -> "bt.Config":
        cfg = bt.Config(**getattr(self, "bt_extra", {}))
        cfg.capital = capital if capital is not None else self.capital.value()
        cfg.fee_pct = fee if fee is not None else self.fee.value()
        return cfg

    def show_bt_settings(self):
        """TradingView-style strategy 'Properties': capital, order size, costs, risk exits."""
        cur = bt.Config(**getattr(self, "bt_extra", {}))
        d = QtWidgets.QDialog(self)
        d.setWindowTitle("Strategy properties")
        d.setMinimumWidth(500)
        v = QtWidgets.QVBoxLayout(d)
        v.setContentsMargins(22, 18, 22, 16)
        v.setSpacing(10)
        t = QtWidgets.QLabel("<span style='font-size:17px;font-weight:600;color:#fff'>Strategy properties</span>")
        v.addWidget(t)
        f = QtWidgets.QFormLayout()
        f.setHorizontalSpacing(18)
        f.setVerticalSpacing(9)
        f.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)

        def combo(items, current):
            c = QtWidgets.QComboBox()
            for text, data in items:
                c.addItem(text, data)
            c.setCurrentIndex(max(0, c.findData(current)))
            return c

        def spin(val, hi=1000.0, dec=2, suffix=""):
            w = QtWidgets.QDoubleSpinBox()
            w.setRange(0, hi)
            w.setDecimals(dec)
            w.setValue(val)
            w.setSuffix(suffix)
            return w

        def head(text):
            h = QtWidgets.QLabel(text.upper())
            h.setStyleSheet(f"color:{theme.MUTED};font-size:11px;font-weight:600;padding-top:8px;")
            f.addRow(h)
        direction = combo([("Long only", "long"), ("Short only", "short"), ("Long and short", "both")], cur.direction)
        fill = combo([("Next bar's open", "next_open"), ("This bar's close", "close")], cur.fill)
        capital = spin(self.capital.value(), 1e10, 0)
        fee = spin(self.fee.value(), 5, 3, " %")
        size, slip = spin(cur.size_pct, 1000, 2, " %"), spin(cur.slippage_pct, 10, 3, " %")
        sl, tp, tr = spin(cur.sl_pct, 100, 2, " %"), spin(cur.tp_pct, 1000, 2, " %"), spin(cur.trail_pct, 100, 2, " %")
        sq = QtWidgets.QLineEdit(cur.square_off)
        sq.setPlaceholderText("HH:MM, e.g. 15:15  (empty = off)")
        head("Orders")
        f.addRow("Initial capital", capital)
        f.addRow("Order size (% of equity)", size)
        f.addRow("Direction", direction)
        f.addRow("Fill at", fill)
        head("Costs")
        f.addRow("Commission per side", fee)
        f.addRow("Slippage per fill", slip)
        head("Exits  (0 = off)")
        f.addRow("Stop loss", sl)
        f.addRow("Take profit", tp)
        f.addRow("Trailing stop", tr)
        f.addRow("Intraday square-off", sq)
        v.addLayout(f)
        note = QtWidgets.QLabel("If a stop and a target are both inside one candle, the stop is assumed first. "
                                "Gaps fill at the open.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.MUTED};font-size:12px;")
        v.addWidget(note)
        foot = QtWidgets.QHBoxLayout()
        foot.addStretch(1)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(d.reject)
        ok = QtWidgets.QPushButton("Ok")
        ok.setObjectName("primary")
        ok.setDefault(True)
        ok.clicked.connect(d.accept)
        foot.addWidget(cancel)
        foot.addWidget(ok)
        v.addLayout(foot)
        if d.exec():
            self.capital.setValue(capital.value())
            self.fee.setValue(fee.value())
            self.bt_extra = dict(direction=direction.currentData(), fill=fill.currentData(), size_pct=size.value(),
                                 slippage_pct=slip.value(), sl_pct=sl.value(), tp_pct=tp.value(),
                                 trail_pct=tr.value(), square_off=sq.text().strip())
            self.run_backtest()

    def run_backtest(self):
        """Run button / Cmd+Enter: the strategy runs in a background thread so the chart never freezes."""
        panel = self.active
        if panel.df is None:
            self._strat_say("Load a chart first.", False)
            return
        code = self.strategy_code()
        if not re.search(r"^def\s+strategy\s*\(", code, re.M):
            r = self.run_backtest_on(panel)                 # script mode already runs as a separate process
            if not r["ok"]:
                self._strat_say(r["error"].splitlines()[-1] if r["error"] else "Script failed", False)
            return
        w = getattr(self, "_bt_worker", None)
        if w is not None and w.isRunning():
            self._bt_again = True                           # run once more with the newest code when this one ends
            return
        self.editor.set_error_line(None)
        self._set_running(True)
        cfg = self.bt_config(None, None)
        self._bt_worker = BacktestWorker(code, panel.chart.effective_df(), cfg, self)
        self._bt_worker.finished_ok.connect(lambda sig, res, log, dt, p=panel: self._bt_done(p, sig, res, log, dt))
        self._bt_worker.failed.connect(lambda err, log, p=panel: self._bt_failed(p, err, log))
        self._bt_worker.start()

    def remove_strategy(self):
        p = self.active
        p._strategy_on = False
        p.result = p.signals = None
        p.chart.clear_strategy()
        p.chart.set_trades(__import__("pandas").DataFrame())
        self._show_result(p)
        self._strat_say("Strategy removed from this chart.", None)

    def _set_running(self, on: bool):
        for b, txt in ((self.run_btn, "\u25b6  Run"), (self.run2_btn, "\u25b6  Run backtest")):
            b.setEnabled(not on)
            b.setText("Running\u2026" if on else txt)
        if on:
            self._strat_say("Running\u2026", None)

    def _strat_say(self, html: str, ok):
        col = theme.UP if ok else (theme.DOWN if ok is False else theme.MUTED)
        self.strat_status.setText(f"<span style='color:{col}'>{html}</span>")
        if ok is False:
            self.statusBar().showMessage(QtGui.QTextDocumentFragment.fromHtml(html).toPlainText(), 8000)

    def _bt_finished_common(self):
        self._set_running(False)
        if getattr(self, "_bt_again", False):
            self._bt_again = False
            QtCore.QTimer.singleShot(0, self.run_backtest)

    def _bt_done(self, panel, sig, res, log, dt):
        if log:
            self.add_log(log)
        panel.result, panel.signals = res, sig
        panel._strategy_on = True
        panel.chart.set_strategy_overlays(sig.overlays)
        panel.chart.set_trades(res.trades)
        if panel is self.active:
            self._show_result(panel)
            self.results_dock.show()
            self.tabs.setCurrentIndex(0)
        mt = res.metrics
        self._strat_say(f"\u2713 {mt['trades']} trades \u00b7 net {mt['total_return'] * 100:+.2f}% \u00b7 "
                        f"max DD {mt['max_drawdown'] * 100:.2f}% \u00b7 {dt * 1000:.0f} ms", True)
        self.statusBar().showMessage(
            f"Backtest ({panel.params['symbol']}): {mt['trades']} trades, net {mt['total_return'] * 100:+.2f}% "
            f"(buy & hold {mt['buy_hold'] * 100:+.2f}%), max drawdown {mt['max_drawdown'] * 100:.2f}%")
        self._bt_finished_common()

    def _bt_failed(self, panel, err, log):
        self.add_log((log or "") + err)
        line = bt.error_line(err)
        last = err.strip().splitlines()[-1] if err.strip() else "Error"
        if line is not None:
            self.editor.set_error_line(line)
        import html as _html
        self._strat_say(("Line %d: " % line if line else "") + _html.escape(last), False)
        self._bt_finished_common()

    def run_backtest_on(self, panel, capital: float | None = None, fee: float | None = None) -> dict:
        if panel.df is None:
            return {"ok": False, "error": "No data loaded in this chart yet", "log": ""}
        if not re.search(r"^def\s+strategy\s*\(", self.strategy_code(), re.M):
            return self.run_script_mode(panel, capital)
        data = panel.chart.effective_df()
        buf = io.StringIO()
        try:
            sig = bt.run_user_strategy(self.strategy_code(), data, out=buf)
            cfg = self.bt_config(capital, fee)
            res = bt.run_backtest(data, sig, cfg)
        except Exception:  # noqa: BLE001 - show user-code errors in the Log tab
            err = traceback.format_exc()
            self.add_log(buf.getvalue() + err)
            line = bt.error_line(err)
            if line is not None:
                self.editor.set_error_line(line)
            return {"ok": False, "error": err, "log": buf.getvalue()}
        if buf.getvalue():
            self.add_log(buf.getvalue())
        panel.result, panel.signals = res, sig
        panel._strategy_on = True
        panel.chart.set_strategy_overlays(sig.overlays)
        panel.chart.set_trades(res.trades)
        if panel is self.active:
            self._show_result(panel)
            self.results_dock.show()
            self.tabs.setCurrentIndex(0)
        m = res.metrics
        self.statusBar().showMessage(
            f"Backtest ({panel.params['symbol']}): {m['trades']} trades, net {m['total_return'] * 100:+.2f}% "
            f"(buy & hold {m['buy_hold'] * 100:+.2f}%), max drawdown {m['max_drawdown'] * 100:.2f}%")
        return {"ok": True, "result": res, "log": buf.getvalue()}

    def _show_result(self, panel):
        res = panel.result
        if res is None:
            self.summary.setRowCount(0)
            self.trades_tbl.setRowCount(0)
            self.equity_plot.clear()
            self._clear_overview()
            return
        self._fill_summary(res)
        self._fill_trades(res.trades, panel)
        self._fill_equity(res, panel)

    def _fill_summary(self, res):
        rows = bt.format_metrics(res.metrics)
        half = (len(rows) + 1) // 2
        self.summary.setRowCount(half)
        self.summary.clearContents()
        for n, (k, v) in enumerate(rows):
            r, c = n % half, 0 if n < half else 2
            ki, vi = QtWidgets.QTableWidgetItem(k), QtWidgets.QTableWidgetItem(v)
            ki.setForeground(QtGui.QColor(theme.MUTED))
            if k == "Net profit":
                vi.setForeground(QtGui.QColor(theme.UP if res.metrics["net_profit"] >= 0 else theme.DOWN))
            self.summary.setItem(r, c, ki)
            self.summary.setItem(r, c + 1, vi)

    _SIGNAL = {"signal": "Signal", "sl": "Stop loss", "tp": "Take profit", "trail": "Trailing stop",
               "square_off": "Square-off"}

    def _fill_trades(self, trades, panel):
        intraday = panel.chart.axes[0].intraday
        tf = "%d %b %y %H:%M" if intraday else "%d %b %Y"
        AL = QtCore.Qt.AlignmentFlag
        self.trades_tbl.setRowCount(len(trades))
        cum = 0.0
        for r, t in enumerate(trades.itertuples(index=False)):
            cum += float(t.pnl)
            side = str(getattr(t, "side", "long") or "long")
            reason = "Open" if t.open else self._SIGNAL.get(str(getattr(t, "reason", "") or ""), str(getattr(t, "reason", "")))
            vals = [str(r + 1), side.capitalize(), reason, f"{t.entry_time:{tf}}", f"{t.exit_time:{tf}}",
                    f"{t.entry_price:,.2f}", f"{t.exit_price:,.2f}", f"{t.qty:,.2f}",
                    f"{t.pnl:+,.2f}", f"{t.pnl_pct * 100:+.2f}%", f"{cum:+,.2f}", str(t.bars)]
            for c, text in enumerate(vals):
                item = QtWidgets.QTableWidgetItem(text)
                item.setTextAlignment((AL.AlignRight if c >= 5 else AL.AlignLeft) | AL.AlignVCenter)
                if c in (8, 9):
                    item.setForeground(QtGui.QColor(theme.UP if t.pnl >= 0 else theme.DOWN))
                elif c == 10:
                    item.setForeground(QtGui.QColor(theme.UP if cum >= 0 else theme.DOWN))
                elif c == 1:
                    item.setForeground(QtGui.QColor(theme.UP if side == "long" else theme.DOWN))
                elif c == 2 and t.open:
                    item.setForeground(QtGui.QColor("#ff9800"))
                self.trades_tbl.setItem(r, c, item)

    def _clear_overview(self):
        for lab in self.ov_cards.values():
            lab.setText(f"<span style='color:{theme.MUTED};font-size:12px'>{lab._title}</span><br>"
                        f"<span style='color:#5d606b;font-size:17px'>\u2014</span>")

    def _fill_overview(self, res):
        mt = res.metrics
        fm = dict(bt.format_metrics(mt))

        def card(key, value, sub="", color="#dbdbdb"):
            lab = self.ov_cards[key]
            lab.setText(f"<span style='color:{theme.MUTED};font-size:12px'>{lab._title}</span><br>"
                        f"<span style='color:{color};font-size:17px;font-weight:600'>{value}</span>"
                        + (f"&nbsp;<span style='color:{color};font-size:12px'>{sub}</span>" if sub else ""))
        net = mt.get("net_profit", 0.0)
        ncol = theme.UP if net >= 0 else theme.DOWN
        card("net", f"{net:+,.2f}", f"{mt.get('total_return', 0) * 100:+.2f}%", ncol)
        card("trades", f"{int(mt.get('trades', 0))}")
        card("win", fm.get("Win rate", "\u2014"))
        pf = mt.get("profit_factor", float("nan"))
        card("pf", fm.get("Profit factor", "\u2014"), color=(theme.UP if pf == pf and pf >= 1 else theme.DOWN))
        card("dd", fm.get("Max drawdown", "\u2014"), color=theme.DOWN)
        avg = mt.get("avg_trade", 0.0)
        card("avg", fm.get("Avg trade", "\u2014"), color=theme.UP if avg >= 0 else theme.DOWN)
        card("bars", fm.get("Avg bars in trade", "\u2014"))

    def _fill_equity(self, res, panel):
        self.equity_axis.set_index(panel.df.index)
        self.equity_plot.clear()
        x = np.arange(len(res.equity))
        eq = res.equity.to_numpy(float)
        base = float(eq[0]) if len(eq) else 0.0
        self.equity_plot.plot(x, res.buy_hold.to_numpy(), pen=pg.mkPen("#5d606b", width=1.2), name="Buy & hold")
        self.equity_plot.plot(x, eq, pen=pg.mkPen("#089981", width=1.8), fillLevel=base,
                              brush=pg.mkBrush(8, 153, 129, 40), name="Strategy equity")
        self._fill_overview(res)

    def _trade_clicked(self, row, _col):
        res = self.active.result
        if res is None or row >= len(res.trades):
            return
        t = res.trades.iloc[row]
        self.active.chart.focus_range(t.entry_idx - 25, t.exit_idx + 25)

    # ------------------------------------------------------------------ Claude connection
    def show_connect_dialog(self):
        from .connect_dialog import ConnectDialog
        ConnectDialog(self).exec()


def _setup_logging():
    d = datamod.USER_DIR
    d.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(d / "chartlab.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    def hook(exc_type, exc, tb):
        logging.error("Uncaught exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook


def selftest() -> int:
    """`ChartLab --selftest`: start the real window without a screen, load bars, draw, run a strategy, leave.
    The Mac build runs this so a build that cannot open never reaches the download page."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import tempfile
    import numpy as np
    import pandas as pd
    theme.apply_pg_config()
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setStyle(theme.make_style())
    theme.check_icon_path()
    app.setStyleSheet(theme.STYLESHEET)
    win = MainWindow(QtCore.QSettings(os.path.join(tempfile.mkdtemp(), "t.ini"), QtCore.QSettings.Format.IniFormat))
    win.resize(1440, 860)
    win.show()
    rng = np.random.default_rng(3)
    c = 1000 * np.exp(np.cumsum(rng.normal(0, 0.01, 400)))
    df = pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c * 1.003, "Volume": 1e5},
                      index=pd.bdate_range(end="2026-10-30", periods=400))
    p = win.active
    p.set_dataframe(df, "SELFTEST")
    for k in ("sma20", "bb", "rsi"):
        win.ind_actions[k].setChecked(True)
    win.start_auto_update()
    win.set_auto_refresh(0)
    win.set_strategy_code(FALLBACK_STRATEGY)
    win.run_backtest()
    t = QtCore.QElapsedTimer()
    t.start()
    while t.elapsed() < 15000 and not win.run2_btn.isEnabled():
        app.processEvents()
    for _ in range(20):
        app.processEvents()
    ok = (p.chart.df is not None and not win.grab().isNull() and p.result is not None
          and p.chart.price.vb.viewRange()[0][1] > 0)
    # real-time prices: the socket module and its TLS plug-in must be inside the app, and a tick must move the bar
    from PySide6 import QtNetwork
    from . import live
    ws = live.QtWebSockets is not None
    tls = bool(QtNetwork.QSslSocket.supportsSsl())
    px = float(c[-1]) * 1.02
    tick = p.on_tick(px, None) == "same" and abs(float(p.df["Close"].iloc[-1]) - px) < 1e-6
    print(f"live: websockets={ws} tls={tls} tick={tick}", flush=True)
    ok = ok and ws and tls and tick
    print(("SELFTEST OK " if ok else "SELFTEST FAILED ") + win.version, flush=True)
    quit_cleanly(win, 0 if ok else 1)
    return 0 if ok else 1


def app_icon_path():
    """The picture for the Dock / taskbar while the app runs. On a Mac every app's picture sits inside a margin
    (Apple's icon grid: an 824 body in a 1024 square), so the Mac one is the padded picture - the plain logo fills
    the whole tile and looks a size bigger than its neighbours in the Dock."""
    names = ("icon_mac.png", "logo.png") if sys.platform == "darwin" else ("logo.png",)
    for n in names:
        p = datamod.ROOT / "assets" / n
        if p.exists():
            return p
    return None


def main():
    _setup_logging()
    theme.apply_pg_config()
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle(theme.make_style())
    app.setApplicationName("ChartLab")
    _ic = app_icon_path()
    if _ic is not None:
        app.setWindowIcon(QtGui.QIcon(str(_ic)))
    theme.check_icon_path()                       # tick / chevron images the style sheet points at
    app.setStyleSheet(theme.STYLESHEET)
    win = MainWindow()
    geo = app.primaryScreen().availableGeometry()
    win.resize(min(1680, int(geo.width() * 0.96)), min(980, int(geo.height() * 0.94)))
    win.setWindowOpacity(0.0)
    win.show()
    fade = QtCore.QPropertyAnimation(win, b"windowOpacity", win)
    fade.setDuration(450)
    fade.setStartValue(0.0)
    fade.setEndValue(1.0)
    fade.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
    fade.start()
    try:
        from .api import ApiServer
        win.api = ApiServer(win)
        win.api.start()
        win.api_label.setText(f"Claude link: ON (port {win.api.port})  ")
        win.clock.setToolTip(f"Claude link: ON (port {win.api.port})")
    except Exception as e:  # noqa: BLE001 - the app must still work without the API
        logging.exception("API server failed")
        win.api_label.setText("Claude link: off  ")
        win.clock.setToolTip("Claude link: off")
        win.add_log(f"Claude link could not start: {e}")
    QtCore.QTimer.singleShot(300, win.on_load)
    if win.source.currentData() in ("yahoo", "auto"):
        QtCore.QTimer.singleShot(2500, win.watchlist.refresh)
    win.start_auto_update()
    win.start_update_checks()                     # (was only wired into the self-test before: never ran in the app)
    rc = app.exec()
    quit_cleanly(win, rc)


def quit_cleanly(win, rc: int = 0, exit_process: bool = True):
    """Leave without a macOS 'ChartLab quit unexpectedly' report.

    Qt aborts the whole process when a QThread object is destroyed while its thread still runs - which is
    exactly what happens if you quit while a price / quote fetch is in flight. So: give running fetches a
    moment to finish, flush settings + log to disk, then end the process without tearing those objects down."""
    import os
    import time
    try:
        threads = [t for t in win.findChildren(QtCore.QThread) if t.isRunning()]
        end = time.monotonic() + 1.2
        for t in threads:
            t.requestInterruption()
        for t in threads:
            t.wait(max(0, int((end - time.monotonic()) * 1000)))
        win.settings.sync()
    except Exception:  # noqa: BLE001 - never block quitting
        logging.exception("shutdown")
    logging.shutdown()
    for s in (sys.stdout, sys.stderr):
        try:
            s.flush()
        except Exception:  # noqa: BLE001
            pass
    if exit_process:
        os._exit(rc)
