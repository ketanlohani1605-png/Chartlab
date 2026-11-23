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
ROLE_FLASH = QtCore.Qt.ItemDataRole.UserRole + 3      # (+1 up / -1 down, time.monotonic()) of the last price change
FLASH_S = 0.9                                         # how long a changed price glows


def group_of(sym: str) -> str:
    s = sym.upper()
    from . import indices as _ix
    if _ix.is_index(s) or s.split(":")[-1] in INDICES or s.startswith("^"):
        return "INDICES"
    if s.endswith("-USD") or s.endswith("USDT"):
        return "CRYPTO"
    if s.startswith("US:"):
        return "US STOCKS"
    return "STOCKS"


GROUP_ORDER = ["INDICES", "STOCKS", "US STOCKS", "CRYPTO"]


def exchange_of(sym: str) -> str:
    s = sym.upper()
    from . import indices as _ix
    ix = _ix.get(s)
    if ix is not None:
        return ix.exchange
    if s.startswith("BSE:") or s in ("SENSEX", "BANKEX", "SENSEX50"):
        return "BSE"
    if s.startswith("US:"):
        return "NASDAQ"
    if s.endswith(("-USD", "-USDT", "-INR")):
        return "CRYPTO"
    return "NSE"


def kind_of(sym: str) -> str:
    """The line under a symbol's name, before its exchange: an index is 'Index', an ETF 'ETF', a company its
    sector as NSE classifies it ('Financial Services', 'Information Technology' ...), else 'Stock'."""
    s = (sym or "").upper()
    from . import indices as _ix
    if _ix.is_index(s):
        return "Index"
    if exchange_of(s) == "CRYPTO":
        return "Crypto"
    try:
        from . import symbols as _sy
        if _sy.is_etf(s):
            return "ETF"
        sec = _sy.sector_of(s)
        if sec:
            return sec
    except Exception:  # noqa: BLE001
        pass
    return "Stock"


def currency_of(sym: str) -> str:
    """What a price is counted in: INR on NSE / BSE (and BTC-INR), USD in the US and for -USD / -USDT pairs."""
    s = sym.upper()
    if s.endswith("-INR"):
        return "INR"
    if s.endswith("-USDT"):
        return "USDT"
    return "USD" if exchange_of(sym) in ("NASDAQ", "CRYPTO") else "INR"


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


def _unfinished(h) -> bool:
    """Yahoo's newest daily bar with its open but no close yet (for hours after the close, over a weekend all
    weekend)."""
    try:
        return h is not None and len(h) > 0 and pd.isna(h["Close"].iloc[-1]) and pd.notna(h["Open"].iloc[-1])
    except Exception:  # noqa: BLE001
        return False


def _short(h) -> bool:
    """Less than two daily bars with a close: no previous close in it."""
    try:
        return h is None or len(h.dropna(subset=["Close"])) < 2
    except Exception:  # noqa: BLE001
        return True


def finish_bar(h, live: dict | None, sym: str):
    """That unfinished bar completed with the newest price of the same day (the close), else left as it was -
    dropping it would show the day before as 'last' with the day before's change."""
    if not live or not live.get("price") or not live.get("time") or not _unfinished(h):
        return h
    import datetime as _dt
    ex = exchange_of(sym)
    try:
        if ex == "NASDAQ":
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("America/New_York")
        elif ex == "CRYPTO":
            tz = _dt.timezone.utc
        else:
            tz = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
        day = _dt.datetime.fromtimestamp(float(live["time"]), tz).date()
    except Exception:  # noqa: BLE001
        return h
    last = h.index[-1]
    if pd.Timestamp(last).date() != day:
        return h
    h = h.copy()
    px = float(live["price"])
    h.loc[last, "Close"] = px
    h.loc[last, "High"] = max(px, float(h["High"].iloc[-1])) if pd.notna(h["High"].iloc[-1]) else px
    h.loc[last, "Low"] = min(px, float(h["Low"].iloc[-1])) if pd.notna(h["Low"].iloc[-1]) else px
    if "Adj Close" in h.columns:
        h.loc[last, "Adj Close"] = px
    return h


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
            vol = 0.0 if _is_index_ticker(ysyms.get(sym, "")) else float(h["Volume"].iloc[-1])  # (Yahoo's index
            extra[sym] = {"high": float(h["High"].iloc[-1]), "low": float(h["Low"].iloc[-1]),      # volume is wrong)
                          "open": float(h["Open"].iloc[-1]), "prev": prev,
                          "volume": vol, "time": time.strftime("%H:%M")}
        pending = {}
        try:                                            # one request for the whole list (fast)
            data = yf.download(list(dict.fromkeys(ysyms.values())), period="5d", interval="1d", group_by="ticker",
                               auto_adjust=False, progress=False, threads=True)
            for sym, ys in ysyms.items():
                try:
                    h = data[ys] if len(set(ysyms.values())) > 1 else data
                    if hasattr(h.columns, "levels"):
                        h = h.droplevel(0, axis=1) if h.columns.nlevels > 1 else h
                    if _unfinished(h) or _short(h):
                        pending[sym] = h
                    else:
                        take(sym, h)
                except Exception:  # noqa: BLE001 - symbol missing from the batch: try it alone below
                    pass
        except Exception:  # noqa: BLE001
            pass
        if pending:                                     # Yahoo's unfinished newest bars: one request for their prices
            try:
                live = spark_prices([ysyms[s] for s in pending])
            except Exception:  # noqa: BLE001
                live = {}
            for sym, h in pending.items():
                lv = live.get(ysyms[sym])
                h = finish_bar(h, lv, sym)
                take(sym, h)
                if sym in out and _short(h) and lv and lv.get("prev"):
                    # one day of daily history (Nifty MidCap Select, Private Bank ...): yesterday's close from
                    # the hourly bars, else the change would read 0.00 %
                    last, pv = out[sym][0], float(lv["prev"])
                    out[sym] = (last, last / pv - 1 if pv else 0.0)
                    extra[sym]["prev"] = pv
        for sym, ys in ysyms.items():                   # anything the batch missed, one by one
            if sym in out:
                continue
            try:
                t = yf.Ticker(ys)
                h = t.history(period="5d", interval="1d", auto_adjust=False)
                if _unfinished(h):
                    datamod.fill_last_session(h, getattr(t, "history_metadata", None) or {})
                take(sym, h)
            except Exception:  # noqa: BLE001
                continue
        if out or not self.symbols:
            self.done.emit(out, extra)
        else:
            self.failed.emit("no prices returned (offline?)")


SPARK_URL = "https://query1.finance.yahoo.com/v8/finance/spark"
SPARK_CHUNK = 20                                   # symbols per request
SPARK_HEAD = {"User-Agent": "Mozilla/5.0 (Macintosh) ChartLab", "Accept": "application/json"}


_IST = 19800                                       # s: India's offset from UTC
_IDX_PREV: dict = {}                               # yahoo ticker -> (day of the price, previous close, when asked)


def _index_of_ticker(ys: str):
    try:
        from . import indices as _ix
        return _ix.by_yahoo(ys)
    except Exception:  # noqa: BLE001
        return None


def _is_index_ticker(ys: str) -> bool:
    return _index_of_ticker(ys) is not None


def _index_prevs(ses, want: dict, timeout: float) -> dict:
    """{ticker: previous close} for indices Yahoo keeps daily bars of, from those daily bars: the close of the
    trading day before the day of the price (`want`: ticker -> that day). Yahoo's own 'previous close' is a day old
    for most of them (20 Nov 2026: NIFTY 25,227.9 and SENSEX 82,031.8 - the 18th's), and the day's last hourly or
    minute value misses the official close for many (Nifty Realty 922.8 for 923.1, India VIX 15.90 for 16.11)."""
    import datetime as _dt
    out, ask = {}, []
    now = time.time()
    for ys, day in want.items():
        hit = _IDX_PREV.get(ys)
        if hit and hit[0] == day and now - hit[2] < 1800:
            out[ys] = hit[1]
        else:
            ask.append(ys)
    for i in range(0, len(ask), SPARK_CHUNK):
        part = ask[i:i + SPARK_CHUNK]
        try:
            r = ses.get(SPARK_URL, params={"symbols": ",".join(part), "range": "5d", "interval": "1d"},
                        timeout=timeout)
            r.raise_for_status()
            j = r.json() or {}
        except Exception:  # noqa: BLE001 - no previous close this round: the next one asks again
            continue
        for ys in part:
            d = j.get(ys) or {}
            last_of: dict = {}
            for t, x in zip(d.get("timestamp") or [], d.get("close") or []):
                if x is not None and t:
                    last_of[_dt.datetime.utcfromtimestamp(float(t) + _IST).date()] = float(x)
            before = [k for k in last_of if k < want[ys]]
            if before:
                pv = last_of[max(before)]
                out[ys] = pv
                _IDX_PREV[ys] = (want[ys], pv, now)
    return out


def spark_prices(ysyms, timeout: float = 6.0) -> dict:
    """The newest price of many symbols in one request (Yahoo's 'spark': today's minute closes of each):
    {yahoo symbol: {'price', 'time' (Unix), 'prev'}}. 'prev', the previous session's close: a stock's is Yahoo's;
    for an index with daily bars on Yahoo (NIFTY, Nifty IT ...) Yahoo's is a day old, so it comes from those daily
    bars (see _index_prevs); for one without (Nifty Media, MidCap Select ...) Yahoo's own previous close is right
    (both checked against TradingView for every index, 20 Nov 2026)."""
    import datetime as _dt
    import requests
    out: dict = {}
    syms = list(dict.fromkeys(str(s) for s in ysyms if s))
    if not syms:
        return out
    with requests.Session() as ses:
        # (the live feed's own short name: Yahoo answers a full browser name - and requests' own - with '429 Too Many
        # Requests' here)
        ses.headers.update(SPARK_HEAD)
        for i in range(0, len(syms), SPARK_CHUNK):
            part = syms[i:i + SPARK_CHUNK]
            r = ses.get(SPARK_URL, params={"symbols": ",".join(part), "range": "1d", "interval": "1m"},
                        timeout=timeout)
            r.raise_for_status()
            j = r.json() or {}
            for ys in part:
                d = j.get(ys) or {}
                closes, stamps = d.get("close") or [], d.get("timestamp") or []
                k = len(closes) - 1
                while k >= 0 and closes[k] is None:
                    k -= 1
                if k < 0 or k >= len(stamps):
                    continue
                ix = _index_of_ticker(ys)
                if ix is not None:
                    prev = None if ix.daily else (d.get("chartPreviousClose") or d.get("previousClose"))
                elif ys.startswith("^"):
                    prev = None                                # an index we do not know: no trustworthy close
                else:
                    prev = d.get("chartPreviousClose") or d.get("previousClose")
                out[ys] = {"price": float(closes[k]), "time": float(stamps[k]),
                           "prev": float(prev) if prev else None}
        want = {ys: _dt.datetime.utcfromtimestamp(r["time"] + _IST).date() for ys, r in out.items()
                if (_index_of_ticker(ys) is not None and _index_of_ticker(ys).daily)}
        if want:
            for ys, pv in _index_prevs(ses, want, timeout).items():
                out[ys]["prev"] = pv
    return out


class SparkWorker(QtCore.QThread):
    """Off the UI thread: spark_prices() for the watchlist's symbols."""
    done = QtCore.Signal(dict)

    def __init__(self, symbols, parent=None):
        super().__init__(parent)
        self.symbols = list(symbols)

    def run(self):
        out = {}
        try:
            ys = {s: datamod.normalize_symbol(s) for s in self.symbols}
            got = spark_prices(ys.values())
            out = {s: got[y] for s, y in ys.items() if y in got}
        except Exception:  # noqa: BLE001 - offline / Yahoo busy: the next round tries again
            out = {}
        self.done.emit(out)


SORT_COLS = ("sym", "last", "chg", "pct")
SORT_NAMES = {"sym": "Symbol", "last": "Last", "chg": "Chg", "pct": "Chg%"}


class _WatchHeader(QtWidgets.QWidget):
    """The column headings, placed on the same columns the rows draw (which fit their numbers). A click on one
    sorts by it (TradingView): numbers biggest first, names A-Z; again the other way; a third time back to your
    own order. The sorted heading is brighter and carries the arrow."""
    clicked = QtCore.Signal(str)

    def __init__(self, lst):
        super().__init__()
        self.lst = lst
        self.sort = None                                  # (column, descending) or None
        self._hover = None
        self.setFixedHeight(24)
        self.setMouseTracking(True)

    def _cells(self) -> dict:
        r = QtCore.QRect(0, 0, self.lst.viewport().width() or self.width(), self.height() - 4)
        last, chg, pct = QuoteDelegate.cols(r)
        f = QtGui.QFont(self.font())
        f.setPixelSize(12)
        fm = QtGui.QFontMetrics(f)
        sym = QtCore.QRect(14, 0, fm.horizontalAdvance("Symbol") + 18, r.height())
        return {"sym": sym, "last": last, "chg": chg, "pct": pct}

    def column_at(self, pos) -> str | None:
        for k, cell in self._cells().items():
            if cell.adjusted(-4, 0, 4, 4).contains(pos):
                return k
        return None

    def mouseMoveEvent(self, ev):
        k = self.column_at(ev.position().toPoint())
        if k != self._hover:
            self._hover = k
            self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor if k else QtCore.Qt.CursorShape.ArrowCursor)
            self.setToolTip(f"Sort by {SORT_NAMES[k]}" if k else "")
            self.update()

    def leaveEvent(self, _ev):
        self._hover = None
        self.update()

    def mousePressEvent(self, ev):
        k = self.column_at(ev.position().toPoint())
        if k and ev.button() == QtCore.Qt.MouseButton.LeftButton:
            self.clicked.emit(k)

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        f = QtGui.QFont(self.font())
        f.setPixelSize(12)
        p.setFont(f)
        fm = p.fontMetrics()
        al = QtCore.Qt.AlignmentFlag
        cells = self._cells()
        for k, label in SORT_NAMES.items():
            cell = cells[k]
            on = self.sort is not None and self.sort[0] == k
            p.setPen(QtGui.QColor("#dbdbdb" if on else ("#b8b8b8" if k == self._hover else theme.MUTED)))
            if k == "sym":
                p.drawText(cell, al.AlignVCenter | al.AlignLeft, label)
                tx = cell.x() + fm.horizontalAdvance(label) + 4
            else:
                aw = 10 if on else 0                       # the arrow sits left of a right-aligned heading
                p.drawText(cell, al.AlignVCenter | al.AlignRight, label)
                tx = cell.right() - fm.horizontalAdvance(label) - aw - 1
            if on:
                cy = cell.center().y() + 1
                path = QtGui.QPainterPath()
                if self.sort[1]:                             # ▼ biggest first
                    path.moveTo(tx, cy - 2.5)
                    path.lineTo(tx + 7, cy - 2.5)
                    path.lineTo(tx + 3.5, cy + 2)
                else:                                        # ▲
                    path.moveTo(tx, cy + 2)
                    path.lineTo(tx + 7, cy + 2)
                    path.lineTo(tx + 3.5, cy - 2.5)
                path.closeSubpath()
                p.fillPath(path, QtGui.QColor(theme.ACCENT))
        p.end()


class QuoteDelegate(QtWidgets.QStyledItemDelegate):
    """TradingView watchlist row: logo · Symbol | Last | Chg | Chg%  and collapsible section headers."""

    GAP = 10                          # px between the number columns
    NUM_PX = 13                       # the numbers' size (the names keep 14 px: BANKNIFTY fits whole)
    PAD = 14                          # right padding of the row
    widths = (64, 50, 52)             # Last / Chg / Chg% columns: as wide as their widest number (see fit())

    def sizeHint(self, option, index):
        return QtCore.QSize(option.rect.width(), 30 if index.data(ROLE_SECTION) else 32)

    @staticmethod
    def texts(sym: str, q) -> tuple[str, str, str]:
        """What a row writes: last price, change, change % (each symbol in its own tick, like TradingView)."""
        chg = q[0] - q[0] / (1 + q[1]) if q[1] > -1 else 0.0
        dec = price_decimals_of(sym, q[0])
        return (f"{q[0]:,.{dec}f}", f"{chg:+,.{dec}f}".replace("+", "").replace("-", "\u2212"),
                f"{q[1] * 100:+.2f}%".replace("+", "").replace("-", "\u2212"))

    @classmethod
    def fit(cls, quotes: dict, font: QtGui.QFont):
        """Columns as wide as their widest number (and their heading): '64,500.00' and '1,450.15' never touch."""
        f = QtGui.QFont(font)
        f.setPixelSize(cls.NUM_PX)
        f.setWeight(QtGui.QFont.Weight.Normal)
        fm = QtGui.QFontMetrics(f)
        hf = QtGui.QFont(font)
        hf.setPixelSize(12)
        hm = QtGui.QFontMetrics(hf)
        w = [hm.horizontalAdvance(h) for h in ("Last", "Chg", "Chg%")]
        for sym, q in (quotes or {}).items():
            if q:
                for k, t in enumerate(cls.texts(sym, q)):
                    w[k] = max(w[k], fm.horizontalAdvance(t))
        cls.widths = tuple(int(x) + 2 for x in w)

    @classmethod
    def cols(cls, r: QtCore.QRect):
        """(Last, Chg, Chg%) cells of a row, laid out from the right."""
        wl, wc, wp = cls.widths
        right = r.right() - cls.PAD
        pct = QtCore.QRect(right - wp, r.y(), wp, r.height())
        chg = QtCore.QRect(pct.left() - cls.GAP - wc, r.y(), wc, r.height())
        last = QtCore.QRect(chg.left() - cls.GAP - wl, r.y(), wl, r.height())
        return last, chg, pct

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
        box = QtCore.QRectF(r).adjusted(4, 1, -4, -1)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        if sel:                                                 # the chosen row: a soft fill and a blue edge
            p.setBrush(QtGui.QColor("#242424"))
            p.drawRoundedRect(box, 6, 6)
            p.setBrush(QtGui.QColor(theme.ACCENT))
            p.drawRoundedRect(QtCore.QRectF(box.x() + 1, box.y() + 7, 3, box.height() - 14), 1.5, 1.5)
        elif option.state & QtWidgets.QStyle.StateFlag.State_MouseOver:
            p.setBrush(QtGui.QColor("#1b1b1b"))
            p.drawRoundedRect(box, 6, 6)
        sym = index.data(ROLE_SYM)
        q = index.data(ROLE_QUOTE)
        draw_logo(p, QtCore.QRectF(r.x() + 12, r.center().y() - 9, 18, 18), sym, f)
        f.setPixelSize(14)
        f.setWeight(QtGui.QFont.Weight.Normal)
        p.setFont(f)
        p.setPen(QtGui.QColor("#dbdbdb"))
        c_last, c_chg, c_pct = self.cols(r)
        name_rect = QtCore.QRect(r.x() + 38, r.y(), max(20, c_last.left() - 8 - (r.x() + 38)), r.height())
        p.drawText(name_rect, al.AlignVCenter | al.AlignLeft,
                   p.fontMetrics().elidedText(sym.split(":")[-1], QtCore.Qt.TextElideMode.ElideRight, name_rect.width()))
        nf = QtGui.QFont(f)
        nf.setPixelSize(self.NUM_PX)
        p.setFont(nf)
        if q:
            col = QtGui.QColor(theme.UP if q[1] >= 0 else theme.DOWN)
            last_txt, chg_txt, pct_txt = self.texts(sym, q)
            fl = index.data(ROLE_FLASH)
            glow = 0.0
            if fl:
                age = time.monotonic() - fl[1]
                if 0 <= age < FLASH_S:
                    glow = (1.0 - age / FLASH_S) ** 1.6           # a changed price glows and fades out
            if glow > 0.01:
                tw = p.fontMetrics().horizontalAdvance(last_txt)
                pill = QtCore.QRectF(c_last.right() - tw - 6, r.center().y() - 10, tw + 9, 20)
                gc = QtGui.QColor(theme.UP if fl[0] > 0 else theme.DOWN)
                gc.setAlphaF(0.42 * glow)
                p.setPen(QtCore.Qt.PenStyle.NoPen)
                p.setBrush(gc)
                p.drawRoundedRect(pill, 4, 4)
            p.setPen(QtGui.QColor("#ffffff" if glow > 0.3 else "#dbdbdb"))
            p.drawText(c_last, al.AlignVCenter | al.AlignRight, last_txt)
            p.setPen(col)
            p.drawText(c_chg, al.AlignVCenter | al.AlignRight, chg_txt)
            p.drawText(c_pct, al.AlignVCenter | al.AlignRight, pct_txt)
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
            t = yf.Ticker(datamod.normalize_symbol(self.sym))
            h = t.history(period="1y", interval="1d", auto_adjust=False)
            if _unfinished(h):                          # Yahoo's newest bar without its close: finish it
                datamod.fill_last_session(h, getattr(t, "history_metadata", None) or {})
            h = h.dropna(subset=["Close"])
            if h.empty:
                return
            c, o = h["Close"], h["Open"]
            last, t_last = float(c.iloc[-1]), c.index[-1]

            def ago(days=None, ytd=False):
                # TradingView's 'Performance': from the OPEN of the first bar on or after the start (7 / 30 / 91 /
                # 182 / 365 days back, 1 January for YTD) to the last close - NIFTY on 20 Nov 2026: 1W +2.34 % from
                # 13 Nov's open 25,209.85, 3M +6.25 % from 21 Aug's 24,284.05 (split-adjusted prices, as here)
                cut = (t_last.normalize().replace(month=1, day=1) if ytd else
                       t_last.normalize() - pd.Timedelta(days=days))
                after = o[(o.index >= cut) & o.notna()]
                base = float(after.iloc[0]) if len(after) else float(o.dropna().iloc[0])
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
        self.setVisible(lo is not None and hi is not None)     # nothing to show yet: no empty gap in the card
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
        cur = currency_of(sym)
        self.sub.setText(f"{kind_of(sym)}  •  {exchange_of(sym)}")
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
            from . import indices as _ix
            v = 0.0 if _ix.is_index(sym) else (ex.get("volume", 0.0) or 0.0)   # an index has no volume of its own
            vs = theme.fmt_vol(v)
            self.vol.setText((f"<span style='color:{theme.MUTED};font-size:12px'>Volume</span><br>"
                              f"<span style='color:#dbdbdb;font-size:13px'>{vs}</span>") if v > 0 else "")
            self.vol.setVisible(v > 0)                  # (Yahoo has no volume for an index: no 'Volume 0')
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
        import os as _os
        if self._perf_job is not None and self._perf_job.isRunning() or _os.environ.get("CHARTLAB_OFFLINE"):
            return                                       # (tests: no requests)
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
    sortChanged = QtCore.Signal(str)                        # 'pct:desc' / '' - for the app to remember

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sort: tuple[str, bool] | None = None           # a view order only: the saved list keeps yours
        self._order: list[str] = []
        self.quotes: dict[str, tuple[float, float]] = {}
        self.extra: dict[str, dict] = {}
        self._collapsed: set[str] = set()
        self._worker = None
        self._spark = None                                  # the running SparkWorker (poll_live)
        self._flash = QtCore.QTimer(self)                    # repaints while a changed price glows
        self._flash.setInterval(33)
        self._flash.timeout.connect(self._flash_tick)
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

        self.list = QtWidgets.QListWidget()
        hdr = _WatchHeader(self.list)                       # 'Symbol  Last  Chg  Chg%' over the rows' own columns
        hdr.clicked.connect(self.sort_click)
        v.addWidget(hdr)
        self.header_row = hdr
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
        """Your list, section by section, in your order (a sorted view does not change it)."""
        return [s for g in GROUP_ORDER for s in self._order if group_of(s) == g]

    def shown(self) -> list[str]:
        """The rows as they are on screen (sorted when a heading is)."""
        out = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            if not it.data(ROLE_SECTION):
                out.append(it.data(ROLE_SYM))
        return out

    def set_symbols(self, syms):
        self._rebuild([str(s).upper() for s in syms])

    # ------------------------------------------------------------------ sorting (TradingView: click a heading)
    def _sort_value(self, sym: str):
        col, desc = self.sort
        if col == "sym":
            return sym.split(":")[-1]
        q = self.quotes.get(sym)
        if not q:
            return None
        if col == "last":
            return q[0]
        if col == "pct":
            return q[1]
        return q[0] - q[0] / (1 + q[1]) if q[1] > -1 else 0.0

    def _sorted(self, syms: list[str]) -> list[str]:
        if not self.sort:
            return list(syms)
        have = [s for s in syms if self._sort_value(s) is not None]
        none = [s for s in syms if self._sort_value(s) is None]          # no price yet: at the end, as listed
        have.sort(key=self._sort_value, reverse=self.sort[1])
        return have + none

    def set_sort(self, spec):
        """('pct', True) / 'pct:desc' / None or '' to go back to your own order."""
        if isinstance(spec, str):
            col, _, way = spec.partition(":")
            spec = (col, way != "asc") if col in SORT_COLS else None
        self.sort = tuple(spec) if spec else None
        self.header_row.sort = self.sort
        self.header_row.update()
        self._rebuild(self.symbols())
        self.sortChanged.emit(f"{self.sort[0]}:{'desc' if self.sort[1] else 'asc'}" if self.sort else "")

    def sort_click(self, col: str):
        """First click sorts (numbers biggest first, names A-Z), the second reverses, the third clears."""
        first = col != "sym"
        if not self.sort or self.sort[0] != col:
            self.set_sort((col, first))
        elif self.sort[1] == first:
            self.set_sort((col, not first))
        else:
            self.set_sort(None)

    def _resort(self):
        """Prices moved: keep a sorted view in order, moving the rows (their glow and the chosen row stay)."""
        if not self.sort:
            return
        bar = self.list.verticalScrollBar().value()
        cur = self.list.currentItem()
        cur_sym = cur.data(ROLE_SYM) if cur is not None else None
        moved = False
        self.list.blockSignals(True)
        i = 0
        while i < self.list.count():
            if not self.list.item(i).data(ROLE_SECTION):
                i += 1
                continue
            sec = self.list.item(i).data(ROLE_SECTION)
            j = i + 1
            while j < self.list.count() and not self.list.item(j).data(ROLE_SECTION):
                j += 1
            have = [self.list.item(k).data(ROLE_SYM) for k in range(i + 1, j)]
            want = self._sorted(have)
            if want != have:
                moved = True
                taken = {}
                for _ in range(j - i - 1):
                    it = self.list.takeItem(i + 1)
                    taken[it.data(ROLE_SYM)] = it
                for k, sym in enumerate(want):
                    it = taken[sym]
                    self.list.insertItem(i + 1 + k, it)
                    it.setHidden(sec in self._collapsed)
            i = j
        if moved and cur_sym:
            for k in range(self.list.count()):
                it = self.list.item(k)
                if not it.data(ROLE_SECTION) and it.data(ROLE_SYM) == cur_sym:
                    self.list.setCurrentItem(it)
                    break
        self.list.blockSignals(False)
        self.list.verticalScrollBar().setValue(bar)

    def _rebuild(self, syms: list[str], keep: str | None = None):
        keep = keep or (self.list.currentItem().data(ROLE_SYM) if self.list.currentItem() else None)
        self._order = list(dict.fromkeys(syms))
        self.list.clear()
        groups = {g: self._sorted([s for s in self._order if group_of(s) == g]) for g in GROUP_ORDER}
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
        self._refit()
        self._update_card()

    def add(self, sym: str) -> bool:
        sym = sym.strip().upper()
        from . import indices as _ix
        sym = _ix.canonical(sym) or datamod.RENAMED.get(sym, "") or sym    # 'NIFTY IT' -> NIFTYIT, ZOMATO -> ETERNAL
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
        if self.sort:
            m.addAction(f"Stop sorting by {SORT_NAMES[self.sort[0]]}", lambda: self.set_sort(None))
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

    LIVE_HOLD = 15.0                    # s: a live price this fresh is not replaced by the slower daily quote

    def _quotes_in(self, q, extra=None):
        now = time.time()
        for sym, val in (q or {}).items():
            ex = self.extra.setdefault(sym, {})
            new = dict((extra or {}).get(sym) or {})
            if now - ex.get("live_at", 0.0) < self.LIVE_HOLD and sym in self.quotes:
                pv = new.get("prev") or ex.get("prev")      # keep the live price; take the day's numbers
                last = self.quotes[sym][0]
                val = (last, last / pv - 1 if pv else val[1])
                for k in ("time", "live"):
                    new.pop(k, None)
                if "high" in new:
                    new["high"], new["low"] = max(new["high"], last), min(new["low"], last)
            ex.update(new)
            self.quotes[sym] = val
        for i in range(self.list.count()):
            it = self.list.item(i)
            if not it.data(ROLE_SECTION):
                self._put_quote(it, self.quotes.get(it.data(ROLE_SYM)))
        self._update_card()
        self._resort()
        self.refresh_btn.setEnabled(True)
        if any(now - ex.get("live_at", 0.0) < self.LIVE_HOLD for ex in self.extra.values()) and self._any_open():
            self.status.setText(f"Live  ·  {QtCore.QTime.currentTime().toString('HH:mm:ss')}")
        elif not self._any_open():
            self.status.setText("Market closed  ·  last prices")
        else:
            self.status.setText(f"Updated {QtCore.QTime.currentTime().toString('HH:mm:ss')}  ·  delayed quotes")

    def _any_open(self, syms=None) -> bool:
        """Is any of these (the list's) markets trading now? After the close a price is the last one, not live."""
        try:
            return any(market_state(s)[0] != "closed" for s in (syms if syms is not None else self.symbols()))
        except Exception:  # noqa: BLE001
            return True

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
        ex["time"], ex["live"], ex["live_at"] = time.strftime("%H:%M:%S"), True, time.time()
        for i in range(self.list.count()):
            it = self.list.item(i)
            if not it.data(ROLE_SECTION) and it.data(ROLE_SYM) == sym:
                self._put_quote(it, self.quotes[sym])
        if not getattr(self, "_live_note", False):
            self._live_note = True
            self.status.setText("Live prices from Dhan")
        cur = self.list.currentItem()
        if cur is not None and cur.data(ROLE_SYM) == sym:
            self._update_card()

    def chart_price(self, sym: str, last: float, prev: float | None, live: bool = False):
        """The chart of this symbol just got new bars (or a live price): its row shows the same price as the chart.
        The chart's own previous close counts only until the exchange's is known - the last minute bar of yesterday
        is not quite the official close, and a weekly bar's previous one is last week."""
        if sym not in self.symbols() or not last:
            return
        ex = self.extra.setdefault(sym, {})
        if prev and not ex.get("prev"):
            ex["prev"] = float(prev)
        pv = ex.get("prev")
        old = self.quotes.get(sym)
        self.quotes[sym] = (float(last), (last / pv - 1) if pv else (old[1] if old else 0.0))
        if "high" in ex:
            ex["high"], ex["low"] = max(ex["high"], last), min(ex["low"], last)
        ex["time"] = time.strftime("%H:%M:%S" if live else "%H:%M")
        if live:
            ex["live"], ex["live_at"] = True, time.time()
        for i in range(self.list.count()):
            it = self.list.item(i)
            if not it.data(ROLE_SECTION) and it.data(ROLE_SYM) == sym:
                self._put_quote(it, self.quotes[sym])
        cur = self.list.currentItem()
        if cur is not None and cur.data(ROLE_SYM) == sym:
            self._update_card()

    def _put_quote(self, it, new):
        """A row's new price; a moved price glows green / red for a moment (like the brokers' watchlists)."""
        old = it.data(ROLE_QUOTE)
        it.setData(ROLE_QUOTE, new)
        self._refit()
        if old and new and old[0] and new[0] != old[0]:
            it.setData(ROLE_FLASH, (1 if new[0] > old[0] else -1, time.monotonic()))
            if not self._flash.isActive():
                self._flash.start()

    def _refit(self):
        """Columns follow the widest number (once per round of updates, not per row)."""
        if not getattr(self, "_fit_due", False):
            self._fit_due = True
            QtCore.QTimer.singleShot(0, self._do_fit)

    def _do_fit(self):
        self._fit_due = False
        old = QuoteDelegate.widths
        QuoteDelegate.fit({s: q for s, q in self.quotes.items() if s in set(self.symbols())}, self.list.font())
        if QuoteDelegate.widths != old:
            self.list.viewport().update()
            self.header_row.update()

    def _flash_tick(self):
        now = time.monotonic()
        live = False
        for i in range(self.list.count()):
            fl = self.list.item(i).data(ROLE_FLASH)
            if fl and now - fl[1] < FLASH_S + 0.05:
                live = True
                break
        self.list.viewport().update()
        if not live:
            self._flash.stop()

    def poll_live(self) -> bool:
        """Every row's newest price in one request (TradingView's watchlist moves all the time, not once a minute).
        False when a round is still running."""
        if self._spark is not None and self._spark.isRunning():
            return False
        syms = self.symbols()
        if not syms:
            return False
        self._spark = SparkWorker(syms, self)
        self._spark.done.connect(self._spark_in)
        self._spark.start()
        return True

    def _spark_in(self, got: dict):
        changed = False
        for sym, r in (got or {}).items():
            ex = self.extra.setdefault(sym, {})
            if r.get("prev"):                                # (stocks: the same answer's previous close - the right
                ex["prev"] = float(r["prev"])               # one for this price, also on a new day's first minute)
            pv = ex.get("prev")
            if not pv:                                       # the change needs yesterday's close: next round
                continue
            price = float(r["price"])
            old = self.quotes.get(sym)
            new = (price, price / pv - 1)
            if old == new:
                continue
            self.quotes[sym] = new
            if "high" in ex:
                ex["high"], ex["low"] = max(ex["high"], price), min(ex["low"], price)
            ex["time"] = time.strftime("%H:%M:%S", time.localtime(r.get("time") or time.time()))
            ex["live"], ex["live_at"] = True, time.time()
            for i in range(self.list.count()):
                it = self.list.item(i)
                if not it.data(ROLE_SECTION) and it.data(ROLE_SYM) == sym:
                    self._put_quote(it, new)
            changed = True
        if changed:
            self._update_card()
            self._resort()
        if got:
            self.status.setText(f"Live  ·  {QtCore.QTime.currentTime().toString('HH:mm:ss')}" if self._any_open(got)
                                else "Market closed  ·  last prices")

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
