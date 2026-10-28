"""Headless UI smoke test: real window, demo data, indicators, drawings, 4-chart layout, backtest, screenshots.
Run:  QT_QPA_PLATFORM=offscreen python tests/smoke_ui.py out_prefix
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6 import QtCore, QtWidgets  # noqa: E402

from chartlab import data as dm  # noqa: E402
from chartlab import theme  # noqa: E402
from chartlab.mainwindow import MainWindow  # noqa: E402

prefix = sys.argv[1] if len(sys.argv) > 1 else "smoke"
theme.apply_pg_config()
app = QtWidgets.QApplication([])
app.setStyleSheet(theme.STYLESHEET)
settings = QtCore.QSettings(os.path.join(tempfile.mkdtemp(), "t.ini"), QtCore.QSettings.Format.IniFormat)
w = MainWindow(settings)
w.resize(1680, 980)
w.show()
app.processEvents()

p = w.panels[0]
df, label = dm.load_provider("demo_random_walk", "RELIANCE", "1d", "2y")
p.params.update(source="provider:demo_random_walk", symbol="RELIANCE")
p.set_dataframe(df, label)
for key in ("sma50", "bb", "rsi"):
    p.chart.set_indicator(key, True)

# drawings via the layer (same code path as clicks)
lay = p.chart.layer
last_t = float(p.chart._times[-1])
t0 = float(p.chart._times[-80])
lay.add("trend", [[t0, float(df.Close.iloc[-80])], [last_t, float(df.Close.iloc[-1])]], "#2962ff")
lay.add("hline", [[last_t, float(df.Close.iloc[-40])]], "#f5a623")
lay.add("rect", [[float(p.chart._times[-60]), float(df.High.iloc[-60:].max())],
                 [float(p.chart._times[-30]), float(df.Low.iloc[-60:].min())]], "#e91e63")
lay.add("fib", [[float(p.chart._times[-120]), float(df.High.iloc[-120:].max())],
                [float(p.chart._times[-70]), float(df.Low.iloc[-120:-70].min())]], "#9c27b0")
assert len(lay.drawings) == 4

# click-based drawing + select + delete through real handlers
lay.set_tool("trend")
lay.handle_click(100.0, float(df.Close.iloc[100]))
lay.handle_move(120.0, float(df.Close.iloc[120]))
lay.handle_click(140.0, float(df.Close.iloc[140]))
assert len(lay.drawings) == 5 and lay.tool is None
lay.handle_click(120.0, float(df.Close.iloc[120]))  # select it (approximately on the line)
lay.selected = lay.drawings[-1]["id"]
lay.delete_selected()
assert len(lay.drawings) == 4

# persistence round trip
w._save_drawings(p)
lay.clear_silent()
w._load_drawings(p)
assert len(lay.drawings) == 4

w.run_backtest()
app.processEvents()
assert p.result is not None and w.summary.rowCount() == 7
assert w.trades_tbl.rowCount() == len(p.result.trades)
w.grab().save(prefix + "_1chart.png")

for style in ("hollow", "line", "area", "candles"):
    p.chart.set_style(style)
    app.processEvents()
p.chart.set_style("hollow")
app.processEvents()
w.grab().save(prefix + "_hollow.png")
p.chart.set_style("candles")

# 4-chart layout auto-fills the extra charts (synchronously here: no network)
w.set_layout("4")
syms = ["NIFTY", "TCS", "INFY"]
for i, sym in enumerate(syms, start=1):
    d, lb = dm.load_provider("demo_random_walk", sym, "1d", "2y")
    w.panels[i].params.update(source="provider:demo_random_walk", symbol=sym)
    w.panels[i].set_dataframe(d, lb)
w.set_active(w.panels[2])
app.processEvents()
w.grab().save(prefix + "_4charts.png")
assert sum(1 for x in w.panels if x.isVisible()) == 4

# intraday + layout back + indicator toggle must not crash
w.set_layout("1")
w.set_active(w.panels[0])
df2, label2 = dm.load_provider("demo_random_walk", "TCS", "15m", "1mo")
p.set_dataframe(df2, label2)
p.chart.set_indicator("rsi", False)
w.run_backtest()
app.processEvents()

# watchlist
assert "RELIANCE" in w.watchlist.symbols()
assert w.watchlist.add("WIPRO") and not w.watchlist.add("wipro")
assert w.watchlist.remove("WIPRO")
print("UI SMOKE OK", len(df), len(df2), "trades:", len(p.result.trades))
