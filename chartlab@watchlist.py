"""Watchlist side panel, TradingView style: grouped rows (Indices / Stocks / US / Crypto) with logo, Last, Chg,
Chg%, and a detail card for the selected symbol. Click a row to open it in the active chart."""
from __future__ import annotations

import json

import pandas as pd
import time
import zlib

from PySide6 import QtCore, QtGui, QtWidgets

from . import data as datamod
from . import icons
from . import theme

DEFAULT_WATCHLIST = ["NIFTY", "BANKNIFTY", "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
                     "SBIN", "ITC", "LT", "BHARTIARTL", "TMPV"]
INDICES = {"NIFTY", "NIFTY50", "BANKNIFTY", "NIFTYBANK", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "NIFTYIT", "INDIAVIX"}
LOGO_COLORS = ["#2962ff", "#089981", "#f23645", "#ff9800", "#9c27b0", "#00bcd4", "#e91e63", "#4caf50", "#795548",
               "#607d8b"]
ROLE_SYM = QtCore.Qt.ItemDataRole.UserRole
ROLE_QUOTE = QtCore.Qt.ItemDataRole.UserRole + 1
ROLE_SECTION = QtCore.Qt.ItemDataRole.UserRole + 2


def group_of(sym: str) -> str:
    s = sym.upper()
    if s.split(":")[-1] in INDICES or s.startswith("^"):
        return "INDICES"
    if s.endswith("-USD") or s.endswith("USDT"):
        return "CRYPTO"
    if s.startswith("US:"):
        return "US STOCKS"
    return "STOCKS"


GROUP_ORDER = ["INDICES", "STOCKS", "US STOCKS", "CRYPTO"]


def exchange_of(sym: str) -> str:
    s = sym.upper()
    if s.startswith("BSE:"):
        return "BSE"
    if s.startswith("US:"):
        return "NASDAQ"
    if s.endswith("-USD"):
        return "CRYPTO"
    return "NSE"


def logo_color(sym: str) -> str:
    return LOGO_COLORS[zlib.crc32(sym.encode()) % len(LOGO_COLORS)]


def draw_logo(p: QtGui.QPainter, rect: QtCore.QRectF, sym: str, font: QtGui.QFont):
    p.setPen(QtCore.Qt.PenStyle.NoPen)
    p.setBrush(QtGui.QColor(logo_color(sym)))
    p.drawEllipse(rect)
    f = QtGui.QFont(font)
    f.setPixelSize(max(int(rect.height() * 0.5), 8))
    f.setBold(True)
    p.setFont(f)
    p.setPen(QtGui.QColor("white"))
    p.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, sym.split(":")[-1][:1])


class QuoteWorker(QtCore.QThread):
    done = QtCore.Signal(dict, dict)
    failed = QtCore.Signal(str)

    def __init__(self, symbols, parent=None):
        super().__init__(parent)
        self.symbols = list(symbols)

    def run(self):
        try:
            import yfinance as yf
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")
            return
        out, extra = {}, {}
        ysyms = {s: datamod.normalize_symbol(s) for s in self.symbols}

        def take(sym, h):
            h = h.dropna(subset=["Close"]) if h is not None and len(h) else None
            if h is None or h.empty:
                return
            last = float(h["Close"].iloc[-1])
            prev = float(h["Close"].iloc[-2]) if len(h) >= 2 else last
            out[sym] = (last, last / prev - 1 if prev else 0.0)
            extra[sym] = {"high": float(h["High"].iloc[-1]), "low": float(h["Low"].iloc[-1]),
                          "open": float(h["Open"].iloc[-1]), "prev": prev,
                          "volume": float(h["Volume"].iloc[-1]), "time": time.strftime("%H:%M")}
        try:                                            # one request for the whole list (fast)
            data = yf.download(list(dict.fromkeys(ysyms.values())), period="5d", interval="1d", group_by="ticker",
                               auto_adjust=False, progress=False, threads=True)
            for sym, ys in ysyms.items():
                try:
                    h = data[ys] if len(set(ysyms.values())) > 1 else data
                    if hasattr(h.columns, "levels"):
                        h = h.droplevel(0, axis=1) if h.columns.nlevels > 1 else h
                    take(sym, h)
                except Exception:  # noqa: BLE001 - symbol missing from the batch: try it alone below
                    pass
        except Exception:  # noqa: BLE001
            pass
        for sym, ys in ysyms.items():                   # anything the batch missed, one by one
            if sym in out:
                continue
            try:
                take(sym, yf.Ticker(ys).history(period="5d", interval="1d", auto_adjust=False))
            except Exception:  # noqa: BLE001
                continue
        if out or not self.symbols:
            self.done.emit(out, extra)
        else:
            self.failed.emit("no prices returned (offline?)")


class QuoteDelegate(QtWidgets.QStyledItemDelegate):
    """TradingView watchlist row: logo · Symbol | Last | Chg | Chg%  and collapsible section headers."""

    def sizeHint(self, option, index):
        return QtCore.QSize(option.rect.width(), 30 if index.data(ROLE_SECTION) else 32)

    @staticmethod
    def cols(r: QtCore.QRect):
        w = r.width() - 14
        x = r.x()
        return (QtCore.QRect(x + int(w * 0.38), r.y(), int(w * 0.25), r.height()),
                QtCore.QRect(x + int(w * 0.63), r.y(), int(w * 0.17), r.height()),
                QtCore.QRect(x + int(w * 0.80), r.y(), int(w * 0.20), r.height()))

    def paint(self, p, option, index):
        p.save()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = option.rect
        al = QtCore.Qt.AlignmentFlag
        f = QtGui.QFont(option.font)
        sec = index.data(ROLE_SECTION)
        if sec:
            f.setPixelSize(11)
            f.setWeight(QtGui.QFont.Weight.DemiBold)
            p.setFont(f)
            p.setPen(QtGui.QColor(theme.MUTED))
            collapsed = bool(index.data(ROLE_QUOTE))
            p.drawText(r.adjusted(12, 0, 0, 0), al.AlignVCenter | al.AlignLeft, ("›  " if collapsed else "⌄  ") + sec)
            p.restore()
            return
        sel = bool(option.state & QtWidgets.QStyle.StateFlag.State_Selected)
        if sel:
            p.setPen(QtGui.QPen(QtGui.QColor("#868993"), 1))
            p.setBrush(QtGui.QColor("#1e1e1e"))
            p.drawRoundedRect(QtCore.QRectF(r).adjusted(2.5, 0.5, -2.5, -0.5), 5, 5)
        elif option.state & QtWidgets.QStyle.StateFlag.State_MouseOver:
            p.fillRect(r, QtGui.QColor("#1a1a1a"))
        sym = index.data(ROLE_SYM)
        q = index.data(ROLE_QUOTE)
        draw_logo(p, QtCore.QRectF(r.x() + 12, r.center().y() - 9, 18, 18), sym, f)
        f.setPixelSize(14)
        f.setWeight(QtGui.QFont.Weight.Normal)
        p.setFont(f)
        p.setPen(QtGui.QColor("#dbdbdb"))
        name_rect = QtCore.QRect(r.x() + 38, r.y(), int((r.width() - 14) * 0.38) - 30, r.height())
        p.drawText(name_rect, al.AlignVCenter | al.AlignLeft,
                   p.fontMetrics().elidedText(sym.split(":")[-1], QtCore.Qt.TextElideMode.ElideRight, name_rect.width()))
        c_last, c_chg, c_pct = self.cols(r)
        if q:
            col = QtGui.QColor(theme.UP if q[1] >= 0 else theme.DOWN)
            chg = q[0] - q[0] / (1 + q[1]) if q[1] > -1 else 0.0
            dec = price_decimals_of(sym, q[0])                 # TradingView: each symbol in its own tick
            p.setPen(QtGui.QColor("#dbdbdb"))
            p.drawText(c_last, al.AlignVCenter | al.AlignRight, f"{q[0]:,.{dec}f}")
            p.setPen(col)
            p.drawText(c_chg, al.AlignVCenter | al.AlignRight,
                       f"{chg:+,.{dec}f}".replace("+", "").replace("-", "−"))
            p.drawText(c_pct, al.AlignVCenter | al.AlignRight,
                       f"{q[1] * 100:+.2f}%".replace("+", "").replace("-", "−"))
        else:
            p.setPen(QtGui.QColor("#4a4a4a"))
            for c in (c_last, c_chg, c_pct):
                p.drawText(c, al.AlignVCenter | al.AlignRight, "—")
        p.restore()


def price_decimals_of(sym: str, price: float) -> int:
    """Decimals TradingView writes this symbol's price with: an NSE stock the tick of its price level (RELIANCE
    1,358.5, HDFCBANK 809.20), anything else two."""
    try:
        from .symbols import band_decimals, variable_tick
        if exchange_of(sym) == "NSE" and variable_tick(sym):
            return band_decimals(price)
    except Exception:  # noqa: BLE001
        pass
    return 2


def market_state(sym: str, now=None) -> tuple[str, str]:
    """('open' | 'pre' | 'closed', text) for TradingView's market-status line. NSE / BSE 09:15-15:30 IST Mon-Fri
    (pre-open from 09:00), US 09:30-16:00 New York, crypto always. Exchange holidays are not known here."""
    import datetime as _dt
    ex = exchange_of(sym)
    if ex == "CRYPTO":
        return "open", "Market open \u00b7 24/7"
    utc = _dt.datetime.now(_dt.timezone.utc) if now is None else now
    if ex == "NASDAQ":
        try:
            from zoneinfo import ZoneInfo
            t = utc.astimezone(ZoneInfo("America/New_York"))
        except Exception:  # noqa: BLE001 - no tz database: rough EST
            t = utc - _dt.timedelta(hours=5)
        if t.weekday() < 5 and _dt.time(9, 30) <= t.time() < _dt.time(16, 0):
            return "open", "Market open"
        return "closed", "Market closed"
    t = utc + _dt.timedelta(hours=5, minutes=30)
    if t.weekday() < 5:
        if _dt.time(9, 15) <= t.time() < _dt.time(15, 30):
            return "open", "Market open"
        if _dt.time(9, 0) <= t.time() < _dt.time(9, 15):
            return "pre", "Pre-open session"
    return "closed", "Market closed"


def session_close(sym: str, now=None):
    """The UTC time today's session closes, while it is open - TradingView's countdown on a daily bar runs to it
    (02:20:43 left at 13:09 IST on NSE). Crypto's daily candle closes at 00:00 UTC. None when closed / unknown."""
    import datetime as _dt
    utc = _dt.datetime.now(_dt.timezone.utc) if now is None else now
    ex = exchange_of(sym)
    if ex == "CRYPTO":
        return (utc + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if market_state(sym, utc)[0] != "open":
        return None
    if ex == "NASDAQ":
        try:
            from zoneinfo import ZoneInfo
            ny = utc.astimezone(ZoneInfo("America/New_York"))
            return ny.replace(hour=16, minute=0, second=0, microsecond=0).astimezone(_dt.timezone.utc)
        except Exception:  # noqa: BLE001 - no tz database
            return None
    ist = _dt.timedelta(hours=5, minutes=30)
    return (utc + ist).replace(hour=15, minute=30, second=0, microsecond=0) - ist


STATE_COLOR = {"open": "#089981", "pre": "#ff9800", "closed": "#787b86"}


class PerfWorker(QtCore.QObject):
    """One year of daily bars for the selected symbol: 52-week range and the 1W ... 1Y performance tiles.
    Runs on a plain daemon thread (quitting while it downloads must never abort the app)."""
    done = QtCore.Signal(str, dict)

    def __init__(self, sym, parent=None):
        super().__init__(parent)
        self.sym = sym
        self._t = None

    def start(self):
        import threading
        self._t = threading.Thread(target=self.run, daemon=True)
        self._t.start()

    def isRunning(self) -> bool:
        return self._t is not None and self._t.is_alive()

    def run(self):
        try:
            import yfinance as yf
            h = yf.Ticker(datamod.normalize_symbol(self.sym)).history(period="1y", interval="1d", auto_adjust=False)
            h = h.dropna(subset=["Close"])
            if h.empty:
                return
            c = h["Close"]
            last, t_last = float(c.iloc[-1]), c.index[-1]

            def ago(days=None, ytd=False):
                cut = (t_last.normalize().replace(month=1, day=1) if ytd else t_last - pd.Timedelta(days=days))
                before = c[c.index <= cut]
                base = float(before.iloc[-1]) if len(before) else float(c.iloc[0])
                return (last / base - 1) * 100 if base else None
            perf = {"1W": ago(7), "1M": ago(30), "3M": ago(91), "6M": ago(182), "YTD": ago(ytd=True), "1Y": ago(365)}
            self.done.emit(self.sym, {"perf": perf, "hi52": float(h["High"].max()), "lo52": float(h["Low"].min()),
                                      "avgvol": float(h["Volume"].tail(30).mean()), "t": time.time()})
        except Exception:  # noqa: BLE001 - extra details are optional
            return


class PerfTiles(QtWidgets.QWidget):
    """TradingView's 'Performance' squares: 1W 1M 3M / 6M YTD 1Y, green or red by how much it moved."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.perf = None
        self.setFixedHeight(118)

    def set_perf(self, perf):
        self.perf = perf
        self.setVisible(bool(perf))
        self.update()

    def paintEvent(self, _ev):
        if not self.perf:
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        f = self.font()
        f.setPixelSize(12)
        p.setFont(f)
        p.setPen(QtGui.QColor(theme.MUTED))
        p.drawText(QtCore.QRect(0, 0, self.width(), 16), QtCore.Qt.AlignmentFlag.AlignLeft, "Performance")
        keys = ["1W", "1M", "3M", "6M", "YTD", "1Y"]
        gap, top = 6, 22
        w = (self.width() - 2 * gap) / 3
        h = (self.height() - top - gap) / 2
        for k, key in enumerate(keys):
            v = self.perf.get(key)
            r = QtCore.QRectF((k % 3) * (w + gap), top + (k // 3) * (h + gap), w, h)
            if v is None:
                col, txtc = QtGui.QColor("#1e222d"), QtGui.QColor("#787b86")
            else:
                base = QtGui.QColor(theme.UP if v >= 0 else theme.DOWN)
                a = min(0.18 + abs(v) / 25.0, 0.55)
                bg = QtGui.QColor("#000000")
                col = QtGui.QColor(int(base.red() * a + bg.red() * (1 - a)), int(base.green() * a + bg.green() * (1 - a)),
                                   int(base.blue() * a + bg.blue() * (1 - a)))
                txtc = QtGui.QColor(theme.UP if v >= 0 else theme.DOWN).lighter(130)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(r, 6, 6)
            f.setPixelSize(14)
            f.setWeight(QtGui.QFont.Weight.DemiBold)
            p.setFont(f)
            p.setPen(txtc)
            p.drawText(r.adjusted(0, 6, 0, -h / 2 + 4), QtCore.Qt.AlignmentFlag.AlignCenter,
                       "\u2014" if v is None else f"{v:+.2f}%")
            f.setPixelSize(11)
            f.setWeight(QtGui.QFont.Weight.Normal)
            p.setFont(f)
            p.setPen(QtGui.QColor("#9598a1"))
            p.drawText(r.adjusted(0, h / 2, 0, -4), QtCore.Qt.AlignmentFlag.AlignCenter, key)


class RangeBar(QtWidgets.QWidget):
    """Day's range: low ———●——— high."""

    def __init__(self, parent=None, title: str = "Day's range"):
        super().__init__(parent)
        self.setFixedHeight(42)
        self.title = title
        self.lo = self.hi = self.cur = None

    def set_range(self, lo, hi, cur):
        self.lo, self.hi, self.cur = lo, hi, cur
        self.update()

    def paintEvent(self, _ev):
        if self.lo is None or self.hi is None:
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w = self.width()
        f = self.font()
        f.setPixelSize(12)
        p.setFont(f)
        p.setPen(QtGui.QColor(theme.MUTED))
        p.drawText(QtCore.QRect(0, 0, w, 14), QtCore.Qt.AlignmentFlag.AlignLeft, self.title)
        p.setPen(QtGui.QColor("#dbdbdb"))
        p.drawText(QtCore.QRect(0, 26, w, 16), QtCore.Qt.AlignmentFlag.AlignLeft, f"{self.lo:,.2f}")
        p.drawText(QtCore.QRect(0, 26, w, 16), QtCore.Qt.AlignmentFlag.AlignRight, f"{self.hi:,.2f}")
        y = 19
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor("#2a2a2a"))
        p.drawRoundedRect(QtCore.QRectF(0, y, w, 3), 1.5, 1.5)
        span = (self.hi - self.lo) or 1.0
        x = (min(max(self.cur, self.lo), self.hi) - self.lo) / span * w
        p.setBrush(QtGui.QColor("#2962ff"))
        p.drawRoundedRect(QtCore.QRectF(0, y, x, 3), 1.5, 1.5)
        p.setBrush(QtGui.QColor("white"))
        p.drawEllipse(QtCore.QPointF(min(max(x, 4), w - 4), y + 1.5), 4, 4)


class SymbolCard(QtWidgets.QFrame):
    """TradingView-like details for the selected watchlist symbol."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("symcard")
        self.setStyleSheet("QFrame#symcard { background: #000; border-top: 4px solid #1a1a1a; }"
                           "QLabel { background: transparent; }")
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(4)
        top = QtWidgets.QHBoxLayout()
        self.logo = QtWidgets.QLabel()
        self.logo.setFixedSize(26, 26)
        self.title = QtWidgets.QLabel("")
        self.title.setStyleSheet("font-size:16px; font-weight:600; color:white;")
        top.addWidget(self.logo)
        top.addWidget(self.title, 1)
        v.addLayout(top)
        self.sub = QtWidgets.QLabel("")
        self.sub.setStyleSheet(f"color:{theme.MUTED}; font-size:12px;")
        v.addWidget(self.sub)
        v.addSpacing(6)
        self.price = QtWidgets.QLabel("")
        self.price.setTextFormat(QtCore.Qt.TextFormat.RichText)
        v.addWidget(self.price)
        self.when = QtWidgets.QLabel("")
        self.when.setStyleSheet(f"color:{theme.MUTED}; font-size:12px;")
        v.addWidget(self.when)
        v.addSpacing(6)
        self.state = QtWidgets.QLabel("")
        self.state.setTextFormat(QtCore.Qt.TextFormat.RichText)
        v.addWidget(self.state)
        v.addSpacing(6)
        self.range = RangeBar()
        v.addWidget(self.range)
        self.range52 = RangeBar(title="52 week range")
        v.addWidget(self.range52)
        self.vol = QtWidgets.QLabel("")
        self.vol.setTextFormat(QtCore.Qt.TextFormat.RichText)
        v.addWidget(self.vol)
        v.addSpacing(6)
        self.tiles = PerfTiles()
        self.tiles.hide()
        v.addWidget(self.tiles)
        self.details: dict = {}                            # sym -> {perf, hi52, lo52, avgvol, t}
        self._perf_job = None

    def show_symbol(self, sym: str | None, q, ex: dict | None):
        if not sym:
            self.hide()
            return
        self.show()
        pm = QtGui.QPixmap(52, 52)
        pm.fill(QtCore.Qt.GlobalColor.transparent)
        pp = QtGui.QPainter(pm)
        pp.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        draw_logo(pp, QtCore.QRectF(0, 0, 52, 52), sym, self.font())
        pp.end()
        pm.setDevicePixelRatio(2.0)
        self.logo.setPixmap(pm)
        self.title.setText(sym.split(":")[-1])
        cur = "USD" if exchange_of(sym) in ("NASDAQ", "CRYPTO") else "INR"
        self.sub.setText(f"{group_of(sym).title()}  •  {exchange_of(sym)}")
        st, text = market_state(sym)
        self.state.setText(f"<span style='color:{STATE_COLOR[st]};font-size:13px'>\u25cf</span>"
                           f"<span style='color:#d1d4dc;font-size:12px'>&nbsp;{text}</span>")
        self._show_details(sym, q)
        if not q:
            self.price.setText(f"<span style='color:{theme.MUTED};font-size:13px'>Prices not loaded yet - "
                               "press ↻ above</span>")
            self.when.setText("")
            self.range.set_range(None, None, None)
            self.vol.setText("")
            return
        col = theme.UP if q[1] >= 0 else theme.DOWN
        chg = q[0] - q[0] / (1 + q[1]) if q[1] > -1 else 0.0
        dec = price_decimals_of(sym, q[0])
        ctxt = f"{chg:+,.{dec}f}&nbsp;&nbsp;{q[1] * 100:+.2f}%".replace("-", "−")
        self.price.setText(f"<span style='font-size:28px;color:white;font-weight:500'>{q[0]:,.{dec}f}</span>"
                           f"<span style='font-size:11px;color:{theme.MUTED}'>&nbsp;{cur}&nbsp;&nbsp;</span>"
                           f"<span style='font-size:16px;color:{col}'>{ctxt}</span>")
        ex = ex or {}
        self.when.setText(f"Last update at {ex.get('time', '--:--')}  ({'live' if ex.get('live') else 'delayed'})")
        if "low" in ex:
            self.range.set_range(ex["low"], ex["high"], q[0])
            v = ex.get("volume", 0.0)
            vs = theme.fmt_vol(v)
            self.vol.setText(f"<span style='color:{theme.MUTED};font-size:12px'>Volume</span><br>"
                             f"<span style='color:#dbdbdb;font-size:13px'>{vs}</span>")
        else:
            self.range.set_range(None, None, None)
            self.vol.setText("")

    def _show_details(self, sym, q):
        d = self.details.get(sym)
        if d is None or time.time() - d.get("t", 0) > 1800:
            self._fetch_details(sym)
        if d is None:
            self.range52.set_range(None, None, None)
            self.range52.setVisible(False)
            self.tiles.set_perf(None)
            return
        last = q[0] if q else None
        lo, hi = d["lo52"], d["hi52"]
        if last:
            lo, hi = min(lo, last), max(hi, last)
        self.range52.setVisible(True)
        self.range52.set_range(lo, hi, last if last else hi)
        self.tiles.set_perf(d["perf"])

    def _fetch_details(self, sym):
        if self._perf_job is not None and self._perf_job.isRunning():
            return
        job = PerfWorker(sym, self)
        self._perf_job = job
        job.done.connect(self._details_in)
        job.start()

    def _details_in(self, sym, d):
        self.details[sym] = d
        if self.title.text() == sym.split(":")[-1]:
            par = self.parent()
            while par is not None and not hasattr(par, "_update_card"):
                par = par.parent()
            if par is not None:
                par._update_card()


class WatchlistWidget(QtWidgets.QWidget):
    symbolChosen = QtCore.Signal(str)
    changed = QtCore.Signal()
    addRequested = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.quotes: dict[str, tuple[float, float]] = {}
        self.extra: dict[str, dict] = {}
        self._collapsed: set[str] = set()
        self._worker = None
        self.setObjectName("watchlistPanel")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("QWidget#watchlistPanel { background: #000; }")
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        head = QtWidgets.QHBoxLayout()
        head.setContentsMargins(14, 8, 6, 4)
        t = QtWidgets.QLabel("Watchlist")
        t.setStyleSheet("font-size:15px; font-weight:600; color:white;")
        head.addWidget(t)
        head.addStretch(1)

        def ibtn(icon, tip, fn):
            b = QtWidgets.QToolButton()
            b.setIcon(icons.get(icon))
            b.setIconSize(QtCore.QSize(20, 20))
            b.setToolTip(tip)
            b.clicked.connect(fn)
            head.addWidget(b)
            return b
        ibtn("plus", "Add symbol", self.addRequested.emit)
        self.refresh_btn = ibtn("refresh", "Refresh prices", self.refresh)
        v.addLayout(head)

        self.edit = QtWidgets.QLineEdit()                   # used by tests / the API; the + button opens search
        self.edit.returnPressed.connect(self._add_from_edit)
        self.edit.hide()
        v.addWidget(self.edit)

        hdr = QtWidgets.QWidget()
        hl = QtWidgets.QHBoxLayout(hdr)
        hl.setContentsMargins(14, 2, 14, 4)
        hl.setSpacing(0)
        for txt, stretch, al in (("Symbol", 38, QtCore.Qt.AlignmentFlag.AlignLeft),
                                 ("Last", 25, QtCore.Qt.AlignmentFlag.AlignRight),
                                 ("Chg", 17, QtCore.Qt.AlignmentFlag.AlignRight),
                                 ("Chg%", 20, QtCore.Qt.AlignmentFlag.AlignRight)):
            lab = QtWidgets.QLabel(txt)
            lab.setStyleSheet(f"color:{theme.MUTED}; font-size:12px;")
            lab.setAlignment(al | QtCore.Qt.AlignmentFlag.AlignVCenter)
            hl.addWidget(lab, stretch)
        v.addWidget(hdr)

        self.list = QtWidgets.QListWidget()
        self.list.setStyleSheet("QListWidget { background:#000; border:0; outline:0; }")
        self.list.setItemDelegate(QuoteDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.itemClicked.connect(self._clicked)
        self.list.itemActivated.connect(self._clicked)
        self.list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.currentItemChanged.connect(lambda *_: self._update_card())
        self.list.installEventFilter(self)
        v.addWidget(self.list, 1)

        self.card = SymbolCard()
        self.card.hide()
        v.addWidget(self.card)
        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet(f"color:{theme.MUTED}; font-size:11px; padding: 2px 14px 6px 14px;")
        v.addWidget(self.status)

    # ------------------------------------------------------------------ content
    def symbols(self) -> list[str]:
        out = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            if not it.data(ROLE_SECTION):
                out.append(it.data(ROLE_SYM))
        return out

    def set_symbols(self, syms):
        self._rebuild([str(s).upper() for s in syms])

    def _rebuild(self, syms: list[str], keep: str | None = None):
        keep = keep or (self.list.currentItem().data(ROLE_SYM) if self.list.currentItem() else None)
        self._order = list(dict.fromkeys(syms))
        self.list.clear()
        groups = {g: [s for s in self._order if group_of(s) == g] for g in GROUP_ORDER}
        for g in GROUP_ORDER:
            if not groups[g]:
                continue
            h = QtWidgets.QListWidgetItem()
            h.setData(ROLE_SECTION, g)
            h.setData(ROLE_QUOTE, g in self._collapsed)
            h.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            self.list.addItem(h)
            for s in groups[g]:
                it = QtWidgets.QListWidgetItem()
                it.setData(ROLE_SYM, s)
                it.setData(ROLE_QUOTE, self.quotes.get(s))
                self.list.addItem(it)
                it.setHidden(g in self._collapsed)
                if s == keep:
                    self.list.setCurrentItem(it)
        self._update_card()

    def add(self, sym: str) -> bool:
        sym = sym.strip().upper()
        if not sym or sym in self.symbols():
            return False
        self._rebuild(self.symbols() + [sym], keep=sym)
        self.changed.emit()
        return True

    def remove(self, sym: str) -> bool:
        sym = sym.strip().upper()
        syms = self.symbols()
        if sym not in syms:
            return False
        syms.remove(sym)
        self._rebuild(syms)
        self.changed.emit()
        return True

    def remove_selected(self):
        it = self.list.currentItem()
        if it and not it.data(ROLE_SECTION):
            self.remove(it.data(ROLE_SYM))

    def select(self, sym: str):
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.data(ROLE_SYM) == sym:
                self.list.blockSignals(True)
                self.list.setCurrentItem(it)
                self.list.blockSignals(False)
                self._update_card()
                return

    def _clicked(self, it):
        sec = it.data(ROLE_SECTION)
        if sec:
            if sec in self._collapsed:
                self._collapsed.discard(sec)
            else:
                self._collapsed.add(sec)
            self._rebuild(self.symbols())
            return
        self.symbolChosen.emit(it.data(ROLE_SYM))

    def eventFilter(self, obj, ev):
        if obj is self.list and ev.type() == QtCore.QEvent.Type.KeyPress:
            k = ev.key()
            if k in (QtCore.Qt.Key.Key_Delete, QtCore.Qt.Key.Key_Backspace):
                self.remove_selected()
                return True
            if k in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter, QtCore.Qt.Key.Key_Space):
                it = self.list.currentItem()
                if it is not None:
                    self._clicked(it)
                return True
        return super().eventFilter(obj, ev)

    def _update_card(self):
        it = self.list.currentItem()
        if it is None or it.data(ROLE_SECTION):
            self.card.show_symbol(None, None, None)
            return
        sym = it.data(ROLE_SYM)
        self.card.show_symbol(sym, self.quotes.get(sym), self.extra.get(sym))

    def _add_from_edit(self):
        if self.add(self.edit.text()):
            self.edit.clear()

    def _menu(self, pos):
        it = self.list.itemAt(pos)
        m = QtWidgets.QMenu(self)
        if it and not it.data(ROLE_SECTION):
            sym = it.data(ROLE_SYM)
            m.addAction(f"Open {sym}", lambda: self.symbolChosen.emit(sym))
            m.addAction(f"Remove {sym}\t{theme.sc('Del')}", lambda: self.remove(sym))
            m.addSeparator()
        m.addAction("Add symbol…", self.addRequested.emit)
        m.addAction("Refresh prices", self.refresh)
        m.addAction("Reset to default list", lambda: (self._rebuild(list(DEFAULT_WATCHLIST)), self.changed.emit()))
        m.exec(self.list.mapToGlobal(pos))

    # ------------------------------------------------------------------ quotes
    def refresh(self):
        if self._worker is not None and self._worker.isRunning():
            return
        self.refresh_btn.setEnabled(False)
        self.status.setText("Fetching prices…")
        self._worker = QuoteWorker(self.symbols(), self)
        self._worker.done.connect(self._quotes_in)
        self._worker.failed.connect(self._quotes_failed)
        self._worker.start()

    def _quotes_in(self, q, extra=None):
        self.quotes.update(q)
        self.extra.update(extra or {})
        for i in range(self.list.count()):
            it = self.list.item(i)
            if not it.data(ROLE_SECTION):
                it.setData(ROLE_QUOTE, self.quotes.get(it.data(ROLE_SYM)))
        self._update_card()
        self.refresh_btn.setEnabled(True)
        self.status.setText(f"Updated {QtCore.QTime.currentTime().toString('HH:mm:ss')}  ·  delayed quotes")

    def live_price(self, sym: str, ltp: float, prev: float | None = None):
        """A real-time trade from the broker feed: this row moves now, between the slower full refreshes."""
        ex = self.extra.setdefault(sym, {})
        if prev:
            ex["prev"] = float(prev)
        pv = ex.get("prev")
        if not pv:
            q = self.quotes.get(sym)
            if q and q[0] and (1 + q[1]):
                pv = ex["prev"] = q[0] / (1 + q[1])
        self.quotes[sym] = (float(ltp), (ltp / pv - 1) if pv else 0.0)
        ex["high"], ex["low"] = max(ex.get("high", ltp), ltp), min(ex.get("low", ltp), ltp)
        ex["time"], ex["live"] = time.strftime("%H:%M:%S"), True
        for i in range(self.list.count()):
            it = self.list.item(i)
            if not it.data(ROLE_SECTION) and it.data(ROLE_SYM) == sym:
                it.setData(ROLE_QUOTE, self.quotes[sym])
        if not getattr(self, "_live_note", False):
            self._live_note = True
            self.status.setText("Live prices from Dhan")
        cur = self.list.currentItem()
        if cur is not None and cur.data(ROLE_SYM) == sym:
            self._update_card()

    def chart_price(self, sym: str, last: float, prev: float | None):
        """The chart of this symbol just got new bars: its row shows the same price as the chart (no stale quote)."""
        if sym not in self.symbols() or not last:
            return
        ex = self.extra.setdefault(sym, {})
        if prev:
            ex["prev"] = float(prev)
        pv = ex.get("prev")
        self.quotes[sym] = (float(last), (last / pv - 1) if pv else 0.0)
        ex["time"] = time.strftime("%H:%M")
        for i in range(self.list.count()):
            it = self.list.item(i)
            if not it.data(ROLE_SECTION) and it.data(ROLE_SYM) == sym:
                it.setData(ROLE_QUOTE, self.quotes[sym])
        cur = self.list.currentItem()
        if cur is not None and cur.data(ROLE_SYM) == sym:
            self._update_card()

    def _quotes_failed(self, msg):
        self.refresh_btn.setEnabled(True)
        self.status.setText("Could not fetch prices: " + msg)

    # ------------------------------------------------------------------ persistence helpers
    @staticmethod
    def dumps(syms) -> str:
        return json.dumps(list(syms))

    @staticmethod
    def loads(text) -> list[str]:
        try:
            v = json.loads(text)
            out = [str(x).upper() for x in v] if isinstance(v, list) and v else list(DEFAULT_WATCHLIST)
        except Exception:  # noqa: BLE001
            out = list(DEFAULT_WATCHLIST)
        ren = datamod.RENAMED                           # symbols the exchange renamed (TATAMOTORS -> TMPV)
        seen, fixed = set(), []
        for x in out:
            y = ren.get(x, x)
            if y not in seen:
                seen.add(y)
                fixed.append(y)
        return fixed
