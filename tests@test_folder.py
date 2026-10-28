import os, sys, tempfile, time
from pathlib import Path
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np, pandas as pd
from PySide6 import QtCore, QtWidgets
app = QtWidgets.QApplication([])
from chartlab.mainwindow import MainWindow
tmp = tempfile.mkdtemp(); rng = np.random.default_rng(2)
for k, day in enumerate(pd.bdate_range("2024-03-04", periods=4)):
    t = pd.date_range(day + pd.Timedelta(hours=9, minutes=15), periods=375, freq="min")
    c = 100 + np.cumsum(rng.normal(0, .05, 375))
    pd.DataFrame({"Datetime": t, "Open": c, "High": c + .1, "Low": c - .1, "Close": c, "Volume": 10.}).to_parquet(f"{tmp}/{day:%Y%m%d}.parquet")
w = MainWindow(QtCore.QSettings(tempfile.mktemp(suffix=".ini"), QtCore.QSettings.Format.IniFormat)); w.show()
def wait(cond, s=30):
    t0 = time.time()
    while time.time() - t0 < s and not cond():
        app.processEvents(); time.sleep(.02)
w.load_into(w.active, "file", "days", "auto", "max", tmp); wait(lambda: not w.active.loading and w.active.df is not None)
assert len(w.active.df) == 1500, len(w.active.df)
w._interval_clicked("5m"); wait(lambda: w.active.loading); wait(lambda: not w.active.loading)
assert len(w.active.df) == 300 and w.active.df.index[0].strftime("%H:%M") == "09:15", len(w.active.df)
print("FOLDER + TIMEFRAME OK", w.active.label)
