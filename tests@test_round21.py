"""v0.21: crosshair follows a still mouse, data window, h-line price tags, position tool R/R, Indicators dialog,
editor autocomplete, settings combos, clean quit with a fetch in flight."""
import os, sys, subprocess, tempfile, textwrap
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, ".")
import numpy as np, pandas as pd
from PySide6 import QtWidgets, QtCore, QtGui, QtTest
from chartlab import theme
theme.apply_pg_config()
app = QtWidgets.QApplication([])
app.setStyleSheet(theme.STYLESHEET)
from chartlab.mainwindow import MainWindow, quit_cleanly


def pump(ms=40):
    QtTest.QTest.qWait(ms)


r = np.random.default_rng(1)
n = 300
c = 1000 * np.exp(np.cumsum(r.normal(0, 0.01, n)))
df = pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c * 1.002, "Volume": 1e6},
                  index=pd.bdate_range(end="2026-10-30", periods=n))
w = MainWindow(QtCore.QSettings(tempfile.mktemp(suffix=".ini"), QtCore.QSettings.Format.IniFormat))
w.resize(1500, 900)
w.show()
pump(100)
p = w.active
ch = p.chart
p.set_dataframe(df, "TEST")
pump(100)
vp = ch.glw.viewport()

# 1) crosshair: zoom under a still mouse -> the date label follows the bar now under the cursor
QtTest.QTest.mouseMove(vp, QtCore.QPoint(500, 300))
pump(50)
i0 = ch._hover_i
ch.price.vb.zoom_x(0.6, ch._n() - 1)          # zoom anchored at the right edge: a different bar is under the mouse
pump(50)
assert ch._hover_i != i0, "crosshair did not follow the chart under a still mouse"
bp = ch._bottom_plot()
assert bp.dtag.text().strip() == ch.df.index[ch._hover_i].strftime("%a %d %b '%y"), bp.dtag.text()

# 2) data window: full table incl. indicator values, opens on the current bar
w.ind_actions["sma20"].setChecked(True)
w.show_right_panel(w.data_dock)
pump(50)
html = w.data_lbl.text()
assert "width='100%'" in html and "SMA 20" in html and "Volume" in html, html[:200]

# 3) horizontal line shows its price on the scale; follows drag / undo / remove
lay = ch.layer
price = float(df["Close"].iloc[150])
hid = lay.add("hline", [[ch.x_to_time(150), price]], "#ffeb3b")
pump(30)
tag = lay._tags[hid]
assert tag.isVisible() and tag.text().strip() == ch.axis_price(price)
assert "color: #000000" in tag.styleSheet(), "light line colour needs dark text"
lay.selected = hid
x, y = lay._xy(next(d for d in lay.drawings if d["id"] == hid))[0]
lay.begin_drag(x, y); lay.drag_to(x, y * 1.02); lay.end_drag()
assert lay._tags[hid].text().strip() != ch.axis_price(price)
lay.undo()
assert lay._tags[hid].text().strip() == ch.axis_price(price)
lay.remove(hid)
assert hid not in lay._tags

# 4) position tool: independent stop, risk/reward follows it
lid = lay.add("long", [[ch.x_to_time(200), 100.0], [ch.x_to_time(220), 110.0]])
d = next(d for d in lay.drawings if d["id"] == lid)
assert len(d["pts"]) == 3 and abs(d["pts"][2][1] - 90.0) < 1e-9          # mirrored stop to start with
lay.selected = lid
lay.rebuild()
sx, sy = lay._xy(d)[2]
assert lay.begin_drag(sx, sy) and lay._drag["mode"] == ("pt", 2)
lay.drag_to(sx, ch.ty(95.0)); lay.end_drag()
assert abs(d["pts"][2][1] - 95.0) < 1e-6 and d["pts"][2][0] == d["pts"][1][0]
texts = [it.toPlainText() for it in lay._items[lid] if hasattr(it, "toPlainText")]
assert any("Risk/Reward 2.00" in t for t in texts), texts
lay.clear()

# 5) Indicators dialog: toggles and stays open; '/' opens it
seen = {}


def drive():
    dlg = app.activeModalWidget()
    seen["type"] = type(dlg).__name__
    dlg.edit.setText("bollinger")
    pump(30)
    dlg._go()
    seen["on"] = w.ind_actions["bb"].isChecked()
    seen["still_open"] = dlg.isVisible()
    dlg.reject()


QtCore.QTimer.singleShot(200, drive)
w.open_indicators()
assert seen == {"type": "IndicatorsDialog", "on": True, "still_open": True}, seen
assert "bb" in ch._enabled
QtCore.QTimer.singleShot(200, lambda: (seen.__setitem__("slash", type(app.activeModalWidget()).__name__),
                                       app.activeModalWidget().reject()))
vp.setFocus()
QtTest.QTest.keyClick(vp, QtCore.Qt.Key.Key_Slash)
pump(500)
assert seen.get("slash") == "IndicatorsDialog", seen

# 6) editor autocomplete
ed = w.editor
w.show_right_panel(w.strategy_dock)
ed.setPlainText("x = ")
cur = ed.textCursor(); cur.movePosition(QtGui.QTextCursor.MoveOperation.End); ed.setTextCursor(cur)
ed.setFocus()
QtTest.QTest.keyClicks(ed, "ta.cro")
pump(30)
assert ed._comp.popup().isVisible() and ed._comp.currentCompletion() == "crossover"
QtTest.QTest.keyClick(ed, QtCore.Qt.Key.Key_Return)
QtTest.QTest.keyClicks(ed, 'df["Hi')
QtTest.QTest.keyClick(ed, QtCore.Qt.Key.Key_Tab)
assert ed.toPlainText() == 'x = ta.crossover(df["High"]', repr(ed.toPlainText())
QtTest.QTest.keyClicks(ed, ", 1)")
QtTest.QTest.keyClick(ed, QtCore.Qt.Key.Key_Return)
QtTest.QTest.keyClicks(ed, "ret")
assert not ed._comp.popup().isVisible()                  # plain words only on demand
QtTest.QTest.keyClick(ed, QtCore.Qt.Key.Key_Space, QtCore.Qt.KeyboardModifier.ControlModifier)
assert ed._comp.popup().isVisible() and ed._comp.currentCompletion() == "return"
w._escape()                                               # the window's Esc closes the list first
assert not ed._comp.popup().isVisible()

# 7) settings: date format combo shows an example, stores the pattern
from chartlab import settings as cs
dlg = cs.SettingsDialog(w, 2)
combo = dlg._ctl["date_format"]
combo.setCurrentIndex(combo.findData("%Y-%m-%d"))
assert dlg.v["date_format"] == "%Y-%m-%d" and "%" not in combo.currentText()
dlg.reject()

# 8) quitting while a fetch is still running ends cleanly (no 'quit unexpectedly' abort)
code = textwrap.dedent("""
    import os, sys, time
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, ".")
    from PySide6 import QtWidgets, QtCore
    app = QtWidgets.QApplication([])
    import tempfile
    from chartlab.mainwindow import MainWindow, quit_cleanly
    from chartlab.panel import LoadWorker
    w = MainWindow(QtCore.QSettings(tempfile.mktemp(suffix=".ini"), QtCore.QSettings.Format.IniFormat))
    t = LoadWorker(lambda: (time.sleep(10), None)[1], w.active); t.start()
    QtCore.QTimer.singleShot(100, app.quit)
    app.exec()
    quit_cleanly(w, 0)
""")
rc = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60).returncode
assert rc == 0, rc

quit_cleanly(w, 0, exit_process=False)
print("ROUND 21 OK")
os._exit(0)
