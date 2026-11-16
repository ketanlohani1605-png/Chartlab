"""v0.27: option charts (symbols, contracts, candles), real-time ticks (binary feed, polling fallback, in-place last
bar, new bars), option chain panel, Options tab in symbol search. Everything runs against a fake Dhan."""
import os, sys, json, struct, tempfile, time, datetime as dt
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, ".")
sys.path.insert(0, "tests")
from pathlib import Path
import numpy as np, pandas as pd
from PySide6 import QtWidgets, QtCore, QtTest, QtNetwork, QtWebSockets
from chartlab import theme
theme.apply_pg_config()
app = QtWidgets.QApplication([])
app.setStyleSheet(theme.STYLESHEET)
import chartlab.dhan_core as D
from chartlab import accounts as A, live as L
import fake_opts

tmp = Path(tempfile.mkdtemp())
A.PATH = tmp / "accounts.json"
A._CANDLE_DIR = tmp / "candles"
D._OPT, D._IDS = tmp / "opt.pkl", tmp / "ids.json"
MASTER, EXPS = fake_opts.master()
D._master = lambda: MASTER
calls = []
IST = 19800


def pump(ms=40):
    QtTest.QTest.qWait(ms)


def wait(cond, ms=4000):
    end = time.time() + ms / 1000
    while time.time() < end:
        if cond():
            return True
        pump(20)
    return bool(cond())


def last_sessions(n_days):
    d, out = dt.date.today() - dt.timedelta(days=1), []      # finished sessions only: no bar lies in the future
    while len(out) < n_days:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return sorted(out)


def fake_http(url, body, headers, timeout=30):
    calls.append((url, dict(body or {})))
    if url.endswith("/charts/intraday"):
        step = int(body["interval"]) * 60
        f, t = (dt.datetime.strptime(body[k][:10], "%Y-%m-%d").date() for k in ("fromDate", "toDate"))
        ts = []
        for d in last_sessions(4):
            if f <= d <= t:
                base = int(pd.Timestamp(d).timestamp()) - IST + 9 * 3600 + 15 * 60       # 09:15 India time, as UTC
                ts += [base + i * step for i in range(int(375 * 60 / step))]
        n = len(ts)
        px = 100 + np.cumsum(np.sin(np.arange(n) / 7.0))
        return json.dumps({"open": list(px), "high": list(px + 2), "low": list(px - 2), "close": list(px + 1),
                           "volume": [1000] * n, "timestamp": ts}).encode()
    if url.endswith("/charts/historical"):
        days = pd.bdate_range(end=dt.date.today(), periods=30)
        ts = [int(pd.Timestamp(d).timestamp()) - IST + 4 * 3600 for d in days]
        n = len(ts)
        return json.dumps({"open": [100.0] * n, "high": [105.0] * n, "low": [95.0] * n, "close": [101.0] * n,
                           "volume": [5000] * n, "timestamp": ts}).encode()
    if url.endswith("/optionchain/expirylist"):
        return json.dumps({"data": [str(e) for e in EXPS], "status": "success"}).encode()
    if url.endswith("/optionchain"):
        oc = {}
        for k in range(-20, 21):
            s = 25000 + 50 * k
            oc[f"{s:.6f}"] = {"ce": {"last_price": max(25030 - s, 0) + 40.0, "oi": 120000 + k, "volume": 9000,
                                     "implied_volatility": 12.5, "previous_close_price": max(25030 - s, 0) + 38.0,
                                     "top_bid_price": 1.0, "top_ask_price": 2.0, "greeks": {"delta": 0.5}},
                              "pe": {"last_price": max(s - 25030, 0) + 35.0, "oi": 90000, "volume": 7000,
                                     "implied_volatility": 13.0, "previous_close_price": max(s - 25030, 0) + 37.0,
                                     "top_bid_price": 1.0, "top_ask_price": 2.0, "greeks": {"delta": -0.5}}}
        return json.dumps({"data": {"last_price": 25030.0, "oc": oc}, "status": "success"}).encode()
    if url.endswith("/marketfeed/ltp"):
        return json.dumps({"data": {seg: {str(i): {"last_price": 111.5} for i in ids} for seg, ids in body.items()},
                           "status": "success"}).encode()
    raise RuntimeError("unexpected url " + url)


A._http = D._http = fake_http
acct = {"name": "main", "broker": "dhan", "client_id": "1000001", "access_token": "tok", "live": False}

# ------------------------------------------------------------------ 1) ways to write an option
con = D.resolve_option("NIFTY 25000 CE")
assert con["expiry"] == EXPS[0] and con["segment"] == "NSE_FNO" and con["instrument"] == "OPTIDX" and con["lot"] == 75
canon = con["symbol"]
assert canon == f"NIFTY{EXPS[0]:%y%m%d}C25000" and D.resolve_option(canon)["security_id"] == con["security_id"]
e2 = EXPS[1]
for text in (f"nifty {e2.day}{e2:%b} 25100 pe", f"NIFTY {e2.day} {e2:%b} {e2.year} 25100 PE", f"NIFTY {e2} 25100 PUT",
             f"NIFTY{e2.day}{e2:%b}{e2:%y}25100PE", f"NSE:NIFTY {e2.day}/{e2.month} 25100 p"):
    c = D.resolve_option(text.upper() if " " not in text else text)
    assert (c["expiry"], c["strike"], c["kind"]) == (e2, 25100.0, "PE"), (text, c)
assert D.resolve_option("SENSEX 82000 CE")["segment"] == "BSE_FNO"
for plain in ("RELIANCE", "NIFTY", "BTC-USD", "360ONE", "^NSEI", "RELIANCE.NS", "US:C", "EURUSD=X", "NIFTY 25000"):
    assert not D.is_option(plain), plain
for bad, word in (("NIFTY 25025 CE", "Nearest strikes"), ("NIFTY 1JAN 25000 CE", "Upcoming"), ("FOO 100 CE", "no options")):
    try:
        D.resolve_option(bad)
        raise SystemExit("should not resolve: " + bad)
    except RuntimeError as e:
        assert word in str(e), e

# ------------------------------------------------------------------ 2) candles of an option, 3m / 30m built locally
df = A.fetch_dhan(acct, "NIFTY 25000 CE", "5m", "1mo")
body = next(b for u, b in calls if u.endswith("/intraday"))
assert (body["securityId"], body["exchangeSegment"], body["instrument"]) == (con["security_id"], "NSE_FNO", "OPTIDX")
assert df["Date"].iloc[0].time() == dt.time(9, 15) and len(df) == 4 * 75
d30 = A.fetch_dhan(acct, "NIFTY 25000 CE", "30m", "1mo")
assert [t.strftime("%H:%M") for t in d30["Date"].iloc[:3]] == ["09:15", "09:45", "10:15"] and len(d30) == 4 * 13
assert all(b["interval"] in ("5", "15") for u, b in calls if u.endswith("/intraday"))
d3 = A.fetch_dhan(acct, "RELIANCE", "3m", "5d")
assert d3["Date"].iloc[1] - d3["Date"].iloc[0] == pd.Timedelta(minutes=3) and d3["Volume"].iloc[0] == 3000
A._MEM.clear()

# ------------------------------------------------------------------ 3) the binary feed
def hdr(code, size, seg, sid):
    return struct.pack("<BHBI", code, size, seg, sid)


tick_pkt = hdr(2, 16, 0, 13) + struct.pack("<fI", 25031.5, 1700000000)
quote_pkt = hdr(4, 50, 2, 40041) + struct.pack("<fHIfIIIffff", 123.45, 75, 1700000001, 120.0, 54321, 10, 20, 110.0,
                                                0.0, 130.0, 105.0)
prev_pkt = hdr(6, 16, 2, 40041) + struct.pack("<fI", 118.0, 999)
bye_pkt = hdr(50, 10, 0, 0) + struct.pack("<H", 806)
out = L.parse_packets(tick_pkt + quote_pkt + prev_pkt + bye_pkt)
assert [m["kind"] for m in out] == ["tick", "tick", "prev", "bye"], out
assert out[0]["seg"] == "IDX_I" and out[0]["sid"] == "13" and abs(out[0]["ltp"] - 25031.5) < 1e-3
assert out[1]["seg"] == "NSE_FNO" and out[1]["vol"] == 54321 and abs(out[1]["high"] - 130) < 1e-6
assert abs(out[2]["prev"] - 118) < 1e-6 and out[3]["code"] == 806
assert L.parse_packets(quote_pkt[:30]) == [] and L.parse_packets(b"") == []

# trade time: learnt from a fresh trade whichever way Dhan counts it
now = 1_800_000_000.0
for stamp in (now - 2, now + IST - 2):                       # UTC seconds / India-clock seconds
    ck = L.Clock()
    assert abs(ck.chart_time(stamp, now) - (now + IST - 2)) < 1e-6
    assert abs(ck.chart_time(stamp - 3600, now) - (now + IST - 3602)) < 1e-6
assert L.Clock().chart_time(0, now) is None and abs(L.Clock().chart_time(-now, now) - (now + IST)) < 1e-6

# ------------------------------------------------------------------ 4) the window: option chart end to end
from chartlab.mainwindow import MainWindow, quit_cleanly
from chartlab.live import LiveFeed

srv = QtWebSockets.QWebSocketServer("fake-dhan", QtWebSockets.QWebSocketServer.SslMode.NonSecureMode)
assert srv.listen(QtNetwork.QHostAddress.SpecialAddress.LocalHost, 0)
LiveFeed.URL = f"ws://127.0.0.1:{srv.serverPort()}"
socks, subs, urls = [], [], []


def on_conn():
    s = srv.nextPendingConnection()
    socks.append(s)
    urls.append(s.requestUrl().toString())
    s.textMessageReceived.connect(lambda m: subs.append(json.loads(m)))


srv.newConnection.connect(on_conn)

w = MainWindow(QtCore.QSettings(str(tmp / "s.ini"), QtCore.QSettings.Format.IniFormat))
w.resize(1500, 900)
w.show()
pump(100)
p = w.active
ch = p.chart

# no account yet: a clear message, not a Yahoo error
p.load("auto", "NIFTY 25000 CE", "5m", "1mo")
assert wait(lambda: not p.loading)
assert "Dhan account" in p.last_error and "Yahoo" in p.last_error, p.last_error

A.save_accounts([acct])
w.watchlist.set_symbols(["NIFTY", "RELIANCE", "BTC-USD"])
w.symbol.setText("NIFTY 25000 CE")
w.interval_btns["5m"].click()
assert wait(lambda: not p.loading and p.df is not None and p.label.startswith(canon)), (p.label, p.last_error)
assert p.params["symbol"] == canon                           # remembered as that exact contract
assert ch.ticker() == canon and ch.exchange_tag() == "NSE"
assert ch.display_name() == D.option_name("NIFTY", EXPS[0], 25000, "CE") and "25000 CE" in ch.display_name()
n0 = len(p.df)

# the live socket: subscribed to the option (quote mode), the index and the stock; BTC-USD is not on Dhan
def subscribed():
    out = {}
    for m in subs:
        out.setdefault(m["RequestCode"], set()).update((i["ExchangeSegment"], i["SecurityId"]) for i in m["InstrumentList"])
    return out


assert wait(lambda: len(subscribed().get(17, ())) >= 2 and w.live.feed.mode == "live", 6000), (subs, w.live.feed.mode)
assert "token=tok" in urls[0] and "clientId=1000001" in urls[0] and "version=2" in urls[0]
by_code = subscribed()
assert by_code[15] == {("IDX_I", "13")}, by_code
assert by_code[17] == {("NSE_FNO", con["security_id"]), ("NSE_EQ", "2885")}, by_code
assert w.live_label.isVisible() and "Live" in w.live_label.text()

# a trade inside the newest bar: only that bar changes, nothing is rebuilt
candles = ch.candles
last_t = float(ch._times[-1])
close0, high0 = float(ch._closes[-1]), float(ch._highs[-1])
sid = int(con["security_id"])
w.live.clock.shift = 0                                       # this fake feed stamps India-clock seconds


def quote(price, t, vol, s=sid, seg=2):
    return hdr(4, 50, seg, s) + struct.pack("<fHIfIIIffff", price, 75, int(t), price, vol, 1, 1, 1.0, 0.0, 1.0, 1.0)


socks[0].sendBinaryMessage(quote(high0 + 7.25, last_t + 60, 50000))
assert wait(lambda: abs(float(ch._closes[-1]) - (high0 + 7.25)) < 1e-3), ch._closes[-1]
assert ch.candles is candles and len(p.df) == n0
assert abs(float(p.df["Close"].iloc[-1]) - (high0 + 7.25)) < 1e-3 and abs(float(p.df["High"].iloc[-1]) - (high0 + 7.25)) < 1e-3
assert abs(float(ch.candles.c[-1]) - float(ch.ty(high0 + 7.25))) < 1e-3
assert ch.last_tag.text().strip() == ch.axis_label(float(ch.ty(high0 + 7.25)))
v0 = float(ch._vols[-1])
socks[0].sendBinaryMessage(quote(high0 + 1.0, last_t + 90, 50300))           # 300 more traded today
assert wait(lambda: abs(float(ch._closes[-1]) - (high0 + 1.0)) < 1e-3)
assert float(ch._vols[-1]) == v0 + 300 and abs(float(ch._highs[-1]) - (high0 + 7.25)) < 1e-3

# an old trade (two bars back) is not this bar
socks[0].sendBinaryMessage(quote(5.0, last_t - 700, 50300))
pump(150)
assert abs(float(ch._closes[-1]) - (high0 + 1.0)) < 1e-3, (ch._closes[-1], high0, len(p.df), n0, p.label)

# the next bar opens with the first trade after the bar period; the view keeps following
x_hi = ch.price.vb.viewRange()[0][1]
socks[0].sendBinaryMessage(quote(high0 + 3.0, last_t + 300 + 5, 50400))
assert wait(lambda: len(p.df) == n0 + 1), len(p.df)
assert p.df.index[-1] == p.df.index[-2] + pd.Timedelta(minutes=5)
r = p.df.iloc[-1]
assert r["Open"] == r["High"] == r["Low"] == r["Close"] and abs(r["Close"] - (high0 + 3.0)) < 1e-3 and r["Volume"] == 100
assert abs(ch.price.vb.viewRange()[0][1] - (x_hi + 1)) < 1e-6

# watchlist rows move with the feed too (prev close from the feed -> change %)
socks[0].sendBinaryMessage(hdr(6, 16, 1, 2885) + struct.pack("<fI", 1300.0, 0) + quote(1313.0, last_t + 320, 1000, 2885, 1))
assert wait(lambda: w.watchlist.quotes.get("RELIANCE", (0, 0))[0] == 1313.0)
assert abs(w.watchlist.quotes["RELIANCE"][1] - 0.01) < 1e-6

# indicators follow a tick (at most a second later)
w.ind_actions["sma20"].setChecked(True)
pump(50)
item = ch._ind_items["sma20"][0][1]
y_before = float(item.yData[-1])
socks[0].sendBinaryMessage(quote(high0 + 43.0, last_t + 330, 50500))
assert wait(lambda: "sma20" in ch._ind_items and abs(float(ch._ind_items["sma20"][0][1].yData[-1]) - y_before) > 1, 3000)

# a quiet refresh with the same bars does not rebuild either, and does not step back from the live price
# a quiet refresh: the broker's candle list does not have the bar the feed just opened - it stays, nothing is rebuilt
candles, n_live = ch.candles, len(p.df)
live_close = float(ch._closes[-1])
A._MEM.clear()
assert p.refresh()
assert wait(lambda: not p.loading)
pump(50)
assert len(p.df) == n_live == n0 + 1 and ch.candles is candles
assert abs(float(ch._closes[-1]) - live_close) < 1e-3

# ------------------------------------------------------------------ 5) option chain panel
w.show_right_panel(w.chain_dock)
oc = w.chain
assert wait(lambda: oc.tbl.rowCount() == 25 and oc.spot == 25030.0, 6000), (oc.tbl.rowCount(), oc.info.text())
from chartlab import optionchain as OC
strikes = [float(oc.tbl.item(r, OC.STRIKE).text()) for r in range(25)]
assert strikes == sorted(strikes) and 25050.0 in strikes and oc.exp_box.count() == 8
row = strikes.index(25050.0)
assert oc.tbl.item(row, OC.C_LTP).text() == "40.00" and oc.tbl.item(row, OC.P_LTP).text() == "55.00"
assert oc.tbl.item(row, OC.C_CHG).text() == "+5.3%" and oc.tbl.item(row, OC.C_OI).text() == "1.2L"
got = []
oc.picked.connect(got.append)
oc.tbl.cellClicked.emit(row, OC.P_LTP)
assert got == [f"NIFTY{EXPS[0]:%y%m%d}P25050"], got
assert wait(lambda: not p.loading and p.label.startswith(got[0]), 6000), p.label     # the click opened that chart
ins = oc.instruments()
assert len(ins) == 50
pe_sid = D.resolve_option(got[0])["security_id"]
assert wait(lambda: any(("NSE_FNO", pe_sid) in {(i["ExchangeSegment"], i["SecurityId"]) for i in m["InstrumentList"]}
                        for m in subs if m["RequestCode"] == 17), 4000)              # chain rows are on the live feed
socks[0].sendBinaryMessage(quote(61.5, float(ch._times[-1]) + 30, 777, int(pe_sid)))
assert wait(lambda: oc.tbl.item(row, OC.P_LTP).text() == "61.50")
assert abs(float(ch._closes[-1]) - 61.5) < 1e-3                                       # and the chart of it as well
oc.exp_box.setCurrentIndex(1)
oc._expiry_chosen(1)
assert wait(lambda: any(b.get("Expiry") == str(EXPS[1]) for u, b in calls if u.endswith("/optionchain")))

# ------------------------------------------------------------------ 6) symbol search: Options tab
from chartlab.tvui import SymbolSearch
d = SymbolSearch(w, w.watchlist.symbols(), "")
d._set_kind("Options")
d.edit.setText("NIFTY 25100")
rows = [d.lst.item(i).data(QtCore.Qt.ItemDataRole.UserRole) for i in range(d.lst.count())]
assert rows[:2] == [f"NIFTY{EXPS[0]:%y%m%d}C25100", f"NIFTY{EXPS[0]:%y%m%d}P25100"], rows[:4]
d.edit.setText("nifty 25100 pe")
assert d.lst.item(0).data(QtCore.Qt.ItemDataRole.UserRole) == f"NIFTY{EXPS[0]:%y%m%d}P25100"
d._set_kind("All")
d.edit.setText("RELI")
assert d.lst.item(0).data(QtCore.Qt.ItemDataRole.UserRole) == "RELIANCE"             # plain search is unchanged
d.reject()

# ------------------------------------------------------------------ 7) socket lost -> prices are polled instead
polled = []
w.live.feed.tick.connect(lambda seg, s, px, t, v: polled.append((seg, s, px, t)))
LiveFeed.URL = "ws://127.0.0.1:1"                            # nothing listens there
w.live.feed._poll.setInterval(200)
srv.close()
for s in socks:
    s.abort()
assert wait(lambda: w.live.feed.mode == "polling" and any(px == 111.5 for _, _, px, _ in polled), 8000), w.live.feed.mode
assert wait(lambda: abs(float(ch._closes[-1]) - 111.5) < 1e-3)
assert "1.5 s" in w.live_label.text()

# switching real-time prices off stops everything
w.set_live_prices(False)
pump(100)
assert w.live.feed.mode == "off" and not w.live_label.isVisible()

w.close()
quit_cleanly(w, 0, exit_process=False)
print("ROUND 27 OK")
os._exit(0)
