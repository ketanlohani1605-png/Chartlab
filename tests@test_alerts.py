import os, sys, datetime as dt
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, ".")
from PySide6 import QtWidgets, QtCore
from chartlab.alerts import AlertManager
app = QtWidgets.QApplication([])
m = AlertManager()
fired = []
m.triggered.connect(lambda a, p: fired.append(a["id"]))
t0 = dt.datetime(2026, 1, 1, 10)
# cross_up from below: no fire until price reaches level
a = m.add("X", "X", "cross_up", 100, 95)
assert not m.check("X", 97, now=t0)
assert m.check("X", 101, 99, 101, now=t0) and a["status"] == "triggered"
# created above level with cross_up: must first dip below, no retroactive fire
b = m.add("X", "X", "cross_up", 100, 105)
assert not m.check("X", 110, now=t0)
assert not m.check("X", 99, now=t0)
assert m.check("X", 101, now=t0)
# recurring cross with cooldown
c = m.add("Y", "Y", "cross", 50, 55, once=False)
assert m.check("Y", 49, 49, 56, now=t0)
assert not m.check("Y", 51, now=t0 + dt.timedelta(seconds=10))     # cooldown
assert m.check("Y", 51, 49, 51, now=t0 + dt.timedelta(minutes=5)) and c["status"] == "active"
# gt / lt, pause, expiry
d = m.add("Z", "Z", "gt", 10, 5); m.set_status(d["id"], "paused")
assert not m.check("Z", 20, now=t0)
m.set_status(d["id"], "active")
e = m.add("W", "W", "lt", 1, 5, expires="2025-01-01T00:00:00")
assert not m.check("W", 0.5, now=t0) and e["status"] == "expired"
assert m.check("Z", 20, now=t0)
# persistence
s = QtCore.QSettings("ChartLabTest", "alerts"); s.clear()
m2 = AlertManager(s); m2.add("Q", "Q", "gt", 1, 0); s.sync()
assert len(AlertManager(s).alerts) == 1; s.clear()
# UI integration
from chartlab.mainwindow import MainWindow
import pandas as pd, numpy as np
w = MainWindow(QtCore.QSettings("ChartLabTest", "win")); w.alerts.alerts.clear(); w.show()
idx = pd.date_range("2025-01-01", periods=200, freq="D"); c_ = np.linspace(100, 120, 200)
df = pd.DataFrame({"Open": c_, "High": c_ + 1, "Low": c_ - 1, "Close": c_, "Volume": 1e3}, index=idx)
p = w.active; p.set_dataframe(df, "TEST")
a = w.alerts.add(p.symbol_key, "TEST", "cross_up", 130, 120)
assert len(p.chart._alert_lines) == 1
w.alerts.check(p.symbol_key, 131, 119, 131)
app.processEvents()
assert a["status"] == "triggered" and w.alerts_panel.table.rowCount() == 1
print("ALERTS OK")
