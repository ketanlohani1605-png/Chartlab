"""Script mode end-to-end: run a user's standalone script in a subprocess through the real main window."""
import os, sys, tempfile
from pathlib import Path
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np, pandas as pd
from PySide6 import QtCore, QtWidgets
app = QtWidgets.QApplication([])
from chartlab.mainwindow import MainWindow
st = QtCore.QSettings(tempfile.mktemp(suffix=".ini"), QtCore.QSettings.Format.IniFormat)
w = MainWindow(st); w.show()
idx = pd.date_range("2024-01-01 09:15", periods=400, freq="min")
c = 100 + np.cumsum(np.random.default_rng(3).normal(0, .1, 400))
w.active.set_dataframe(pd.DataFrame({"Open": c, "High": c + .1, "Low": c - .1, "Close": c, "Volume": 1.0}, index=idx), "T")
w.editor.setPlainText('''
import pandas as pd, chartlab
df = chartlab.data()
print("hello from user script", len(df))
trades = pd.DataFrame({"entry_time": [df.index[5]], "exit_time": [df.index[60]], "entry_price": [df.Close.iloc[5]],
                       "exit_price": [df.Close.iloc[60]], "pnl": [1234.5]})
''')
w.run_backtest()
import time
t0 = time.time()
while time.time() - t0 < 60 and w.active.result is None:
    app.processEvents(); time.sleep(0.05)
assert w.active.result is not None, w.log_view.toPlainText()
assert w.active.result.metrics["trades"] == 1 and abs(w.active.result.metrics["net_profit"] - 1234.5) < 1e-6
assert "hello from user script" in w.log_view.toPlainText()
# failing script shows traceback, keeps app alive
w.active.result = None
w.editor.setPlainText("raise RuntimeError('kaboom')")
w.run_backtest()
t0 = time.time()
while time.time() - t0 < 30 and "kaboom" not in w.log_view.toPlainText():
    app.processEvents(); time.sleep(0.05)
assert "kaboom" in w.log_view.toPlainText()
print("SCRIPT MODE OK")
