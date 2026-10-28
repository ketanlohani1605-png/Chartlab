"""Small vector icons (drawn with QPainter, crisp on Retina) in the TradingView toolbar style."""
from __future__ import annotations

from PySide6 import QtCore, QtGui

_COLOR = "#d1d4dc"
_cache: dict = {}


def _icon(name: str, draw, color: str = _COLOR, size: int = 24) -> QtGui.QIcon:
    key = (name, color, size)
    if key in _cache:
        return _cache[key]
    ic = QtGui.QIcon()
    for mode, col in ((QtGui.QIcon.Mode.Normal, color), (QtGui.QIcon.Mode.Active, "#ffffff"),
                      (QtGui.QIcon.Mode.Disabled, "#5d606b")):
        for state_on in (False, True):
            dpr = 2
            pm = QtGui.QPixmap(size * dpr, size * dpr)
            pm.setDevicePixelRatio(dpr)
            pm.fill(QtCore.Qt.GlobalColor.transparent)
            p = QtGui.QPainter(pm)
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            c = QtGui.QColor("#ffffff" if (state_on and mode == QtGui.QIcon.Mode.Normal) else col)
            pen = QtGui.QPen(c, 1.5)
            pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            draw(p, QtCore.QRectF(0, 0, size, size), c)
            p.end()
            ic.addPixmap(pm, mode, QtGui.QIcon.State.On if state_on else QtGui.QIcon.State.Off)
    _cache[key] = ic
    return ic


def _dot(p, x, y, r=2.2):
    p.save()
    p.setBrush(p.pen().color())
    p.drawEllipse(QtCore.QPointF(x, y), r, r)
    p.restore()


def cross(p, r, c):
    p.drawLine(QtCore.QPointF(12, 4), QtCore.QPointF(12, 20))
    p.drawLine(QtCore.QPointF(4, 12), QtCore.QPointF(20, 12))


def cursor(p, r, c):
    path = QtGui.QPainterPath(QtCore.QPointF(7, 4))
    for x, y in ((7, 18), (11, 14.5), (14, 20.5), (16.2, 19.4), (13.2, 13.6), (18.2, 13.4)):
        path.lineTo(x, y)
    path.closeSubpath()
    p.drawPath(path)


def trend(p, r, c):
    p.drawLine(QtCore.QPointF(6, 18), QtCore.QPointF(18, 6))
    _dot(p, 6, 18)
    _dot(p, 18, 6)


def ray(p, r, c):
    p.drawLine(QtCore.QPointF(5, 17), QtCore.QPointF(21, 7))
    _dot(p, 5, 17)
    p.drawEllipse(QtCore.QPointF(12, 12), 2.2, 2.2)


def hline(p, r, c):
    p.drawLine(QtCore.QPointF(4, 12), QtCore.QPointF(20, 12))
    _dot(p, 12, 12)


def vline(p, r, c):
    p.drawLine(QtCore.QPointF(12, 4), QtCore.QPointF(12, 20))
    _dot(p, 12, 12)


def rect(p, r, c):
    p.drawRect(QtCore.QRectF(5, 7, 14, 10))
    _dot(p, 5, 7)
    _dot(p, 19, 17)


def fib(p, r, c):
    for i, y in enumerate((5, 9.5, 14, 19)):
        p.drawLine(QtCore.QPointF(4, y), QtCore.QPointF(20, y))
    p.drawLine(QtCore.QPointF(6, 19), QtCore.QPointF(18, 5))


def magnet(p, r, c):
    path = QtGui.QPainterPath(QtCore.QPointF(7, 5))
    path.lineTo(7, 12)
    path.arcTo(QtCore.QRectF(7, 8, 10, 10), 180, 180)
    path.lineTo(17, 5)
    p.drawPath(path)
    p.drawLine(QtCore.QPointF(5.5, 5), QtCore.QPointF(8.5, 5))
    p.drawLine(QtCore.QPointF(15.5, 5), QtCore.QPointF(18.5, 5))


def eraser(p, r, c):
    p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(5, 14), QtCore.QPointF(13, 6), QtCore.QPointF(19, 12),
                                   QtCore.QPointF(13, 18), QtCore.QPointF(9, 18)]))
    p.drawLine(QtCore.QPointF(9, 10), QtCore.QPointF(15, 16))


def trash(p, r, c):
    p.drawLine(QtCore.QPointF(5, 7), QtCore.QPointF(19, 7))
    p.drawRect(QtCore.QRectF(7, 7, 10, 12))
    p.drawLine(QtCore.QPointF(10, 4.5), QtCore.QPointF(14, 4.5))
    p.drawLine(QtCore.QPointF(10, 10), QtCore.QPointF(10, 16))
    p.drawLine(QtCore.QPointF(14, 10), QtCore.QPointF(14, 16))


def measure(p, r, c):
    p.drawRect(QtCore.QRectF(4, 8, 16, 8))
    for x in (8, 12, 16):
        p.drawLine(QtCore.QPointF(x, 8), QtCore.QPointF(x, 11))


def text(p, r, c):
    p.drawLine(QtCore.QPointF(6, 6), QtCore.QPointF(18, 6))
    p.drawLine(QtCore.QPointF(12, 6), QtCore.QPointF(12, 19))
    p.drawLine(QtCore.QPointF(9, 19), QtCore.QPointF(15, 19))


def pause(p, r, c):
    p.setBrush(p.pen().color())
    p.drawRect(QtCore.QRectF(7, 6, 3.5, 12))
    p.drawRect(QtCore.QRectF(13.5, 6, 3.5, 12))


def step(p, r, c):
    p.setBrush(p.pen().color())
    p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(6, 6), QtCore.QPointF(15, 12), QtCore.QPointF(6, 18)]))
    p.drawLine(QtCore.QPointF(18, 6), QtCore.QPointF(18, 18))


def close(p, r, c):
    p.drawLine(QtCore.QPointF(7, 7), QtCore.QPointF(17, 17))
    p.drawLine(QtCore.QPointF(17, 7), QtCore.QPointF(7, 17))


def replay_icon(p, r, c):
    p.drawArc(QtCore.QRectF(5, 5, 14, 14), 40 * 16, 280 * 16)
    p.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(4, 6), QtCore.QPointF(6.5, 9.5), QtCore.QPointF(10, 7.5)]))
    p.setBrush(p.pen().color())
    p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(10.5, 9), QtCore.QPointF(15, 12), QtCore.QPointF(10.5, 15)]))


def bell(p, r, c):
    path = QtGui.QPainterPath(QtCore.QPointF(6, 16))
    path.cubicTo(8, 15, 8, 12, 8, 10)
    path.cubicTo(8, 6.5, 16, 6.5, 16, 10)
    path.cubicTo(16, 12, 16, 15, 18, 16)
    path.closeSubpath()
    p.drawPath(path)
    p.drawArc(QtCore.QRectF(10, 16, 4, 4), 200 * 16, 140 * 16)


def watchlist(p, r, c):
    for y in (7, 12, 17):
        p.drawLine(QtCore.QPointF(10, y), QtCore.QPointF(20, y))
        _dot(p, 6, y, 1.2)


def code(p, r, c):
    p.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(9, 7), QtCore.QPointF(4, 12), QtCore.QPointF(9, 17)]))
    p.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(15, 7), QtCore.QPointF(20, 12), QtCore.QPointF(15, 17)]))
    p.drawLine(QtCore.QPointF(13, 5), QtCore.QPointF(11, 19))


def table(p, r, c):
    p.drawRect(QtCore.QRectF(4, 6, 16, 12))
    p.drawLine(QtCore.QPointF(4, 11), QtCore.QPointF(20, 11))
    p.drawLine(QtCore.QPointF(10, 6), QtCore.QPointF(10, 18))


def candles_icon(p, r, c):
    for x, top, bot in ((7, 6, 15), (13, 9, 19), (19, 4, 12)):
        p.drawLine(QtCore.QPointF(x, top - 2), QtCore.QPointF(x, bot + 2))
        p.drawRect(QtCore.QRectF(x - 2, top + 2, 4, bot - top - 4))


def layout_icon(p, r, c):
    p.drawRect(QtCore.QRectF(4, 5, 16, 14))
    p.drawLine(QtCore.QPointF(12, 5), QtCore.QPointF(12, 19))
    p.drawLine(QtCore.QPointF(4, 12), QtCore.QPointF(20, 12))


def fx(p, r, c):
    f = QtGui.QFont("Helvetica Neue", 11)
    f.setItalic(True)
    p.setFont(f)
    p.drawText(QtCore.QRectF(2, 2, 22, 20), QtCore.Qt.AlignmentFlag.AlignCenter, "fx")


def play(p, r, c):
    p.setBrush(p.pen().color())
    p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(8, 5.5), QtCore.QPointF(19, 12), QtCore.QPointF(8, 18.5)]))


def latest(p, r, c):
    p.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(7, 6), QtCore.QPointF(13, 12), QtCore.QPointF(7, 18)]))
    p.drawLine(QtCore.QPointF(18, 6), QtCore.QPointF(18, 18))


def undo(p, r, c):
    path = QtGui.QPainterPath(QtCore.QPointF(9, 8))
    path.lineTo(5, 12)
    path.lineTo(9, 16)
    p.drawPath(path)
    path2 = QtGui.QPainterPath(QtCore.QPointF(5, 12))
    path2.lineTo(15, 12)
    path2.cubicTo(19, 12, 20, 17, 16, 18)
    path2.lineTo(11, 18)
    p.drawPath(path2)


def redo(p, r, c):
    p.save()
    p.translate(24, 0)
    p.scale(-1, 1)
    undo(p, r, c)
    p.restore()


def camera(p, r, c):
    p.drawRoundedRect(QtCore.QRectF(4, 8, 16, 11), 2, 2)
    p.drawPolyline([QtCore.QPointF(9, 8), QtCore.QPointF(10.5, 5.5), QtCore.QPointF(13.5, 5.5), QtCore.QPointF(15, 8)])
    p.drawEllipse(QtCore.QPointF(12, 13.5), 3, 3)


def fullscreen(p, r, c):
    for (x, y, dx, dy) in ((5, 5, 1, 1), (19, 5, -1, 1), (5, 19, 1, -1), (19, 19, -1, -1)):
        p.drawPolyline([QtCore.QPointF(x + 5 * dx, y), QtCore.QPointF(x, y), QtCore.QPointF(x, y + 5 * dy)])


def zoom(p, r, c):
    p.drawEllipse(QtCore.QPointF(11, 11), 6, 6)
    p.drawLine(QtCore.QPointF(15.5, 15.5), QtCore.QPointF(20, 20))
    p.drawLine(QtCore.QPointF(8.5, 11), QtCore.QPointF(13.5, 11))
    p.drawLine(QtCore.QPointF(11, 8.5), QtCore.QPointF(11, 13.5))


def plus(p, r, c):
    p.drawEllipse(QtCore.QPointF(12, 12), 8, 8)
    p.drawLine(QtCore.QPointF(12, 8), QtCore.QPointF(12, 16))
    p.drawLine(QtCore.QPointF(8, 12), QtCore.QPointF(16, 12))


_DRAWERS = {n: f for n, f in globals().items() if callable(f) and n in (
    "cross", "pause", "step", "close", "replay_icon", "bell", "cursor", "measure", "text", "trend", "ray", "hline", "vline", "rect", "fib", "magnet", "eraser", "trash",
    "watchlist", "code", "table", "candles_icon", "layout_icon", "fx", "play", "latest",
    "undo", "redo", "camera", "fullscreen", "zoom", "plus")}


def get(name: str, color: str = _COLOR, size: int = 24) -> QtGui.QIcon:
    return _icon(name, _DRAWERS[name], color, size)
