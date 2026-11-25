"""Moving the chart stays smooth:

- one paint per frame: after a pan every view's transform is in place (it was painted twice with indicator panes)
- while the chart moves, indicator lines take the quick 1-pixel pen and the legend follows every 50 ms; when it
  rests the lines get their own pens back (exactly) and the legend catches up
- a crosshair move restrokes only the bits of each line it uncovered - pixel for pixel what a full repaint draws
- the dotted grid drawn with one-pixel lines side by side: the same pixels as the 2-pixel dashed pen
- the time scale's label choices are reused while panning: the same labels as choosing again
- trackpad: gathered and applied once per frame; one direction per swipe (a sideways swipe never zooms, its drift is
  ignored); the glide after an up / down swipe is dropped; the glide after a sideways swipe follows the setting
- Settings -> Navigation: wheel pans / zooms, zoom point, speed, direction, two fingers up / down, pinch, glide
- every list and table scrolls by the pixel
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["CHARTLAB_OFFLINE"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["HOME"] = tempfile.mkdtemp()

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pyqtgraph as pg  # noqa: E402
from PySide6 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from chartlab import theme  # noqa: E402

theme.apply_pg_config()
app.setStyle(theme.make_style())
app.setStyleSheet(theme.STYLESHEET)
from chartlab import fastpaint, gestures, settings as cs  # noqa: E402
from chartlab.mainwindow import MainWindow  # noqa: E402

Ph = QtCore.Qt.ScrollPhase


def pump(ms=40):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents()
        time.sleep(0.003)


win = MainWindow(QtCore.QSettings(tempfile.mktemp(suffix=".ini"), QtCore.QSettings.Format.IniFormat))
win.resize(1440, 860)
win.show()
pump(150)
r = np.random.default_rng(5)
n = 3000
c = 1300 * np.exp(np.cumsum(r.normal(0, 0.004, n)))
o = np.r_[c[0], c[:-1]]
df = pd.DataFrame({"Open": o, "High": np.maximum(o, c) * 1.002, "Low": np.minimum(o, c) * 0.998, "Close": c,
                   "Volume": r.integers(5e3, 3e5, n).astype(float)},
                  index=pd.date_range(end="2026-11-20 15:25", periods=n, freq="5min"))
p = win.active
ch = p.chart
p.params.update(symbol="RELIANCE", source="yahoo", interval="5m")
p.set_dataframe(df, "RELIANCE.NS · 5m")
pump(150)
for k in ("ema", "rsi", "macd", "bb"):
    ch.set_indicator(k, True)
pump(250)
vb = ch.price.vb
vp = ch.glw.viewport()
paints = [0]


class Count(QtCore.QObject):
    def eventFilter(self, o, e):
        if e.type() == QtCore.QEvent.Type.Paint:
            paints[0] += 1
        return False


counter = Count()
vp.installEventFilter(counter)

# ---------------------------------------------------------------- one paint per frame
paints[0] = 0
for k in range(12):
    vb.pan_x(1.5 if k % 2 else -1.5)
    app.processEvents()
assert paints[0] == 12, f"{paints[0]} paints for 12 moves (each move was painted twice with indicator panes)"
for pl in ch._plots():
    assert not pl.vb._matrixNeedsUpdate, "every view's transform is in place before the paint"

# ---------------------------------------------------------------- quick pens while moving, own pens at rest
ch._motion_end()                                  # (the pans above: rest first)
lines = [it for it in ch._line_items() if isinstance(it, pg.PlotDataItem)]
own = {id(it): QtGui.QPen(it.opts["pen"]) for it in lines}
assert any(pen.widthF() > 1.0 for pen in own.values())
ch._motion_end()
vb.pan_x(2.0)
assert ch._drafting and all(it.opts["pen"].widthF() <= 1.0 for it in lines), "moving: the quick 1-pixel pens"
pump(ch.MOTION_MS + 150)
assert not ch._drafting, "rests after MOTION_MS"
for it in lines:
    pen = it.opts["pen"]
    assert pen == own[id(it)] and pen.widthF() == own[id(it)].widthF(), "its own pen back, exactly"
assert all(it.curve.opts["pen"].widthF() == own[id(it)].widthF() for it in lines)
# a thick line keeps its width while moving (drawn plainly, not anti-aliased)
thick = lines[0]
thick.setPen(pg.mkPen("#ff9800", width=2.5))
ch._motion()
assert thick.opts["pen"].widthF() == 2.5 and thick.opts["antialias"] is False
ch._motion_end()
assert thick.opts["pen"].widthF() == 2.5 and thick.opts["antialias"] is not False
assert thick.curve.opts["segmentedLineMode"] == "auto"
thick.setPen(own[id(thick)])
# the legend follows at most every 50 ms while moving
ch._cross_vp = QtCore.QPoint(300, 200)
calls = []
real_mm = ch._mouse_moved
ch._mouse_moved = lambda *a, **k: calls.append(1)
try:
    ch._motion()
    ch._xh_at = 0.0
    for k in range(10):
        ch._cross_key = None
        ch._refresh_crosshair()
    assert len(calls) == 1, calls
    ch._motion_end()
    assert len(calls) == 2, "at rest: the legend catches up"
finally:
    ch._mouse_moved = real_mm

# ---------------------------------------------------------------- partial repaints: the same pixels as a full one
for pl in ch._plots():
    pl.vline.hide()
    pl.hline.hide()
pump(80)
W, H = vp.width(), vp.height()


def grab():
    img = app.primaryScreen().grabWindow(win.winId()).toImage().convertToFormat(QtGui.QImage.Format.Format_ARGB32)
    a = np.frombuffer(img.constBits(), np.uint8).reshape(img.height(), img.width(), 4).copy()
    g = vp.mapTo(win, QtCore.QPoint(0, 0))
    dpr = vp.devicePixelRatioF()
    y0, x0 = int(g.y() * dpr), int(g.x() * dpr)
    return a[y0 + int(80 * dpr):y0 + int(H * dpr), x0:x0 + int(W * dpr)]      # (below the legends)


rng = np.random.default_rng(2)
used = 0
for trial in range(8):
    x = int(rng.integers(40, W - 120))
    y = int(rng.integers(100, H - 60))
    reg = QtGui.QRegion(x, 0, 3, H).united(QtGui.QRegion(0, y, W, 3))
    if trial % 2:
        reg = reg.united(QtGui.QRegion(10, y - 40, 500, 20)).united(QtGui.QRegion(x + 30, 0, 2, H))
    before = fastpaint.stats["fast"]
    vp.repaint(reg)
    pump(10)
    used += fastpaint.stats["fast"] - before
    part = grab()
    vp.repaint()
    pump(10)
    full = grab()
    diff = int(np.count_nonzero(np.any(part != full, axis=2)))
    assert diff == 0, f"trial {trial}: {diff} pixels differ from a full repaint"
assert used >= 8, "the quick strokes were used"
# a full repaint goes the usual way
before = dict(fastpaint.stats)
vp.repaint()
pump(10)
assert fastpaint.stats["full"] > before["full"]

# ---------------------------------------------------------------- the grid: one-pixel lines side by side
dpr = 2
img_a = QtGui.QImage(400 * dpr, 200 * dpr, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
img_a.setDevicePixelRatio(dpr)
img_b = QtGui.QImage(img_a)
for img in (img_a, img_b):
    img.fill(QtGui.QColor("black"))
pen = QtGui.QPen(QtGui.QColor("#303030"))
pen.setCosmetic(True)
pen.setWidthF(dpr)
pen.setCapStyle(QtCore.Qt.PenCapStyle.FlatCap)
pen.setDashPattern([1.0, 3.0])
pa = QtGui.QPainter(img_a)
pa.setPen(pen)
pa.drawLines([QtCore.QLineF(0, 50.5, 400, 50.5), QtCore.QLineF(120.5, 0, 120.5, 200)])
pa.end()


class _Fake:
    class chart:
        class glw:
            @staticmethod
            def devicePixelRatioF():
                return 2.0


fake = _Fake()
gp, _S = gestures.TVViewBox._grid_pen(fake)
offs = fake._gp_offs
assert gp.widthF() == 1.0 and len(offs) == 2
pb = QtGui.QPainter(img_b)
pb.setPen(gp)
pb.drawLines([QtCore.QLineF(0, 50.5 + o, 400, 50.5 + o) for o in offs] +
             [QtCore.QLineF(120.5 + o, 0, 120.5 + o, 200) for o in offs])
pb.end()
assert img_a == img_b, "the same dots"

# ---------------------------------------------------------------- the time scale reuses its label choices
ax = ch._bottom_plot().getAxis("bottom")
for zoom in (80, 300):
    for start in range(n - zoom - 5, n - zoom - 1500, -53):
        a = ax.tickValues(float(start), float(start + zoom), 1000)
        ax._tv_cache = None
        assert a == ax.tickValues(float(start), float(start + zoom), 1000), (zoom, start)

# ---------------------------------------------------------------- trackpad
S = cs.get()


def tp(dx, dy, phase=Ph.ScrollUpdate, pos=None):
    pos = pos or QtCore.QPoint(W // 2, H // 3)
    ev = QtGui.QWheelEvent(QtCore.QPointF(pos), QtCore.QPointF(vp.mapToGlobal(pos)), QtCore.QPoint(dx, dy),
                           QtCore.QPoint(dx * 2, dy * 2), QtCore.Qt.MouseButton.NoButton,
                           QtCore.Qt.KeyboardModifier.NoModifier, phase, False)
    QtWidgets.QApplication.sendEvent(vp, ev)


def span():
    x0, x1 = vb.viewRange()[0]
    return x0, x1, x1 - x0


ch.go_latest()
vb.zoom_x(0.5)
pump(30)
# gathered: many events, one range change
changes = []
vb.sigXRangeChanged.connect(lambda *_: changes.append(1))
x0, x1, w0 = span()
tp(0, 0, Ph.ScrollBegin)
for _ in range(10):
    tp(-12, 1)                                   # sideways, the fingers drifting down a little
assert not changes, "nothing applied inside one burst of events"
pump(30)
a0, a1, w1 = span()
assert len(changes) == 1, f"{len(changes)} range changes for one burst"
assert abs(w1 - w0) < 1e-9, "a sideways swipe never zooms (its drift is ignored)"
assert abs((a0 - x0) - 120 * w0 / vb.width()) < 1e-6, "1:1 with the fingers (all 10 events)"
# its glide follows; drift during the glide still does not zoom
tp(0, 0, Ph.ScrollEnd)
tp(-6, 4, Ph.ScrollMomentum)
pump(30)
b0, b1, w2 = span()
assert abs(w2 - w1) < 1e-9 and b0 > a0, "the glide of a sideways swipe moves on"
S["momentum"] = False
tp(-6, 0, Ph.ScrollMomentum)
pump(30)
assert span()[0] == b0, "glide off: it stops when the fingers lift"
S["momentum"] = True
# a new swipe, up / down: zoom; its glide is dropped
tp(0, 0, Ph.ScrollBegin)
tp(1, 30)
tp(-1, 30)
pump(30)
z0, z1, wz = span()
assert wz < w2, "up / down zooms"
tp(0, 0, Ph.ScrollEnd)
for _ in range(5):
    tp(0, 20, Ph.ScrollMomentum)
pump(30)
assert abs(span()[2] - wz) < 1e-9, "no zoom after the fingers lift"
# the first pixels decide: tiny movements wait
tp(0, 0, Ph.ScrollBegin)
tp(1, 1)
pump(30)
assert abs(span()[2] - wz) < 1e-9 and ch._tp["axis"] is None
tp(0, 10)
pump(30)
assert ch._tp["axis"] == "y" and span()[2] < wz
# a quick second swipe the other way: decided again at its ScrollBegin
tp(0, 0, Ph.ScrollBegin)
w3 = span()[2]
tp(-20, 2)
pump(30)
assert ch._tp["axis"] == "x" and abs(span()[2] - w3) < 1e-9
# settings: up / down moves the chart, or nothing
S["trackpad_v"] = "move"
tp(0, 0, Ph.ScrollBegin)
y0 = vb.viewRange()[1][0]
w4 = span()[2]
tp(0, 25)
pump(30)
assert abs(span()[2] - w4) < 1e-9 and vb.viewRange()[1][0] > y0 and not ch.auto_y, "moves up / down"
ch.set_auto_y(True)
S["trackpad_v"] = "off"
tp(0, 0, Ph.ScrollBegin)
before = vb.viewRange()
tp(0, 25)
pump(30)
assert vb.viewRange() == before
S["trackpad_v"] = "zoom"
# zoom direction and speed
tp(0, 0, Ph.ScrollBegin)
w5 = span()[2]
tp(0, 24)
pump(30)
k_norm = span()[2] / w5
S["zoom_invert"] = True
tp(0, 0, Ph.ScrollBegin)
w6 = span()[2]
tp(0, 24)
pump(30)
assert span()[2] > w6, "reversed: the same swipe zooms out"
S["zoom_invert"] = False
S["zoom_speed"] = "fast"
tp(0, 0, Ph.ScrollBegin)
w7 = span()[2]
tp(0, 24)
pump(30)
assert span()[2] / w7 < k_norm, "fast zooms further"
S["zoom_speed"] = "normal"
# pinch, and pinch off
ch.go_latest()
pump(30)


def pinch(v):
    pos = QtCore.QPointF(W / 2, H / 3)
    ev = QtGui.QNativeGestureEvent(QtCore.Qt.NativeGestureType.ZoomNativeGesture, QtGui.QPointingDevice.primaryPointingDevice(),
                                   2, pos, pos, vp.mapToGlobal(pos), v, QtCore.QPointF(0, 0))
    QtWidgets.QApplication.sendEvent(vp, ev)


w8 = span()[2]
pinch(0.2)
pinch(0.2)
pump(30)
assert span()[2] < w8, "pinch out zooms in"
S["pinch"] = False
w9 = span()[2]
pinch(0.3)
pump(30)
assert abs(span()[2] - w9) < 1e-9
S["pinch"] = True

# ---------------------------------------------------------------- mouse wheel settings
vp.setFocus()


def wheel(dy, mods=QtCore.Qt.KeyboardModifier.NoModifier, pos=None):
    pos = pos or QtCore.QPoint(W // 2, H // 3)
    ev = QtGui.QWheelEvent(QtCore.QPointF(pos), QtCore.QPointF(vp.mapToGlobal(pos)), QtCore.QPoint(0, 0),
                           QtCore.QPoint(0, dy), QtCore.Qt.MouseButton.NoButton, mods, Ph.NoScrollPhase, False)
    QtWidgets.QApplication.sendEvent(vp, ev)
    pump(30)


ch.go_latest()
pump(30)
S["wheel"] = "pan"
x0, x1, w0 = span()
wheel(120)
a0, a1, w1 = span()
assert abs(w1 - w0) < 1e-9 and a0 < x0, "the wheel scrolls through time"
wheel(120, QtCore.Qt.KeyboardModifier.ControlModifier)
assert span()[2] < w1, "Ctrl+wheel still zooms"
S["wheel"] = "zoom"
S["zoom_anchor"] = "cursor"
pos = QtCore.QPoint(W // 3, H // 3)
under = vb.mapSceneToView(ch.glw.mapToScene(pos)).x()
wheel(120, pos=pos)
assert abs(vb.mapSceneToView(ch.glw.mapToScene(pos)).x() - under) < 0.6, "zooms around the bar under the mouse"
S["zoom_anchor"] = "latest"
x0, x1, w0 = span()
wheel(120)
a0, a1, w1 = span()
assert w1 < w0 and abs(a1 - x1) < 0.6, "TradingView: the latest bar stays in place"
# smooth off: a wheel notch lands at once even with animations on
gestures.ANIMATE = True
S["smooth"] = False
try:
    w0 = span()[2]
    vb.zoom_x(0.5, smooth=True)
    assert span()[2] < w0 * 0.6 and vb._anim is None
finally:
    gestures.ANIMATE = False
    S["smooth"] = True

# ---------------------------------------------------------------- the Navigation page
d = cs.SettingsDialog(win, cs.SettingsDialog.NAV_TAB)
d.show()
pump(60)
assert d.tabs.currentItem().text().strip() == "Navigation"
assert cs.SettingsDialog.UPDATES_TAB == 5 and cs.SettingsDialog.DATA_TAB == 6
for key in ("wheel", "zoom_anchor", "zoom_speed", "zoom_invert", "trackpad_v", "momentum", "pinch", "smooth",
            "kinetic"):
    assert key in d._ctl and key in cs.DEFAULTS, key
d._ctl["wheel"].setCurrentIndex(d._ctl["wheel"].findData("pan"))
d._ctl["kinetic"].setChecked(False)
d._ok()
assert cs.get()["wheel"] == "pan" and cs.get()["kinetic"] is False
cs.get().update(wheel="zoom", kinetic=True)

# ---------------------------------------------------------------- lists scroll by the pixel
for view in win.findChildren(QtWidgets.QAbstractItemView):
    if view.isVisible():
        assert view.verticalScrollMode() == QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel, view

print("SMOOTH OK")
sys.stdout.flush()
os._exit(0)
