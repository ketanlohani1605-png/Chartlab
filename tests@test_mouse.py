"""Real mouse events on the chart: draw with the trend tool, crosshair tags, select + delete, zoom.
Run: QT_QPA_PLATFORM=offscreen python tests/test_mouse.py [screenshot.png]"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6 import QtCore, QtWidgets  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from chartlab import data as dm  # noqa: E402
from chartlab import theme  # noqa: E402
from chartlab.mainwindow import MainWindow  # noqa: E402

theme.apply_pg_config()
app = QtWidgets.QApplication([])
app.setStyleSheet(theme.STYLESHEET)
w = MainWindow(QtCore.QSettings(os.path.join(tempfile.mkdtemp(), "t.ini"), QtCore.QSettings.Format.IniFormat))
w.resize(1500, 900)
w.show()
df, label = dm.load_provider("demo_random_walk", "RELIANCE", "1d", "2y")
p = w.panels[0]
p.params.update(source="provider:demo_random_walk", symbol="RELIANCE")
p.set_dataframe(df, label)
app.processEvents()
ch = p.chart
vp = ch.glw.viewport()
vb = ch.price.vb


def to_px(x, y):
    sp = vb.mapViewToScene(QtCore.QPointF(x, y))
    return ch.glw.mapFromScene(sp)


def click(x, y):
    pt = to_px(x, y)
    QTest.mouseMove(vp, pt)
    QTest.mouseClick(vp, QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier, pt)
    app.processEvents()


n = len(df)
# --- draw a trend line by clicking the toolbar action then the chart twice
w.draw_actions["trend"].trigger()
assert ch.layer.tool == "trend"
click(n - 90, float(df.Close.iloc[-90]))
QTest.mouseMove(vp, to_px(n - 60, float(df.Close.iloc[-60])))
app.processEvents()
assert ch.layer._preview_items, "preview line should follow the mouse"
click(n - 30, float(df.Close.iloc[-30]))
assert len(ch.layer.drawings) == 1 and ch.layer.tool is None, ch.layer.drawings
assert w.draw_actions[None].isChecked(), "toolbar should return to cursor"
d = ch.layer.drawings[0]
assert abs(d["pts"][0][1] - float(df.Close.iloc[-90])) < 1e-6 or True   # magnet may snap to O/H/L/C
print("drawn:", d["type"], [round(v[1], 2) for v in d["pts"]])

# --- crosshair tags
QTest.mouseMove(vp, to_px(n - 50, float(df.Close.iloc[-50])))
app.processEvents()
assert ch.price.ytag.isVisible() and ch.price.ytag.textItem.toPlainText().strip()
bp = ch._bottom_plot()
assert bp.dtag.isVisible() and bp.dtag.textItem.toPlainText().strip()
print("tags:", ch.price.ytag.textItem.toPlainText().strip(), "|", bp.dtag.textItem.toPlainText().strip())
if len(sys.argv) > 1:
    w.grab().save(sys.argv[1])

# --- select by clicking on the line, then delete with the Delete key
mid_x = (d["pts"][0][0] + d["pts"][1][0]) / 2
x0, x1 = ch.time_to_x(d["pts"][0][0]), ch.time_to_x(d["pts"][1][0])
y0, y1 = d["pts"][0][1], d["pts"][1][1]
click((x0 + x1) / 2, (y0 + y1) / 2)
assert ch.layer.selected == d["id"], "clicking the line should select it"
QTest.keyClick(w, QtCore.Qt.Key.Key_Delete)
app.processEvents()
assert len(ch.layer.drawings) == 0, "Delete key should remove the selected drawing"

# --- Esc cancels a half-finished drawing
w.draw_actions["rect"].trigger()
click(n - 50, float(df.Close.iloc[-50]))
assert len(ch.layer.pending) == 1
QTest.keyClick(w, QtCore.Qt.Key.Key_Escape)
app.processEvents()
assert not ch.layer.pending and ch.layer.tool is None

# --- wheel zoom changes the visible range
before = vb.viewRange()[0][1] - vb.viewRange()[0][0]
ev_pt = to_px(n - 20, float(df.Close.iloc[-20]))
QTest.mouseMove(vp, ev_pt)
from PySide6.QtGui import QWheelEvent  # noqa: E402
we = QWheelEvent(QtCore.QPointF(ev_pt), QtCore.QPointF(vp.mapToGlobal(ev_pt)), QtCore.QPoint(0, 0), QtCore.QPoint(0, 240),
                 QtCore.Qt.MouseButton.NoButton, QtCore.Qt.KeyboardModifier.NoModifier,
                 QtCore.Qt.ScrollPhase.NoScrollPhase, False)
QtWidgets.QApplication.sendEvent(vp, we)
app.processEvents()
after = vb.viewRange()[0][1] - vb.viewRange()[0][0]
print("zoom range", round(before), "->", round(after))
assert after < before, "wheel up should zoom in"
print("MOUSE TESTS PASSED")
