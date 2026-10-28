"""Replay: pick a start bar, step/play, no look-ahead for strategies, exit restores everything."""
import os, sys, time, tempfile
from pathlib import Path
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np, pandas as pd
from PySide6 import QtCore, QtTest, QtWidgets
app = QtWidgets.QApplication([])
from chartlab.mainwindow import MainWindow
w = MainWindow(QtCore.QSettings(tempfile.mktemp(suffix=".ini"), QtCore.QSettings.Format.IniFormat)); w.resize(1500, 900); w.show()
n = 500
c = 100 + np.cumsum(np.random.default_rng(5).normal(0, 1, n))
df = pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": 10.}, index=pd.date_range("2022-01-03", periods=n, freq="D"))
p = w.active; p.set_dataframe(df, "T"); app.processEvents()
ch = p.chart
w.toggle_replay(); app.processEvents()
assert ch.replay["picking"] and ch.replay_bar.isVisible()
# click on the chart at bar ~300 picks the start
vb = ch.price.vb
xs = ch.price.vb.viewRange()[0]; target = int(xs[0] + (xs[1] - xs[0]) * 0.4)
sp = vb.mapViewToScene(QtCore.QPointF(target, df.Close.iloc[target]))
pt = ch.glw.mapFromScene(sp)
QtTest.QTest.mouseMove(ch.glw.viewport(), pt); app.processEvents()
QtTest.QTest.mouseClick(ch.glw.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=pt); app.processEvents()
assert not ch.replay["picking"] and ch._limit == target + 1 or abs(ch._limit - (target + 1)) <= 1, (ch._limit, target)
k0 = ch._limit
ch.replay_step(3); assert ch._limit == k0 + 3
assert ch.candles.limit == ch._limit and ch.volbars.limit == ch._limit
# strategies see only revealed bars
w.editor.setPlainText("def strategy(df):\n    print('LEN', len(df))\n    return {'buy': df['Close'] > 0, 'sell': df['Close'] < 0}")
w.run_backtest(); app.processEvents()
assert f"LEN {ch._limit}" in w.log_view.toPlainText(), w.log_view.toPlainText()[-200:]
# play advances on a timer, then pause
ch.replay_set_speed(10.0); ch.replay_play(); t0 = time.time()
while time.time() - t0 < 0.6: app.processEvents(); time.sleep(0.01)
ch.replay_pause(); adv = ch._limit - (k0 + 3); assert adv >= 3, adv
# header shows the replay bar, not the future
assert f"{df.Close.iloc[ch._limit - 1]:,.2f}" in ch.header.text()
# exit -> full data again
w.toggle_replay(); app.processEvents()
assert ch._limit is None and not ch.replay["active"] and ch.candles.limit is None and not ch.replay_bar.isVisible()
print("REPLAY OK")
