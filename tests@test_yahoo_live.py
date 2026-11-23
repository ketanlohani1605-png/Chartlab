"""Without a broker account the newest candle still moves: Yahoo's latest price is polled for the charts' symbols.
A fake fetch stands in for Yahoo: a price inside the bar moves it, a later minute opens a bar, a stale price (closed
market) does nothing, custom intervals keep their own bars, and a broker account takes over when it has the symbol."""
import os
import sys
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["CHARTLAB_HOME"] = "/tmp/chartlab_test_ylive"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from PySide6 import QtCore, QtTest, QtWidgets  # noqa: E402

app = QtWidgets.QApplication([])
QtCore.QCoreApplication.setOrganizationName("clt")
QtCore.QCoreApplication.setApplicationName("ylive_test")
from chartlab import accounts, live  # noqa: E402
from chartlab.mainwindow import MainWindow  # noqa: E402

accounts.load_accounts = lambda: []
live.YAHOO_EVERY = 0.05
win = MainWindow(QtCore.QSettings(os.path.join(__import__("tempfile").mkdtemp(), "t.ini"), QtCore.QSettings.Format.IniFormat))
win._chart_market_closed = lambda: False                     # (any hour: the badge tells the feed's state)
win.show()
app.processEvents()
QtTest.QTest.qWait(300)
if hasattr(win, "_auto_timer"):
    win.set_auto_refresh(0)
now = pd.Timestamp.now().floor("min")
idx = pd.DatetimeIndex([now - pd.Timedelta(minutes=5 * (60 - i)) for i in range(61)])
idx = idx.floor("5min")
c = 100 + np.arange(len(idx)) * 0.1
df = pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c + 0.2, "Volume": 100.0}, index=idx)
p = win.active
p.params.update(source="yahoo", symbol="RELIANCE", interval="5m")
p.set_dataframe(df, "RELIANCE.NS · 5m")
quotes = {}
calls = []


def fetch(sym):
    calls.append(sym)
    return dict(quotes[sym])


win.live.yahoo.fetch = fetch
states = []
win.live.status.connect(lambda m, d: states.append(m))
last = df.index[-1]
t_in = last.value / 1e9 + 30                    # chart time; offset 0 in this test
quotes["RELIANCE.NS"] = {"price": 111.0, "time": time.time(), "offset": t_in - time.time(), "volume": 5000.0, "prev": 100.0}
win.live.sync()


def pump(ms=400):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents()
        QtTest.QTest.qWait(10)


pump()
assert calls and set(calls) == {"RELIANCE.NS"}, calls
assert abs(float(p.df["Close"].iloc[-1]) - 111.0) < 1e-9 and len(p.df) == 61, (p.df["Close"].iloc[-1], len(p.df))
assert "yahoo" in states and win.live_label.isVisible() and "Live" in win.live_label.text(), states
# a higher price in the same bar: the high follows
quotes["RELIANCE.NS"].update(price=113.5, time=time.time() + 1)
pump()
assert abs(float(p.df["High"].iloc[-1]) - 113.5) < 1e-9 and abs(float(p.df["Close"].iloc[-1]) - 113.5) < 1e-9
# five minutes later: a new bar opens
q = quotes["RELIANCE.NS"]
q.update(price=112.0, time=q["time"] + 1, offset=q["offset"] + 300, volume=5600.0)
pump()
assert len(p.df) == 62 and p.df.index[-1] == last + pd.Timedelta(minutes=5), p.df.index[-2:]
assert abs(float(p.df["Open"].iloc[-1]) - 112.0) < 1e-9 and float(p.df["Volume"].iloc[-1]) == 600.0, p.df.iloc[-1]
# nothing new from Yahoo: nothing changes
snap = p.df.iloc[-1].copy()
pump(300)
assert (p.df.iloc[-1] == snap).all()

# a stale first price (market closed since hours) never touches the chart
p2df = df.copy()
p.params.update(symbol="TCS")
p.set_dataframe(p2df, "TCS.NS · 5m")
quotes["TCS.NS"] = {"price": 999.0, "time": time.time() - 7200, "offset": 0.0, "volume": 1.0, "prev": 1.0}
win.live.sync()
pump()
assert "TCS.NS" in calls and abs(float(p.df["Close"].iloc[-1]) - float(p2df["Close"].iloc[-1])) < 1e-9

# own files are not followed
p.params.update(source="file", symbol="X")
win.live.sync()
pump(150)
assert win.live.yahoo.symbols == ()
# switching live prices off stops it
p.params.update(source="yahoo", symbol="RELIANCE")
win.live.sync()
assert win.live.yahoo.symbols == ("RELIANCE.NS",)
win.set_live_prices(False)
assert win.live.yahoo.symbols == ()
win.set_live_prices(True)

# Yahoo's answer is read correctly
import json  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"chart": {"result": [{"meta": {"regularMarketPrice": 1375.6, "regularMarketTime": 1795000000,
                                                         "gmtoffset": 19800, "regularMarketVolume": 123456,
                                                         "chartPreviousClose": 1360.0}}], "error": None}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
live.YAHOO_URL = f"http://127.0.0.1:{srv.server_port}/v8/finance/chart/{{sym}}?interval=1m&range=1d"
os.environ["no_proxy"] = "127.0.0.1"
q = live.yahoo_quote("RELIANCE.NS")
assert q == {"price": 1375.6, "time": 1795000000.0, "offset": 19800.0, "volume": 123456.0, "prev": 1360.0}, q
srv.shutdown()
win.live.stop()
print("YAHOO LIVE OK")
