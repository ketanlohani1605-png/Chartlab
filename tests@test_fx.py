"""The small things that make the app feel finished:

- a chart that could not load says so in the middle, in plain words, with 'Try again' and 'Search symbols'
- a thin line runs along the top of a chart while its bars are on their way
- the live badge's dot breathes while prices stream in (the words stay quiet; an error is red)
- a watchlist price that moves glows green / red for a moment; the chosen row has a blue edge, no white outline
- the watchlist's number columns are as wide as their widest number (64,500.00 and 1,450.15 never touch) and the
  names keep their room (BANKNIFTY whole)
- the active chart of a layout has a blue edge
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["CHARTLAB_OFFLINE"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["HOME"] = tempfile.mkdtemp()

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from PySide6 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from chartlab import data as dm, fx, theme, watchlist as W  # noqa: E402

theme.apply_pg_config()
app.setStyle(theme.make_style())
app.setStyleSheet(theme.STYLESHEET)
from chartlab.mainwindow import MainWindow  # noqa: E402


def pump(ms=40):
    QtTest.QTest.qWait(ms)


def wait(cond, secs=8.0):
    t = time.time()
    while not cond() and time.time() - t < secs:
        pump(30)
    return cond()


# ---------------------------------------------------------------- errors in plain words
assert fx.human("RuntimeError: No data returned for XYZ.NS") == "No data returned for XYZ.NS"
assert fx.human("requests.exceptions.ConnectionError: HTTPSConnectionPool failed") == "HTTPSConnectionPool failed"
assert fx.human("ValueError: x\n   y") == "x y" and len(fx.human("E: " + "a" * 500)) <= 220

win = MainWindow(QtCore.QSettings(tempfile.mktemp(suffix=".ini"), QtCore.QSettings.Format.IniFormat))
win.resize(1411, 786)
win.show()
pump(200)
p = win.active

# ---------------------------------------------------------------- a chart that cannot load
calls = []


def boom(symbol, interval="1d", period="2y"):
    calls.append(symbol)
    raise RuntimeError(f"No data returned for {symbol}.NS (Yahoo has no such symbol)")


real = dm.load_yahoo
dm.load_yahoo = boom
try:
    win.symbol.setText("XYZABC")
    win.interval_btns["1d"].click()
    pump(30)
    win.on_load()
    assert p.loadbar.isVisible(), "the line runs while the bars are on their way"
    assert wait(lambda: not p.loading)
    pump(400)
    assert not p.loadbar.running and wait(lambda: not p.loadbar.isVisible(), 2.0), "... and fades when done"
    e = p.empty
    assert e.isVisible() and e.geometry() == p.chart.rect()
    assert e.title.text() == "Couldn’t load XYZABC", e.title.text()
    assert e.detail.text() == "No data returned for XYZABC.NS (Yahoo has no such symbol)", e.detail.text()
    assert "RuntimeError" not in win.statusBar().currentMessage()
    assert e.retry_btn.isVisible() and e.search_btn.isVisible()
    # Try again: the same symbol is asked for again
    n = len(calls)
    QtTest.QTest.mouseClick(e.retry_btn, QtCore.Qt.MouseButton.LeftButton)
    assert wait(lambda: len(calls) > n and not p.loading)
    pump(100)
    assert e.isVisible(), "still nothing: still the message"
finally:
    dm.load_yahoo = real

# 'Search symbols' opens the symbol search for this chart
opened = []
real_open = win.open_symbol_search
win.open_symbol_search = lambda text="": opened.append(text)
QtTest.QTest.mouseClick(p.empty.search_btn, QtCore.Qt.MouseButton.LeftButton)
assert opened == [""], opened
win.open_symbol_search = real_open

# bars arrive: the message goes
r = np.random.default_rng(3)
c = 24000 * np.exp(np.cumsum(r.normal(0.0004, 0.011, 300)))
df = pd.DataFrame({"Open": c, "High": c * 1.004, "Low": c * 0.996, "Close": c, "Volume": 1e6},
                  index=pd.bdate_range(end="2026-11-20", periods=300))
p.params.update(symbol="NIFTY", source="yahoo", interval="1d")
p.set_dataframe(df, "NIFTY")
pump(50)
assert not p.empty.isVisible()

# ---------------------------------------------------------------- the live badge
b = win.live_label
assert isinstance(b, fx.LiveBadge)
win._chart_market_closed = lambda: False                 # (any hour: first the feed's own states)
win._live_status("yahoo", "")
assert b.isVisible() and b.text() == "Live (3 s)" and b._breathe and b._timer.isActive()
assert b._col == QtGui.QColor("#f7a600") and "#b8b8b8" in b.styleSheet(), "the dot carries the colour"
win._live_status("live", "")
assert b.text() == "Live" and b._col == QtGui.QColor(theme.UP) and b._breathe
win._live_status("connecting", "")
assert b._ring and not b._breathe and not b._timer.isActive()
win._live_status("error", "no network")
assert b.text() == "Live prices off" and theme.DOWN in b.styleSheet() and not b._breathe
img = b.grab().toImage()
assert img.width() > 40
# the chart's market closed: nothing streams, so the badge says so - grey and still, not 'Live (3 s)'
win._chart_market_closed = lambda: True
win._live_status("yahoo", "")
assert b.isVisible() and b.text() == "Market closed" and not b._breathe and not b._timer.isActive()
assert b._col == QtGui.QColor(theme.MUTED) and "closed" in b.toolTip()
win._live_status("live", "")
assert b.text() == "Market closed", "a broker feed after hours: the same"
win._live_status("connecting", "")
assert b.text().startswith("Connecting"), "the broker's own trouble is still told"
win._live_status("yahoo", "")
win._chart_market_closed = lambda: False                 # 09:15: the clock's next tick turns it live by itself
win._tick_clock()
assert b.text() == "Live (3 s)" and b._breathe and b._timer.isActive()
style = b.styleSheet()
win._tick_clock()
assert b.styleSheet() == style and b.text() == "Live (3 s)", "an unchanged state is not drawn again each second"
# the real check reads the chart's own market (NSE hours for NIFTY, crypto never closes)
del win._chart_market_closed
import chartlab.watchlist as _W
_real_ms = _W.market_state
try:
    _W.market_state = lambda s, now=None: ("closed", "Market closed") if s == "NIFTY" else ("open", "Market open")
    assert win.active.params["symbol"] == "NIFTY" and win._chart_market_closed()
    win.active.params["symbol"] = "BTC-USD"
    assert not win._chart_market_closed()
finally:
    _W.market_state = _real_ms
    win.active.params["symbol"] = "NIFTY"
win._chart_market_closed = lambda: False
win._live_status("off", "")
assert not b.isVisible()

# ---------------------------------------------------------------- the watchlist: glow, chosen row, columns
win.show_right_panel(win.watch_dock)
pump(300)
wl = win.watchlist
wl.quotes.clear()                                        # (the chart above already gave NIFTY a price)
wl.extra.clear()
wl.set_symbols(["NIFTY", "BANKNIFTY", "RELIANCE", "TCS", "HDFCBANK"])
wl._quotes_in({"NIFTY": (25700.0, 0.0073), "BANKNIFTY": (64500.0, 0.023), "RELIANCE": (1366.5, -0.0066),
               "TCS": (2377.2, 0.0147), "HDFCBANK": (848.3, 0.0317)},
              {"NIFTY": {"prev": 25514.3}, "BANKNIFTY": {"prev": 63063.0}, "RELIANCE": {"prev": 1375.6},
               "TCS": {"prev": 2342.7}, "HDFCBANK": {"prev": 822.3}})
pump(50)


def item(sym):
    for i in range(wl.list.count()):
        it = wl.list.item(i)
        if not it.data(W.ROLE_SECTION) and it.data(W.ROLE_SYM) == sym:
            return it


assert item("NIFTY").data(W.ROLE_FLASH) is None, "the first prices do not glow"
wl._spark_in({"NIFTY": {"price": 25712.5, "time": time.time(), "prev": None},
              "TCS": {"price": 2371.0, "time": time.time(), "prev": 2342.7}})
fl_n, fl_t = item("NIFTY").data(W.ROLE_FLASH), item("TCS").data(W.ROLE_FLASH)
assert fl_n and fl_n[0] == 1 and fl_t and fl_t[0] == -1 and wl._flash.isActive()
assert item("RELIANCE").data(W.ROLE_FLASH) is None, "an unchanged price stays still"
assert wait(lambda: not wl._flash.isActive(), 3.0), "the glow fades and the repainting stops"

# the columns fit the widest numbers, and the names keep room for BANKNIFTY
pump(50)
f = QtGui.QFont(wl.list.font())
f.setPixelSize(W.QuoteDelegate.NUM_PX)
fm = QtGui.QFontMetrics(f)
row = wl.list.visualItemRect(item("BANKNIFTY"))
last, chg, pct = W.QuoteDelegate.cols(row)
t_last, t_chg, t_pct = W.QuoteDelegate.texts("BANKNIFTY", wl.quotes["BANKNIFTY"])
assert fm.horizontalAdvance(t_last) <= last.width() and fm.horizontalAdvance(t_chg) <= chg.width()
assert chg.left() - last.right() >= W.QuoteDelegate.GAP - 1, "64,500.00 and 1,450.15 do not touch"
nf = QtGui.QFont(wl.list.font())
nf.setPixelSize(14)
name_room = last.left() - 8 - (row.x() + 38)
assert QtGui.QFontMetrics(nf).horizontalAdvance("BANKNIFTY") <= name_room, (name_room, row.width())

# the chosen row: a soft fill and a blue edge, no white outline
wl.list.setCurrentItem(item("RELIANCE"))
pump(50)
img = wl.list.viewport().grab().toImage()
rr = wl.list.visualItemRect(item("RELIANCE"))
edge = img.pixelColor(int((rr.x() + 5.5) * img.devicePixelRatio()), int(rr.center().y() * img.devicePixelRatio()))
assert edge.blue() > 150 and edge.red() < 120, edge.name()
mid = img.pixelColor(int((rr.x() + 30) * img.devicePixelRatio()), int((rr.top() + 2) * img.devicePixelRatio()))
assert max(mid.red(), mid.green(), mid.blue()) < 60, ("no bright outline", mid.name())

# ---------------------------------------------------------------- the active chart of a layout: a blue edge
win.set_layout("2h")
pump(200)
assert theme.ACCENT in win.active.styleSheet() and theme.ACCENT not in win.panels[1].styleSheet()
win.set_layout("1")
pump(100)

print("FX OK")
os._exit(0)
