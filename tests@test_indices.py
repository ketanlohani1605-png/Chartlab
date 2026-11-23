"""Every index and every company, named and sourced the same way everywhere:

- one table of NSE / BSE indices (chartlab/indices.py): each name people type (NIFTY IT, CNXIT, ^CNXIT, NSE:NIFTYIT)
  means one index; Yahoo's ticker, TradingView's name, Dhan's id come from it; the watchlist's sections, the
  search's kinds, the exchange and the legend's name all agree
- indices Yahoo keeps no daily bars of get daily bars built from its hourly ones; the newest day is Yahoo's official
  daily row and the day before it takes Yahoo's previous close; their weekly / monthly bars come from those
- an index has no volume (Yahoo's numbers for it are not TradingView's)
- previous closes: a stock's is Yahoo's; an index with daily bars takes it from them; one without takes Yahoo's
- an intraday chart of an index takes the official previous close when it has it
- a broker finds every index by its own way of writing it (Dhan by its id)
- company names are NSE's ('ABB India Limited', not 'ABB Limited'); sectors are NSE's; the NIFTY 50 is NSE's
  current list (BSE in, Wipro out); an old symbol opens the new one (ZOMATO -> ETERNAL); ETFs are ETFs
- the Market panel lists the sectors, biggest move first
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
from chartlab import data as dm, dhan_core, indices as IX, symbols as SY, theme, tvui, watchlist as W  # noqa: E402
from chartlab.brokers import base as BB  # noqa: E402

theme.apply_pg_config()
app.setStyle(theme.make_style())
app.setStyleSheet(theme.STYLESHEET)

# ---------------------------------------------------------------- the table
assert len(IX.INDICES) >= 40 and len({i.sym for i in IX.INDICES}) == len(IX.INDICES)
assert len({i.yahoo for i in IX.INDICES}) == len(IX.INDICES), "one Yahoo ticker per index"
for typed, want in (("NIFTY IT", "NIFTYIT"), ("CNXIT", "NIFTYIT"), ("^CNXIT", "NIFTYIT"), ("NSE:NIFTYIT", "NIFTYIT"),
                    ("nifty auto", "NIFTYAUTO"), ("NIFTY_MID_SELECT", "MIDCPNIFTY"), ("NIFTY FIN SERVICE", "FINNIFTY"),
                    ("NIFTY OIL & GAS", "NIFTYOILGAS"), ("BSE:SENSEX", "SENSEX"), ("INDIA VIX", "INDIAVIX"),
                    ("NIFTYJR", "NIFTYNXT50"), ("CNX500", "NIFTY500")):
    assert IX.canonical(typed) == want, (typed, IX.canonical(typed))
for not_index in ("RELIANCE", "M&M", "BANKBEES", "TCS", "ETERNAL", ""):
    assert not IX.is_index(not_index), not_index
for ix in IX.INDICES:
    assert ix.name.endswith("Index") and ix.yahoo, ix
    assert dm.normalize_symbol(ix.sym) == ix.yahoo, ix.sym
    assert W.group_of(ix.sym) == "INDICES" and tvui._kind(ix.sym) == "Indices", ix.sym
    assert W.exchange_of(ix.sym) == ix.exchange and tvui._exch(ix.sym) == ix.exchange, ix.sym
    assert tvui.symbol_name(ix.sym) == ix.name and W.kind_of(ix.sym) == "Index", ix.sym
    if ix.dhan:
        assert dhan_core.INDEX_IDS[ix.sym] == ix.dhan, ix.sym
    sp = BB.parse_symbol(ix.sym)
    assert sp.kind == "IDX" and sp.name == ix.sym and sp.exchange == ix.exchange, (ix.sym, sp)
assert dhan_core.INDEX_IDS["NIFTYIT"] == "29" and dhan_core.INDEX_IDS["CNXIT"] == "29"
assert dhan_core.INDEX_IDS["NIFTY"] == "13" and dhan_core.INDEX_IDS["MIDCPNIFTY"] == "442"
assert "BANKNIFTY" in IX.SECTORS and "NIFTYIT" in IX.SECTORS and "NIFTY" not in IX.SECTORS

# ---------------------------------------------------------------- a broker's own index names
m = pd.DataFrame({"token": ["14", "29", "15", "13"], "exchange": "NSE", "kind": "IDX",
                  "tsym": ["NIFTY AUTO", "NIFTYIT", "Nifty Private Bank", "NIFTY"],
                  "name": ["NIFTY AUTO", "NIFTYIT", "NIFTY PVT BANK", "NIFTY"], "lot": 1, "tick": 0.05, "seg": "IDX_I",
                  "expiry": pd.NaT, "strike": 0.0, "instrument": "INDEX"})
for typed, token in (("NIFTYAUTO", "14"), ("NIFTY IT", "29"), ("NIFTYPVTBANK", "15")):
    assert BB.lookup(m, typed, "Kite").token == token, typed
m2 = m.assign(tsym=["x", "y", "z", "w"], name=["x", "y", "z", "w"])
assert BB.lookup(m2, "NIFTYAUTO", "Dhan").token == "14", "Dhan by its security id"

# ---------------------------------------------------------------- daily bars built from hours
IST = "Asia/Kolkata"
hours = []
for d in pd.bdate_range("2026-11-16", "2026-11-20"):
    for h, px in ((9, 100.0), (10, 102.0), (11, 99.0), (15, 101.0)):
        hours.append((pd.Timestamp(d).tz_localize(IST) + pd.Timedelta(hours=h, minutes=15), px + d.day))
H = pd.DataFrame({"Open": [p for _, p in hours], "High": [p + 1 for _, p in hours], "Low": [p - 1 for _, p in hours],
                  "Close": [p + 0.5 for _, p in hours], "Volume": 0.0}, index=pd.DatetimeIndex([t for t, _ in hours]))
asked = []


class FakeT:
    def __init__(self, s):
        self.s, self.history_metadata = s, {}

    def history(self, period=None, interval=None, auto_adjust=True, **_k):
        asked.append((self.s, period, interval))
        if interval == "1h":
            return H.copy()
        if interval == "1d":                                     # one row: the newest day, official
            self.history_metadata = {"chartPreviousClose": 118.75, "regularMarketPrice": 125.0,
                                     "regularMarketTime": pd.Timestamp("2026-11-20 15:30", tz=IST)}
            return pd.DataFrame({"Open": [120.0], "High": [126.0], "Low": [119.0], "Close": [125.0], "Volume": [0.0],
                                 "Adj Close": [125.0]}, index=pd.DatetimeIndex([pd.Timestamp("2026-11-20", tz=IST)]))
        return pd.DataFrame()


class FakeYF:
    Ticker = FakeT


g = dm._yahoo_daily_from_hours(FakeYF, "^CNXMEDIA", "1mo")
assert list(g.index.normalize().day) == [16, 17, 18, 19, 20], g.index
first = g.iloc[0]
assert first["Open"] == 116.0 and first["High"] == 119.0 and first["Low"] == 114.0 and first["Close"] == 117.5
last = g.iloc[-1]
assert (last["Open"], last["High"], last["Low"], last["Close"]) == (120.0, 126.0, 119.0, 125.0), "the official day"
assert g.iloc[-2]["Close"] == 118.75, "the day before: Yahoo's previous close"
assert ("^CNXMEDIA", "37d", "1h") in asked or any(a[2] == "1h" for a in asked)
real_yf = sys.modules.get("yfinance")
sys.modules["yfinance"] = FakeYF
real_dir = dm.USER_DIR
dm.USER_DIR = __import__("pathlib").Path(tempfile.mkdtemp())
try:
    df, _ = dm.load_yahoo("NIFTYMEDIA", "1d", "1mo")
    assert len(df) == 5 and df["Close"].iloc[-1] == 125.0 and (df["Volume"] == 0).all()
    wk, _ = dm.load_yahoo("NIFTY MEDIA", "1wk", "1mo")
    assert len(wk) == 1 and wk["Close"].iloc[-1] == 125.0 and wk["High"].iloc[-1] == 126.0, wk
finally:
    dm.USER_DIR = real_dir
    if real_yf is not None:
        sys.modules["yfinance"] = real_yf
x = pd.DataFrame({"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [5.0]})
assert dm.no_index_volume(x, "^NSEI")["Volume"].iloc[0] == 0 and dm.no_index_volume(x, "RELIANCE.NS")["Volume"].iloc[0] == 5

# ---------------------------------------------------------------- previous closes, each kind its own way
fri = pd.Timestamp("2026-11-20 15:29", tz=IST).timestamp()
thu, wed = fri - 86400, fri - 2 * 86400


class FakeResp:
    def __init__(self, j):
        self.j = j

    def raise_for_status(self):
        pass

    def json(self):
        return self.j


calls = []


class FakeSession:
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, timeout=None):
        calls.append(params["interval"])
        out = {}
        for ys in params["symbols"].split(","):
            if params["interval"] == "1m":
                out[ys] = {"timestamp": [fri], "close": [200.0], "chartPreviousClose": 150.0, "previousClose": 150.0}
            else:                                                # daily bars: Wed, Thu, Fri
                out[ys] = {"timestamp": [wed, thu, fri], "close": [180.0, 190.0, 200.0]}
        return FakeResp(out)


import requests  # noqa: E402

real_session = requests.Session
requests.Session = FakeSession
W._IDX_PREV.clear()
try:
    got = W.spark_prices(["^NSEI", "^CNXMEDIA", "RELIANCE.NS", "^GSPC"])
finally:
    requests.Session = real_session
assert got["^NSEI"]["prev"] == 190.0, "an index with daily bars: Thursday's daily close"
assert got["^CNXMEDIA"]["prev"] == 150.0, "an index without: Yahoo's own previous close"
assert got["RELIANCE.NS"]["prev"] == 150.0 and got["^GSPC"]["prev"] is None
assert calls.count("1d") == 1, "one more request, for the daily indices only"

# the watchlist's slower quotes: one day of daily history -> the live answer's previous close
one = pd.DataFrame({"Open": [14107.9], "High": [14350.4], "Low": [14079.0], "Close": [14305.15],
                    "Adj Close": [14305.15], "Volume": [0.0]}, index=pd.DatetimeIndex(["2026-11-20"]))


class FakeYF2:
    @staticmethod
    def download(syms, **_k):
        return one.copy()

    class Ticker:
        def __init__(self, s):
            raise RuntimeError("not needed")


sys.modules["yfinance"] = FakeYF2
real_sp = W.spark_prices
W.spark_prices = lambda ys, **_k: {y: {"price": 14305.15, "time": fri, "prev": 14206.05} for y in ys}
try:
    w = W.QuoteWorker(["MIDCPNIFTY"])
    res = {}
    w.done.connect(lambda q, e: res.update(q=q, e=e))
    w.run()
finally:
    W.spark_prices = real_sp
    if real_yf is not None:
        sys.modules["yfinance"] = real_yf
px, ch = res["q"]["MIDCPNIFTY"]
assert px == 14305.15 and abs(ch - (14305.15 / 14206.05 - 1)) < 1e-12, "not 0.00 %"

# ---------------------------------------------------------------- companies
assert SY.name_of("ABB") == "ABB India Limited" and SY.name_of("APOLLOHOSP") == "Apollo Hospitals Enterprise Limited"
assert SY.name_of("BRAHMVIRAT") == "Brahm Virat Industries Corporation Limited", "all-caps names put in title case"
assert not [s for s in SY.nse_symbols() if "NSETEST" in s] and len(SY.nse_symbols()) > 3000
assert SY.sector_of("HDFCBANK") == "Financial Services" and SY.sector_of("TCS") == "Information Technology"
assert W.kind_of("RELIANCE") == "Oil Gas & Consumable Fuels" and W.kind_of("NIFTYBEES") == "ETF"
assert SY.is_etf("BANKCASE") and not SY.variable_tick("BANKCASE") and SY.variable_tick("RELIANCE")
n50 = SY.nifty50()
assert len(n50) == 50 and "BSE" in n50 and "WIPRO" not in n50
from chartlab import market as M  # noqa: E402

assert M.NIFTY50 == n50
assert dm.normalize_symbol("ZOMATO") == "ETERNAL.NS" and dm.normalize_symbol("MCDOWELL-N") == "UNITDSPR.NS"
assert dm.normalize_symbol("TATAMOTORS") == "TMPV.NS" and dm.normalize_symbol("RELIANCE") == "RELIANCE.NS"

# ---------------------------------------------------------------- the app
from chartlab.mainwindow import MainWindow  # noqa: E402

win = MainWindow(QtCore.QSettings(tempfile.mktemp(suffix=".ini"), QtCore.QSettings.Format.IniFormat))
win.resize(1411, 786)
win.show()
QtTest.QTest.qWait(200)
# 'NIFTY IT' typed: the chart opens NIFTYIT
loaded = []
real_load = win.load_into
win.load_into = lambda panel, src, sym, iv, rng, path="": loaded.append(sym)
win.symbol.setText("nifty it")
win.on_load()
win.load_into = real_load
assert loaded == ["NIFTYIT"] and win.symbol.text() == "NIFTYIT", loaded
# an old symbol opens the new one, and says so
win.load_into = lambda panel, src, sym, iv, rng, path="": loaded.append(sym)
win.symbol.setText("ZOMATO")
win.on_load()
win.load_into = real_load
assert loaded[-1] == "ETERNAL" and win.symbol.text() == "ETERNAL", loaded
# the watchlist takes the same names
wl = win.watchlist
assert wl.add("MCDOWELL-N") and "UNITDSPR" in wl.symbols() and "MCDOWELL-N" not in wl.symbols()
assert wl.add("CNX AUTO".replace(" ", "")) and "NIFTYAUTO" in wl.symbols()

# search: an alias finds its index first; the Indices chip lists them all
d = tvui.SymbolSearch(win, ["RELIANCE"], "")
d.edit.setText("CNXIT")
d._fill()
assert d.lst.item(0).data(tvui.ROLE) == "NIFTYIT", d.lst.item(0).data(tvui.ROLE)
d.edit.setText("")
d._set_kind("Indices")
shown = {d.lst.item(i).data(tvui.ROLE) for i in range(d.lst.count())}
assert set(IX.all_symbols()) <= shown, set(IX.all_symbols()) - shown
d.close()

# an intraday index chart: the official previous close
p = win.active
idx = pd.date_range("2026-11-19 09:15", periods=375, freq="1min").append(
    pd.date_range("2026-11-20 09:15", periods=10, freq="1min"))
c = np.linspace(900, 922.8, len(idx))
bars = pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": 0.0}, index=idx)
p.params.update(symbol="NIFTYREALTY", source="yahoo", interval="1m")
p.set_dataframe(bars, "^CNXREALTY")
QtTest.QTest.qWait(50)
ch = p.chart
k = 374
assert abs(ch.day_prev_close() - float(bars["Close"].iloc[k])) < 1e-9, "no official close known: the last minute"
ch.day_prev = 923.10
assert ch.day_prev_close() == 923.10, "an index: the exchange's close"
ch.day_prev = 500.0
assert abs(ch.day_prev_close() - float(bars["Close"].iloc[k])) < 1e-9, "a close that cannot be yesterday's: ignored"
p.params.update(symbol="RELIANCE")
p.set_dataframe(bars, "RELIANCE.NS")
ch.day_prev = 923.10
assert abs(ch.day_prev_close() - float(bars["Close"].iloc[k])) < 1e-9, "a stock's last minute is its close"

# TradingView's ADJ: off by default (splits only); on adjusts for dividends and reloads the charts
assert not win.adj_btn.isChecked() and dm.ADJUST_DIVIDENDS is False and "splits only" in win.adj_btn.toolTip()
reloads = []
for pn in win.panels:
    pn.load = lambda *a, **k: reloads.append(a)
win.active.params.update(source="yahoo", symbol="RELIANCE", interval="1d", range="2y")
win.adj_btn.click()
assert dm.ADJUST_DIVIDENDS is True and win.settings.value("adj_dividends") == "true" and reloads, reloads
assert reloads[-1][:2] == ("yahoo", "RELIANCE") and "dividend" in win.adj_btn.toolTip()
assert dm._cache_file("RELIANCE.NS", "1d", "2y").name.endswith("_div.parquet"), "kept apart from split-only bars"
win.adj_btn.click()
assert dm.ADJUST_DIVIDENDS is False and dm._cache_file("RELIANCE.NS", "1d", "2y").name.endswith("_spl.parquet")

# ---------------------------------------------------------------- Market: the sectors
mk = M.MarketPanel(symbols=["RELIANCE", "TCS"])
mk.resize(340, 640)
mk.show()
mk.set_data({"RELIANCE": (1377.5, 0.14)}, {"NIFTYIT": (31322.65, 1.304), "NIFTYMETAL": (12271.1, -0.233),
                                            "BANKNIFTY": (64635.2, 2.493), "NIFTYENERGY": (37321.25, -0.225)})
mk.chips["Sectors"].click()
QtTest.QTest.qWait(30)
assert mk.stack.currentIndex() == 2
items = [(mk.sec_list.item(i).data(M.ROLE_HEAD), mk.sec_list.item(i).data(M.ROLE_SYM)) for i in range(mk.sec_list.count())]
assert items == [("SECTORS", None), (None, "BANKNIFTY"), (None, "NIFTYIT"), (None, "NIFTYMETAL"),
                 ("THEMES", None), (None, "NIFTYENERGY")], items
assert (mk.breadth.up, mk.breadth.down) == (2, 2)
assert M.short_name("NIFTYIT") == "IT" and M.short_name("FINNIFTY") == "Financial Services"
assert M.short_name("NIFTYINDDEFENCE") == "Defence"
picked = []
mk.chosen.connect(picked.append)
QtTest.QTest.mouseClick(mk.sec_list.viewport(), QtCore.Qt.MouseButton.LeftButton,
                        pos=mk.sec_list.visualItemRect(mk.sec_list.item(2)).center())
assert picked == ["NIFTYIT"], picked
mk.sec_list.viewport().grab()
mk._got({"NIFTYPHARMA": {"price": 28386.9, "time": time.time(), "prev": 28371.8},
         "RELIANCE": {"price": 1377.5, "time": time.time(), "prev": 1375.6}})
assert abs(mk.sector_data["NIFTYPHARMA"][1] - (28386.9 / 28371.8 - 1) * 100) < 1e-9 and "RELIANCE" in mk.data

print("INDICES OK")
sys.stdout.flush()
os._exit(0)
