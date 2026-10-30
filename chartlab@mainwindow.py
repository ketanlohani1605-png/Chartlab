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
INDICATORS = {"sma20": "SMA 20", "sma50": "SMA 50", "sma200": "SMA 200", "ema20": "EMA 20", "ema50": "EMA 50",
              "bb": "Bollinger Bands (20, 2)", "rsi": "RSI (14)  —  separate pane"}
LAYOUTS = {"1": "Single chart", "2h": "Two charts — side by side", "2v": "Two charts — stacked",
           "4": "Four charts"}
LAYOUT_COUNT = {"1": 1, "2h": 2, "2v": 2, "4": 4}
DRAW_BUTTONS = [("trend", "╱"), ("ray", "↗"), ("hline", "—"), ("vline", "|"),
                ("rect", "▭"), ("fib", "φ"), ("measure", "m"), ("text", "T"),
                ("hray", "→"), ("crossline", "+")]
STYLE_NAMES = {"candles": "Candles", "hollow": "Hollow candles", "line": "Line", "area": "Area"}


def _safe_key(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, settings: QtCore.QSettings | None = None):
        super().__init__()
        self.setWindowTitle("ChartLab")
        self.setUnifiedTitleAndToolBarOnMac(True)
        self.settings = settings or QtCore.QSettings("ChartLab", "ChartLab")
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
        self._build_results_dock()
        self._build_right_rail()
        self._build_menu()
        self.setStatusBar(QtWidgets.QStatusBar())
        self.api_label = QtWidgets.QLabel("")
        self.statusBar().addPermanentWidget(self.api_label)
        self._shortcuts()
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
            p.drawingsChanged.connect(self._save_drawings)
            p.toolChanged.connect(lambda panel, tool: self._tool_changed(tool))
            p.chart.replayChanged.connect(lambda: self.replay_btn.setChecked(self.active.chart.replay['active']))
            p.chart.alertRequested.connect(lambda price, pp=p: self.create_alert(price, pp))
            p.chart.tradeRequested.connect(self._on_trade)
            p.chart.settingsRequested.connect(self.open_settings)
            p.chart.typed.connect(self._chart_typed)
            self.panels.append(p)
        self.active = self.panels[0]
        self.active.set_active(True)

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
        adj = QtWidgets.QLabel("ADJ  ")
        adj.setToolTip("Prices are adjusted for splits / dividends")
        adj.setStyleSheet(f"color:{theme.MUTED}; font-size:12px; font-weight:600;")
        h.addWidget(adj)
        self.clock = QtWidgets.QLabel("")
        self.clock.setStyleSheet(f"color:{theme.TEXT}; font-size:12px;")
        h.addWidget(self.clock)
        self._clock_timer = QtCore.QTimer(self)
        self._clock_timer.timeout.connect(self._tick_clock)
        self._clock_timer.start(1000)
        self._tick_clock()
        return bar

    @staticmethod
    def _tv_interval(text: str) -> str:
        return {"1D": "D", "1W": "W", "1M": "M"}.get(text, text[:-1] if text.endswith("m") else text)

    def _sync_interval_label(self):
        for val, text in INTERVALS:
            if self.interval_btns[val].isChecked():
                self.interval_btn.setText(self._tv_interval(text))
                return

    def _zoom_x(self, f):
        vb = self.active.chart.price.vb
        a, b = vb.viewRange()[0]
        c = b - (b - a) * 0.15
        vb.setXRange(c - (c - a) * f, c + (b - c) * f, padding=0)

    def _on_trade(self, side, price):
        sym = self.active.chart.label or "?"
        self.statusBar().showMessage(f"PAPER {side} {sym} @ {price:,.2f}  (paper trade, no broker order sent)", 6000)

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
        d = SymbolSearch(self, list(self._symbol_model.stringList()), text)
        if d.exec() and d.chosen:
            self.symbol.setText(d.chosen)
            self.on_load()

    def open_interval_popup(self, first: str):
        from .tvui import IntervalPopup
        d = IntervalPopup(self, first)
        if d.exec() and d.code in self.interval_btns:
            self.interval_btns[d.code].click()
            self._sync_interval_label()

    def _tick_clock(self):
        import datetime as _dt
        now = _dt.datetime.now().astimezone()
        off = now.utcoffset().total_seconds() / 3600
        sign = "+" if off >= 0 else "-"
        hh, mm = int(abs(off)), int(round((abs(off) % 1) * 60))
        self.clock.setText(f"{now:%H:%M:%S}  UTC{sign}{hh}" + (f":{mm:02d}" if mm else ""))

    def _build_toolbar(self):
        tb = self.addToolBar("Data")
        tb.setObjectName("topbar")
        tb.setMovable(False)
        tb.setIconSize(QtCore.QSize(22, 22))
        self.symbol = QtWidgets.QLineEdit()
        self.symbol.setObjectName("symbolbox")
        self.symbol.setPlaceholderText("Symbol")
        self.symbol.setToolTip("Symbol: RELIANCE, NIFTY, BSE:SBIN, US:AAPL, BTC-USD  (Enter to load)")
        self.symbol.setFixedWidth(130)
        self.symbol.returnPressed.connect(self.on_load)
        pill = QtWidgets.QFrame()
        pill.setObjectName("symbolpill")
        ph = QtWidgets.QHBoxLayout(pill)
        ph.setContentsMargins(4, 0, 6, 0)
        ph.setSpacing(0)
        ph.addWidget(self.symbol)
        cmp_ = QtWidgets.QLabel()
        cmp_.setPixmap(icons.get("compare").pixmap(20, 20))
        cmp_.setToolTip("Compare or add symbol")
        ph.addWidget(cmp_)
        tb.addWidget(pill)
        tb.addWidget(self._gap(6))
        self._symbol_model = QtCore.QStringListModel(self._suggestions())
        comp = QtWidgets.QCompleter(self._symbol_model, self)
        comp.setCaseSensitivity(QtCore.Qt.CaseSensitivity.CaseInsensitive)
        comp.setFilterMode(QtCore.Qt.MatchFlag.MatchContains)
        comp.setMaxVisibleItems(12)
        comp.activated.connect(lambda _t: QtCore.QTimer.singleShot(0, self.on_load))
        self.symbol.setCompleter(comp)

        self.source = QtWidgets.QComboBox()
        self.source.addItem("Yahoo", "yahoo")
        self.source.addItem("Local file", "file")
        for name in datamod.list_providers():
            self.source.addItem(f"Provider: {name}", f"provider:{name}")
        self._fill_api_sources()
        self.source.setToolTip("Data source")
        self.source.currentIndexChanged.connect(self._source_changed)
        tb.addWidget(self.source)
        tb.addSeparator()

        self.interval_group = QtWidgets.QButtonGroup(self)
        self.interval_btns = {}
        for val, text in INTERVALS:
            b = QtWidgets.QToolButton()
            b.setText(text)
            b.setCheckable(True)
            b.setToolTip(f"Timeframe {text}")
            self.interval_group.addButton(b)
            self.interval_btns[val] = b
            b.clicked.connect(lambda _=False, v=val: self._interval_clicked(v))
            b.setVisible(False)                        # kept for state/tests; TradingView shows just the current one
        self.interval_btns["1d"].setChecked(True)
        self.interval_btn = QtWidgets.QToolButton()
        self.interval_btn.setToolTip("Time interval")
        self.interval_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        im = QtWidgets.QMenu(self)
        for val, text in INTERVALS:
            im.addAction(text, lambda v=val: self.interval_btns[v].click())
        self.interval_btn.setMenu(im)
        tb.addWidget(self.interval_btn)
        self.interval_group.buttonToggled.connect(lambda *_: self._sync_interval_label())
        self._sync_interval_label()
        tb.addSeparator()

        style_menu = QtWidgets.QMenu(self)
        self.style_group = QtGui.QActionGroup(self)
        self.style_actions = {}
        for key in STYLES:
            a = style_menu.addAction(STYLE_NAMES[key])
            a.setCheckable(True)
            self.style_group.addAction(a)
            a.triggered.connect(lambda _=False, k=key: self._style_chosen(k))
            self.style_actions[key] = a
        self.style_actions["candles"].setChecked(True)
        self._menu_button(tb, "", style_menu, icon="candles_icon", tip="Chart type")

        ind_menu = QtWidgets.QMenu(self)
        self.ind_actions = {}
        for key, text in INDICATORS.items():
            a = ind_menu.addAction(text)
            a.setCheckable(True)
            a.toggled.connect(lambda on, k=key: self._indicator_toggled(k, on))
            self.ind_actions[key] = a
        self._menu_button(tb, " Indicators", ind_menu, icon="fx", tip="Indicators")

        layout_menu = QtWidgets.QMenu(self)
        self.layout_group = QtGui.QActionGroup(self)
        self.layout_actions = {}
        for key, text in LAYOUTS.items():
            a = layout_menu.addAction(text)
            a.setCheckable(True)
            self.layout_group.addAction(a)
            a.triggered.connect(lambda _=False, k=key: self.set_layout(k))
            self.layout_actions[key] = a
        self.layout_actions["1"].setChecked(True)
        self._menu_button(tb, "", layout_menu, icon="layout_icon", tip="Chart layout")

        latest = QtWidgets.QToolButton()
        latest.setIcon(icons.get("latest"))
        latest.setToolTip("Jump to the latest bar  (End)")
        latest.clicked.connect(lambda: self.active.chart.go_latest())
        tb.addWidget(latest)

        self.replay_btn = QtWidgets.QToolButton()
        self.replay_btn.setIcon(icons.get("replay_icon"))
        self.replay_btn.setText(" Replay")
        self.replay_btn.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.replay_btn.setCheckable(True)
        self.replay_btn.setToolTip("Bar replay: pick a start bar, then play the market bar by bar")
        self.replay_btn.clicked.connect(self.toggle_replay)
        tb.addWidget(self.replay_btn)

        self.alert_btn = QtWidgets.QToolButton()
        self.alert_btn.setIcon(icons.get("bell"))
        self.alert_btn.setText(" Alert")
        self.alert_btn.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.alert_btn.setToolTip("Create alert  (Alt+A)")
        self.alert_btn.clicked.connect(lambda: self.create_alert())
        tb.addWidget(self.alert_btn)

        self.acct_btn = QtWidgets.QToolButton()
        self.acct_btn.setText("Accounts")
        self.acct_btn.setToolTip("Broker accounts: API data + run one script on many accounts")
        m_ = QtWidgets.QMenu(self.acct_btn)
        m_.addAction("Manage broker accounts…", self.manage_accounts)
        m_.addAction("Run script on multiple accounts…", self.run_on_accounts)
        m_.addAction("Stop all runs", self.stop_script)
        self.acct_btn.setMenu(m_)
        self.acct_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        tb.addWidget(self.acct_btn)

        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        def tbtn(icon, tip, fn):
            b = QtWidgets.QToolButton()
            b.setIcon(icons.get(icon))
            b.setToolTip(tip)
            b.clicked.connect(fn)
            tb.addWidget(b)
            return b
        tbtn("undo", "Undo  (Ctrl+Z)", lambda: self.active.chart.layer.undo())
        tbtn("redo", "Redo  (Ctrl+Shift+Z)", lambda: self.active.chart.layer.redo())
        tb.addSeparator()
        tbtn("gear", "Settings", self.open_settings)
        tbtn("fullscreen", "Full screen  (Ctrl+Cmd+F)", self.toggle_fullscreen)
        tbtn("camera", "Save chart image", self.save_chart_image)
        tb.addSeparator()

        self.range = QtWidgets.QComboBox()
        self.range.addItems(RANGES)
        self.range.setCurrentText("2y")
        self.range.setToolTip("How much history to load")
        self.range.activated.connect(lambda *_: self.on_load())
        tb.addWidget(self.range)
        self.load_btn = QtWidgets.QPushButton("Load")
        self.load_btn.setObjectName("primary")
        self.load_btn.clicked.connect(self.on_load)
        tb.addWidget(self.load_btn)
        tb.addWidget(self._gap(8))
        self.run_btn = QtWidgets.QPushButton("\u25b6  Run")
        self.run_btn.setObjectName("pill")
        self.run_btn.setToolTip("Run the strategy / script  (Cmd+R)")
        self.run_btn.clicked.connect(self.run_backtest)
        tb.addWidget(self.run_btn)
        tb.addWidget(self._gap(4))

    @staticmethod
    def _suggestions() -> list[str]:
        base = ["NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN",
                "ITC", "LT", "BHARTIARTL", "TATAMOTORS", "TATASTEEL", "AXISBANK", "KOTAKBANK", "HINDUNILVR",
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
        tb.setIconSize(QtCore.QSize(24, 24))
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
        tb.addSeparator()
        ICON = {"hray": "ray", "crossline": "cross", "ext": "trend", "arrow": "ray", "ellipse": "rect",
                "pricerange": "measure", "daterange": "measure", "long": "measure", "short": "measure"}
        SC = {"trend": "Alt+T", "hline": "Alt+H", "hray": "Alt+J", "vline": "Alt+V", "crossline": "Alt+C",
              "fib": "Alt+F", "rect": "Shift+Alt+R"}
        GROUPS = [("Trend lines", ["trend", "ray", "ext", "hray", "hline", "vline", "crossline"]),
                  ("Fib", ["fib"]),
                  ("Shapes", ["rect", "ellipse", "arrow"]),
                  ("Forecasting and measurement", ["long", "short", "pricerange", "daterange", "measure"]),
                  ("Annotation", ["text"])]
        self.tool_groups = {}
        for gname, keys in GROUPS:
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
                continue
            menu = QtWidgets.QMenu(self)
            for k in keys:
                act = menu.addAction(icons.get(ICON.get(k, k)), TOOL_LABELS[k] + (f"\t{SC[k]}" if k in SC else ""))
                act.triggered.connect(lambda _=False, k=k, ga=ga: (ga.setIcon(icons.get(ICON.get(k, k))), ga.setData(k), self._tool_clicked(k)))
            ga.setMenu(menu)
            btn = tb.widgetForAction(ga)
            btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self._tool_icon = ICON
        tb.addSeparator()
        self.magnet_action = tb.addAction(icons.get("magnet"), "")
        self.magnet_action.setCheckable(True)
        self.magnet_action.setChecked(True)
        self.magnet_action.setToolTip("Magnet: snap drawings to candle open/high/low/close")
        self.magnet_action.toggled.connect(self._magnet_toggled)
        zi = tb.addAction(icons.get("zoom"), "")
        zi.setToolTip("Zoom in")
        zi.triggered.connect(lambda: self._zoom_x(0.7))
        lk = tb.addAction(icons.get("lock"), "")
        lk.setCheckable(True)
        lk.setToolTip("Lock all drawings")
        lk.toggled.connect(lambda on: [setattr(p.chart.layer, "locked", on) for p in self.panels])
        hd = tb.addAction(icons.get("eye"), "")
        hd.setCheckable(True)
        hd.setToolTip("Hide all drawings")
        hd.toggled.connect(lambda on: [p.chart.layer.set_hidden(on) for p in self.panels])
        d = tb.addAction(icons.get("eraser"), "")
        d.setToolTip("Delete selected drawing  (Delete)")
        d.triggered.connect(lambda: self.active.chart.layer.delete_selected())
        c = tb.addAction(icons.get("trash"), "")
        c.setToolTip("Remove all drawings on this chart")
        c.triggered.connect(lambda: self.active.chart.layer.clear())

    def _build_right_rail(self):
        tb = QtWidgets.QToolBar("Panels")
        tb.setObjectName("rightrail")
        tb.setOrientation(QtCore.Qt.Orientation.Vertical)
        tb.setMovable(False)
        tb.setIconSize(QtCore.QSize(24, 24))
        self.addToolBar(QtCore.Qt.ToolBarArea.RightToolBarArea, tb)
        for dock, ico, tip in ((self.watch_dock, "watchlist", "Watchlist"),
                               (self.strategy_dock, "code", "Python strategy editor"),
                               (self.results_dock, "table", "Backtest results"),
                               (self.alerts_dock, "bell", "Alerts")):
            act = dock.toggleViewAction()
            act.setIcon(icons.get(ico))
            act.setToolTip(tip)
            tb.addAction(act)

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
            self.alerts_dock.show()
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
        v.addWidget(self.editor, 1)

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

        self.strategy_dock = QtWidgets.QDockWidget("Strategy (Python)", self)
        self.strategy_dock.setWidget(w)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.strategy_dock)

    def _build_results_dock(self):
        self.tabs = QtWidgets.QTabWidget()
        self.summary = QtWidgets.QTableWidget(0, 4)
        self.summary.horizontalHeader().setVisible(False)
        self.summary.verticalHeader().setVisible(False)
        self.summary.verticalHeader().setDefaultSectionSize(30)
        self.summary.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.summary.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.summary.setShowGrid(False)
        self.tabs.addTab(self.summary, "Summary")

        cols = ["#", "Entry time", "Exit time", "Entry", "Exit", "Qty", "P&L", "P&L %", "Bars"]
        self.trades_tbl = QtWidgets.QTableWidget(0, len(cols))
        self.trades_tbl.setHorizontalHeaderLabels(cols)
        self.trades_tbl.verticalHeader().setVisible(False)
        self.trades_tbl.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.trades_tbl.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.trades_tbl.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.trades_tbl.setAlternatingRowColors(True)
        self.trades_tbl.cellClicked.connect(self._trade_clicked)
        self.tabs.addTab(self.trades_tbl, "Trades")

        self.equity_axis = DateAxis("bottom")
        self.equity_plot = pg.PlotWidget(axisItems={"bottom": self.equity_axis})
        self.equity_plot.showGrid(x=True, y=True, alpha=0.12)
        self.equity_plot.addLegend(offset=(10, 10))
        self.tabs.addTab(self.equity_plot, "Equity curve")

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.tabs.addTab(self.log_view, "Log")

        self.results_dock = QtWidgets.QDockWidget("Backtest results", self)
        self.results_dock.setWidget(self.tabs)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, self.results_dock)
        self.tabifyDockWidget(self.watch_dock, self.strategy_dock)
        self.tabifyDockWidget(self.watch_dock, self.alerts_dock)
        for d in (self.watch_dock, self.strategy_dock, self.alerts_dock):
            d.setTitleBarWidget(QtWidgets.QWidget())      # tabs already name them; the rail toggles them
        self.results_dock.hide()
        self.watch_dock.raise_()
        self.setTabPosition(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, QtWidgets.QTabWidget.TabPosition.North)
        self.watch_dock.setObjectName("dock_watchlist")
        self.strategy_dock.setObjectName("dock_strategy")
        self.alerts_dock.setObjectName("dock_alerts")
        self.results_dock.setObjectName("dock_results")
        for tb_ in self.findChildren(QtWidgets.QToolBar):
            tb_.setObjectName("tb_" + (tb_.windowTitle() or "main").replace(" ", "_"))
        self._default_state = self.saveState(3)
        self.resizeDocks([self.strategy_dock], [480], QtCore.Qt.Orientation.Horizontal)
        self.resizeDocks([self.watch_dock], [250], QtCore.Qt.Orientation.Horizontal)
        self.resizeDocks([self.results_dock], [260], QtCore.Qt.Orientation.Vertical)

    def _build_menu(self):
        mb = self.menuBar()
        f = mb.addMenu("File")
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
        s.addAction("Stop script", self.stop_script, QtGui.QKeySequence("Ctrl+."))
        s.addAction("Choose Python for scripts…", self.choose_python)
        s.addSeparator()
        s.addAction("Manage broker accounts…", self.manage_accounts)
        s.addAction("Run script on multiple accounts…", self.run_on_accounts)
        v = mb.addMenu("View")
        v.addAction("Reset layout", self.reset_layout)
        v.addAction(self.watch_dock.toggleViewAction())
        v.addAction(self.strategy_dock.toggleViewAction())
        v.addAction(self.results_dock.toggleViewAction())
        c = mb.addMenu("Claude")
        c.addAction("Connect to Claude…", self.show_connect_dialog)

    def _shortcuts(self):
        for key in ("Delete", "Backspace"):
            QtGui.QShortcut(QtGui.QKeySequence(key), self,
                            activated=lambda: self.active.chart.layer.delete_selected())
        QtGui.QShortcut(QtGui.QKeySequence("Escape"), self, activated=self._escape)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Z"), self, activated=lambda: self.active.chart.layer.undo())
        for k in ("Ctrl+Shift+Z", "Ctrl+Y"):
            QtGui.QShortcut(QtGui.QKeySequence(k), self, activated=lambda: self.active.chart.layer.redo())
        QtGui.QShortcut(QtGui.QKeySequence("Alt+A"), self, activated=lambda: self.create_alert())
        QtGui.QShortcut(QtGui.QKeySequence("Alt+R"), self, activated=lambda: self.active.chart.go_latest())
        for key, tool in (("Alt+T", "trend"), ("Alt+H", "hline"), ("Alt+J", "hray"), ("Alt+V", "vline"),
                          ("Alt+C", "crossline"), ("Alt+F", "fib"), ("Shift+Alt+R", "rect")):
            QtGui.QShortcut(QtGui.QKeySequence(key), self, activated=lambda t=tool: self._tool_clicked(t))
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+K"), self, activated=self.quick_search)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+Alt+H"), self, activated=lambda: [p.chart.layer.set_hidden(not p.chart.layer.hidden) for p in self.panels])
        QtGui.QShortcut(QtGui.QKeySequence("Alt+Return"), self, activated=lambda: self.statusBar().showMessage("Maximise chart: use the 1-chart layout", 3000))
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+S"), self, activated=lambda: self.statusBar().showMessage("Layout saved (auto)", 2000))
        QtGui.QShortcut(QtGui.QKeySequence("Alt+G"), self, activated=lambda: self._goto_date())
        QtGui.QShortcut(QtGui.QKeySequence("Alt+I"), self, activated=lambda: self.active.chart.price.vb.invertY(not self.active.chart.price.vb.state["yInverted"]))
        QtGui.QShortcut(QtGui.QKeySequence("Alt+D"), self, activated=lambda: self.statusBar().showMessage("Data window: hover the chart – values show in the legend", 3000))
        QtGui.QShortcut(QtGui.QKeySequence("Shift+F"), self, activated=self.toggle_fullscreen)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+,"), self, activated=self.open_settings)

    def open_settings(self, tab: int = 0):
        from . import settings as cs
        dlg = cs.SettingsDialog(self, tab if isinstance(tab, int) else 0)
        dlg.applied.connect(lambda _s: (cs.save(self.settings), self.apply_chart_settings()))
        dlg.exec()

    def apply_chart_settings(self):
        for p in self.panels:
            p.chart.apply_settings()

    def _chart_typed(self, ch: str):
        if ch.isalpha():
            self.open_symbol_search(ch.upper())
        else:
            self.open_interval_popup(ch)

    def quick_search(self):
        """Ctrl+K: 'Search tool or function' – a filterable list of actions."""
        items = {"Settings": self.open_settings, "Go to date": self._goto_date, "Full screen": self.toggle_fullscreen,
                 "Save chart image": self.save_chart_image, "Reset chart view": lambda: self.active.chart.go_latest(),
                 "Create alert": lambda: self.create_alert(), "Symbol search": self.open_symbol_search,
                 "Change interval": lambda: self.open_interval_popup("")}
        for lbl, fn in getattr(self, "_extra_actions", {}).items():
            items[lbl] = fn
        d = QtWidgets.QDialog(self)
        d.setWindowFlag(QtCore.Qt.WindowType.FramelessWindowHint)
        d.resize(520, 360)
        d.setStyleSheet("QDialog{background:#1e222d;border:1px solid #434651;border-radius:8px;} QLineEdit{background:#131722;border:1px solid #434651;border-radius:6px;padding:8px;font-size:14px;} QListWidget{background:transparent;border:0;} QListWidget::item{padding:8px;} QListWidget::item:selected{background:#2a2e39;}")
        lay = QtWidgets.QVBoxLayout(d)
        e = QtWidgets.QLineEdit()
        e.setPlaceholderText("Search tool or function")
        lst = QtWidgets.QListWidget()
        lay.addWidget(e)
        lay.addWidget(lst)

        def fill(t=""):
            lst.clear()
            for k in items:
                if t.lower() in k.lower():
                    lst.addItem(k)
            if lst.count():
                lst.setCurrentRow(0)

        def go(*_):
            it = lst.currentItem()
            if it:
                d.accept()
                items[it.text()]()
        e.textChanged.connect(fill)
        e.returnPressed.connect(go)
        lst.itemActivated.connect(go)
        fill()
        d.exec()

    def toggle_fullscreen(self):
        self.setWindowState(self.windowState() ^ QtCore.Qt.WindowState.WindowFullScreen)

    def save_chart_image(self):
        ch = self.active.chart
        default = str(Path.home() / "Pictures" / f"{(ch.label or 'chart').replace(' ', '_')}.png")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save chart image", default, "PNG (*.png)")
        if path:
            ch.glw.grab().save(path)
            self.statusBar().showMessage(f"Saved {path}")

    def toggle_replay(self):
        ch = self.active.chart
        if ch.replay["active"]:
            ch.replay_exit()
        else:
            ch.replay_begin()
        self.replay_btn.setChecked(ch.replay["active"])

    def _escape(self):
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
        if iv in self.interval_btns:
            self.interval_btns[iv].setChecked(True)
        i = self.source.findData(st.value("source", "yahoo"))
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
        state = st.value("dock_state_v3")
        if state:
            self.restoreState(state, 3)
        self._source_changed()

    def reset_layout(self):
        self.restoreState(self._default_state, 3)
        for d in (self.watch_dock, self.strategy_dock, self.alerts_dock, self.results_dock):
            d.show()
        self.watch_dock.raise_()

    def closeEvent(self, e):
        st = self.settings
        st.setValue("symbol", self.symbol.text())
        st.setValue("interval", self._current_interval())
        st.setValue("range", self.range.currentText())
        st.setValue("source", self.source.currentData())
        st.setValue("strategy", self.editor.toPlainText())
        st.setValue("layout", self.layout_name)
        st.setValue("geometry", self.saveGeometry())
        st.setValue("dock_state_v3", self.saveState(3))
        self._save_watchlist()
        if self.api is not None:
            self.api.stop()
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
            if prm["interval"] in self.interval_btns:
                self.interval_btns[prm["interval"]].setChecked(True)
            for k, a in self.ind_actions.items():
                a.setChecked(k in p.chart._enabled)
            self.style_actions[p.chart.style].setChecked(True)
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

    def manage_accounts(self):
        acctmod.AccountsDialog(self).exec()
        self._fill_api_sources()

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

    def _indicator_toggled(self, key, on):
        if not self._syncing:
            self.active.chart.set_indicator(key, on)

    def _style_chosen(self, key):
        if not self._syncing:
            self.active.chart.set_style(key)

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
            self.draw_actions.get(tool, self.draw_actions[None]).setChecked(True)
        finally:
            self._syncing = False

    def _watchlist_chosen(self, sym: str):
        self.symbol.setText(sym)
        if self.source.currentData() == "file":
            self.source.setCurrentIndex(self.source.findData("yahoo"))
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
        self.statusBar().showMessage(f"Loading {panel.params['symbol']}…")

    def _on_loaded(self, panel):
        self.load_btn.setEnabled(True)
        self._load_drawings(panel)
        df = panel.df
        self._refresh_alert_lines()
        if len(df):   # catch moves since last session; crossing alerts only arm on this first look
            last = float(df["Close"].iloc[-1])
            self.alerts.check(self._alert_key(panel), last, last, last)
        self.statusBar().showMessage(
            f"{panel.label}: {len(df):,} bars  |  {df.index[0]:%d %b %Y} → {df.index[-1]:%d %b %Y}")
        if panel is self.active:
            self._sync_toolbar()
            self._show_result(panel)

    def _on_load_failed(self, panel, msg):
        self.load_btn.setEnabled(True)
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
            self.tabs.setCurrentWidget(self.summary)
        m = res.metrics
        self.statusBar().showMessage(f"Script done: {m['trades']} trades, net {m['total_return'] * 100:+.2f}%")

    def bt_config(self, capital=None, fee=None) -> "bt.Config":
        cfg = bt.Config(**getattr(self, "bt_extra", {}))
        cfg.capital = capital if capital is not None else self.capital.value()
        cfg.fee_pct = fee if fee is not None else self.fee.value()
        return cfg

    def show_bt_settings(self):
        cur = bt.Config(**getattr(self, "bt_extra", {}))
        d = QtWidgets.QDialog(self)
        d.setWindowTitle("Backtest settings")
        f = QtWidgets.QFormLayout(d)
        direction = QtWidgets.QComboBox()
        direction.addItems(["long", "short", "both"])
        direction.setCurrentText(cur.direction)
        fill = QtWidgets.QComboBox()
        fill.addItems(["next_open", "close"])
        fill.setCurrentText(cur.fill)

        def spin(v, hi=1000.0, dec=2):
            w = QtWidgets.QDoubleSpinBox()
            w.setRange(0, hi)
            w.setDecimals(dec)
            w.setValue(v)
            return w
        size, slip = spin(cur.size_pct, 1000), spin(cur.slippage_pct, 10, 3)
        sl, tp, tr = spin(cur.sl_pct, 100), spin(cur.tp_pct, 1000), spin(cur.trail_pct, 100)
        sq = QtWidgets.QLineEdit(cur.square_off)
        sq.setPlaceholderText("HH:MM, e.g. 15:15 (empty = off)")
        f.addRow("Direction", direction)
        f.addRow("Fill price", fill)
        f.addRow("Position size (% of equity)", size)
        f.addRow("Slippage % per fill", slip)
        f.addRow("Stop loss % (0 = off)", sl)
        f.addRow("Take profit % (0 = off)", tp)
        f.addRow("Trailing stop % (0 = off)", tr)
        f.addRow("Square-off time", sq)
        note = QtWidgets.QLabel("Signals fill at the next bar's open (or this bar's close). If a stop and a target are both "
                                "inside one candle the stop is assumed first. Gaps fill at the open.")
        note.setWordWrap(True)
        f.addRow(note)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        f.addRow(bb)
        if d.exec():
            self.bt_extra = dict(direction=direction.currentText(), fill=fill.currentText(), size_pct=size.value(),
                                 slippage_pct=slip.value(), sl_pct=sl.value(), tp_pct=tp.value(),
                                 trail_pct=tr.value(), square_off=sq.text().strip())
            self.run_backtest()

    def run_backtest(self):
        r = self.run_backtest_on(self.active)
        if not r["ok"]:
            self.statusBar().showMessage(r["error"].splitlines()[-1] if r["error"] else "Backtest failed")
            self.tabs.setCurrentWidget(self.log_view)

    def run_backtest_on(self, panel, capital: float | None = None, fee: float | None = None) -> dict:
        if panel.df is None:
            return {"ok": False, "error": "No data loaded in this chart yet", "log": ""}
        if not re.search(r"^def\s+strategy\s*\(", self.strategy_code(), re.M):
            return self.run_script_mode(panel, capital)
        data = panel.chart.effective_df()
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                sig = bt.run_user_strategy(self.strategy_code(), data)
            cfg = self.bt_config(capital, fee)
            res = bt.run_backtest(data, sig, cfg)
        except Exception:  # noqa: BLE001 - show user-code errors in the Log tab
            err = traceback.format_exc()
            self.add_log(buf.getvalue() + err)
            return {"ok": False, "error": err, "log": buf.getvalue()}
        if buf.getvalue():
            self.add_log(buf.getvalue())
        panel.result, panel.signals = res, sig
        panel.chart.set_strategy_overlays(sig.overlays)
        panel.chart.set_trades(res.trades)
        if panel is self.active:
            self._show_result(panel)
            self.results_dock.show()
            self.tabs.setCurrentWidget(self.summary)
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

    def _fill_trades(self, trades, panel):
        intraday = panel.chart.axes[0].intraday
        tf = "%d %b %y %H:%M" if intraday else "%d %b %Y"
        self.trades_tbl.setRowCount(len(trades))
        for r, t in enumerate(trades.itertuples(index=False)):
            vals = [str(r + 1), f"{t.entry_time:{tf}}", f"{t.exit_time:{tf}}" + (" (open)" if t.open else ""),
                    f"{t.entry_price:,.2f}", f"{t.exit_price:,.2f}", f"{t.qty:,.2f}",
                    f"{t.pnl:+,.2f}", f"{t.pnl_pct * 100:+.2f}%", str(t.bars)]
            for c, text in enumerate(vals):
                item = QtWidgets.QTableWidgetItem(text)
                if c in (6, 7):
                    item.setForeground(QtGui.QColor(theme.UP if t.pnl >= 0 else theme.DOWN))
                self.trades_tbl.setItem(r, c, item)

    def _fill_equity(self, res, panel):
        self.equity_axis.set_index(panel.df.index)
        self.equity_plot.clear()
        x = np.arange(len(res.equity))
        self.equity_plot.plot(x, res.buy_hold.to_numpy(), pen=pg.mkPen(theme.MUTED, width=1.2), name="Buy & hold")
        self.equity_plot.plot(x, res.equity.to_numpy(), pen=pg.mkPen(theme.ACCENT, width=2), name="Strategy")

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


def main():
    _setup_logging()
    theme.apply_pg_config()
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("ChartLab")
    _ic = datamod.ROOT / "assets" / "logo.png"
    if _ic.exists():
        app.setWindowIcon(QtGui.QIcon(str(_ic)))
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
    except Exception as e:  # noqa: BLE001 - the app must still work without the API
        logging.exception("API server failed")
        win.api_label.setText("Claude link: off  ")
        win.add_log(f"Claude link could not start: {e}")
    QtCore.QTimer.singleShot(300, win.on_load)
    if win.source.currentData() == "yahoo":
        QtCore.QTimer.singleShot(2500, win.watchlist.refresh)
    sys.exit(app.exec())
