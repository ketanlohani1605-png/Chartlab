import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, ".")
from PySide6 import QtWidgets, QtCore, QtTest
import numpy as np, pandas as pd, datetime as dt
app = QtWidgets.QApplication([])
from chartlab.mainwindow import MainWindow
w = MainWindow(QtCore.QSettings("ChartLabTest", "lay")); w.resize(1500, 900); w.show()
# countdown: 5-minute bars, last bar started 2 minutes ago
now = dt.datetime.now().replace(second=0, microsecond=0)
idx = pd.date_range(end=now - dt.timedelta(minutes=2), periods=200, freq="5min")
c = np.linspace(100, 110, 200)
df = pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": 1e3}, index=idx)
w.active.set_dataframe(df, "T"); app.processEvents()
ch = w.active.chart
assert abs(ch.bar_seconds() - 300) < 1
ch._update_countdown(); assert ch.cd_tag.isVisible(), "countdown should show"
txt = ch.cd_tag.toPlainText().strip(); assert txt.count(":") == 1 and txt.startswith("0"), txt
# old data -> no countdown
w.active.set_dataframe(df.set_axis(df.index - pd.Timedelta(days=3)), "T"); ch._update_countdown()
assert not ch.cd_tag.isVisible()
# 4-chart layout: drag a divider, sizes follow, and survive a layout switch
w.set_layout("4"); app.processEvents()
root = w.split_root
assert isinstance(root, QtWidgets.QSplitter) and root.count() == 2
tot = sum(root.sizes()); root.setSizes([int(tot * .7), int(tot * .3)]); app.processEvents()
assert w.panels[0].height() > w.panels[2].height() * 1.8
row = root.widget(0); row.setSizes([int(sum(row.sizes()) * .25), int(sum(row.sizes()) * .75)]); app.processEvents()
assert w.panels[0].width() < w.panels[1].width()
w.set_layout("1"); w.set_layout("4"); app.processEvents()
assert w.panels[0].height() > w.panels[2].height() * 1.8, "sizes should be remembered"
w.set_layout("2h"); app.processEvents(); assert w.split_root.orientation() == QtCore.Qt.Orientation.Horizontal
print("LAYOUT+TIMER OK")
