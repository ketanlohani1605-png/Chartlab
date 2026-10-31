"""Watchlist side panel, TradingView style: grouped rows (Indices / Stocks / US / Crypto) with logo, Last, Chg,
Chg%, and a detail card for the selected symbol. Click a row to open it in the active chart."""
from __future__ import annotations

import json
import time
import zlib

from PySide6 import QtCore, QtGui, QtWidgets

from . import data as datamod
from . import icons
from . import theme

DEFAULT_WATCHLIST = ["NIFTY", "BANKNIFTY", "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
                     "SBIN", "ITC", "LT", "BHARTIARTL", "TATAMOTORS"]
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
            out, extra = {}, {}
            for sym in self.symbols:
                try:
                    h = yf.Ticker(datamod.normalize_symbol(sym)).history(period="5d", interval="1d", auto_adjust=False)
                except Exception:  # noqa: BLE001 - one bad symbol must not stop the rest
                    continue
                h = h.dropna(subset=["Close"])
                if h.empty:
                    continue
                last = float(h["Close"].iloc[-1])
                prev = float(h["Close"].iloc[-2]) if len(h) >= 2 else last
                out[sym] = (last, last / prev - 1 if prev else 0.0)
                extra[sym] = {"high": float(h["High"].iloc[-1]), "low": float(h["Low"].iloc[-1]),
                              "open": float(h["Open"].iloc[-1]), "prev": prev,
                              "volume": float(h["Volume"].iloc[-1]), "time": time.strftime("%H:%M")}
            self.done.emit(out, extra)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


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
            p.setPen(QtGui.QColor("#dbdbdb"))
            p.drawText(c_last, al.AlignVCenter | al.AlignRight, f"{q[0]:,.2f}")
            p.setPen(col)
            p.drawText(c_chg, al.AlignVCenter | al.AlignRight, f"{chg:+,.2f}".replace("+", ""))
            p.drawText(c_pct, al.AlignVCenter | al.AlignRight, f"{q[1] * 100:+.2f}%".replace("+", ""))
        else:
            p.setPen(QtGui.QColor("#4a4a4a"))
            for c in (c_last, c_chg, c_pct):
                p.drawText(c, al.AlignVCenter | al.AlignRight, "—")
        p.restore()


class RangeBar(QtWidgets.QWidget):
    """Day's range: low ———●——— high."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(42)
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
        p.drawText(QtCore.QRect(0, 0, w, 14), QtCore.Qt.AlignmentFlag.AlignLeft, "Day's range")
        p.setPen(QtGui.QColor("#dbdbdb"))
        p.drawText(QtCore.QRect(0, 26, w, 16), QtCore.Qt.AlignmentFlag.AlignLeft, f"{self.lo:,.2f}")
        p.drawText(QtCore.QRect(0, 26, w, 16), QtCore.Qt.AlignmentFlag.AlignRight, f"{self.hi:,.2f}")
        y = 19
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor("#2a2e39"))
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
        self.range = RangeBar()
        v.addWidget(self.range)
        self.vol = QtWidgets.QLabel("")
        self.vol.setTextFormat(QtCore.Qt.TextFormat.RichText)
        v.addWidget(self.vol)

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
        if not q:
            self.price.setText(f"<span style='color:{theme.MUTED};font-size:13px'>Prices not loaded yet - "
                               "press ↻ above</span>")
            self.when.setText("")
            self.range.set_range(None, None, None)
            self.vol.setText("")
            return
        col = theme.UP if q[1] >= 0 else theme.DOWN
        chg = q[0] - q[0] / (1 + q[1]) if q[1] > -1 else 0.0
        self.price.setText(f"<span style='font-size:28px;color:white;font-weight:500'>{q[0]:,.2f}</span>"
                           f"<span style='font-size:11px;color:{theme.MUTED}'>&nbsp;{cur}&nbsp;&nbsp;</span>"
                           f"<span style='font-size:16px;color:{col}'>{chg:+,.2f}&nbsp;&nbsp;{q[1] * 100:+.2f}%</span>")
        ex = ex or {}
        self.when.setText(f"Last update at {ex.get('time', '--:--')}  (delayed)")
        if "low" in ex:
            self.range.set_range(ex["low"], ex["high"], q[0])
            v = ex.get("volume", 0.0)
            vs = f"{v / 1e7:.2f} Cr" if v >= 1e7 else (f"{v / 1e5:.2f} L" if v >= 1e5 else f"{v:,.0f}")
            self.vol.setText(f"<span style='color:{theme.MUTED};font-size:12px'>Volume</span><br>"
                             f"<span style='color:#dbdbdb;font-size:13px'>{vs}</span>")
        else:
            self.range.set_range(None, None, None)
            self.vol.setText("")


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
            m.addAction(f"Remove {sym}\tDel", lambda: self.remove(sym))
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
            return [str(x).upper() for x in v] if isinstance(v, list) and v else list(DEFAULT_WATCHLIST)
        except Exception:  # noqa: BLE001
            return list(DEFAULT_WATCHLIST)
