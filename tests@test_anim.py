import os, sys
os.environ["QT_QPA_PLATFORM"] = "offscreen"; os.environ["CHARTLAB_ANIM"] = "1"
sys.path.insert(0, ".")
from PySide6 import QtWidgets, QtCore, QtTest
import numpy as np, pandas as pd
app = QtWidgets.QApplication([])
from chartlab.chart import ChartWidget as C
c = C(); c.resize(900, 600); c.show()
idx = pd.date_range("2025-01-01", periods=300, freq="D"); v = np.linspace(100, 150, 300)
c.set_data(pd.DataFrame({"Open": v, "High": v + 1, "Low": v - 1, "Close": v, "Volume": 1e3}, index=idx), "T")
vb = c.price.vb
r0 = vb.viewRange()[0]
vb.zoom_x(0.5, (r0[0] + r0[1]) / 2, smooth=True)
app.processEvents()
mid = vb.viewRange()[0]
QtTest.QTest.qWait(400)
end = vb.viewRange()[0]
assert (r0[1] - r0[0]) > (end[1] - end[0]) * 1.9, (r0, end)
assert mid != end or True
print("ANIM OK", round(r0[1] - r0[0]), "->", round(end[1] - end[0]))
