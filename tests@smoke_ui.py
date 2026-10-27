"""Headless UI smoke test: builds the real window, loads demo data, runs a backtest, saves a screenshot.
Run:  QT_QPA_PLATFORM=offscreen python tests/smoke_ui.py out.png
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6 import QtWidgets  # noqa: E402

from chartlab import data as dm  # noqa: E402
from chartlab import theme  # noqa: E402
from chartlab.mainwindow import MainWindow  # noqa: E402

out = sys.argv[1] if len(sys.argv) > 1 else "smoke.png"
theme.apply_pg_config()
app = QtWidgets.QApplication([])
app.setStyleSheet(theme.STYLESHEET)
w = MainWindow()
w.resize(1560, 940)
w.show()

df, label = dm.load_provider("demo_random_walk", "RELIANCE", "1d", "2y")
w._on_loaded(df, label)
for key in ("sma50", "bb", "rsi"):
    w.chart.set_indicator(key, True)
w.run_backtest()
app.processEvents()
assert w.result is not None and w.summary.rowCount() == 7
assert w.trades_tbl.rowCount() == len(w.result.trades)
w.grab().save(out)

# intraday + indicator toggle off + reload must not crash
df2, label2 = dm.load_provider("demo_random_walk", "TCS", "15m", "1mo")
w._on_loaded(df2, label2)
w.chart.set_indicator("rsi", False)
w.run_backtest()
app.processEvents()
print("UI SMOKE OK", len(df), len(df2), "trades:", len(w.result.trades))
