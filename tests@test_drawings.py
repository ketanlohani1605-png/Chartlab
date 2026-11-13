"""Drawing editing: drag to move / resize through the real mouse path, undo/redo, measure & text tools."""
import os, sys
from pathlib import Path
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np, pandas as pd
from PySide6 import QtCore, QtTest, QtWidgets
app = QtWidgets.QApplication([])
from chartlab.chart import ChartWidget
n = 400
df = pd.DataFrame({"Open": 100 + np.arange(n) * .1, "High": 101 + np.arange(n) * .1, "Low": 99 + np.arange(n) * .1,
                   "Close": 100.5 + np.arange(n) * .1, "Volume": 1.}, index=pd.date_range("2022-01-03", periods=n, freq="D"))
w = ChartWidget(); w.resize(1200, 700); w.show(); w.set_data(df, "T"); app.processEvents()
w.magnet = False
L = w.layer
did = L.add("trend", [[w.x_to_time(n - 80), 125.], [w.x_to_time(n - 30), 135.]])
orig = [list(p) for p in L.drawings[0]["pts"]]
vb = w.price.vb
def px(x, y):
    sp = vb.mapViewToScene(QtCore.QPointF(x, y)); return w.glw.mapFromScene(sp)
vp = w.glw.viewport()
def _send(kind, p, buttons):
    from PySide6 import QtGui
    pf = QtCore.QPointF(p)
    ev = QtGui.QMouseEvent(kind, pf, pf, vp.mapToGlobal(p).toPointF() if hasattr(vp.mapToGlobal(p), "toPointF") else pf,
                           QtCore.Qt.MouseButton.LeftButton if kind != QtCore.QEvent.Type.MouseMove else QtCore.Qt.MouseButton.NoButton,
                           buttons, QtCore.Qt.KeyboardModifier.NoModifier)
    QtWidgets.QApplication.sendEvent(vp, ev)
    app.processEvents()

def drag(a, b):
    """A real press-move-release with the left button held on every move (QTest.mouseMove can drop it)."""
    QtTest.QTest.qWait(500)
    L_ = QtCore.Qt.MouseButton.LeftButton
    _send(QtCore.QEvent.Type.MouseMove, a, QtCore.Qt.MouseButton.NoButton)
    _send(QtCore.QEvent.Type.MouseButtonPress, a, L_)
    for i in range(1, 11):
        _send(QtCore.QEvent.Type.MouseMove, a + (b - a) * (i / 10), L_)
    _send(QtCore.QEvent.Type.MouseButtonRelease, b, QtCore.Qt.MouseButton.NoButton)
# drag the middle of the line -> whole line moves, view must NOT pan
x0 = vb.viewRange()[0]
mid = px(n - 55, 130.)
drag(mid, mid + QtCore.QPoint(60, 0))
assert vb.viewRange()[0] == x0, "view panned while dragging a drawing"
moved = L.drawings[0]["pts"]
assert moved[0][0] > orig[0][0] and abs(moved[0][1] - orig[0][1]) < 1.5, (moved, orig)
# drag an end handle (selected now) -> only that point changes
p2_before = [list(p) for p in L.drawings[0]["pts"]]
h = px(*w_xy) if (w_xy := (w.time_to_x(p2_before[1][0]), p2_before[1][1])) else None
drag(h, h + QtCore.QPoint(0, -40))
assert L.drawings[0]["pts"][0] == p2_before[0] and L.drawings[0]["pts"][1][1] > p2_before[1][1]
# undo twice -> original; redo -> moved again
assert L.undo() and L.undo()
assert all(abs(a - b) < 1e-6 for p, q in zip(L.drawings[0]["pts"], orig) for a, b in zip(p, q))
assert L.redo()
# empty space drag still pans
yr = vb.viewRange()[1]; empty = px(n - 150, yr[0] + (yr[1] - yr[0]) * 0.8)
a0 = vb.viewRange()[0]
drag(empty, empty + QtCore.QPoint(80, 0))
assert vb.viewRange()[0][0] < a0[0]
# measure tool
L.add("measure", [[w.x_to_time(300), 110.], [w.x_to_time(320), 120.]]); L.add("text", [[w.x_to_time(310), 112.]])
L.drawings[-1]["text"] = "hello"; L.rebuild()
assert len(L.to_json()) == 3 and L.to_json()[-1]["text"] == "hello"
L.clear(); assert L.undo() and len(L.drawings) == 3
print("DRAWING EDIT OK")
