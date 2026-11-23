"""The newest daily bar is not lost:

Yahoo leaves the last session's daily bar unfinished for hours after the close (over a weekend, all weekend): the
open, high, low and volume are there, the close is empty, and yfinance's auto_adjust then blanks the whole bar - so
Friday went missing from every daily chart. The chart's meta still has that session's price, high, low and volume.

- the bar is finished from the meta: close = the last price, high / low widened to the meta's day range
- only when the meta is about the same day, and the open is known (nothing is made up)
- older bars are adjusted for splits and dividends exactly as auto_adjust does (O, H, L x Adj Close / Close)
- a finished bar is left alone
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from chartlab import data as dm  # noqa: E402

IST = "Asia/Kolkata"
idx = pd.DatetimeIndex(["2026-11-18", "2026-11-19", "2026-11-20"]).tz_localize(IST)


def raw(last_close=np.nan, last_open=1369.0):
    return pd.DataFrame({"Open": [1300.0, 1361.9, last_open], "High": [1310.0, 1382.5, 1383.0],
                         "Low": [1290.0, 1349.7, 1356.1], "Close": [1305.0, 1375.6, last_close],
                         "Adj Close": [1300.0, 1375.6, last_close],             # a dividend before the 19th
                         "Volume": [1e6, 14486599.0, 13167730.0], "Dividends": 0.0, "Stock Splits": 0.0}, index=idx)


meta = {"regularMarketPrice": 1377.5, "regularMarketTime": pd.Timestamp("2026-11-20 15:28:10", tz=IST),
        "regularMarketDayHigh": 1383.0, "regularMarketDayLow": 1356.1, "regularMarketVolume": 13167730}

# finished from the meta
r = raw()
assert dm.fill_last_session(r, meta)
assert r["Close"].iloc[-1] == 1377.5 and r["Adj Close"].iloc[-1] == 1377.5
assert r["High"].iloc[-1] == 1383.0 and r["Low"].iloc[-1] == 1356.1 and r["Open"].iloc[-1] == 1369.0
# the meta's range wider than the bar's: widened; an epoch time works too
r = raw()
m2 = dict(meta, regularMarketDayHigh=1390.0, regularMarketDayLow=1350.0,
          regularMarketTime=int(pd.Timestamp("2026-11-20 10:00:00", tz="UTC").timestamp()))
assert dm.fill_last_session(r, m2) and r["High"].iloc[-1] == 1390.0 and r["Low"].iloc[-1] == 1350.0
# not the same day / no open / already finished / no price: left alone
r = raw()
assert not dm.fill_last_session(r, dict(meta, regularMarketTime=pd.Timestamp("2026-11-23 10:00", tz=IST)))
assert np.isnan(r["Close"].iloc[-1])
r = raw(last_open=np.nan)
assert not dm.fill_last_session(r, meta) and np.isnan(r["Close"].iloc[-1]), "no open: nothing is made up"
r = raw(last_close=1377.5)
assert not dm.fill_last_session(r, dict(meta, regularMarketPrice=9999.0)) and r["Close"].iloc[-1] == 1377.5
assert not dm.fill_last_session(raw(), {})


# the whole daily path: adjusted like auto_adjust, the newest bar kept
class FakeTicker:
    def __init__(self, sym):
        self.sym = sym
        self.history_metadata = {}

    def history(self, period=None, interval=None, auto_adjust=True, **_k):
        assert interval == "1d" and auto_adjust is False, "unadjusted, so the open of the newest bar survives"
        self.history_metadata = meta
        return raw()


class FakeYF:
    Ticker = FakeTicker


# as TradingView shows them (ADJ off): adjusted for splits only - Yahoo's own Close - not for the dividend
assert dm.ADJUST_DIVIDENDS is False
out = dm._yahoo_daily(FakeYF, "RELIANCE.NS", "1mo")
assert "Adj Close" not in out.columns and len(out) == 3
assert out["Open"].iloc[0] == 1300.0 and out["Close"].iloc[0] == 1305.0, "the 18th as it traded"
assert out["Close"].iloc[-1] == 1377.5 and out["Open"].iloc[-1] == 1369.0, "Friday is there"
# ADJ on: also for dividends, as yfinance's auto_adjust
dm.ADJUST_DIVIDENDS = True
try:
    adj = dm._yahoo_daily(FakeYF, "RELIANCE.NS", "1mo")
finally:
    dm.ADJUST_DIVIDENDS = False
k = 1300.0 / 1305.0                                            # the 18th, before the dividend
assert abs(adj["Open"].iloc[0] - 1300.0 * k) < 1e-9 and abs(adj["High"].iloc[0] - 1310.0 * k) < 1e-9
assert adj["Close"].iloc[0] == 1300.0 and adj["Close"].iloc[-1] == 1377.5
out = adj.copy()                                               # (the checks below read the adjusted frame)
df = dm.standardise(out)
assert len(df) == 3 and df.index[-1] == pd.Timestamp("2026-11-20"), df.index

# load_yahoo takes this path for daily bars
seen = []
real = dm._yahoo_daily
dm._yahoo_daily = lambda yf, sym, period: seen.append((sym, period)) or raw().assign(Close=[1305.0, 1375.6, 1377.5])
try:
    import tempfile
    dm.USER_DIR = __import__("pathlib").Path(tempfile.mkdtemp())
    got, label = dm.load_yahoo("RELIANCE", "1d", "1mo")
    assert seen and seen[0][0] == "RELIANCE.NS" and got.index[-1] == pd.Timestamp("2026-11-20")
finally:
    dm._yahoo_daily = real

# ---------------------------------------------------------------- the watchlist's quotes: the same bar, finished
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["CHARTLAB_OFFLINE"] = "1"
from PySide6 import QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from chartlab import watchlist as W  # noqa: E402

naive = pd.DatetimeIndex(["2026-11-18", "2026-11-19", "2026-11-20"])                  # (yf.download's daily index)
fri_1528 = pd.Timestamp("2026-11-20 15:28:10", tz=IST).timestamp()


def day5(close=np.nan):
    return pd.DataFrame({"Open": [1300.0, 1361.9, 1369.0], "High": [1310.0, 1382.5, 1383.0],
                         "Low": [1290.0, 1349.7, 1356.1], "Close": [1305.0, 1375.6, close],
                         "Adj Close": [1305.0, 1375.6, close], "Volume": [1e6, 14486599.0, 13167730.0]}, index=naive)


assert W._unfinished(day5()) and not W._unfinished(day5(1377.5))
h = W.finish_bar(day5(), {"price": 1377.5, "time": fri_1528}, "RELIANCE")
assert h["Close"].iloc[-1] == 1377.5 and h["High"].iloc[-1] == 1383.0 and h["Low"].iloc[-1] == 1356.1
assert np.isnan(W.finish_bar(day5(), {"price": 1390.0, "time": fri_1528 + 3 * 86400}, "RELIANCE")["Close"].iloc[-1]), \
    "a price from another day does not finish Friday's bar"
assert np.isnan(W.finish_bar(day5(), None, "RELIANCE")["Close"].iloc[-1])
assert W.finish_bar(day5(), {"price": 1390.0, "time": fri_1528}, "RELIANCE")["High"].iloc[-1] == 1390.0


class FakeYF2:
    """yf.download for the batch: RELIANCE's newest bar unfinished, TCS's finished."""

    @staticmethod
    def download(syms, **_k):
        rel = day5()
        tcs = day5(2376.2).assign(Close=[2379.5, 2342.7, 2376.2], **{"Adj Close": [2379.5, 2342.7, 2376.2]})
        return pd.concat({"RELIANCE.NS": rel, "TCS.NS": tcs}, axis=1)

    class Ticker:
        def __init__(self, s):
            raise RuntimeError("not needed")


sys.modules["yfinance"] = FakeYF2
asked = []
real_spark = W.spark_prices
W.spark_prices = lambda ys, **_k: asked.append(list(ys)) or {"RELIANCE.NS": {"price": 1377.5, "time": fri_1528,
                                                                             "prev": 1375.6}}
try:
    w = W.QuoteWorker(["RELIANCE", "TCS"])
    got = {}
    w.done.connect(lambda q, e: got.update(q=q, e=e))
    w.run()
finally:
    W.spark_prices = real_spark
assert asked == [["RELIANCE.NS"]], "one request, only for the unfinished bars"
px, ch = got["q"]["RELIANCE"]
assert px == 1377.5 and abs(ch - (1377.5 / 1375.6 - 1)) < 1e-12, "Friday's close against Thursday's"
assert got["e"]["RELIANCE"]["prev"] == 1375.6 and got["e"]["RELIANCE"]["high"] == 1383.0
assert got["q"]["TCS"][0] == 2376.2 and got["e"]["TCS"]["prev"] == 2342.7

# a live round brings a stock's own previous close - it wins over an older one
wl = W.WatchlistWidget()
wl.set_symbols(["RELIANCE", "NIFTY"])
wl.extra["RELIANCE"] = {"prev": 1340.0}                       # stale (the day before yesterday's)
wl.extra["NIFTY"] = {"prev": 25514.3}
wl._spark_in({"RELIANCE": {"price": 1377.5, "time": fri_1528, "prev": 1375.6},
              "NIFTY": {"price": 25800.6, "time": fri_1528, "prev": None}})
assert wl.extra["RELIANCE"]["prev"] == 1375.6 and abs(wl.quotes["RELIANCE"][1] - (1377.5 / 1375.6 - 1)) < 1e-12
assert wl.extra["NIFTY"]["prev"] == 25514.3, "an index keeps the known close (Yahoo's is a day old there)"

# ---------------------------------------------------------------- Performance tiles: TradingView's rule
# from the OPEN of the first bar on or after the start (7 / 30 / 91 / 182 / 365 days back, 1 January) to the last
# close; the newest bar finished from the meta first
days = pd.bdate_range("2025-10-01", "2026-11-20")
opens = np.arange(len(days), dtype=float) + 1000.0
year = pd.DataFrame({"Open": opens, "High": opens + 5, "Low": opens - 5, "Close": opens + 1, "Volume": 1e5},
                    index=days.tz_localize(IST))
year.loc[year.index[-1], "Close"] = np.nan                           # Friday unfinished


class FakeYF3:
    class Ticker:
        def __init__(self, s):
            self.history_metadata = {}

        def history(self, period=None, interval=None, auto_adjust=True, **_k):
            assert auto_adjust is False, "split-adjusted only, like TradingView"
            self.history_metadata = dict(meta, regularMarketPrice=2500.0, regularMarketDayHigh=2600.0,
                                         regularMarketDayLow=1000.0)
            return year.copy()


sys.modules["yfinance"] = FakeYF3
pw = W.PerfWorker("RELIANCE")
res = {}
pw.done.connect(lambda sym, d: res.update(d))
pw.run()
perf = res["perf"]


def want(cut):
    o = year["Open"]
    return (2500.0 / float(o[o.index >= pd.Timestamp(cut, tz=IST)].iloc[0]) - 1) * 100


assert abs(perf["1W"] - want("2026-11-13")) < 1e-9, (perf["1W"], want("2026-11-13"))
assert abs(perf["1M"] - want("2026-10-21")) < 1e-9
assert abs(perf["3M"] - want("2026-08-21")) < 1e-9 and abs(perf["6M"] - want("2026-05-22")) < 1e-9
assert abs(perf["YTD"] - want("2026-01-01")) < 1e-9 and abs(perf["1Y"] - want("2025-11-20")) < 1e-9
assert res["hi52"] == 2600.0, "Friday's bar, finished, is in the 52-week range"

print("DAILY FILL OK")
sys.stdout.flush()
os._exit(0)
