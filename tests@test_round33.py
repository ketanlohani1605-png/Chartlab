"""Round 33: trading on every broker. Paper trading end to end in the window (order panel, Trading Panel, order and
position lines on the chart: cancel with x, drag to a new price, close a position), the LIVE confirmation, browser
login through the local redirect catcher, a non-Dhan live feed over a WebSocket, candles from a non-Dhan broker and
a script order on a non-Dhan account. Nothing real is contacted."""
import os as _os; _os.environ["CHARTLAB_OFFLINE"] = "1"      # no public price feed: a live tick during market hours would change the test's prices
import http.server
import json
import os
import struct
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

HOME = tempfile.mkdtemp()
os.environ["CHARTLAB_HOME"] = HOME
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from PySide6 import QtCore, QtNetwork, QtTest, QtWebSockets, QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from chartlab import accounts as A  # noqa: E402
from chartlab import brokers, theme  # noqa: E402
from chartlab.brokers import base  # noqa: E402

theme.apply_pg_config()
app.setStyle("Fusion")
app.setStyleSheet(theme.STYLESHEET)
A.PATH = Path(HOME) / "accounts.json"


def pump(ms=50):
    QtTest.QTest.qWait(ms)


def wait(cond, ms=5000):
    t = time.time()
    while time.time() - t < ms / 1000:
        if cond():
            return True
        pump(30)
    return cond()


# ------------------------------------------------------------------ a fake broker (HTTP)
CALLS, ROUTES = [], {}


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _go(self):
        ln = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(ln) if ln else b""
        path = self.path.split("?")[0]
        CALLS.append({"method": self.command, "path": path, "query": self.path.partition("?")[2], "body": body,
                      "headers": dict(self.headers)})
        hit = ROUTES.get((self.command, path))
        status, out = (hit(self) if callable(hit) else hit) if hit else (404, {"status": "error", "message": "no route"})
        data = json.dumps(out).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = _go

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_address[1]}"

# every Zerodha / Upstox adapter in this test talks to the fake server, with a tiny instrument list
KITE_ROWS = [dict(token="738561", exchange="NSE", tsym="RELIANCE", name="RELIANCE", kind="EQ", expiry=None, strike=0,
                  lot=1, tick=0.1, seg="NSE")]
for cls in (brokers.Zerodha, brokers.Upstox):
    cls.base = URL
brokers.Upstox.order_base = URL
kite_master = base.normalise_master(pd.DataFrame(KITE_ROWS), "zerodha")
brokers.Zerodha.instruments = lambda self: kite_master
upx_master = base.normalise_master(pd.DataFrame([{**KITE_ROWS[0], "token": "NSE_EQ|INE002A01018", "seg": "NSE_EQ"}]),
                                   "upstox")
brokers.Upstox.instruments = lambda self: upx_master

from chartlab.mainwindow import MainWindow, quit_cleanly  # noqa: E402

tmp = Path(tempfile.mkdtemp())
w = MainWindow(QtCore.QSettings(str(tmp / "s.ini"), QtCore.QSettings.Format.IniFormat))
w.resize(1500, 900)
w.show()
pump(150)
c = 1350 + np.cumsum(np.random.default_rng(3).normal(0, 3, 300))
df = pd.DataFrame({"Open": c, "High": c + 5, "Low": c - 5, "Close": c, "Volume": 1e6},
                  index=pd.bdate_range(end="2026-11-18", periods=300))
p = w.active
p.set_dataframe(df, "RELIANCE")
p.params.update(symbol="RELIANCE", source="auto", interval="1d")
pump(100)
w._feed_trade_price(p)
last = float(df["Close"].iloc[-1])
T = w.trader
assert T.name == "Paper trading" and T.broker().id == "paper" and not T.live

# ------------------------------------------------------------------ 1) paper trading through the order panel
from chartlab.trading import OrderTicket  # noqa: E402

tk = OrderTicket(T, "RELIANCE", "BUY", last)
assert tk.badge.text() == "PAPER" and tk.buy.isChecked() and tk.go.text().startswith("Buy 1 RELIANCE @ MKT")
tk.qty.setValue(10)
tk.submit()
assert wait(lambda: any(x.qty == 10 for x in T.snap["positions"])), T.snap
tk = OrderTicket(T, "RELIANCE", "SELL", last, "LIMIT")
tk.qty.setValue(4)
tk.price.setValue(last + 8)
assert "Sell 4 RELIANCE" in tk.go.text() and "Limit" in tk.go.text()
tk.submit()
tk = OrderTicket(T, "RELIANCE", "SELL", last, "SL-M")
tk.qty.setValue(6)
tk.trig.setValue(last - 12)
tk.submit()
assert wait(lambda: sum(1 for o in T.snap["orders"] if o.is_open) == 2)
# bad input stays in the panel with a message, nothing is sent
tk = OrderTicket(T, "RELIANCE", "BUY", last, "LIMIT")
tk.tp_on.setChecked(True)
tk.tp.setValue(last - 100)                                  # take profit below a buy: not allowed
n0 = len(T.snap["orders"])
tk.submit()
assert "take profit" in tk.msg.text() and len(T.snap["orders"]) == n0, (tk.msg.text(), tk.tp_on.isVisible(), tk.tp_on.isChecked(), n0, len(T.snap["orders"]))

# the chart shows both working orders and the position, each with its price on the scale
ch = p.chart
assert wait(lambda: len(getattr(ch, "_trade_objs", [])) == 3)
kinds = sorted((it["kind"], it.get("type", "")) for _l, _t, _lab, it in ch._trade_objs)
assert kinds == [("order", "LIMIT"), ("order", "SL-M"), ("position", "")], kinds
lim = next((ln, tg, lab, it) for ln, tg, lab, it in ch._trade_objs if it.get("type") == "LIMIT")
assert lim[1].isVisible() and lim[1].text() == ch.axis_price(lim[3]["price"]) and lim[2].isVisible(), \
    (lim[1].isVisible(), lim[1].text(), ch.axis_price(lim[3]["price"]), lim[2].isVisible(), ch.price.vb.viewRange()[1], lim[3]["price"])
assert lim[0].movable and "Sell LIMIT" in lim[2].findChildren(QtWidgets.QLabel)[1].text()
# drag the limit line 10 up -> the order's price changes
oid = lim[3]["obj"].id
new_px = lim[3]["price"] + 4
ch.orderMoveRequested.emit(lim[3]["obj"], new_px)
assert wait(lambda: any(o.id == oid and abs(o.price - round(new_px, 2)) < 0.051 for o in T.snap["orders"])), \
    [(o.id, o.price) for o in T.snap["orders"]]
# the price passes the limit -> paper fill, the position shrinks to 6
T.on_price("RELIANCE", new_px + 1)
assert wait(lambda: any(x.qty == 6 for x in T.snap["positions"]))
assert wait(lambda: len(ch._trade_objs) == 2)
# cancel the stop with the x on its line
stop = next(it for _l, _t, _lab, it in ch._trade_objs if it.get("type") == "SL-M")
ch.orderCancelRequested.emit(stop["obj"])
assert wait(lambda: all(not o.is_open for o in T.snap["orders"]))
# close the position from its line
pos = next(it for _l, _t, _lab, it in ch._trade_objs if it["kind"] == "position")
ch.positionCloseRequested.emit(pos["obj"])
assert wait(lambda: all(x.qty == 0 for x in T.snap["positions"]))
assert wait(lambda: len(getattr(ch, "_trade_objs", [])) == 0)

# the Trading Panel
w.show_trading_panel()
pump(200)
tp = w.trade_panel
assert tp.pos.rowCount() == 1 and tp.pos.item(0, 1).text() == "Flat"
assert "Available" in tp.summary.text() and "Realised P&amp;L" in tp.summary.text()
tp.order_filter.setCurrentIndex(tp.order_filter.findData("all"))
assert tp.ords.rowCount() >= 4
assert tp.notes.count() >= 4 and any("Paper fill" in tp.notes.item(i).text() for i in range(tp.notes.count()))
assert not tp.tabs.isTabVisible(tp.tabs.indexOf(tp.hold))            # paper has no holdings
# the chart's Buy button opens the order panel for that side
seen = {}
orig_exec = OrderTicket.exec
OrderTicket.exec = lambda self: seen.update(side=self.side(), sym=self.symbol, last=self.last) or 0
ch.sell_btn.click()
OrderTicket.exec = orig_exec
assert seen == {"side": "SELL", "sym": "RELIANCE", "last": last}, seen

# ------------------------------------------------------------------ 2) a LIVE account: confirmation first
A.save_accounts([{"name": "Kite", "broker": "zerodha", "api_key": "kk", "api_secret": "ss", "access_token": "tt",
                  "live": True}])
ROUTES[("GET", "/user/margins")] = (200, {"status": "success", "data": {"equity": {"net": 50000, "utilised": {"debits": 0}}}})
ROUTES[("GET", "/portfolio/positions")] = (200, {"status": "success", "data": {"net": []}})
ROUTES[("GET", "/portfolio/holdings")] = (200, {"status": "success", "data": []})
ROUTES[("GET", "/orders")] = (200, {"status": "success", "data": []})
ROUTES[("POST", "/orders/regular")] = (200, {"status": "success", "data": {"order_id": "K1"}})
T.set_account("Kite")
assert T.live and T.broker().id == "zerodha"
assert wait(lambda: T.snap["funds"] is not None and T.snap["funds"].available == 50000)
tk = OrderTicket(T, "RELIANCE", "BUY", last)
assert tk.badge.text() == "LIVE" and not tk.prods["MTF"].isHidden() and tk.tp_on.isHidden()   # Kite: no brackets
assert wait(lambda: tk.inst is not None) and "tick 0.1" in tk.sub.text()
answers = []


def fake_box_exec(box, accept):
    answers.append(box.informativeText())
    btn = next(b for b in box.buttons() if (box.buttonRole(b) == QtWidgets.QMessageBox.ButtonRole.AcceptRole) == accept)
    box.clickedButton = lambda b=btn: b
    return 0


QtWidgets.QMessageBox.exec = lambda box: fake_box_exec(box, False)          # say No
n0 = sum(1 for c in CALLS if c["path"] == "/orders/regular")
tk.submit()
pump(300)
assert answers and "BUY 1 RELIANCE at market" in answers[-1]
assert sum(1 for c in CALLS if c["path"] == "/orders/regular") == n0, "a declined LIVE order was sent"
QtWidgets.QMessageBox.exec = lambda box: fake_box_exec(box, True)           # say Yes
tk.submit()
assert wait(lambda: sum(1 for c in CALLS if c["path"] == "/orders/regular") == n0 + 1)
body = dict(urllib.parse.parse_qsl(next(c for c in reversed(CALLS) if c["path"] == "/orders/regular")["body"].decode()))
assert body["tradingsymbol"] == "RELIANCE" and body["market_protection"] == "-1"
assert wait(lambda: any("LIVE order sent" in x[2] for x in T.log))
# a LIVE account switched off (paper) never reaches the broker
A.save_accounts([{"name": "Kite", "broker": "zerodha", "api_key": "kk", "api_secret": "ss", "access_token": "tt",
                  "live": False}])
T.name = ""
T.set_account("Kite")
assert not T.live and T.broker().id == "paper"
n1 = sum(1 for c in CALLS if c["path"] == "/orders/regular")
T.on_price("RELIANCE", last)
OrderTicket(T, "RELIANCE", "BUY", last).submit()
assert wait(lambda: any(x.qty for x in T.snap["positions"]))
assert sum(1 for c in CALLS if c["path"] == "/orders/regular") == n1
T.set_account("Paper trading")

# ------------------------------------------------------------------ 3) browser login: the redirect is caught here
from chartlab.brokers import oauth  # noqa: E402

ROUTES[("POST", "/session/token")] = (200, {"status": "success", "data": {"access_token": "FRESH", "user_id": "ZX1"}})
A.save_accounts([])
dlg = A.AccountsDialog(None, A.PATH)
dlg.show()
dlg.broker.setCurrentIndex(dlg.broker.findData("zerodha"))
pump(50)
assert set(dlg.inputs) == {"api_key", "api_secret", "access_token"} and not dlg.login_btn.isHidden()
assert dlg.totp_btn.isHidden() and "127.0.0.1:8767/callback" in dlg.redirect_lbl.text()
dlg.name.setText("Kite2")
dlg.inputs["api_key"].setText("kk")
dlg.inputs["api_secret"].setText("ss")
opened = []
port = 18000 + os.getpid() % 2000
dlg.inputs  # the redirect for Zerodha is the default one; use a free port for the test
oauth.DEFAULT_REDIRECT = f"http://127.0.0.1:{port}/callback"


def browser(url):
    opened.append(url.toString())
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url.toString()).query))
    state = dict(urllib.parse.parse_qsl(q["redirect_params"]))["state"]

    def visit():                                           # what Kite does after the user logs in
        time.sleep(0.2)
        urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?request_token=RT9&action=login&status=success"
                               f"&state={state}").read()
    threading.Thread(target=visit, daemon=True).start()
    return True


from PySide6 import QtGui  # noqa: E402

QtGui.QDesktopServices.openUrl = browser
dlg.login_browser()
assert wait(lambda: A.load_accounts() and A.load_accounts()[0].get("access_token") == "FRESH", 8000), dlg.msg.text()
assert opened and "api_key=kk" in opened[0]
saved = A.load_accounts()[0]
assert saved["name"] == "Kite2" and saved["broker"] == "zerodha" and saved["user_id"] == "ZX1" and not saved["live"]
chk = dict(urllib.parse.parse_qsl(next(c for c in reversed(CALLS) if c["path"] == "/session/token")["body"].decode()))
assert chk["request_token"] == "RT9"
assert "Logged in" in dlg.msg.text()
# pasting the address works too (when the redirect cannot be caught)
dlg._login_b = brokers.cls_of("zerodha")({"api_key": "kk", "api_secret": "ss"})
ROUTES[("POST", "/session/token")] = (200, {"status": "success", "data": {"access_token": "PASTED"}})
dlg.paste.setText(f"http://127.0.0.1:9/callback?request_token=RT10&status=success")
dlg._use_pasted()
assert wait(lambda: A.load_accounts()[0].get("access_token") == "PASTED", 6000), dlg.msg.text()
# Angel One: PIN + TOTP straight from ChartLab, the secret fields stay masked
dlg._new()
dlg.broker.setCurrentIndex(dlg.broker.findData("angel"))
assert not dlg.totp_btn.isHidden() and dlg.inputs["pin"].echoMode() == QtWidgets.QLineEdit.EchoMode.Password
dlg.reject()
assert A.FIELDS["angel"] == [("api_key", "API key"), ("client_code", "Client code"), ("access_token", "Session token")]

# ------------------------------------------------------------------ 4) a non-Dhan live feed (Zerodha) over a socket
ws_srv = QtWebSockets.QWebSocketServer("fake-kite", QtWebSockets.QWebSocketServer.SslMode.NonSecureMode)
assert ws_srv.listen(QtNetwork.QHostAddress.SpecialAddress.LocalHost, 0)
socks, subs, urls = [], [], []


def on_conn():
    s = ws_srv.nextPendingConnection()
    socks.append(s)
    urls.append(s.requestUrl().toString())
    s.textMessageReceived.connect(lambda m: subs.append(json.loads(m)))


ws_srv.newConnection.connect(on_conn)
from chartlab.brokers import feeds  # noqa: E402

feeds.KiteProto.url = lambda self: (f"ws://127.0.0.1:{ws_srv.serverPort()}/?api_key={self.acct.get('api_key')}"
                                    f"&access_token={self.acct.get('access_token')}")
A.save_accounts([{"name": "Kite3", "broker": "zerodha", "api_key": "kk", "api_secret": "ss", "access_token": "LIVE1"}])
w.live.sync()
assert wait(lambda: w.live.feed.mode == "live" and any(m.get("a") == "mode" for m in subs), 8000), \
    (w.live.feed.mode, w.live.feed.detail, subs)
assert "access_token=LIVE1" in urls[0]
assert {"a": "mode", "v": ["full", [738561]]} in subs
assert w.live.feed.broker.name == "Zerodha"
n_bars = len(p.df)
w.live.clock.shift = 19800                                   # Kite sends true Unix times
t_last = (p.df.index[-1] - pd.Timestamp("1970-01-01")).total_seconds() - 19800 + 3600
pk = struct.pack(">IIIIIIIIIII", 738561, int(round((last + 2.5) * 100)), 5, 0, 777000, 0, 0, 0, 0, 0, 0)
pk += struct.pack(">IIIII", int(t_last), 0, 0, 0, int(t_last)) + b"\0" * 120
socks[0].sendBinaryMessage(QtCore.QByteArray(struct.pack(">H", 1) + struct.pack(">H", len(pk)) + pk))
px_t = round(last + 2.5, 2)                                  # Kite sends paise
assert wait(lambda: abs(float(p.df["Close"].iloc[-1]) - px_t) < 1e-6, 5000), p.df["Close"].iloc[-1]
assert len(p.df) == n_bars
# the paper broker heard the tick too
assert abs(brokers.Paper._prices.get("RELIANCE", 0) - px_t) < 1e-6
ws_srv.close()
for s in socks:
    s.abort()
A.save_accounts([])
w.live.sync()

# ------------------------------------------------------------------ 5) candles from a non-Dhan broker (Upstox)
ROUTES[("GET", "/v3/historical-candle/NSE_EQ|INE002A01018/days/1/" )] = None
hist = {"status": "success", "data": {"candles": [["2026-11-18T00:00:00+05:30", 10, 12, 9, 11, 1000, 0],
                                                  ["2026-11-17T00:00:00+05:30", 9, 11, 8, 10, 900, 0]]}}


def any_hist(handler):
    return 200, hist


class R(dict):
    def get(self, k, d=None):
        if k[1].startswith("/v3/historical-candle/"):
            return any_hist
        return super().get(k, d)


ROUTES = R(ROUTES)
A.save_accounts([{"name": "Upx", "broker": "upstox", "api_key": "uk", "api_secret": "us", "access_token": "UT"}])
out, label = A.load_api("Upx", "RELIANCE", "1d", "1mo")
assert len(out) == 2 and float(out["Close"].iloc[-1]) == 11.0 and "RELIANCE" in label and "Upx" in label
hc = [c for c in CALLS if c["path"].startswith("/v3/historical-candle/")]
assert hc and hc[0]["headers"].get("Authorization") == "Bearer UT" and "NSE_EQ%7CINE002A01018" in hc[0]["path"]

# ------------------------------------------------------------------ 6) a script order on a non-Dhan account
from chartlab import scriptmode  # noqa: E402

work = scriptmode.prepare(None)
assert (work / "chartlab_brokers" / "zerodha.py").exists() and (work / "chartlab_brokers" / "__init__.py").exists()
env = {"CHARTLAB_OUT": str(work), "CHARTLAB_HOME": HOME, "PYTHONPATH": str(work)}
af = work / "accounts.json"
af.write_text(json.dumps([{"name": "K", "broker": "zerodha", "api_key": "kk", "access_token": "tt", "live": True},
                          {"name": "P", "broker": "zerodha", "api_key": "kk", "access_token": "tt", "live": False}]))
script = work / "s.py"
script.write_text(
    "import chartlab, chartlab_brokers as B\n"
    f"B.Zerodha.base = '{URL}'\n"
    "import pandas as pd\n"
    "B.Zerodha.instruments = lambda self: B.base.normalise_master(pd.DataFrame([dict(token='738561', exchange='NSE',"
    " tsym='RELIANCE', name='RELIANCE', kind='EQ', expiry=None, strike=0, lot=1, tick=0.1, seg='NSE')]), 'zerodha')\n"
    "r = chartlab.order('RELIANCE', 'SELL', 3, order_type='SL-M', trigger=1300)\n"
    "print('RESULT', [(x['account'], x['ok'], x.get('paper', False)) for x in r])\n")
import subprocess  # noqa: E402

n2 = sum(1 for c in CALLS if c["path"] == "/orders/regular")
res = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=120,
                     env={**os.environ, **env, "CHARTLAB_ACCOUNTS_FILE": str(af)}, cwd=str(work))
assert "RESULT [('K', True, False), ('P', True, True)]" in res.stdout, res.stdout + res.stderr
assert "[PAPER] P:" in res.stdout and "[LIVE] K" in res.stdout
assert sum(1 for c in CALLS if c["path"] == "/orders/regular") == n2 + 1
sent = dict(urllib.parse.parse_qsl(next(c for c in reversed(CALLS) if c["path"] == "/orders/regular")["body"].decode()))
assert sent["order_type"] == "SL-M" and sent["trigger_price"] == "1300.0" and sent["transaction_type"] == "SELL"

srv.shutdown()
w.close()
quit_cleanly(w, 0, exit_process=False)
print("ROUND 33 OK")
os._exit(0)
