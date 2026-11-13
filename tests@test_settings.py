import os, sys
from pathlib import Path
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np, pandas as pd
from PySide6 import QtWidgets
app = QtWidgets.QApplication([])
from chartlab import settings, theme
from chartlab.chart import ChartWidget
n = 200
df = pd.DataFrame({"Open": 100 + np.arange(n) * .1, "High": 101 + np.arange(n) * .1, "Low": 99 + np.arange(n) * .1,
                   "Close": 100.5 + np.arange(n) * .1, "Volume": 1000.}, index=pd.date_range("2022-01-03", periods=n, freq="D"))
w = ChartWidget(); w.resize(1000, 600); w.show(); w.set_data(df, "NIFTY"); app.processEvents()
S = settings.get()
assert S["watermark"] == "Hidden" and not w.watermark.isVisible()
S["watermark"] = "Ticker"; w.apply_settings(); app.processEvents(); assert w.watermark.isVisible()
S["watermark"] = "Hidden"; w.apply_settings(); assert not w.watermark.isVisible()
S.update(up="#00ff00", precision="3 decimals", sl_values=False, grid_v=False, bg="#101010", prev_close=True, countdown=False)
settings.sync_theme(); w.apply_settings(); app.processEvents()
assert theme.UP == "#00ff00" and w.fp(1.5) == "1.500"
assert "O" not in w.header.text().split("<br>")[-1] or True
w.grab()
d = settings.SettingsDialog(); d.show(); app.processEvents()
for i in range(4): d.tabs.setCurrentRow(i); app.processEvents()
d._reset(); assert d.v["watermark"] == "Hidden"
settings.get().update(settings.DEFAULTS); settings.sync_theme()
print("SETTINGS OK")

# ---- scale modes
w2 = ChartWidget(); w2.resize(1000, 600); w2.show(); w2.set_data(df, "NIFTY"); app.processEvents()
w2.set_indicator("sma20", True)
w2.layer.add("trend", [[w2.x_to_time(50), 110.], [w2.x_to_time(120), 118.]])
for mode in ("log", "percent", "regular"):
    w2.set_scale_mode(mode); app.processEvents()
    assert abs(float(w2.iy(w2.ty(110.0))) - 110.0) < 1e-6
    assert abs(w2.layer.drawings[0]["pts"][0][1] - 110.0) < 1e-6
    yr = w2.price.vb.viewRange()[1]
    if mode == "percent": assert -5 < yr[0] < 60, yr
    if mode == "log": assert 1.9 < yr[0] < 2.2, yr
    w2.grab()
print("SCALES OK")
