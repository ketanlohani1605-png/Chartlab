"""The watchlist, TradingView style: every row moves while the market is open, and the change is against the
exchange's previous close.

- spark_prices(): one request for many symbols, the newest minute close of each; Yahoo's previous close is used
  for stocks only (for the Indian indices it is a day old there)
- rows move from the live poll and from a chart's live feed; the slower daily quote does not push a fresh live price
  back
- a weekly / monthly chart of the symbol does not turn last week's close into 'yesterday' (NIFTY showed +2.46%
  instead of +0.98% after a weekly chart)
- the header pill is wide enough for BANKNIFTY (was 'BANKNIF...')
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["CHARTLAB_OFFLINE"] = "1"                          # no real requests from the timers
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
HOME = tempfile.mkdtemp()
os.environ["HOME"] = HOME

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from PySide6 import QtCore, QtTest, QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from chartlab import theme, watchlist as W  # noqa: E402

theme.apply_pg_config()
app.setStyle(theme.make_style())
app.setStyleSheet(theme.STYLESHEET)


def pump(ms=30):
    QtTest.QTest.qWait(ms)


# ---------------------------------------------------------------- spark_prices: one request, many symbols
calls = []


class FakeResp:
    def __init__(self, j):
        self._j = j

    def raise_for_status(self):
        pass

    def json(self):
        return self._j


class FakeSession:
    def __init__(self):
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, timeout=None):
        syms = params["symbols"].split(",")
        calls.append((url, syms, params["range"], params["interval"]))
        out = {}
        for i, s in enumerate(syms):
            if s == "GONE.NS":
                continue
            out[s] = {"timestamp": [1000, 1060, 1120], "close": [10.0 + i, 11.0 + i, None],
                      "previousClose": 9.0 + i, "chartPreviousClose": 9.0 + i, "symbol": s}
        return FakeResp(out)


import requests  # noqa: E402

real_session = requests.Session
requests.Session = FakeSession
try:
    syms = ["^NSEI", "RELIANCE.NS", "GONE.NS"] + [f"S{i}.NS" for i in range(25)]
    got = W.spark_prices(syms)
finally:
    requests.Session = real_session
assert [len(c[1]) for c in calls][:2] == [20, 8], "chunks of 20 symbols"
assert calls[0][0] == W.SPARK_URL and calls[0][2:] == ("1d", "1m")
# ... and one more for the indices with daily bars: their previous close from those (Yahoo's own is a day old)
assert len(calls) == 3 and calls[2][1] == ["^NSEI"] and calls[2][2:] == ("5d", "1d"), calls[2:]
assert got["^NSEI"] == {"price": 11.0, "time": 1060.0, "prev": None}, got["^NSEI"]   # newest non-empty close; no prev
assert got["RELIANCE.NS"]["price"] == 12.0 and got["RELIANCE.NS"]["prev"] == 10.0    # a stock keeps Yahoo's prev
assert "GONE.NS" not in got and len(got) == 27

# ---------------------------------------------------------------- the widget: live rows, official previous close
wl = W.WatchlistWidget()
wl.resize(360, 700)
wl.show()
wl.set_symbols(["NIFTY", "RELIANCE", "TCS"])
pump()


def row(sym):
    for i in range(wl.list.count()):
        it = wl.list.item(i)
        if not it.data(W.ROLE_SECTION) and it.data(W.ROLE_SYM) == sym:
            return it.data(W.ROLE_QUOTE)


# the daily quotes (the QuoteWorker): NIFTY closed at 25,514.30 yesterday
wl._quotes_in({"NIFTY": (25700.0, 25700.0 / 25514.3 - 1), "RELIANCE": (1370.0, 1370.0 / 1375.6 - 1)},
              {"NIFTY": {"prev": 25514.3, "high": 25710.0, "low": 25500.0, "open": 25520.0, "volume": 0.0,
                         "time": "14:00"},
               "RELIANCE": {"prev": 1375.6, "high": 1380.0, "low": 1360.0, "open": 1376.0, "volume": 1.0,
                            "time": "14:00"}})
assert abs(row("NIFTY")[1] - (25700.0 / 25514.3 - 1)) < 1e-12 and row("TCS") is None

# a live round: NIFTY's spark prev (a day old) is not used - the known close is; TCS has no close yet: no change
wl._spark_in({"NIFTY": {"price": 25760.55, "time": time.time(), "prev": None},
              "RELIANCE": {"price": 1367.1, "time": time.time(), "prev": 1375.6},
              "TCS": {"price": 2378.2, "time": time.time(), "prev": None}})
n = row("NIFTY")
assert n[0] == 25760.55 and abs(n[1] - (25760.55 / 25514.3 - 1)) < 1e-12, n          # +0.97%, not +2.1%
assert abs(n[1] * 100 - 0.965) < 0.01
assert row("RELIANCE")[0] == 1367.1 and row("TCS") is None
assert wl.extra["NIFTY"]["live"] and wl.extra["NIFTY"]["high"] == 25760.55, "the day's high follows the price"
wl._spark_in({"TCS": {"price": 2378.2, "time": time.time(), "prev": 2342.7}})           # a stock brings its close
assert abs(row("TCS")[1] - (2378.2 / 2342.7 - 1)) < 1e-12

# the slower daily quote does not push a fresh live price back (but brings the day's numbers)
wl._quotes_in({"NIFTY": (25750.0, 25750.0 / 25514.3 - 1)},
              {"NIFTY": {"prev": 25514.3, "high": 25790.0, "low": 25500.0, "open": 25520.0, "volume": 0.0,
                         "time": "14:01"}})
assert row("NIFTY")[0] == 25760.55 and wl.extra["NIFTY"]["high"] == 25790.0 and wl.extra["NIFTY"]["live"]
wl.extra["NIFTY"]["live_at"] -= W.WatchlistWidget.LIVE_HOLD + 1                        # ... once it is old, it does
wl._quotes_in({"NIFTY": (25750.0, 25750.0 / 25514.3 - 1)}, {"NIFTY": {"prev": 25514.3, "time": "14:02"}})
assert row("NIFTY")[0] == 25750.0

# a chart's previous close counts only until the exchange's is known
wl.chart_price("NIFTY", 25770.0, 25144.3)                                              # (a weekly chart's)
assert abs(row("NIFTY")[1] - (25770.0 / 25514.3 - 1)) < 1e-12
wl.extra.pop("TCS")
wl.chart_price("TCS", 2380.0, 2340.0)                                                   # nothing known: it does
assert abs(row("TCS")[1] - (2380.0 / 2340.0 - 1)) < 1e-12
wl.chart_price("TCS", 2385.0, None, live=True)
assert row("TCS")[0] == 2385.0 and wl.extra["TCS"]["live"] and wl.extra["TCS"]["time"].count(":") == 2

# poll_live: one worker at a time, results land in the rows
seen = []
real = W.spark_prices
W.spark_prices = lambda ys, timeout=6.0: (seen.append(list(ys)) or
                                          {y: {"price": 1.0, "time": time.time(), "prev": None} for y in ys})
try:
    assert wl.poll_live()
    assert not wl.poll_live(), "a round is still running"
    t0 = time.time()
    while wl._spark.isRunning() and time.time() - t0 < 5:
        pump(20)
    pump(50)
finally:
    W.spark_prices = real
assert seen == [["^NSEI", "RELIANCE.NS", "TCS.NS"]], seen
assert row("NIFTY")[0] == 1.0 and row("TCS")[0] == 1.0

# ---------------------------------------------------------------- in the window
from chartlab.mainwindow import MainWindow  # noqa: E402
from chartlab import live as livemod  # noqa: E402

win = MainWindow(QtCore.QSettings(tempfile.mktemp(suffix=".ini"), QtCore.QSettings.Format.IniFormat))
win.resize(1411, 786)
win.show()
pump(200)
win.watchlist.set_symbols(["NIFTY", "BANKNIFTY", "RELIANCE"])
win.watchlist._quotes_in({"NIFTY": (25700.0, 25700.0 / 25514.3 - 1)}, {"NIFTY": {"prev": 25514.3}})

# a live price on a chart moves its row
win.live.tickPrice.emit("NIFTY", 25801.0)
pump()
q = win.watchlist.quotes["NIFTY"]
assert q[0] == 25801.0 and abs(q[1] - (25801.0 / 25514.3 - 1)) < 1e-12
win.live.tickPrice.emit("NSE:RELIANCE", 1371.0)                                         # (a broker's way of naming)
pump()
assert win.watchlist.quotes["RELIANCE"][0] == 1371.0

# a weekly chart of NIFTY: its row keeps yesterday's close (was: last week's, +2.46%)
wk = pd.DataFrame({"Open": [25000.0, 25150.0], "High": [25200.0, 25800.0], "Low": [24900.0, 25100.0],
                   "Close": [25144.3, 25762.75], "Volume": [0.0, 0.0]},
                  index=pd.to_datetime(["2026-11-09", "2026-11-16"]))
p = win.active
p.chart.set_data(wk, "NIFTY")
p.df = wk
p.params.update(symbol="NIFTY", source="yahoo", interval="1wk")
win._sync_watch_price(p)
q = win.watchlist.quotes["NIFTY"]
assert q[0] == 25762.75 and abs(q[1] * 100 - (25762.75 / 25514.3 - 1) * 100) < 1e-9, q[1] * 100   # +0.97%

# the live poll: only while the market is open and the list is on screen; never in tests' offline mode
polled = []
real_poll = win.watchlist.poll_live
win.watchlist.poll_live = lambda: polled.append(1) or True
real_open = livemod.market_open
try:
    win._watch_live_poll()
    assert not polled, "offline (tests): no requests"
    del os.environ["CHARTLAB_OFFLINE"]
    livemod.market_open = lambda now=None: True
    win.show_right_panel(win.watch_dock)
    pump(400)
    polled.clear()
    win._watch_live_poll()
    assert polled == [1], "market open, list on screen: polled"
    win.show_right_panel(None)
    pump(100)
    polled.clear()
    win._watch_live_poll()
    assert not polled, "the list is hidden: not polled"
    win.show_right_panel(win.watch_dock)
    pump(400)
    livemod.market_open = lambda now=None: False
    polled.clear()
    win._watch_live_poll()
    assert not polled, "market closed (Indian symbols only): not polled"
    win.watchlist.set_symbols(["NIFTY", "BTC-USD"])
    win._watch_live_poll()
    assert polled == [1], "crypto trades all day"
finally:
    os.environ["CHARTLAB_OFFLINE"] = "1"
    livemod.market_open = real_open
    win.watchlist.poll_live = real_poll
assert win._watch_live_timer.isActive() if hasattr(win, "_watch_live_timer") else True

# the pill holds BANKNIFTY whole
win.symbol.setText("BANKNIFTY")
pump()
assert win.sym_btn.text() == "BANKNIFTY" and "…" not in win.sym_btn.text()

print("WATCH LIVE OK")
os._exit(0)
