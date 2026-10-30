"""TradingView-style popups: symbol search, 'Change interval' card, Go-to dialog."""
from __future__ import annotations

import re

from PySide6 import QtCore, QtWidgets

DARK = ("QDialog{background:#1e222d;border:1px solid #434651;border-radius:10px;} QLabel{color:#d1d4dc;background:transparent;} "
        "QLineEdit{background:#131722;border:1px solid #434651;border-radius:6px;padding:8px 10px;font-size:15px;color:#fff;} "
        "QListWidget{background:transparent;border:0;outline:0;} QListWidget::item{padding:9px 10px;color:#d1d4dc;} "
        "QListWidget::item:selected{background:#2a2e39;} "
        "QPushButton{background:#2a2e39;border:1px solid #434651;border-radius:14px;padding:5px 12px;color:#d1d4dc;} "
        "QPushButton:checked{background:#d1d4dc;color:#131722;} QPushButton#ok{background:#d1d4dc;color:#131722;font-weight:600;border-radius:6px;padding:7px 18px;} "
        "QCalendarWidget QWidget{background:#1e222d;color:#d1d4dc;} QDateEdit,QTimeEdit{background:#131722;border:1px solid #434651;border-radius:4px;padding:4px;color:#fff;}")

CHIPS = ["All", "Stocks", "Indices", "Crypto", "Forex", "Futures"]


def _kind(sym: str) -> str:
    s = sym.upper()
    if "-USD" in s:
        return "Crypto"
    if s in ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY"):
        return "Indices"
    return "Stocks"


def _exch(sym: str) -> str:
    return "BSE" if sym.startswith("BSE:") else ("US" if sym.startswith("US:") else ("Crypto" if "-USD" in sym else "NSE"))


class SymbolSearch(QtWidgets.QDialog):
    def __init__(self, parent, symbols: list[str], text: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Symbol Search")
        self.setStyleSheet(DARK)
        self.resize(640, 520)
        self.symbols, self.chosen, self.kind = symbols, None, "All"
        lay = QtWidgets.QVBoxLayout(self)
        self.edit = QtWidgets.QLineEdit(text)
        self.edit.setPlaceholderText("Search")
        lay.addWidget(self.edit)
        row = QtWidgets.QHBoxLayout()
        self.chips = []
        for c in CHIPS:
            b = QtWidgets.QPushButton(c)
            b.setCheckable(True)
            b.setChecked(c == "All")
            b.clicked.connect(lambda _=False, c=c: self._set_kind(c))
            row.addWidget(b)
            self.chips.append(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.lst = QtWidgets.QListWidget()
        lay.addWidget(self.lst, 1)
        self.edit.textChanged.connect(self._fill)
        self.edit.returnPressed.connect(self._accept)
        self.lst.itemActivated.connect(lambda *_: self._accept())
        self._fill()
        QtCore.QTimer.singleShot(0, lambda: (self.edit.setFocus(), self.edit.setCursorPosition(len(self.edit.text()))))

    def _set_kind(self, k):
        self.kind = k
        for b in self.chips:
            b.setChecked(b.text() == k)
        self._fill()

    def _fill(self, *_):
        t = self.edit.text().strip().lower()
        self.lst.clear()
        for s in self.symbols:
            if (self.kind == "All" or _kind(s) == self.kind) and t in s.lower():
                it = QtWidgets.QListWidgetItem(f"{s.split(':')[-1]}      {_kind(s)}      {_exch(s)}")
                it.setData(QtCore.Qt.ItemDataRole.UserRole, s)
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
        self.chosen = it.data(QtCore.Qt.ItemDataRole.UserRole) if it else (t.upper() or None)
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
