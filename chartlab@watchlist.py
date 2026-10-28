"""Watchlist side panel: saved symbols with last price and % change, click to open in the active chart."""
from __future__ import annotations

import json

from PySide6 import QtCore, QtGui, QtWidgets

from . import data as datamod
from . import theme

DEFAULT_WATCHLIST = ["NIFTY", "BANKNIFTY", "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
                     "SBIN", "ITC", "LT", "BHARTIARTL", "TATAMOTORS"]


class QuoteWorker(QtCore.QThread):
    done = QtCore.Signal(dict)
    failed = QtCore.Signal(str)

    def __init__(self, symbols, parent=None):
        super().__init__(parent)
        self.symbols = list(symbols)

    def run(self):
        try:
            import yfinance as yf
            out = {}
            for sym in self.symbols:
                ysym = datamod.normalize_symbol(sym)
                h = yf.Ticker(ysym).history(period="5d", interval="1d", auto_adjust=False)
                closes = h["Close"].dropna()
                if len(closes) >= 2:
                    out[sym] = (float(closes.iloc[-1]), float(closes.iloc[-1] / closes.iloc[-2] - 1))
                elif len(closes) == 1:
                    out[sym] = (float(closes.iloc[-1]), 0.0)
            self.done.emit(out)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


class QuoteDelegate(QtWidgets.QStyledItemDelegate):
    """Symbol on the left, last price + coloured change badge on the right (TradingView style)."""

    def sizeHint(self, option, index):
        return QtCore.QSize(option.rect.width(), 46)

    def paint(self, p, option, index):
        p.save()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(6, 3, -6, -3)
        if option.state & QtWidgets.QStyle.StateFlag.State_Selected:
            p.setBrush(QtGui.QColor("#1e2a55"))
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.drawRoundedRect(r, 8, 8)
        elif option.state & QtWidgets.QStyle.StateFlag.State_MouseOver:
            p.setBrush(QtGui.QColor(theme.HOVER))
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.drawRoundedRect(r, 8, 8)
        sym = index.data(QtCore.Qt.ItemDataRole.UserRole)
        q = index.data(QtCore.Qt.ItemDataRole.UserRole + 1)
        f = QtGui.QFont(option.font)
        f.setBold(True)
        f.setPointSize(12)
        p.setFont(f)
        p.setPen(QtGui.QColor("white"))
        p.drawText(r.adjusted(10, 0, 0, 0), QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft, sym)
        if q:
            col = QtGui.QColor(theme.UP if q[1] >= 0 else theme.DOWN)
            f.setBold(False)
            f.setPointSize(11)
            p.setFont(f)
            badge = QtCore.QRect(r.right() - 74, r.center().y() - 11, 66, 22)
            c2 = QtGui.QColor(col)
            c2.setAlpha(45)
            p.setBrush(c2)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.drawRoundedRect(badge, 6, 6)
            p.setPen(col)
            p.drawText(badge, QtCore.Qt.AlignmentFlag.AlignCenter, f"{q[1] * 100:+.2f}%")
            p.setPen(QtGui.QColor(theme.TEXT))
            p.drawText(r.adjusted(0, 0, -82, 0), QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignRight,
                       f"{q[0]:,.2f}")
        p.restore()


class WatchlistWidget(QtWidgets.QWidget):
    symbolChosen = QtCore.Signal(str)
    changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.quotes: dict[str, tuple[float, float]] = {}
        self._worker = None
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        row = QtWidgets.QHBoxLayout()
        self.edit = QtWidgets.QLineEdit()
        self.edit.setPlaceholderText("Add symbol…")
        self.edit.returnPressed.connect(self._add_from_edit)
        row.addWidget(self.edit, 1)
        add = QtWidgets.QPushButton("+")
        add.setFixedWidth(30)
        add.clicked.connect(self._add_from_edit)
        row.addWidget(add)
        v.addLayout(row)

        self.list = QtWidgets.QListWidget()
        self.list.setUniformItemSizes(True)
        self.list.setStyleSheet(
            f"QListWidget {{ background:{theme.BG}; border:1px solid {theme.GRID}; }}"
            f"QListWidget {{ border:0; outline:0; }}")
        self.list.setItemDelegate(QuoteDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.itemClicked.connect(lambda it: self.symbolChosen.emit(it.data(QtCore.Qt.ItemDataRole.UserRole)))
        self.list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        v.addWidget(self.list, 1)

        row2 = QtWidgets.QHBoxLayout()
        self.refresh_btn = QtWidgets.QPushButton("Refresh prices")
        self.refresh_btn.clicked.connect(self.refresh)
        row2.addWidget(self.refresh_btn)
        rm = QtWidgets.QPushButton("Remove")
        rm.clicked.connect(self.remove_selected)
        row2.addWidget(rm)
        v.addLayout(row2)
        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet(f"color:{theme.MUTED}; font-size:11px;")
        v.addWidget(self.status)

    # ------------------------------------------------------------------ content
    def symbols(self) -> list[str]:
        return [self.list.item(i).data(QtCore.Qt.ItemDataRole.UserRole) for i in range(self.list.count())]

    def set_symbols(self, syms):
        self.list.clear()
        for s in syms:
            self._append(s)

    def add(self, sym: str) -> bool:
        sym = sym.strip().upper()
        if not sym or sym in self.symbols():
            return False
        self._append(sym)
        self.changed.emit()
        return True

    def remove(self, sym: str) -> bool:
        sym = sym.strip().upper()
        for i in range(self.list.count()):
            if self.list.item(i).data(QtCore.Qt.ItemDataRole.UserRole) == sym:
                self.list.takeItem(i)
                self.changed.emit()
                return True
        return False

    def remove_selected(self):
        it = self.list.currentItem()
        if it:
            self.remove(it.data(QtCore.Qt.ItemDataRole.UserRole))

    def _append(self, sym: str):
        it = QtWidgets.QListWidgetItem()
        it.setData(QtCore.Qt.ItemDataRole.UserRole, sym)
        self.list.addItem(it)
        self._paint(it)

    def _paint(self, it):
        sym = it.data(QtCore.Qt.ItemDataRole.UserRole)
        q = self.quotes.get(sym)
        it.setData(QtCore.Qt.ItemDataRole.UserRole + 1, q)
        if q:
            it.setText(f"{sym:<14}{q[0]:>12,.2f}   {q[1] * 100:+.2f}%")
            it.setForeground(QtGui.QColor(theme.UP if q[1] >= 0 else theme.DOWN))
        else:
            it.setText(sym)
            it.setForeground(QtGui.QColor(theme.TEXT))
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(11)
        it.setFont(font)

    def _add_from_edit(self):
        if self.add(self.edit.text()):
            self.edit.clear()

    def _menu(self, pos):
        it = self.list.itemAt(pos)
        if not it:
            return
        m = QtWidgets.QMenu(self)
        m.addAction("Remove from watchlist", lambda: self.remove(it.data(QtCore.Qt.ItemDataRole.UserRole)))
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

    def _quotes_in(self, q):
        self.quotes.update(q)
        for i in range(self.list.count()):
            self._paint(self.list.item(i))
        self.refresh_btn.setEnabled(True)
        self.status.setText(f"Updated {QtCore.QTime.currentTime().toString('HH:mm:ss')} (delayed)")

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
