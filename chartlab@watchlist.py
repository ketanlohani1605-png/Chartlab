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
    """TradingView watchlist row: Symbol | Last | Chg | Chg%  (flat, no badges)."""

    def sizeHint(self, option, index):
        return QtCore.QSize(option.rect.width(), 30)

    def paint(self, p, option, index):
        p.save()
        r = option.rect
        if option.state & QtWidgets.QStyle.StateFlag.State_Selected:
            p.fillRect(r, QtGui.QColor("#2a2e39"))
        elif option.state & QtWidgets.QStyle.StateFlag.State_MouseOver:
            p.fillRect(r, QtGui.QColor(theme.HOVER))
        sym = index.data(QtCore.Qt.ItemDataRole.UserRole)
        q = index.data(QtCore.Qt.ItemDataRole.UserRole + 1)
        f = QtGui.QFont(option.font)
        f.setPointSize(12)
        f.setBold(True)
        p.setFont(f)
        al = QtCore.Qt.AlignmentFlag
        p.setPen(QtGui.QColor("white"))
        p.drawText(r.adjusted(12, 0, 0, 0), al.AlignVCenter | al.AlignLeft, sym)
        if q:
            f.setBold(False)
            p.setFont(f)
            w = r.width()
            col = QtGui.QColor(theme.UP if q[1] >= 0 else theme.DOWN)
            chg = q[0] - q[0] / (1 + q[1]) if q[1] > -1 else 0.0
            p.setPen(QtGui.QColor("white"))
            p.drawText(QtCore.QRect(r.x() + w * 38 // 100, r.y(), w * 24 // 100, r.height()), al.AlignVCenter | al.AlignRight, f"{q[0]:,.2f}")
            p.setPen(col)
            p.drawText(QtCore.QRect(r.x() + w * 62 // 100, r.y(), w * 18 // 100, r.height()), al.AlignVCenter | al.AlignRight, f"{chg:+,.2f}")
            p.drawText(QtCore.QRect(r.x() + w * 80 // 100, r.y(), w * 20 // 100 - 10, r.height()), al.AlignVCenter | al.AlignRight, f"{q[1] * 100:+.2f}%")
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
        add.setFixedWidth(34)
        add.clicked.connect(self._add_from_edit)
        row.addWidget(add)
        v.addLayout(row)

        hdr = QtWidgets.QLabel("<table width='100%'><tr><td width='38%'>Symbol</td><td width='24%' align='right'>Last</td>"
                               "<td width='18%' align='right'>Chg</td><td width='20%' align='right'>Chg%</td></tr></table>")
        hdr.setStyleSheet(f"color:{theme.MUTED}; font-size:11px; padding:4px 12px; border-bottom:1px solid {theme.GRID};")
        v.addWidget(hdr)
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

        self.card = QtWidgets.QLabel("")
        self.card.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.card.setStyleSheet(f"background:{theme.BG}; border-top:1px solid {theme.GRID}; padding:10px 12px;")
        v.addWidget(self.card)
        self.list.currentItemChanged.connect(lambda *_: self._update_card())
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

    def _update_card(self):
        it = self.list.currentItem()
        if it is None:
            self.card.setText("")
            return
        sym = it.data(QtCore.Qt.ItemDataRole.UserRole)
        q = self.quotes.get(sym)
        if not q:
            self.card.setText(f"<span style='font-size:16px;font-weight:600;color:white'>{sym}</span>")
            return
        col = theme.UP if q[1] >= 0 else theme.DOWN
        chg = q[0] - q[0] / (1 + q[1]) if q[1] > -1 else 0.0
        self.card.setText(
            f"<div style='font-size:15px;font-weight:600;color:white'>{sym} <span style='color:{theme.MUTED};font-size:11px'>NSE</span></div>"
            f"<div><span style='font-size:26px;color:white'>{q[0]:,.2f}</span> "
            f"<span style='font-size:13px;color:{col}'>{chg:+,.2f} &nbsp;{q[1] * 100:+.2f}%</span></div>")

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
        self._update_card()
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
