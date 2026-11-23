"""Small moving touches that make the app feel alive: the live-feed badge that breathes, the thin line that runs
across a chart while it loads."""
from __future__ import annotations

import math
import time

from PySide6 import QtCore, QtGui, QtWidgets

from . import theme


class LiveBadge(QtWidgets.QLabel):
    """'● Live (3 s)' for the bottom bar: the dot carries the colour (and breathes while prices stream in), the
    words stay quiet. setText / text() as a QLabel."""

    DOT = 3.5
    PERIOD = 1.8                                        # s for one breath

    def __init__(self, parent=None):
        super().__init__(parent)
        self._col = QtGui.QColor(theme.MUTED)
        self._breathe = False
        self._ring = False                              # 'connecting': a hollow dot
        self._t0 = time.monotonic()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self.update)
        self.setStyleSheet("color:#b8b8b8; font-size:12px; padding-left:16px; padding-right:12px;")

    def set_state(self, text: str, color: str, breathe: bool = False, ring: bool = False, loud: bool = False):
        self._col = QtGui.QColor(color)
        self._breathe, self._ring = bool(breathe), bool(ring)
        self.setText(text)
        self.setStyleSheet(f"color:{color if loud else '#b8b8b8'}; font-size:12px; padding-left:16px;"
                           " padding-right:12px;")
        self.setVisible(bool(text))
        if self._breathe and self.isVisible():
            if not self._timer.isActive():
                self._t0 = time.monotonic()
                self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def hideEvent(self, ev):
        self._timer.stop()
        super().hideEvent(ev)

    def showEvent(self, ev):
        if self._breathe:
            self._timer.start()
        super().showEvent(ev)

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if not self.text():
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        c = QtCore.QPointF(7.5, self.height() / 2.0 + 0.5)
        if self._breathe:                               # a soft ring swells out of the dot and fades
            ph = ((time.monotonic() - self._t0) % self.PERIOD) / self.PERIOD
            ring = QtGui.QColor(self._col)
            ring.setAlphaF(0.45 * (1.0 - ph) ** 1.5)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(ring)
            rr = self.DOT + 4.0 * math.sin(ph * math.pi / 2)
            p.drawEllipse(c, rr, rr)
        if self._ring:
            p.setPen(QtGui.QPen(self._col, 1.4))
            p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        else:
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(self._col)
        p.drawEllipse(c, self.DOT, self.DOT)
        p.end()


class LoadBar(QtWidgets.QWidget):
    """A 2 px line along the top of a chart that runs while it loads, and fades away when the bars are in."""

    H = 2

    def __init__(self, host: QtWidgets.QWidget):
        super().__init__(host)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._t0 = time.monotonic()
        self._fade = 1.0
        self._stopping = None
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        host.installEventFilter(self)
        self.hide()

    def eventFilter(self, host, ev):
        if ev.type() == QtCore.QEvent.Type.Resize:
            self.setGeometry(0, 0, host.width(), self.H)
        return False

    @property
    def running(self) -> bool:
        return self.isVisible() and self._stopping is None

    def start(self):
        self._t0 = time.monotonic()
        self._fade, self._stopping = 1.0, None
        self.setGeometry(0, 0, self.parentWidget().width(), self.H)
        self.show()
        self.raise_()
        self._timer.start()

    def stop(self):
        if self.isVisible() and self._stopping is None:
            self._stopping = time.monotonic()

    def _tick(self):
        if self._stopping is not None:
            self._fade = max(0.0, 1.0 - (time.monotonic() - self._stopping) / 0.3)
            if self._fade <= 0.0:
                self._timer.stop()
                self.hide()
                return
        self.update()

    def paintEvent(self, ev):
        w = self.width()
        if w <= 0:
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        t = time.monotonic() - self._t0
        ph = (t % 1.25) / 1.25                         # a bright segment sweeps across, easing in and out
        e = 0.5 - 0.5 * math.cos(ph * math.pi)
        seg = max(80.0, w * 0.28)
        x = -seg + e * (w + seg)
        acc = QtGui.QColor(theme.ACCENT)
        g = QtGui.QLinearGradient(x, 0, x + seg, 0)
        c0 = QtGui.QColor(acc)
        c0.setAlphaF(0.0)
        c1 = QtGui.QColor(acc)
        c1.setAlphaF(self._fade)
        g.setColorAt(0.0, c0)
        g.setColorAt(0.55, c1)
        g.setColorAt(1.0, c0)
        p.fillRect(QtCore.QRectF(0, 0, w, self.H), QtGui.QColor(41, 98, 255, int(38 * self._fade)))
        p.fillRect(QtCore.QRectF(x, 0, seg, self.H), g)
        p.end()


def human(msg: str) -> str:
    """An error as a person reads it: no 'RuntimeError:' in front, one line, not endless."""
    import re
    s = re.sub(r"^(?:[A-Za-z_.]*(?:Error|Exception|Warning)\s*:\s*)+", "", str(msg or "").strip())
    s = re.sub(r"\s+", " ", s)
    return s if len(s) <= 220 else s[:217] + "…"


class EmptyState(QtWidgets.QWidget):
    """What a chart shows when its bars could not be loaded: what happened, in plain words, and what to do next."""

    retry = QtCore.Signal()
    search = QtCore.Signal()

    def __init__(self, host: QtWidgets.QWidget):
        super().__init__(host)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, False)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(0)
        lay.addStretch(1)
        self.icon = QtWidgets.QLabel()
        self.icon.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        try:
            from . import icons
            self.icon.setPixmap(icons.get("search", "#5d5d5d", 44).pixmap(44, 44))
        except Exception:  # noqa: BLE001
            pass
        lay.addWidget(self.icon)
        lay.addSpacing(16)
        self.title = QtWidgets.QLabel("")
        self.title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.title.setStyleSheet("color:#dbdbdb; font-size:17px; font-weight:600; background:transparent;")
        lay.addWidget(self.title)
        lay.addSpacing(8)
        self.detail = QtWidgets.QLabel("")
        self.detail.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.detail.setWordWrap(True)
        self.detail.setMaximumWidth(460)
        self.detail.setStyleSheet("color:#8c8c8c; font-size:13px; background:transparent;")
        row0 = QtWidgets.QHBoxLayout()
        row0.addStretch(1)
        row0.addWidget(self.detail)
        row0.addStretch(1)
        lay.addLayout(row0)
        lay.addSpacing(20)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(10)
        row.addStretch(1)
        self.retry_btn = QtWidgets.QPushButton("Try again")
        self.search_btn = QtWidgets.QPushButton("Search symbols")
        for b, main in ((self.retry_btn, True), (self.search_btn, False)):
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            b.setFixedHeight(34)
            b.setStyleSheet(
                ("QPushButton{background:#f2f2f2;color:#000;border:0;border-radius:8px;padding:0 16px;font-size:14px;}"
                 "QPushButton:hover{background:#ffffff;}") if main else
                ("QPushButton{background:transparent;color:#dbdbdb;border:1px solid #3a3a3a;border-radius:8px;"
                 "padding:0 16px;font-size:14px;} QPushButton:hover{background:#1e1e1e;}"))
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        self.retry_btn.clicked.connect(self.retry.emit)
        self.search_btn.clicked.connect(self.search.emit)
        host.installEventFilter(self)
        self.hide()

    def eventFilter(self, host, ev):
        if ev.type() == QtCore.QEvent.Type.Resize and self.isVisible():
            self.setGeometry(host.rect())
        return False

    def show_error(self, symbol: str, msg: str):
        self.title.setText(f"Couldn’t load {symbol}" if symbol else "Couldn’t load the chart")
        self.detail.setText(human(msg))
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 235))   # over whatever the chart had: quiet, readable
        p.end()


def pill_button(text: str, main: bool = False, small: bool = False) -> QtWidgets.QPushButton:
    """The empty states' buttons: white for the thing to do next, outlined for the other way."""
    b = QtWidgets.QPushButton(text)
    b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
    b.setFixedHeight(30 if small else 34)
    fs = 13 if small else 14
    b.setStyleSheet(
        (f"QPushButton{{background:#f2f2f2;color:#000;border:0;border-radius:8px;padding:0 16px;font-size:{fs}px;}}"
         "QPushButton:hover{background:#ffffff;} QPushButton:pressed{background:#dbdbdb;}") if main else
        ("QPushButton{background:transparent;color:#dbdbdb;border:1px solid #3a3a3a;border-radius:8px;"
         f"padding:0 16px;font-size:{fs}px;}} QPushButton:hover{{background:#1e1e1e;}}"))
    return b


class Placeholder(QtWidgets.QWidget):
    """A panel with nothing in it yet says what will be here and how to get it there: a quiet icon, a title, a line
    of help and the button for the next step (TradingView's empty panels)."""

    def __init__(self, icon: str, title: str, text: str, buttons=(), parent=None, icon_size: int = 36,
                 compact: bool = False):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(*((16, 4, 16, 4) if compact else (20, 16, 20, 16)))
        lay.setSpacing(0)
        lay.addStretch(1)
        self.icon = QtWidgets.QLabel()
        self.icon.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.icon.setStyleSheet("background:transparent;")
        try:
            from . import icons
            self.icon.setPixmap(icons.get(icon, "#5d5d5d", icon_size).pixmap(icon_size, icon_size))
        except Exception:  # noqa: BLE001 - an unknown icon: the words alone
            self.icon.hide()
        lay.addWidget(self.icon)
        lay.addSpacing(12)
        if compact:                                     # a short panel (the Strategy Tester): the words only
            self.icon.hide()
        self.title = QtWidgets.QLabel(title)
        self.title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.title.setWordWrap(True)
        self.title.setStyleSheet("color:#dbdbdb; font-size:15px; font-weight:600; background:transparent;")
        lay.addWidget(self.title)
        lay.addSpacing(6)
        self.text = QtWidgets.QLabel(text)
        self.text.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.text.setWordWrap(True)
        self.text.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.text.setMaximumWidth(560 if compact else 420)
        self.text.setStyleSheet("color:#8c8c8c; font-size:13px; background:transparent;")
        row0 = QtWidgets.QHBoxLayout()
        row0.addStretch(1)
        row0.addWidget(self.text, 10)
        row0.addStretch(1)
        lay.addLayout(row0)
        self.buttons = []
        if buttons:
            lay.addSpacing(10 if compact else 16)
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(10)
            row.addStretch(1)
            for k, (label, fn) in enumerate(buttons):
                b = pill_button(label, main=k == 0, small=True)
                b.clicked.connect(fn)
                row.addWidget(b)
                self.buttons.append(b)
            row.addStretch(1)
            lay.addLayout(row)
        lay.addStretch(1)


class EmptyHint(QtWidgets.QLabel):
    """One quiet line in the middle of an empty table ('No trades yet ...'), gone as soon as a row comes in."""

    def __init__(self, view: QtWidgets.QAbstractItemView, html: str):
        super().__init__(html, view.viewport())
        self.view = view
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background:transparent; color:#787b86; font-size:13px;")
        view.viewport().installEventFilter(self)
        m = view.model()
        for sig in (m.rowsInserted, m.rowsRemoved, m.modelReset, m.layoutChanged):
            sig.connect(self._update)
        self._update()

    def eventFilter(self, obj, ev):
        if ev.type() == QtCore.QEvent.Type.Resize:
            self._place()
        return False

    def _place(self):
        vp = self.view.viewport()
        self.setFixedWidth(max(160, min(vp.width() - 32, 460)))
        self.adjustSize()
        self.move((vp.width() - self.width()) // 2, max(8, (vp.height() - self.height()) // 2))

    def _update(self, *_a):
        empty = self.view.model().rowCount() == 0
        self.setVisible(empty)
        if empty:
            self._place()
