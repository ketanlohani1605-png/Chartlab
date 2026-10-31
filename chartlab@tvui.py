"""TradingView-style popups: symbol search, 'Change interval' card, Go-to dialog."""
from __future__ import annotations

import re

from PySide6 import QtCore, QtGui, QtWidgets

from . import icons

DARK = ("QDialog{background:#1e1e1e;border:1px solid #2e2e2e;border-radius:10px;} "
        "QDialog QWidget{background:transparent;} QLabel{color:#dbdbdb;background:transparent;} "
        "QLineEdit{background:#121212;border:1px solid #3a3a3a;border-radius:6px;padding:8px 10px;font-size:15px;color:#fff;} "
        "QLineEdit:focus{border-color:#5a5a5a;} "
        "QListWidget{background:transparent;border:0;outline:0;} "
        "QPushButton{background:#2e2e2e;border:0;border-radius:16px;padding:7px 14px;color:#dbdbdb;font-size:14px;} "
        "QPushButton:hover{background:#3a3a3a;} "
        "QPushButton:checked{background:#ffffff;color:#000;} "
        "QPushButton#ok{background:#ffffff;color:#000;font-weight:600;border-radius:8px;padding:8px 20px;} "
        "QPushButton#ghost{background:transparent;border:1px solid #5a5a5a;border-radius:8px;padding:8px 18px;} "
        "QTabWidget::pane{border:0;} QTabBar::tab{background:transparent;color:#9598a1;padding:8px 14px;border:0;"
        "border-bottom:2px solid transparent;font-size:14px;} QTabBar::tab:selected{color:#fff;border-bottom:2px solid #fff;} "
        "QCalendarWidget QWidget{background:#1e1e1e;color:#dbdbdb;alternate-background-color:#1e1e1e;} "
        "QCalendarWidget QToolButton{color:#fff;background:transparent;font-size:14px;} "
        "QCalendarWidget QAbstractItemView{selection-background-color:#2962ff;selection-color:#fff;} "
        "QDateEdit,QTimeEdit{background:#121212;border:1px solid #3a3a3a;border-radius:6px;padding:6px;color:#fff;}")

CHIPS = ["All", "Stocks", "Indices", "Crypto", "US"]

NAMES = {"NIFTY": "Nifty 50 Index", "BANKNIFTY": "Nifty Bank Index", "SENSEX": "S&P BSE Sensex", "FINNIFTY":
         "Nifty Financial Services", "RELIANCE": "Reliance Industries Ltd", "TCS": "Tata Consultancy Services Ltd",
         "HDFCBANK": "HDFC Bank Ltd", "INFY": "Infosys Ltd", "ICICIBANK": "ICICI Bank Ltd", "SBIN": "State Bank of India",
         "ITC": "ITC Ltd", "LT": "Larsen & Toubro Ltd", "BHARTIARTL": "Bharti Airtel Ltd", "TATAMOTORS": "Tata Motors Ltd",
         "TATASTEEL": "Tata Steel Ltd", "AXISBANK": "Axis Bank Ltd", "KOTAKBANK": "Kotak Mahindra Bank Ltd",
         "HINDUNILVR": "Hindustan Unilever Ltd", "BAJFINANCE": "Bajaj Finance Ltd", "MARUTI": "Maruti Suzuki India Ltd",
         "SUNPHARMA": "Sun Pharmaceutical Industries", "WIPRO": "Wipro Ltd", "HCLTECH": "HCL Technologies Ltd",
         "ADANIENT": "Adani Enterprises Ltd", "ADANIPORTS": "Adani Ports & SEZ Ltd", "ONGC": "Oil & Natural Gas Corp",
         "NTPC": "NTPC Ltd", "POWERGRID": "Power Grid Corp of India", "COALINDIA": "Coal India Ltd",
         "ASIANPAINT": "Asian Paints Ltd", "ULTRACEMCO": "UltraTech Cement Ltd", "TITAN": "Titan Company Ltd",
         "M&M": "Mahindra & Mahindra Ltd", "JSWSTEEL": "JSW Steel Ltd", "TECHM": "Tech Mahindra Ltd",
         "NESTLEIND": "Nestle India Ltd", "DRREDDY": "Dr. Reddy's Laboratories", "CIPLA": "Cipla Ltd",
         "EICHERMOT": "Eicher Motors Ltd", "HEROMOTOCO": "Hero MotoCorp Ltd", "BPCL": "Bharat Petroleum Corp",
         "GRASIM": "Grasim Industries Ltd", "AAPL": "Apple Inc", "MSFT": "Microsoft Corp", "NVDA": "NVIDIA Corp",
         "TSLA": "Tesla Inc", "BTC-USD": "Bitcoin / US Dollar", "ETH-USD": "Ethereum / US Dollar"}


def _kind(sym: str) -> str:
    s = sym.upper()
    if "-USD" in s:
        return "Crypto"
    if s.split(":")[-1] in ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY"):
        return "Indices"
    if s.startswith("US:"):
        return "US"
    return "Stocks"


def _exch(sym: str) -> str:
    return "BSE" if sym.startswith("BSE:") else ("NASDAQ" if sym.startswith("US:") else ("CRYPTO" if "-USD" in sym else "NSE"))


ROLE = QtCore.Qt.ItemDataRole.UserRole


class _SearchDelegate(QtWidgets.QStyledItemDelegate):
    """Row like TradingView: logo · TICKER (typed part highlighted) / description + badge · exchange."""

    def __init__(self, dlg):
        super().__init__(dlg.lst)
        self.dlg = dlg

    def sizeHint(self, option, index):
        return QtCore.QSize(option.rect.width(), 56)

    def paint(self, p, option, index):
        from .watchlist import draw_logo
        p.save()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = option.rect
        if option.state & QtWidgets.QStyle.StateFlag.State_Selected:
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor("#2e2e2e"))
            p.drawRoundedRect(QtCore.QRectF(r).adjusted(6, 1, -6, -1), 8, 8)
        elif option.state & QtWidgets.QStyle.StateFlag.State_MouseOver:
            p.fillRect(r.adjusted(6, 1, -6, -1), QtGui.QColor("#262626"))
        sym = index.data(ROLE)
        typed = index.data(ROLE + 1)
        tick = sym.split(":")[-1]
        draw_logo(p, QtCore.QRectF(r.x() + 20, r.center().y() - 15, 30, 30), sym, option.font)
        f = QtGui.QFont(option.font)
        f.setPixelSize(16)
        f.setWeight(QtGui.QFont.Weight.Medium)
        p.setFont(f)
        x, y = r.x() + 66, r.y() + 8
        q = (self.dlg.edit.text().strip().upper() if not typed else "")
        i = tick.find(q) if q else -1
        parts = [(tick, "#dbdbdb")] if i < 0 else [(tick[:i], "#dbdbdb"), (tick[i:i + len(q)], "#3179f5"),
                                                    (tick[i + len(q):], "#dbdbdb")]
        fm = p.fontMetrics()
        for txt, col in parts:
            p.setPen(QtGui.QColor(col))
            p.drawText(x, y + fm.ascent(), txt)
            x += fm.horizontalAdvance(txt)
        f.setPixelSize(12)
        f.setWeight(QtGui.QFont.Weight.Normal)
        p.setFont(f)
        p.setPen(QtGui.QColor("#9598a1"))
        desc = "Use the typed symbol" if typed else NAMES.get(tick, NAMES.get(sym, tick))
        fm = p.fontMetrics()
        dy = r.y() + 32 + fm.ascent()
        p.drawText(r.x() + 66, dy, desc)
        badge = ("SYMBOL" if typed else _kind(sym).upper().replace("STOCKS", "STOCK").replace("INDICES", "INDEX")
                 .replace("US", "STOCK"))
        bx = r.x() + 66 + fm.horizontalAdvance(desc) + 8
        f.setPixelSize(9)
        f.setWeight(QtGui.QFont.Weight.Bold)
        p.setFont(f)
        bw = p.fontMetrics().horizontalAdvance(badge) + 10
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor("#2e2e2e"))
        p.drawRoundedRect(QtCore.QRectF(bx, r.y() + 33, bw, 14), 3, 3)
        p.setPen(QtGui.QColor("#b2b5be"))
        p.drawText(QtCore.QRectF(bx, r.y() + 33, bw, 14), QtCore.Qt.AlignmentFlag.AlignCenter, badge)
        f.setPixelSize(13)
        f.setWeight(QtGui.QFont.Weight.Normal)
        p.setFont(f)
        p.setPen(QtGui.QColor("#9598a1"))
        p.drawText(r.adjusted(0, 0, -24, 0), QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignRight,
                   "" if typed else _exch(sym))
        p.restore()


class SymbolSearch(QtWidgets.QDialog):
    """TradingView Symbol Search: type to filter, arrows to move, Enter to open, Esc to close."""

    def __init__(self, parent, symbols: list[str], text: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Symbol Search")
        self.setWindowFlag(QtCore.Qt.WindowType.FramelessWindowHint)
        self.setStyleSheet(DARK)
        self.resize(760, 600)
        self.symbols, self.chosen, self.kind = list(dict.fromkeys(symbols)), None, "All"
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 10)
        lay.setSpacing(10)
        top = QtWidgets.QHBoxLayout()
        ic = QtWidgets.QLabel()
        ic.setPixmap(icons.get("search", "#dbdbdb").pixmap(22, 22))
        top.addWidget(ic)
        self.edit = QtWidgets.QLineEdit(text)
        self.edit.setPlaceholderText("Symbol, ISIN or company")
        self.edit.setStyleSheet("QLineEdit{background:transparent;border:0;font-size:18px;padding:6px 4px;color:#fff;}")
        top.addWidget(self.edit, 1)
        clr = QtWidgets.QToolButton()
        clr.setIcon(icons.get("close", "#9598a1"))
        clr.setToolTip("Clear")
        clr.setStyleSheet("QToolButton{border:0;background:transparent;}")
        clr.clicked.connect(lambda: (self.edit.clear(), self.edit.setFocus()))
        top.addWidget(clr)
        lay.addLayout(top)
        line = QtWidgets.QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background:#2e2e2e;")
        lay.addWidget(line)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        self.chips = []
        for c in CHIPS:
            b = QtWidgets.QPushButton(c)
            b.setCheckable(True)
            b.setChecked(c == "All")
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, c=c: self._set_kind(c))
            b.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            row.addWidget(b)
            self.chips.append(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.lst = QtWidgets.QListWidget()
        self.lst.setItemDelegate(_SearchDelegate(self))
        self.lst.setMouseTracking(True)
        self.lst.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.lst.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.lst.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lay.addWidget(self.lst, 1)
        hint = QtWidgets.QLabel("\u2191 \u2193 to move  \u00b7  Enter to open  \u00b7  Esc to close  \u00b7  "
                                "any NSE/BSE/US/crypto symbol works, even if it is not listed")
        hint.setStyleSheet("color:#6a6d78;font-size:11px;padding:2px 6px;")
        lay.addWidget(hint)
        self.edit.textChanged.connect(self._fill)
        self.edit.returnPressed.connect(self._accept)
        self.lst.itemClicked.connect(lambda *_: self._accept())
        self._fill()
        if parent is not None:
            g = parent.geometry()
            self.move(g.x() + (g.width() - self.width()) // 2, g.y() + 90)
        QtCore.QTimer.singleShot(0, lambda: (self.edit.setFocus(), self.edit.setCursorPosition(len(self.edit.text()))))

    def _set_kind(self, k):
        self.kind = k
        for b in self.chips:
            b.setChecked(b.text() == k)
        self._fill()
        self.edit.setFocus()

    def _fill(self, *_):
        t = self.edit.text().strip().upper()
        self.lst.clear()
        scored = []
        for s in self.symbols:
            if self.kind != "All" and _kind(s) != self.kind:
                continue
            tick = s.split(":")[-1].upper()
            name = NAMES.get(tick, "").upper()
            if not t:
                score = 3
            elif tick == t or s.upper() == t:
                score = 0
            elif tick.startswith(t):
                score = 1
            elif t in tick or t in s.upper():
                score = 2
            elif t in name:
                score = 3
            else:
                continue
            scored.append((score, ":" in s, len(tick), s))       # NSE before BSE:/US: on ties
        scored.sort()
        exact = any(sc[0] == 0 for sc in scored)
        if t and not exact and re.fullmatch(r"[A-Z0-9&\-_.:^]+", t):
            it = QtWidgets.QListWidgetItem()
            it.setData(ROLE, t)
            it.setData(ROLE + 1, True)
            scored_items = [it]
        else:
            scored_items = []
        for *_, s in scored:
            it = QtWidgets.QListWidgetItem()
            it.setData(ROLE, s)
            it.setData(ROLE + 1, False)
            scored_items.append(it)
        if scored_items and scored_items[0].data(ROLE + 1) and len(scored_items) > 1 and scored[0][0] <= 1:
            scored_items.append(scored_items.pop(0))        # a real prefix match goes first, typed text last
        for it in scored_items:
            self.lst.addItem(it)
        if self.lst.count():
            self.lst.setCurrentRow(0)

    def keyPressEvent(self, ev):
        K = QtCore.Qt.Key
        if ev.key() in (K.Key_Down, K.Key_Up):
            r = self.lst.currentRow() + (1 if ev.key() == K.Key_Down else -1)
            self.lst.setCurrentRow(max(0, min(r, self.lst.count() - 1)))
            return
        super().keyPressEvent(ev)

    def _accept(self):
        it = self.lst.currentItem()
        t = self.edit.text().strip()
        self.chosen = it.data(ROLE) if it else (t.upper() or None)
        if self.chosen:
            self.accept()


UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_interval(text: str) -> tuple[str | None, str]:
    """'5' -> 5 minutes ('5m'); '1h'; '1d'; 'D'; 'W'. Returns (code or None, caption)."""
    t = text.strip().lower().replace(",", "")
    if not t:
        return None, ""
    m = re.fullmatch(r"(\d*)\s*([smhdwk]?)", t)
    if not m:
        return None, ""
    n, u = m.group(1), m.group(2)
    if u == "k":
        u = "m"
    if not u:
        u = "m"
    n = int(n or 1)
    if u == "d" and n == 1:
        return "1d", "1 day"
    if u == "w" and n == 1:
        return "1wk", "1 week"
    if u == "m" and n == 60:
        return "1h", "1 hour"
    if u == "m" and n in (1, 3, 5, 15, 30):
        return f"{n}m", f"{n} minute{'s' if n > 1 else ''}"
    if u == "h" and n == 1:
        return "1h", "1 hour"
    return None, f"{n} {UNITS.get(u, '')} (not available)"


class IntervalPopup(QtWidgets.QDialog):
    def __init__(self, parent, first: str):
        super().__init__(parent)
        self.setWindowFlag(QtCore.Qt.WindowType.FramelessWindowHint)
        self.setStyleSheet(DARK)
        self.setFixedWidth(280)
        self.code = None
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(QtWidgets.QLabel("Change interval"))
        self.edit = QtWidgets.QLineEdit(first)
        self.cap = QtWidgets.QLabel("")
        self.cap.setStyleSheet("color:#787b86;")
        lay.addWidget(self.edit)
        lay.addWidget(self.cap)
        self.edit.textChanged.connect(self._upd)
        self.edit.returnPressed.connect(self._ok)
        self._upd(first)
        QtCore.QTimer.singleShot(0, lambda: (self.edit.setFocus(), self.edit.setCursorPosition(len(first))))

    def _upd(self, t):
        code, cap = parse_interval(t)
        self.code = code
        self.cap.setText(cap)

    def _ok(self):
        if self.code:
            self.accept()


class GoTo(QtWidgets.QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Go to")
        self.setStyleSheet(DARK)
        lay = QtWidgets.QVBoxLayout(self)
        self.tabs = QtWidgets.QTabWidget()
        page = QtWidgets.QWidget()
        pl = QtWidgets.QVBoxLayout(page)
        row = QtWidgets.QHBoxLayout()
        self.date = QtWidgets.QDateEdit(QtCore.QDate.currentDate())
        self.date.setCalendarPopup(False)
        self.time = QtWidgets.QTimeEdit(QtCore.QTime(0, 0))
        row.addWidget(self.date)
        row.addWidget(self.time)
        pl.addLayout(row)
        self.cal = QtWidgets.QCalendarWidget()
        self.cal.setGridVisible(False)
        self.cal.clicked.connect(self.date.setDate)
        self.date.dateChanged.connect(self.cal.setSelectedDate)
        pl.addWidget(self.cal)
        self.tabs.addTab(page, "Date")
        page2 = QtWidgets.QWidget()
        p2 = QtWidgets.QFormLayout(page2)
        self.d0 = QtWidgets.QDateEdit(QtCore.QDate.currentDate().addMonths(-1))
        self.d1 = QtWidgets.QDateEdit(QtCore.QDate.currentDate())
        p2.addRow("Start", self.d0)
        p2.addRow("End", self.d1)
        self.tabs.addTab(page2, "Custom range")
        lay.addWidget(self.tabs)
        foot = QtWidgets.QHBoxLayout()
        foot.addStretch(1)
        c = QtWidgets.QPushButton("Cancel")
        c.clicked.connect(self.reject)
        ok = QtWidgets.QPushButton("Go to")
        ok.setObjectName("ok")
        ok.clicked.connect(self.accept)
        foot.addWidget(c)
        foot.addWidget(ok)
        lay.addLayout(foot)
