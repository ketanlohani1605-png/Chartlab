"""TradingView: scroll to the oldest bar and the older history comes in front of it; Yahoo's intraday bars go as
far back as Yahoo allows.

- clamp_period: 1-minute bars 29 days (in pieces of 7: Yahoo's limit per request), 5-minute 60 days (was 1 month)
- the oldest bar in view: the next longer range is fetched once, its older bars go in front; the bars on screen,
  the drawings (anchored by time) and the newest bars stay as they were
- intraday charts whose source has nothing older ask for nothing; a fresh load may scroll back again
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
from PySide6 import QtCore, QtTest, QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from chartlab import data as dm, theme  # noqa: E402

theme.apply_pg_config()
app.setStyle(theme.make_style())
app.setStyleSheet(theme.STYLESHEET)
from chartlab.mainwindow import MainWindow  # noqa: E402


def pump(ms=40):
    QtTest.QTest.qWait(ms)


def wait(cond, secs=10.0):
    t = time.time()
    while not cond() and time.time() - t < secs:
        pump(30)
    return cond()


# ---------------------------------------------------------------- the 1-minute pieces
calls = []


class FakeTicker:
    def __init__(self, sym):
        self.sym = sym

    def history(self, start=None, end=None, period=None, interval=None, auto_adjust=True):
        calls.append((self.sym, start, end, period, interval))
        if start is None:
            return pd.DataFrame()
        a, b = pd.Timestamp(start), pd.Timestamp(end)
        idx = pd.date_range(a.ceil("D") + pd.Timedelta(hours=4), b, freq="1D")       # one bar a day is enough here
        if len(calls) == 3:                                                        # an older piece fails: left out
            raise RuntimeError("Yahoo busy")
        return pd.DataFrame({"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 10.0}, index=idx)


class FakeYF:
    Ticker = FakeTicker


got = dm._yahoo_1m(FakeYF, "^NSEI", 29)
assert [c[4] for c in calls] == ["1m"] * 5 and all(c[3] is None for c in calls), calls
spans = sorted((pd.Timestamp(c[1]), pd.Timestamp(c[2])) for c in calls)
assert all((b - a) <= pd.Timedelta(days=8) for a, b in spans), "no piece longer than Yahoo allows"
assert pd.Timestamp.now(tz="UTC") - spans[0][0] <= pd.Timedelta(days=29, minutes=1)
assert spans[-1][1] > pd.Timestamp.now(tz="UTC"), "the newest piece reaches now"
assert len(got) and got.index.is_monotonic_increasing and got.index.is_unique

# ---------------------------------------------------------------- scroll back on a daily chart
win = MainWindow(QtCore.QSettings(tempfile.mktemp(suffix=".ini"), QtCore.QSettings.Format.IniFormat))
win.resize(1411, 786)
win.show()
pump(200)
p = win.active
ch = p.chart


def bars(start, end):
    idx = pd.bdate_range(start, end)
    c = 1000 + np.cumsum(np.ones(len(idx)))
    return pd.DataFrame({"Open": c - 0.5, "High": c + 1, "Low": c - 1, "Close": c, "Volume": 100.0}, index=idx)


ALL = bars("2016-01-01", "2026-11-20")
asked = []


def fake_yahoo(symbol, interval="1d", period="2y"):
    asked.append((symbol, interval, period))
    days = dm.period_days(period)
    if period == "max" or not days:
        return ALL.copy(), "RELIANCE.NS · 1d"
    return ALL[ALL.index >= ALL.index[-1] - pd.Timedelta(days=days)].copy(), "RELIANCE.NS · 1d"


real_yahoo = dm.load_yahoo
dm.load_yahoo = fake_yahoo
try:
    p.params.update(source="yahoo", symbol="RELIANCE", interval="1d", range="2y")
    two = fake_yahoo("RELIANCE", "1d", "2y")[0]
    asked.clear()
    p.df = two
    p.label = ch.label = "RELIANCE.NS · 1d"
    ch.set_data(two, "RELIANCE.NS · 1d")
    pump(100)
    n0 = len(two)
    first_t = two.index[0]
    # a horizontal line drawn at a date in the middle
    mid_t = two.index[n0 // 2]
    ch.layer.load_json([{"type": "trend", "pts": [[ch.x_to_time(n0 // 2), 1100.0], [ch.x_to_time(n0 - 10), 1200.0]]}])
    pump(30)
    t_line = ch.layer.drawings[0]["pts"][0][0]

    # not at the left edge: nothing
    ch.price.setXRange(n0 - 120, n0 + 5, padding=0)
    pump(400)
    assert not asked and p.maybe_older() is False

    # the oldest bar in view: 5 years are asked for, the older bars go in front
    ch.price.setXRange(-2, 100, padding=0)
    vis_before = (two.index[0], two.index[min(99, n0 - 1)])
    assert wait(lambda: len(p.df) > n0), (asked, len(p.df))
    pump(100)
    assert asked == [("RELIANCE", "1d", "5y")], asked
    added = len(p.df) - n0
    assert p.df.index[0] < first_t and p.df.index[-1] == two.index[-1] and p.df.index.is_unique
    assert p.params["range"] == "5y"
    x0, x1 = ch.price.vb.viewRange()[0]
    assert abs(x0 - (-2 + added)) < 1.0 and abs(x1 - (100 + added)) < 1.0, (x0, x1, added)
    assert p.df.index[int(round(x0)) + 2] == vis_before[0], "the same bars are on screen"
    assert ch.layer.drawings[0]["pts"][0][0] == t_line, "the drawing keeps its date"
    assert abs(ch.time_to_x(t_line) - (n0 // 2 + added)) < 0.6

    # asked once per range; scrolling to the new edge asks for the next one, then max, then nothing
    asked.clear()
    ch.price.setXRange(-1, 80, padding=0)
    assert wait(lambda: p.params["range"] == "10y"), (asked, p.params)
    assert asked == [("RELIANCE", "1d", "10y")]
    ch.price.setXRange(-1, 80, padding=0)
    assert wait(lambda: p.params["range"] == "max"), (asked, p.params)
    n_all = len(p.df)
    assert n_all == len(ALL) and p.df.index[0] == ALL.index[0]
    asked.clear()
    ch.price.setXRange(-1, 80, padding=0)
    pump(500)
    assert not asked and p.maybe_older() is False, "all of it is here"

    # intraday from Yahoo: as far back as it goes is loaded already - nothing is asked
    m = bars("2026-11-01", "2026-11-20")
    m.index = pd.date_range("2026-11-20 09:15", periods=len(m), freq="1min")
    p.params.update(interval="1m", range="2y")
    p.df = m
    ch.set_data(m, "^NSEI · 1m")
    pump(80)
    asked.clear()
    ch.price.setXRange(-1, 10, padding=0)
    pump(500)
    assert p._older_range() is None and not asked
    p.params.update(interval="1h", range="2y")
    assert p._older_range() is None                       # (two years of hours is Yahoo's all)
    p.params.update(interval="1d", range="2y")
    assert p._older_range() == "5y"
    p.params.update(interval="1wk", range="10y")
    assert p._older_range() == "max"

    # a file of your own: never
    p.params.update(source="file", interval="1d", range="2y")
    p.df = two
    ch.set_data(two, "my.csv")
    pump(50)
    ch.price.setXRange(-1, 50, padding=0)
    assert p.maybe_older() is False
finally:
    dm.load_yahoo = real_yahoo

print("OLDER OK")
os._exit(0)
