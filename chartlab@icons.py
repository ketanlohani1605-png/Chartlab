"""Small vector icons (drawn with QPainter, crisp on Retina) in the TradingView toolbar style: a 28 px box,
1-point lines, #b8b8b8 (TradingView's icon colour, measured), white under the mouse. The drawings are ChartLab's own
(generic symbols: a clock, a rewind sign, a bar chart ...)."""
from __future__ import annotations

from PySide6 import QtCore, QtGui

_COLOR = "#b8b8b8"
_HOT = "#ffffff"
_BOX = 28                         # TradingView's toolbar icons are 28 x 28
_cache: dict = {}


def _icon(name: str, draw, color: str = _COLOR, size: int = _BOX) -> QtGui.QIcon:
    key = (name, color, size)
    if key in _cache:
        return _cache[key]
    ic = QtGui.QIcon()
    k = size / 24.0                # the drawings use a 24-unit grid, centred in the box
    for mode, col in ((QtGui.QIcon.Mode.Normal, color), (QtGui.QIcon.Mode.Active, _HOT),
                      (QtGui.QIcon.Mode.Disabled, "#5d5d5d")):
        for state_on in (False, True):
            dpr = 2
            pm = QtGui.QPixmap(size * dpr, size * dpr)
            pm.setDevicePixelRatio(dpr)
            pm.fill(QtCore.Qt.GlobalColor.transparent)
            p = QtGui.QPainter(pm)
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            c = QtGui.QColor(_HOT if (state_on and mode == QtGui.QIcon.Mode.Normal) else col)
            if size >= 26:                         # big toolbar box: the 24-grid drawing at its natural size, centred
                off = (size - 24) / 2.0
                p.translate(off, off)
                pen = QtGui.QPen(c, 1.0)           # 1 point, like TradingView
            else:
                p.scale(k, k)
                pen = QtGui.QPen(c, 1.15 / k)
            pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            draw(p, QtCore.QRectF(0, 0, 24, 24), c)
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
    """The crosshair cursor: four arms with an open centre."""
    for a, b in (((12, 3.5), (12, 9.5)), ((12, 14.5), (12, 20.5)), ((3.5, 12), (9.5, 12)), ((14.5, 12), (20.5, 12))):
        p.drawLine(QtCore.QPointF(*a), QtCore.QPointF(*b))


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
    """Horseshoe magnet (TradingView's snap tool)."""
    path = QtGui.QPainterPath(QtCore.QPointF(5, 5))
    path.lineTo(5, 12)
    path.arcTo(QtCore.QRectF(5, 5, 14, 14), 180, 180)
    path.lineTo(19, 5)
    path.lineTo(15, 5)
    path.lineTo(15, 12)
    path.arcTo(QtCore.QRectF(9, 9, 6, 6), 0, -180)
    path.lineTo(9, 5)
    path.closeSubpath()
    p.drawPath(path)
    p.drawLine(QtCore.QPointF(5, 8.5), QtCore.QPointF(9, 8.5))
    p.drawLine(QtCore.QPointF(15, 8.5), QtCore.QPointF(19, 8.5))


def refresh(p, r, c):
    p.drawArc(QtCore.QRectF(5, 5, 14, 14), 60 * 16, 290 * 16)
    p.drawLine(QtCore.QPointF(17.5, 4.5), QtCore.QPointF(17.5, 8.8))
    p.drawLine(QtCore.QPointF(17.5, 8.8), QtCore.QPointF(13.3, 8.8))


def datawin(p, r, c):
    p.drawRoundedRect(QtCore.QRectF(4.5, 4.5, 15, 15), 2, 2)
    for y in (9, 12.5, 16):
        p.drawLine(QtCore.QPointF(7.5, y), QtCore.QPointF(11, y))
        p.drawLine(QtCore.QPointF(13.5, y), QtCore.QPointF(16.5, y))


def chain(p, r, c):
    """Option chain: two columns of rows either side of a strike spine."""
    p.drawLine(QtCore.QPointF(12, 4.5), QtCore.QPointF(12, 19.5))
    for y in (7, 12, 17):
        p.drawLine(QtCore.QPointF(4.5, y), QtCore.QPointF(9.5, y))
        p.drawLine(QtCore.QPointF(14.5, y), QtCore.QPointF(19.5, y))


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
    """Bar replay: a rewind sign (two triangles pointing back)."""
    for x0 in (4.5, 11.5):
        p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(x0 + 7.5, 6.5), QtCore.QPointF(x0, 12),
                                       QtCore.QPointF(x0 + 7.5, 17.5)]))


def bell(p, r, c):
    """Alerts: an alarm clock with a small plus (create alert)."""
    p.drawEllipse(QtCore.QPointF(11.5, 13), 7, 7)
    p.drawLine(QtCore.QPointF(11.5, 9), QtCore.QPointF(11.5, 13))
    p.drawLine(QtCore.QPointF(11.5, 13), QtCore.QPointF(14, 14.5))
    p.drawArc(QtCore.QRectF(2.5, 3, 6, 6), 100 * 16, 80 * 16)        # the two bells on top
    p.drawArc(QtCore.QRectF(14.5, 3, 6, 6), 0, 80 * 16)
    p.drawLine(QtCore.QPointF(19, 17), QtCore.QPointF(19, 23))       # plus
    p.drawLine(QtCore.QPointF(16, 20), QtCore.QPointF(22, 20))


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
    """Chart layout: the shape of the current layout (one chart = one square, like TradingView's button)."""
    p.drawRoundedRect(QtCore.QRectF(4.5, 4.5, 15, 15), 2, 2)


def layout_2h(p, r, c):
    p.drawRoundedRect(QtCore.QRectF(4.5, 4.5, 15, 15), 2, 2)
    p.drawLine(QtCore.QPointF(12, 4.5), QtCore.QPointF(12, 19.5))


def layout_2v(p, r, c):
    p.drawRoundedRect(QtCore.QRectF(4.5, 4.5, 15, 15), 2, 2)
    p.drawLine(QtCore.QPointF(4.5, 12), QtCore.QPointF(19.5, 12))


def layout_4(p, r, c):
    layout_2h(p, r, c)
    p.drawLine(QtCore.QPointF(4.5, 12), QtCore.QPointF(19.5, 12))


def fx(p, r, c):
    """Indicators: a small bar chart with a rising line across it."""
    for x, top in ((5.5, 14), (9.5, 11), (13.5, 13), (17.5, 8)):
        p.drawLine(QtCore.QPointF(x, top), QtCore.QPointF(x, 19.5))
    p.drawLine(QtCore.QPointF(3.5, 19.5), QtCore.QPointF(20.5, 19.5))
    p.drawPolyline([QtCore.QPointF(4, 11), QtCore.QPointF(9.5, 6.5), QtCore.QPointF(13.5, 9),
                    QtCore.QPointF(20, 3.5)])


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


def search(p, r, c):
    p.drawEllipse(QtCore.QPointF(10.5, 10.5), 6, 6)
    p.drawLine(QtCore.QPointF(15, 15), QtCore.QPointF(20, 20))


def plus(p, r, c):
    p.drawEllipse(QtCore.QPointF(12, 12), 8, 8)
    p.drawLine(QtCore.QPointF(12, 8), QtCore.QPointF(12, 16))
    p.drawLine(QtCore.QPointF(8, 12), QtCore.QPointF(16, 12))


def calendar(p, r, c):
    p.drawRoundedRect(QtCore.QRectF(4.5, 6, 15, 13), 2, 2)
    p.drawLine(QtCore.QPointF(4.5, 10), QtCore.QPointF(19.5, 10))
    p.drawLine(QtCore.QPointF(8.5, 4), QtCore.QPointF(8.5, 7.5))
    p.drawLine(QtCore.QPointF(15.5, 4), QtCore.QPointF(15.5, 7.5))


def eye(p, r, c):
    path = QtGui.QPainterPath(QtCore.QPointF(3.5, 12))
    path.quadTo(12, 3, 20.5, 12)
    path.quadTo(12, 21, 3.5, 12)
    p.drawPath(path)
    p.drawEllipse(QtCore.QPointF(12, 12), 3, 3)


def lock(p, r, c):
    p.drawRoundedRect(QtCore.QRectF(6, 11, 12, 9), 2, 2)
    path = QtGui.QPainterPath(QtCore.QPointF(8.5, 11))
    path.lineTo(8.5, 8)
    path.arcTo(QtCore.QRectF(8.5, 4, 7, 8), 180, -180)
    path.lineTo(15.5, 11)
    p.drawPath(path)


def menu(p, r, c):
    for y in (7, 12, 17):
        p.drawLine(QtCore.QPointF(5, y), QtCore.QPointF(19, y))


def compare(p, r, c):
    p.drawEllipse(QtCore.QPointF(12, 12), 8, 8)
    p.drawLine(QtCore.QPointF(12, 8), QtCore.QPointF(12, 16))
    p.drawLine(QtCore.QPointF(8, 12), QtCore.QPointF(16, 12))


def gear(p, r, c):
    import math
    path = QtGui.QPainterPath()
    for k in range(6):
        a = math.radians(60 * k - 30)
        pt = QtCore.QPointF(12 + 8.6 * math.cos(a), 12 + 8.6 * math.sin(a))
        path.moveTo(pt) if k == 0 else path.lineTo(pt)
    path.closeSubpath()
    p.drawPath(path)
    p.drawEllipse(QtCore.QPointF(12, 12), 3, 3)


# ---- chart-type icons (TradingView shows one per style)
def st_bars(p, r, c):
    for x, top, bot, o, cl in ((7, 6, 17, 9, 14), (13, 9, 20, 17, 11), (19, 4, 13, 6, 11)):
        p.drawLine(QtCore.QPointF(x, top), QtCore.QPointF(x, bot))
        p.drawLine(QtCore.QPointF(x - 3, o), QtCore.QPointF(x, o))
        p.drawLine(QtCore.QPointF(x, cl), QtCore.QPointF(x + 3, cl))


def st_candles(p, r, c):
    p.setBrush(c)
    for x, top, bot in ((7, 6, 15), (13, 9, 19), (19, 4, 12)):
        p.drawLine(QtCore.QPointF(x, top - 2), QtCore.QPointF(x, bot + 2))
        p.drawRect(QtCore.QRectF(x - 2, top + 2, 4, bot - top - 4))


def st_hollow(p, r, c):
    candles_icon(p, r, c)


def chev(p, r, c):
    """A small down chevron (8 x 4, TradingView's list arrow next to a toolbar button)."""
    p.drawPolyline([QtCore.QPointF(8, 10), QtCore.QPointF(12, 14), QtCore.QPointF(16, 10)])


def datatype(p, r, c):
    """Two small outlined candles - the button inside the symbol pill that switches the data feed."""
    for x, top, bot, b0, b1 in ((8.5, 4.5, 20.5, 8.5, 17.5), (15.5, 3.5, 17.5, 6.5, 14.5)):
        p.drawLine(QtCore.QPointF(x, top), QtCore.QPointF(x, b0))
        p.drawLine(QtCore.QPointF(x, b1), QtCore.QPointF(x, bot))
        p.drawRoundedRect(QtCore.QRectF(x - 2.5, b0, 5, b1 - b0), 1, 1)


def st_heikin(p, r, c):
    p.setBrush(c)
    for x, top, bot in ((6, 12, 19), (12, 8, 16), (18, 4, 12)):
        p.drawLine(QtCore.QPointF(x, top - 2), QtCore.QPointF(x, bot + 2))
        p.drawRect(QtCore.QRectF(x - 2, top + 1, 4, bot - top - 2))


def st_line(p, r, c):
    p.drawPolyline([QtCore.QPointF(4, 17), QtCore.QPointF(9, 11), QtCore.QPointF(13, 14), QtCore.QPointF(20, 6)])


def st_linemark(p, r, c):
    pts = [QtCore.QPointF(4, 17), QtCore.QPointF(9, 11), QtCore.QPointF(13, 14), QtCore.QPointF(20, 6)]
    p.drawPolyline(pts)
    p.setBrush(c)
    for q in pts:
        p.drawEllipse(q, 1.6, 1.6)


def st_step(p, r, c):
    p.drawPolyline([QtCore.QPointF(4, 17), QtCore.QPointF(9, 17), QtCore.QPointF(9, 11), QtCore.QPointF(14, 11),
                    QtCore.QPointF(14, 7), QtCore.QPointF(20, 7)])


def st_area(p, r, c):
    path = QtGui.QPainterPath(QtCore.QPointF(4, 19))
    for x, y in ((4, 16), (9, 10), (13, 13), (20, 6), (20, 19)):
        path.lineTo(x, y)
    path.closeSubpath()
    fill = QtGui.QColor(c)
    fill.setAlpha(70)
    p.fillPath(path, fill)
    p.drawPolyline([QtCore.QPointF(4, 16), QtCore.QPointF(9, 10), QtCore.QPointF(13, 13), QtCore.QPointF(20, 6)])


def st_baseline(p, r, c):
    pen = QtGui.QPen(p.pen())
    dash = QtGui.QPen(pen)
    dash.setStyle(QtCore.Qt.PenStyle.DashLine)
    p.setPen(dash)
    p.drawLine(QtCore.QPointF(3, 12), QtCore.QPointF(21, 12))
    p.setPen(pen)
    p.drawPolyline([QtCore.QPointF(4, 16), QtCore.QPointF(9, 8), QtCore.QPointF(14, 15), QtCore.QPointF(20, 7)])


# ---- drawing-tool icons
def t_ext(p, r, c):
    p.drawLine(QtCore.QPointF(3, 20), QtCore.QPointF(21, 4))
    _dot(p, 8, 15.5)
    _dot(p, 16, 8.5)


def t_arrow(p, r, c):
    p.drawLine(QtCore.QPointF(5, 19), QtCore.QPointF(18, 6))
    p.drawPolyline([QtCore.QPointF(11, 6), QtCore.QPointF(18, 6), QtCore.QPointF(18, 13)])


def t_channel(p, r, c):
    p.drawLine(QtCore.QPointF(4, 14), QtCore.QPointF(20, 6))
    p.drawLine(QtCore.QPointF(4, 19), QtCore.QPointF(20, 11))
    for x, y in ((4, 14), (20, 6), (4, 19)):
        p.drawEllipse(QtCore.QPointF(x, y), 1.6, 1.6)


def t_brush(p, r, c):
    path = QtGui.QPainterPath(QtCore.QPointF(4, 17))
    path.cubicTo(QtCore.QPointF(8, 6), QtCore.QPointF(12, 22), QtCore.QPointF(15, 11))
    path.cubicTo(QtCore.QPointF(16, 8), QtCore.QPointF(18, 7), QtCore.QPointF(20, 6))
    p.drawPath(path)


def t_stay(p, r, c):
    p.drawPolygon(QtGui.QPolygonF([QtCore.QPointF(5, 19), QtCore.QPointF(6, 15), QtCore.QPointF(15, 6),
                                   QtCore.QPointF(18, 9), QtCore.QPointF(9, 18)]))
    p.drawLine(QtCore.QPointF(13, 8), QtCore.QPointF(16, 11))
    p.drawRoundedRect(QtCore.QRectF(14, 15, 6, 4.5), 1, 1)
    p.drawArc(QtCore.QRectF(15.2, 12.2, 3.6, 5), 0, 180 * 16)


def t_ellipse(p, r, c):
    p.drawEllipse(QtCore.QRectF(4, 7, 16, 11))


def t_long(p, r, c):
    p.drawRect(QtCore.QRectF(5, 4, 14, 8))
    pen = QtGui.QPen(p.pen())
    p.drawRect(QtCore.QRectF(5, 12, 14, 7))
    p.setPen(pen)
    p.drawLine(QtCore.QPointF(8, 9), QtCore.QPointF(12, 6))
    p.drawLine(QtCore.QPointF(12, 6), QtCore.QPointF(16, 9))


def t_short(p, r, c):
    p.drawRect(QtCore.QRectF(5, 5, 14, 7))
    p.drawRect(QtCore.QRectF(5, 12, 14, 8))
    p.drawLine(QtCore.QPointF(8, 14), QtCore.QPointF(12, 17))
    p.drawLine(QtCore.QPointF(12, 17), QtCore.QPointF(16, 14))


def t_pricerange(p, r, c):
    p.drawLine(QtCore.QPointF(5, 5), QtCore.QPointF(19, 5))
    p.drawLine(QtCore.QPointF(5, 19), QtCore.QPointF(19, 19))
    p.drawLine(QtCore.QPointF(12, 6), QtCore.QPointF(12, 18))
    p.drawPolyline([QtCore.QPointF(9.5, 8.5), QtCore.QPointF(12, 6), QtCore.QPointF(14.5, 8.5)])
    p.drawPolyline([QtCore.QPointF(9.5, 15.5), QtCore.QPointF(12, 18), QtCore.QPointF(14.5, 15.5)])


def t_daterange(p, r, c):
    p.drawLine(QtCore.QPointF(5, 5), QtCore.QPointF(5, 19))
    p.drawLine(QtCore.QPointF(19, 5), QtCore.QPointF(19, 19))
    p.drawLine(QtCore.QPointF(6, 12), QtCore.QPointF(18, 12))
    p.drawPolyline([QtCore.QPointF(8.5, 9.5), QtCore.QPointF(6, 12), QtCore.QPointF(8.5, 14.5)])
    p.drawPolyline([QtCore.QPointF(15.5, 9.5), QtCore.QPointF(18, 12), QtCore.QPointF(15.5, 14.5)])



def buy_order(p, r, c):
    """Buy at a price: an arrow rising onto a line."""
    p.drawLine(QtCore.QPointF(5, 7), QtCore.QPointF(19, 7))
    p.drawLine(QtCore.QPointF(12, 19), QtCore.QPointF(12, 10))
    p.drawPolyline([QtCore.QPointF(8.5, 13.5), QtCore.QPointF(12, 10), QtCore.QPointF(15.5, 13.5)])


def sell_order(p, r, c):
    """Sell at a price: an arrow falling onto a line."""
    p.drawLine(QtCore.QPointF(5, 17), QtCore.QPointF(19, 17))
    p.drawLine(QtCore.QPointF(12, 5), QtCore.QPointF(12, 14))
    p.drawPolyline([QtCore.QPointF(8.5, 10.5), QtCore.QPointF(12, 14), QtCore.QPointF(15.5, 10.5)])


def order_add(p, r, c):
    """Add an order: a ticket with a plus."""
    p.drawRoundedRect(QtCore.QRectF(4.5, 6.5, 15, 11), 1.5, 1.5)
    p.drawLine(QtCore.QPointF(12, 9.5), QtCore.QPointF(12, 14.5))
    p.drawLine(QtCore.QPointF(9.5, 12), QtCore.QPointF(14.5, 12))


def cloud(p, r, c):
    """The data store: a cloud."""
    path = QtGui.QPainterPath()
    path.setFillRule(QtCore.Qt.FillRule.WindingFill)
    path.addEllipse(QtCore.QPointF(8.0, 14.5), 3.5, 3.5)
    path.addEllipse(QtCore.QPointF(12.5, 10.8), 4.6, 4.6)
    path.addEllipse(QtCore.QPointF(16.6, 14.8), 3.2, 3.2)
    path.addRect(QtCore.QRectF(8.0, 14.0, 8.6, 4.0))
    p.drawPath(path.simplified())


def update_icon(p, r, c):
    """Updates: an arrow coming down into a tray."""
    p.drawLine(QtCore.QPointF(12, 4.5), QtCore.QPointF(12, 14.5))
    p.drawPolyline([QtCore.QPointF(8, 10.5), QtCore.QPointF(12, 14.5), QtCore.QPointF(16, 10.5)])
    p.drawPolyline([QtCore.QPointF(5, 16), QtCore.QPointF(5, 19.5), QtCore.QPointF(19, 19.5), QtCore.QPointF(19, 16)])


_DRAWERS = {n: f for n, f in globals().items() if callable(f) and n in (
    "cloud", "update_icon",
    "cross", "pause", "step", "close", "replay_icon", "bell", "cursor", "measure", "text", "trend", "ray", "hline", "vline", "rect", "fib", "magnet", "eraser", "trash",
    "watchlist", "code", "table", "candles_icon", "layout_icon", "layout_2h", "layout_2v", "layout_4", "fx", "play",
    "latest",
    "undo", "redo", "camera", "fullscreen", "zoom", "plus", "eye", "lock", "menu", "compare", "calendar", "gear", "search", "refresh", "datawin", "chain", "st_bars", "st_candles", "st_hollow", "st_heikin", "st_line",
    "st_linemark", "st_step", "st_area", "st_baseline", "t_ext", "t_arrow", "t_ellipse", "t_long", "t_short",
    "t_pricerange", "t_daterange", "t_channel", "t_brush", "t_stay", "datatype", "chev", "buy_order", "sell_order",
    "order_add")}


def get(name: str, color: str = _COLOR, size: int = _BOX) -> QtGui.QIcon:
    return _icon(name, _DRAWERS[name], color, size)
