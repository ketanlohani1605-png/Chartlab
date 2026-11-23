"""TradingView's legend and layouts (measured on its chart page, Nov 2026):

- with the mouse off the chart the legend shows the newest price and the change since the previous session's
  close, in the day's colour ("64,609.05  +1,548.75 (+2.46%)"); over the chart, the O H L C of the bar under the
  mouse and that bar's change
- a weekly / monthly chart takes the previous close from the daily quotes
- two / four charts share the space equally (the chart that was alone used to keep its width, the new one squeezed)
"""
import os
import re
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["CHARTLAB_OFFLINE"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["HOME"] = tempfile.mkdtemp()

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from PySide6 import QtCore, QtTest, QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from chartlab import theme  # noqa: E402

theme.apply_pg_config()
app.setStyle(theme.make_style())
app.setStyleSheet(theme.STYLESHEET)
from chartlab.mainwindow import MainWindow  # noqa: E402


def pump(ms=40):
    QtTest.QTest.qWait(ms)


win = MainWindow(QtCore.QSettings(tempfile.mktemp(suffix=".ini"), QtCore.QSettings.Format.IniFormat))
win.resize(1411, 786)
win.show()
pump(200)
p = win.active
ch = p.chart

# two sessions of 1-minute bars: yesterday closed at 25,514.30, today the last bar is 25,760.55
t1 = pd.date_range("2026-11-19 09:15", periods=375, freq="1min")
t2 = pd.date_range("2026-11-20 09:15", periods=200, freq="1min")
c1 = np.linspace(25300.0, 25514.3, len(t1))
c2 = np.linspace(25520.0, 25760.55, len(t2))
c2[-2] = 25765.0                                                   # the newest bar itself is down (red)
cl = np.r_[c1, c2]
op = np.r_[cl[0], cl[:-1]]
df = pd.DataFrame({"Open": op, "High": np.maximum(op, cl) + 1, "Low": np.minimum(op, cl) - 1, "Close": cl,
                   "Volume": 0.0}, index=t1.append(t2))
p.params.update(symbol="NIFTY", source="yahoo", interval="1m")
p.set_dataframe(df, "^NSEI · 1m")
pump(150)
assert ch.day_prev_close() == 25514.3

hdr = ch.header.text()
assert "25,760.55" in hdr and "+246.25 (+0.97%)" in hdr, hdr[-300:]
assert ">O<" not in hdr and ">H<" not in hdr, "no O H L C without the mouse"
assert hdr.count(theme.UP) >= 2, "price and change in the day's colour (up), though the bar itself is down"

# the mouse over a bar: that bar's O H L C and its own change
vb = ch.price.vb
k = len(df) - 50
sp = vb.mapViewToScene(QtCore.QPointF(k, float(df.Close.iloc[k])))
ch._mouse_moved(sp)
pump(30)
hdr = ch.header.text()
assert ch._hovering and ">O<" in hdr and ">C<" in hdr and f"{df.Close.iloc[k]:,.2f}" in hdr, hdr[-300:]
# right of the newest bar (still on the chart): the newest bar's O H L C
sp = vb.mapViewToScene(QtCore.QPointF(len(df) + 3, float(df.Close.iloc[-1])))
ch._mouse_moved(sp)
pump(30)
assert ">O<" in ch.header.text()
# the mouse leaves: price and day change again
QtTest.QTest.mouseMove(win.symbol_bar if hasattr(win, "symbol_bar") else win.sym_btn)
ch._crosshair_off()
pump(30)
hdr = ch.header.text()
assert not ch._hovering and "+246.25 (+0.97%)" in hdr and ">O<" not in hdr, hdr[-300:]

# a live price arrives: the legend follows it (no mouse)
df0 = df.copy()                                                      # (a tick changes the chart's own frame)
r = p.on_tick(25700.0, float(ch._times[-1]) + 10, None)
pump(30)
assert r == "same", r
assert "25,700.00" in ch.header.text() and "+185.70 (+0.73%)" in ch.header.text(), ch.header.text()[-300:]
df = df0
p.set_dataframe(df.copy(), "^NSEI · 1m")
pump(50)

# only today's bars: no 'yesterday' in the data, the day_prev from outside is used, else the bar's O H L C
today = df[df.index >= "2026-11-20"].copy()
p.set_dataframe(today, "^NSEI · 1m")
pump(80)
ch.day_prev = None
ch._refresh_header()
assert ch.day_prev_close() is None and ">O<" in ch.header.text()
ch.day_prev = 25514.3
ch._refresh_header()
assert "+246.25 (+0.97%)" in ch.header.text(), ch.header.text()[-300:]

# a weekly chart: the watchlist's daily close gives the day change
wk = pd.DataFrame({"Open": [25000.0, 25150.0], "High": [25200.0, 25800.0], "Low": [24900.0, 25100.0],
                   "Close": [25144.3, 25760.55], "Volume": [0.0, 0.0]}, index=pd.to_datetime(["2026-11-09", "2026-11-16"]))
p.params.update(symbol="NIFTY", source="yahoo", interval="1wk")
ch.day_prev = None
win.watchlist.extra.pop("NIFTY", None)                              # (the 1-minute chart above taught it the close)
p.set_dataframe(wk, "^NSEI · 1wk")
pump(80)
assert ch.day_prev_close() is None and ">O<" in ch.header.text()
win.watchlist.extra.setdefault("NIFTY", {})["prev"] = 25514.3
win._feed_day_prev(p)
pump(30)
assert ch.day_prev == 25514.3 and "+246.25 (+0.97%)" in ch.header.text(), ch.header.text()[-300:]
assert "+616.25" not in ch.header.text(), "not last week's change"

# daily bars: the bar before is yesterday
dd = pd.DataFrame({"Open": [25200.0, 25514.3], "High": [25600.0, 25800.0], "Low": [25100.0, 25500.0],
                   "Close": [25514.3, 25760.55], "Volume": [0.0, 0.0]}, index=pd.to_datetime(["2026-11-19", "2026-11-20"]))
ch.day_prev = None
p.params.update(interval="1d")
p.set_dataframe(dd, "^NSEI · 1d")
pump(80)
assert ch.day_prev_close() == 25514.3 and "+246.25 (+0.97%)" in ch.header.text()
assert re.search(r"[+−][\d,]+\.\d\d \([+−]\d+\.\d\d%\)", ch.header.text())

# ---------------------------------------------------------------- layouts share the space equally
for name, n in (("2h", 2), ("2v", 2), ("4", 4)):
    win.set_layout(name)
    pump(250)
    ws = [w.width() for w in win.panels[:n]]
    hs = [w.height() for w in win.panels[:n]]
    if name == "2h":
        assert abs(ws[0] - ws[1]) <= 8, ws
    elif name == "2v":
        assert abs(hs[0] - hs[1]) <= 8, hs
    else:
        assert max(ws) - min(ws) <= 8 and max(hs) - min(hs) <= 8, (ws, hs)
# the user's own divider positions are kept
win.set_layout("2h")
pump(150)
win.split_root.setSizes([900, 300])
pump(50)
mine = win.split_root.sizes()
win.set_layout("1")
pump(100)
win.set_layout("2h")
pump(200)
assert abs(win.split_root.sizes()[0] - mine[0]) <= 8, (win.split_root.sizes(), mine)
win.set_layout("1")
pump(100)

# ---------------------------------------------------------------- a scale number never peeks out from under a label
from PySide6 import QtGui  # noqa: E402
import pyqtgraph as pg  # noqa: E402
ax = ch.price.getAxis("right")
fm = QtGui.QFontMetricsF(ax.style.get("tickFont") or QtGui.QFont())
H = fm.height()
seen_specs = []
real_draw = pg.AxisItem.drawPicture
pg.AxisItem.drawPicture = lambda self, p_, a_, t_, specs: seen_specs.append([s_[2] for s_ in specs])
try:
    img = QtGui.QImage(100, 400, QtGui.QImage.Format.Format_ARGB32)
    pt = QtGui.QPainter(img)
    tag = (200.0, 222.0)                                   # the last-price label: rows 200 - 222
    ax._blocked = (tag,)
    digits = lambda y: (y - H / 2 + fm.ascent() - fm.capHeight(), y - H / 2 + fm.ascent())  # noqa: E731
    specs = []
    for name, centre in (("far", 150.0), ("touching", None), ("clear", None), ("under", 211.0)):
        if name == "touching":                             # digits end 1.5 px inside the tag (was drawn, cut)
            centre = tag[0] + 1.5 - (fm.ascent() - H / 2)
        if name == "clear":                                # digits end 3 px above the tag: drawn
            centre = tag[0] - 3.0 - (fm.ascent() - H / 2)
        specs.append((QtCore.QRectF(0, centre - H / 2, 90, H), 0, name))
    type(ax).drawPicture(ax, pt, None, [], specs)
    pt.end()
finally:
    pg.AxisItem.drawPicture = real_draw
    ax._blocked = ()
assert seen_specs and seen_specs[-1] == ["far", "clear"], seen_specs

print("LEGEND DAY OK")
os._exit(0)
