"""TradingView's look for a selected drawing (measured on its own chart canvas): ring handles in #1e53e5 filled with the
chart background, 12 px across; the line keeps its width; a blue label at each anchor's price and time and a 25 %
band between them on both scales; a box has 8 handles, a channel 6, a position tool 4, a horizontal line one handle
90 % across the pane. Each handle reshapes the drawing the way TradingView's does (through the real mouse path)."""
import os
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from PySide6 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from chartlab import drawings as D  # noqa: E402
from chartlab.chart import ChartWidget  # noqa: E402

n = 300
base = 100 + np.cumsum(np.sin(np.arange(n) / 7.0))
df = pd.DataFrame({"Open": base, "High": base + 1.5, "Low": base - 1.5, "Close": base + 0.3, "Volume": 1000.0},
                  index=pd.date_range("2024-01-01 09:15", periods=n, freq="15min"))
w = ChartWidget()
w.resize(1300, 760)
w.show()
w.set_data(df, "T")
app.processEvents()
w.magnet = False
L = w.layer
vb = w.price.vb
vp = w.glw.viewport()
QtTest.QTest.qWait(300)


def handles(did=None):
    did = L.selected if did is None else did
    return [it for it in L._items.get(did, []) if isinstance(it, D._Handle)]


def px(x, y):
    return w.glw.mapFromScene(vb.mapViewToScene(QtCore.QPointF(x, y)))


def send(kind, p, button, buttons):
    pf = QtCore.QPointF(p)
    ev = QtGui.QMouseEvent(kind, pf, pf, QtCore.QPointF(vp.mapToGlobal(p)), button, buttons,
                           QtCore.Qt.KeyboardModifier.NoModifier)
    QtWidgets.QApplication.sendEvent(vp, ev)
    app.processEvents()


def drag(a, b):
    QtTest.QTest.qWait(450)                           # (not a double click with the last one)
    LB, NB = QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.MouseButton.NoButton
    send(QtCore.QEvent.Type.MouseMove, a, NB, NB)
    send(QtCore.QEvent.Type.MouseButtonPress, a, LB, LB)
    for i in range(1, 11):
        send(QtCore.QEvent.Type.MouseMove, a + (b - a) * (i / 10), NB, LB)
    send(QtCore.QEvent.Type.MouseButtonRelease, b, LB, NB)


def tags_shown():
    ys = [t for t in L._sel_y if not t.isHidden()]
    xs = [t for t in L._sel_x if not t.isHidden()]
    return ys, xs


x0, x1 = vb.viewRange()[0]
ylo, yhi = vb.viewRange()[1]
T = w.x_to_time

# ------------------------------------------------------------------ a trend line
tid = L.add("trend", [[T(n - 90), ylo + (yhi - ylo) * 0.7], [T(n - 40), ylo + (yhi - ylo) * 0.4]])
L.selected = tid
L.rebuild()
hs = handles()
assert len(hs) == 2, hs
h = hs[0]
assert h.pen().color().name() == D.SEL_RING and abs(h.pen().widthF() - 1.5) < 1e-9
assert abs(h.rect().width() - 10.5) < 1e-9                      # 12 px outside with the 1.5 px ring
assert h.brush().color().name() == w.glw.backgroundBrush().color().name()
assert h.flags() & QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations
line = next(it for it in L._items[tid] if not isinstance(it, D._Handle))
assert abs(line.opts["pen"].widthF() - D.DEFAULT_WIDTH) < 1e-9, "a selected line must keep its width (TradingView)"
ys, xs = tags_shown()
assert len(ys) == 2 and len(xs) == 2, (len(ys), len(xs))
assert all(t.fill == D.SEL_LABEL for t in ys + xs)
p_hi = max(p for _t, p in L.get(tid)["pts"])
assert any(t.text() == w.axis_label(p_hi) for t in ys), [t.text() for t in ys]
assert all(":" in t.text() for t in xs), [t.text() for t in xs]      # intraday: date and time, like the crosshair
pax, bax = w.price.getAxis("right"), w._bottom_plot().getAxis("bottom")
assert pax.sel_band is not None and bax.sel_band is not None
# the band is painted on the price scale behind the numbers: #2962ff at 25 % over the background
img = QtGui.QImage(int(pax.width()), int(pax.height()), QtGui.QImage.Format.Format_ARGB32)
img.fill(w.glw.backgroundBrush().color())
qp = QtGui.QPainter(img)
pax.picture = None
pax.paint(qp, None, None)
qp.end()
mid_v = (pax.sel_band[0] + pax.sel_band[1]) / 2
ly = int(pax.mapFromScene(vb.mapViewToScene(QtCore.QPointF(0, mid_v))).y())
c = QtGui.QColor(img.pixel(3, ly))
bgc = w.glw.backgroundBrush().color()
want = [round(b + (f - b) * 64 / 255) for b, f in zip((bgc.red(), bgc.green(), bgc.blue()), (41, 98, 255))]
assert max(abs(a - b) for a, b in zip((c.red(), c.green(), c.blue()), want)) <= 2, ((c.red(), c.green(), c.blue()), want)
# deselect: no handles, no labels, no bands
L.selected = None
L.rebuild()
assert not handles(tid) and tags_shown() == ([], []) and pax.sel_band is None and bax.sel_band is None
# hovering a line does not show handles either (TradingView)
send(QtCore.QEvent.Type.MouseMove, px(w.time_to_x(L.get(tid)["pts"][0][0]) + 5, L.get(tid)["pts"][0][1]),
     QtCore.Qt.MouseButton.NoButton, QtCore.Qt.MouseButton.NoButton)
assert not handles(tid)
L.remove(tid)

# ------------------------------------------------------------------ horizontal / vertical line
hid = L.add("hline", [[T(n - 50), (ylo + yhi) / 2]])
L.selected = hid
L.rebuild()
(hx, hy) = (handles()[0].pos().x(), handles()[0].pos().y())
x0, x1 = vb.viewRange()[0]
assert abs(hx - (x0 + 0.9 * (x1 - x0))) < 1e-6 and abs(hy - (ylo + yhi) / 2) < 1e-6
ys, xs = tags_shown()
assert not xs and not ys                       # its own price label is the one on the scale (in the line colour)
vb.setXRange(x0 - 30, x1 - 30, padding=0)
app.processEvents()
QtTest.QTest.qWait(50)
nx0, nx1 = vb.viewRange()[0]
assert abs(handles()[0].pos().x() - (nx0 + 0.9 * (nx1 - nx0))) < 1e-6, "the handle keeps its place on screen"
vb.setXRange(x0, x1, padding=0)
app.processEvents()
# dragging that handle moves the line up / down only
before = list(L.get(hid)["pts"][0])
hx = handles()[0].pos().x()
a = px(hx, (ylo + yhi) / 2)
drag(a, a + QtCore.QPoint(0, -50))
after = L.get(hid)["pts"][0]
assert after[1] > before[1] and abs(after[0] - before[0]) < 1e-6, (before, after)
L.remove(hid)
vid = L.add("vline", [[T(n - 60), 0.0]])
L.selected = vid
L.rebuild()
y0, y1 = vb.viewRange()[1]
assert abs(handles()[0].pos().y() - (y1 - 0.9 * (y1 - y0))) < 1e-6
ys, xs = tags_shown()
assert len(xs) == 1 and not ys
L.remove(vid)

# ------------------------------------------------------------------ a box: 4 corners + 4 side middles
pa, pb = ylo + (yhi - ylo) * 0.3, ylo + (yhi - ylo) * 0.6
rid = L.add("rect", [[T(n - 100), pa], [T(n - 50), pb]])
L.selected = rid
L.rebuild()
hs = handles()
assert len(hs) == 8, len(hs)
pos = sorted((round(h.pos().x(), 4), round(h.pos().y(), 4)) for h in hs)
xm, ym = (n - 100 + n - 50) / 2, (pa + pb) / 2
for q in [(n - 100, pa), (n - 50, pb), (n - 100, pb), (n - 50, pa), (xm, pa), (xm, pb), (n - 100, ym), (n - 50, ym)]:
    assert any(abs(q[0] - p[0]) < 1e-3 and abs(q[1] - p[1]) < 1e-3 for p in pos), (q, pos)
# drag the top side's middle up: only the top price moves
before = [list(p) for p in L.get(rid)["pts"]]
a = px(xm, pb)
drag(a, a + QtCore.QPoint(0, -40))
after = L.get(rid)["pts"]
assert after[1][1] > before[1][1] and after[0] == before[0] and abs(after[1][0] - before[1][0]) < 1e-6, (before, after)
# drag the top-left corner (x of point 0, price of point 1) left and up
before = [list(p) for p in L.get(rid)["pts"]]
a = px(n - 100, before[1][1])
drag(a, a + QtCore.QPoint(-60, -30))
after = L.get(rid)["pts"]
assert after[0][0] < before[0][0] and after[1][1] > before[1][1], (before, after)
assert abs(after[0][1] - before[0][1]) < 1e-6 and abs(after[1][0] - before[1][0]) < 1e-6, (before, after)
ys, xs = tags_shown()
assert len(ys) == 2 and len(xs) == 2
L.remove(rid)

# ------------------------------------------------------------------ long position: entry, target, stop (left), width (right)
e = (ylo + yhi) / 2
lid = L.add("long", [[T(n - 90), e], [T(n - 40), e + (yhi - ylo) * 0.15]])
L.selected = lid
L.rebuild()
hs = handles()
assert len(hs) == 4, len(hs)
d = L.get(lid)
stop0 = d["pts"][2][1]
a = px(n - 90, stop0)                                   # the stop handle sits on the left side
drag(a, a + QtCore.QPoint(0, 30))
assert L.get(lid)["pts"][2][1] < stop0, "the stop handle moves the stop"
tgt0 = L.get(lid)["pts"][1][1]
a = px(n - 90, tgt0)
drag(a, a + QtCore.QPoint(0, -25))
assert L.get(lid)["pts"][1][1] > tgt0, "the target handle moves the target"
right0 = L.get(lid)["pts"][1][0]
a = px(n - 40, L.get(lid)["pts"][0][1])
drag(a, a + QtCore.QPoint(80, 0))
assert L.get(lid)["pts"][1][0] > right0 and L.get(lid)["pts"][2][0] == L.get(lid)["pts"][1][0]
ys, xs = tags_shown()
assert len(ys) == 3 and len(xs) == 2                    # entry, target and stop prices; start and end times
L.remove(lid)

# ------------------------------------------------------------------ parallel channel: 6 handles
c1, c2 = ylo + (yhi - ylo) * 0.3, ylo + (yhi - ylo) * 0.4
cid = L.add("channel", [[T(n - 100), c1], [T(n - 40), c2], [T(n - 100), c1 + (yhi - ylo) * 0.15]])
L.selected = cid
L.rebuild()
assert len(handles()) == 6
off0 = L._channel_offset(L._xy(L.get(cid)))
# the second line's middle handle sets the width; the first line stays
ln1 = [list(p) for p in L.get(cid)["pts"][:2]]
a = px(n - 70, (c1 + c2) / 2 + off0)
drag(a, a + QtCore.QPoint(0, -30))
off1 = L._channel_offset(L._xy(L.get(cid)))
assert off1 > off0 and L.get(cid)["pts"][:2] == ln1, (off0, off1)
# an end of the first line re-aims it and the channel keeps its width
a = px(n - 40, c2)
drag(a, a + QtCore.QPoint(0, 40))
off2 = L._channel_offset(L._xy(L.get(cid)))
assert L.get(cid)["pts"][1][1] < c2 and abs(off2 - off1) < 1e-6 * max(1.0, abs(off1)), (off1, off2)

# a locked drawing shows no handles
L.set_locked(cid, True)
L.selected = cid
L.rebuild()
assert not handles()
L.set_locked(cid, False)

# undo still works through all of it
assert L.undo()
w.grab().save(os.path.join(os.environ.get("SHOTS", "/tmp"), "handles.png"))
print("HANDLES OK")
