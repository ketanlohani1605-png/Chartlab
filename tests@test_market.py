"""The Market panel: how the NIFTY 50 is moving, beside the chart.

- Gainers lists the fifty biggest gain first, Losers biggest fall first; each row a logo, the name, the price, the
  change in a coloured pill
- the breadth line counts how many are up and down (unchanged counts for neither)
- the heatmap shows every stock as a tile, coloured by its change (grey near 0 %, deeper up to +-3 %), whole names
- a click on a row or a tile opens that stock's chart
- Yahoo's batch answer becomes (price, change %) per stock; a stock without a previous close is left out
- after the close it says 'Market closed', not 'Live'; the same in the watchlist
- it fetches only while it is on screen: every few seconds in market hours, once otherwise, never offline
- the panel is black from top to bottom, like the watchlist (no grey header, no boxes behind the words)
"""
import datetime as dt
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["CHARTLAB_OFFLINE"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["HOME"] = tempfile.mkdtemp()

from PySide6 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from chartlab import live, market as M, theme, watchlist as W  # noqa: E402

theme.apply_pg_config()
app.setStyle(theme.make_style())
app.setStyleSheet(theme.STYLESHEET)
from chartlab.mainwindow import MainWindow  # noqa: E402


def pump(ms=40):
    QtTest.QTest.qWait(ms)


# ---------------------------------------------------------------- tile colours
grey = M.heat_color(0.0)
assert grey == QtGui.QColor("#262626") and M.heat_color(0.03) == grey, "flat is grey"
g1, g3, g5 = M.heat_color(1.0), M.heat_color(3.0), M.heat_color(5.0)
assert g3 == g5, "deepest at 3 %"
assert g3.green() > g1.green() > grey.green() and g3.red() < grey.red(), (g1.name(), g3.name())
r3 = M.heat_color(-3.0)
assert r3.red() > grey.red() and r3.green() < grey.green(), r3.name()
assert max(g3.green(), r3.red()) < 200, "a deep colour, not a neon one"
assert len(M.NIFTY50) == 50 and len(set(M.NIFTY50)) == 50

# ---------------------------------------------------------------- the panel on its own
panel = M.MarketPanel(symbols=["RELIANCE", "TCS", "HDFCBANK", "INFY", "SBIN", "ITC", "LT"])
panel.resize(340, 640)
panel.show()
pump(50)

# nothing yet: one quiet line, which cannot be clicked and does not break the painting
assert panel.list.count() == 1 and panel.list.item(0).flags() == QtCore.Qt.ItemFlag.NoItemFlags
assert "Loading" in panel.list.item(0).text()
panel.list.viewport().grab()
picked = []
panel.chosen.connect(picked.append)
QtTest.QTest.mouseClick(panel.list.viewport(), QtCore.Qt.MouseButton.LeftButton,
                        pos=panel.list.visualItemRect(panel.list.item(0)).center())
assert picked == []

# Yahoo's batch answer: price and previous close per stock -> (price, change %)
panel._got({"RELIANCE": {"price": 1400.0, "prev": 1380.0, "time": time.time()},
            "TCS": {"price": 3000.0, "prev": None, "time": time.time()}})
assert abs(panel.data["RELIANCE"][1] - (1400 / 1380 - 1) * 100) < 1e-9
assert "TCS" not in panel.data, "no previous close: no change to show"
assert panel.status.text(), "it says when"

panel.set_data({"RELIANCE": (1400.0, 1.45), "TCS": (3000.0, -0.8), "HDFCBANK": (846.0, 2.94),
                "INFY": (1500.0, 0.0), "SBIN": (800.0, -2.1), "ITC": (410.0, 0.3)})
pump(30)

# Gainers: biggest gain first
syms = [panel.list.item(i).data(M.ROLE_SYM) for i in range(panel.list.count())]
assert syms == ["HDFCBANK", "RELIANCE", "ITC", "INFY", "TCS", "SBIN"], syms
assert panel.list.item(0).data(M.ROLE_ROW) == (846.0, 2.94)
assert (panel.breadth.up, panel.breadth.down) == (3, 2), "INFY is unchanged: neither up nor down"
# Losers: biggest fall first
panel.chips["Losers"].click()
pump(30)
assert panel.mode == "Losers" and panel.chips["Losers"].isChecked() and not panel.chips["Gainers"].isChecked()
syms = [panel.list.item(i).data(M.ROLE_SYM) for i in range(panel.list.count())]
assert syms[:2] == ["SBIN", "TCS"], syms

# a click on a row opens that stock
QtTest.QTest.mouseClick(panel.list.viewport(), QtCore.Qt.MouseButton.LeftButton,
                        pos=panel.list.visualItemRect(panel.list.item(0)).center())
assert picked == ["SBIN"], picked
img = panel.list.viewport().grab().toImage()
assert img.width() > 100

# the heatmap: every stock a tile, biggest gain first
panel.chips["Heatmap"].click()
pump(50)
h = panel.heat
assert panel.stack.currentIndex() == 1 and [r[0] for r in h.rows][:2] == ["HDFCBANK", "RELIANCE"]
tiles = h._tiles()
assert len(tiles) == 6 and h._cols() == 3, (h.width(), h._cols())
assert h.heightForWidth(h.width()) >= tiles[-1].bottom()
f = QtGui.QFont(h.font())
f.setPixelSize(12)
f.setWeight(QtGui.QFont.Weight.DemiBold)
for name in ("ULTRACEMCO", "APOLLOHOSP", "BAJAJ-AUTO"):
    assert QtGui.QFontMetrics(f).horizontalAdvance(name) <= tiles[0].width() - 8, (name, tiles[0].width())
QtTest.QTest.mouseMove(h, tiles[1].center().toPoint())
pump(20)
assert h._hover == 1 and "RELIANCE" in h.toolTip()
QtTest.QTest.mouseClick(h, QtCore.Qt.MouseButton.LeftButton, pos=tiles[4].center().toPoint())
assert picked[-1] == h.rows[4][0] == "TCS", picked
himg = h.grab().toImage()
dpr = himg.devicePixelRatio()
c0 = himg.pixelColor(int((tiles[0].x() + 3) * dpr), int((tiles[0].y() + 3) * dpr))
assert c0.green() > c0.red() + 30, ("the top gainer's tile is green", c0.name())
c5 = himg.pixelColor(int((tiles[5].x() + 3) * dpr), int((tiles[5].y() + 3) * dpr))
assert c5.red() > c5.green() + 30, ("the biggest fall is red", c5.name())

# after the close it says so; in the session it is live
panel.stamp(dt.datetime(2026, 11, 23, 5, 0, tzinfo=dt.timezone.utc))           # Monday 10:30 India
assert panel.status.text().startswith("Live · "), panel.status.text()
panel.stamp(dt.datetime(2026, 11, 23, 3, 35, tzinfo=dt.timezone.utc))          # Monday 09:05
assert panel.status.text().startswith("Pre-open"), panel.status.text()
panel.stamp(dt.datetime(2026, 11, 21, 5, 0, tzinfo=dt.timezone.utc))           # Saturday
assert panel.status.text() == "Market closed · last close", panel.status.text()

# black from top to bottom: the header, the chips' row and the list are one surface
panel.set_mode("Gainers")
pump(30)
img = panel.grab().toImage()
dpr = img.devicePixelRatio()
title = next(w for w in panel.findChildren(QtWidgets.QLabel) if w.text() == "NIFTY 50")
st = panel.status.geometry()
pts = [(3, 3), (panel.width() - 3, 3), (title.geometry().right() + 6, title.geometry().center().y()),
       (st.left() - 4, st.center().y()), (3, panel.breadth.geometry().center().y()), (3, panel.height() - 3)]
pts += [(x, st.top() + 1) for x in range(st.left() + 1, st.right(), 7)]             # inside the label, over the words
pts += [(x, title.geometry().top() + 1) for x in range(title.geometry().left() + 1, title.geometry().right(), 7)]
for x, y in pts:
    c = img.pixelColor(int(x * dpr), int(y * dpr))
    assert (c.red(), c.green(), c.blue()) == (0, 0, 0), ((x, y), c.name())
panel.close()

# ---------------------------------------------------------------- in the app
win = MainWindow(QtCore.QSettings(tempfile.mktemp(suffix=".ini"), QtCore.QSettings.Format.IniFormat))
win.resize(1411, 786)
win.show()
pump(200)
assert not win.market_dock.isVisible() and win.market_dock in win.right_docks
win.show_right_panel(win.market_dock)
pump(300)
assert win.market_dock.isVisible() and win.market.width() >= 330

# a click in the panel opens that chart
opened = []
real_load = win.on_load
win.on_load = lambda *a, **k: opened.append(win.symbol.text())
win.market.chosen.emit("ITC")
win.on_load = real_load
assert opened == ["ITC"] and win.symbol.text() == "ITC", opened

# it fetches only while on screen, in market hours (once otherwise), never offline
calls = []
win.market.refresh = lambda: calls.append(1) or True
real_open = live.market_open
try:
    win._market_poll(force=True)
    assert calls == [], "offline: no fetching"
    del os.environ["CHARTLAB_OFFLINE"]
    live.market_open = lambda now=None: True
    win._market_poll()
    assert calls == [1], "on screen in market hours: fetch"
    live.market_open = lambda now=None: False
    win.market.data = {"TCS": (3000.0, 0.1)}
    win._market_poll()
    assert calls == [1], "closed and already filled: no new fetch"
    win._market_poll(force=True)
    assert calls == [1, 1], "opening the panel always fetches once"
    win.market.data = {}
    win._market_poll()
    assert calls == [1, 1, 1], "closed but empty: fetch the last close"
    win.show_right_panel(None)
    pump(100)
    live.market_open = lambda now=None: True
    win._market_poll()
    assert calls == [1, 1, 1], "hidden: nothing"
finally:
    os.environ["CHARTLAB_OFFLINE"] = "1"
    live.market_open = real_open

# the watchlist says 'Market closed' after hours, 'Live' in them
wl = win.watchlist
real_state = W.market_state
try:
    W.market_state = lambda sym, now=None: ("closed", "Market closed")
    wl._spark_in({"RELIANCE": {"price": 1401.0, "time": time.time(), "prev": 1380.0}})
    assert wl.status.text() == "Market closed  ·  last prices", wl.status.text()
    W.market_state = lambda sym, now=None: ("open", "Market open")
    wl._spark_in({"RELIANCE": {"price": 1402.0, "time": time.time(), "prev": 1380.0}})
    assert wl.status.text().startswith("Live  ·  "), wl.status.text()
finally:
    W.market_state = real_state

print("MARKET OK")
os._exit(0)
