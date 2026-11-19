"""Broker layer: symbols, order checks, every adapter against a local fake broker (requests, headers, answers,
errors, the never-twice rule for orders), the socket parsers and the paper broker. No real broker is contacted."""
import datetime as dt
import gzip
import http.server
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time

os.environ["CHARTLAB_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pandas as pd  # noqa: E402

from chartlab import brokers  # noqa: E402
from chartlab.brokers import base, feeds  # noqa: E402
from chartlab.brokers.base import OrderRequest, parse_symbol, pick_expiry  # noqa: E402

# ------------------------------------------------------------------ symbols
s = parse_symbol("RELIANCE")
assert (s.kind, s.name, s.exchange) == ("EQ", "RELIANCE", "NSE")
assert parse_symbol("BSE:SBIN").exchange == "BSE"
assert parse_symbol("reliance.ns").name == "RELIANCE" and parse_symbol("TCS.BO").exchange == "BSE"
assert parse_symbol("NIFTY 50").kind == "IDX" and parse_symbol("NIFTYBANK").name == "BANKNIFTY"
assert parse_symbol("SENSEX").exchange == "BSE"
o = parse_symbol("NIFTY 24NOV 25000 CE")
assert (o.kind, o.name, o.strike, o.expiry) == ("CE", "NIFTY", 25000.0, ("d", None, 11, 24))
o = parse_symbol("NIFTY261124C25000")
assert (o.kind, o.expiry) == ("CE", ("d", 2026, 11, 24))
o = parse_symbol("NIFTY26N2425000PE")                          # Kite weekly
assert (o.kind, o.expiry, o.strike) == ("PE", ("d", 2026, 11, 24), 25000.0)
o = parse_symbol("NIFTY26NOV25000CE")                          # Kite monthly: day-26 first, year+month as the alt
assert o.kind == "CE" and any(a[1] == ("m", 2026, 11) for a in o.alts)
f = parse_symbol("BANKNIFTY DEC FUT")
assert (f.kind, f.name, f.expiry) == ("FUT", "BANKNIFTY", ("m", None, 12))
assert parse_symbol("CRUDEOIL FUT").exchange == "MCX" and parse_symbol("CRUDEOIL").kind == "FUT"
assert parse_symbol("NIFTY FUT").expiry is None
exps = [dt.date(2026, 11, 24), dt.date(2026, 12, 1), dt.date(2026, 12, 29)]
assert pick_expiry(exps, None, dt.date(2026, 11, 20)) == dt.date(2026, 11, 24)
assert pick_expiry(exps, ("m", None, 12), dt.date(2026, 11, 20)) == dt.date(2026, 12, 29)     # the monthly one
assert pick_expiry(exps, ("d", None, 12, 1), dt.date(2026, 11, 20)) == dt.date(2026, 12, 1)

# ------------------------------------------------------------------ order checks
inst_eq = base.Instrument("x", "1", "NSE", "RELIANCE", "RELIANCE", "EQ", tick=0.1)
inst_opt = base.Instrument("x", "2", "NFO", "NIFTY26NOV25000CE", "NIFTY", "CE", dt.date(2026, 11, 24), 25000, 65, 0.05)
inst_idx = base.Instrument("x", "3", "NSE", "NIFTY 50", "NIFTY", "IDX")
r = OrderRequest("RELIANCE", "buy", 10, "lmt", "mis", price=1234.56, instrument=inst_eq).normalised()
assert (r.side, r.order_type, r.product, r.price) == ("BUY", "LIMIT", "INTRADAY", 1234.6)      # rounded to the tick
r = OrderRequest("X", "SELL", lots=2, instrument=inst_opt).normalised()
assert r.qty == 130 and r.price == 0
for bad, why in [(OrderRequest("X", "BUY", 10, "LIMIT", instrument=inst_eq), "price"),
                 (OrderRequest("X", "BUY", 10, "SL-M", instrument=inst_eq), "trigger"),
                 (OrderRequest("X", "BUY", 0, instrument=inst_eq), "quantity"),
                 (OrderRequest("X", "BUY", 100, instrument=inst_opt), "lots of 65"),
                 (OrderRequest("X", "BUY", 1, instrument=inst_idx), "index"),
                 (OrderRequest("X", "HOLD", 1, instrument=inst_eq), "BUY or SELL")]:
    try:
        bad.normalised()
        raise AssertionError(f"accepted a bad order ({why})")
    except ValueError as e:
        assert why in str(e), (why, e)

# ------------------------------------------------------------------ a fake broker server
CALLS = []
ROUTES = {}          # (METHOD, path) -> (status, body) | callable(handler) -> (status, body)


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _go(self):
        ln = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(ln) if ln else b""
        path = self.path.split("?")[0]
        CALLS.append({"method": self.command, "path": path, "query": self.path.partition("?")[2],
                      "headers": dict(self.headers), "body": body})
        hit = ROUTES.get((self.command, path))
        if hit is None:
            status, out = 404, {"status": "error", "message": f"no route {self.command} {path}"}
        else:
            status, out = hit(self) if callable(hit) else hit
        if out == "DROP":                      # read the request, then hang up without answering
            self.close_connection = True
            self.wfile.flush()
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        data = out if isinstance(out, bytes) else json.dumps(out).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _go

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_address[1]}"


def last(method=None, path=None):
    for c in reversed(CALLS):
        if (method is None or c["method"] == method) and (path is None or c["path"] == path):
            return c
    raise AssertionError(f"no call {method} {path}")


def form(c):
    import urllib.parse
    return dict(urllib.parse.parse_qsl(c["body"].decode()))


def mk(bid, acct, master_rows):
    b = brokers.get({"name": f"t-{bid}", "broker": bid, **acct}, base_url=URL)
    for attr in ("order_base", "data_base"):
        if hasattr(b, attr):
            setattr(b, attr, URL)
    df = base.normalise_master(pd.DataFrame(master_rows), bid)
    b.instruments = lambda df=df: df
    return b


ROW = dict(token="", exchange="NSE", tsym="", name="", kind="EQ", expiry=None, strike=0, lot=1, tick=0.05, seg="")

# ------------------------------------------------------------------ Zerodha
z = mk("zerodha", {"api_key": "kk", "api_secret": "ss", "access_token": "tt"},
       [{**ROW, "token": "738561", "tsym": "RELIANCE", "name": "RELIANCE", "tick": 0.1, "seg": "NSE"},
        {**ROW, "token": "16499970", "exchange": "NFO", "tsym": "NIFTY26NOV25250CE", "name": "NIFTY", "kind": "CE",
         "expiry": dt.date(2030, 11, 26), "strike": 25250, "lot": 65, "seg": "NFO-OPT"}])
ROUTES[("POST", "/orders/regular")] = (200, {"status": "success", "data": {"order_id": "151220000000000"}})
oid = z.place(OrderRequest("RELIANCE", "BUY", 5, "MARKET", "INTRADAY"))
c = last("POST", "/orders/regular")
assert oid == "151220000000000"
assert c["headers"]["Authorization"] == "token kk:tt" and c["headers"]["X-Kite-Version"] == "3"
fb = form(c)
assert fb["tradingsymbol"] == "RELIANCE" and fb["product"] == "MIS" and fb["quantity"] == "5"
assert fb["market_protection"] == "-1" and "price" not in fb
z.place(OrderRequest("NIFTY 26NOV30 25250 CE", "SELL", lots=1, order_type="LIMIT", product="MARGIN", price=101.03))
fb = form(last("POST", "/orders/regular"))
assert fb["exchange"] == "NFO" and fb["quantity"] == "65" and fb["product"] == "NRML" and fb["price"] == "101.05"
ROUTES[("POST", "/orders/regular")] = (403, {"status": "error", "error_type": "TokenException",
                                             "message": "Incorrect `api_key` or `access_token`."})
try:
    z.place(OrderRequest("RELIANCE", "BUY", 1))
    raise AssertionError("token error not raised")
except brokers.AuthError as e:
    assert "log in again" in str(e)
ROUTES[("POST", "/orders/regular")] = (400, {"status": "error", "error_type": "InputException",
                                             "message": "Order rejected: your IP is not whitelisted"})
try:
    z.place(OrderRequest("RELIANCE", "BUY", 1))
    raise AssertionError("IP error not raised")
except brokers.AccessError as e:
    assert "static IP" in str(e)
ROUTES[("POST", "/orders/regular")] = (400, {"status": "error", "error_type": "MarginException",
                                             "message": "Insufficient funds"})
try:
    z.place(OrderRequest("RELIANCE", "BUY", 1))
    raise AssertionError("rejection not raised")
except brokers.Rejected:
    pass
# the never-twice rule: the order reached the server, the answer was lost -> SentNoAnswer, exactly one request
ROUTES[("POST", "/orders/regular")] = (200, "DROP")
n0 = sum(1 for c in CALLS if c["path"] == "/orders/regular")
try:
    z.place(OrderRequest("RELIANCE", "BUY", 1))
    raise AssertionError("lost answer not reported")
except brokers.SentNoAnswer as e:
    assert "CHECK" in str(e)
assert sum(1 for c in CALLS if c["path"] == "/orders/regular") - n0 == 1, "an order was sent twice"
# a GET may be retried
ROUTES[("GET", "/portfolio/positions")] = (200, {"status": "success", "data": {"net": [
    {"tradingsymbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "quantity": -5, "average_price": 1400.5,
     "last_price": 1390, "pnl": 52.5, "realised": 0, "unrealised": 52.5, "instrument_token": 738561,
     "buy_quantity": 0, "sell_quantity": 5}]}})
p = z.positions()[0]
assert (p.symbol, p.qty, p.product, p.avg, p.pnl, p.token) == ("RELIANCE", -5, "INTRADAY", 1400.5, 52.5, "738561")
ROUTES[("GET", "/orders")] = (200, {"status": "success", "data": [
    {"order_id": "1", "tradingsymbol": "RELIANCE", "exchange": "NSE", "transaction_type": "BUY", "quantity": 5,
     "filled_quantity": 0, "pending_quantity": 5, "order_type": "LIMIT", "product": "CNC", "price": 1300,
     "trigger_price": 0, "average_price": 0, "status": "OPEN", "variety": "regular", "instrument_token": 738561},
    {"order_id": "2", "tradingsymbol": "RELIANCE", "exchange": "NSE", "transaction_type": "SELL", "quantity": 5,
     "filled_quantity": 0, "pending_quantity": 5, "order_type": "SL-M", "product": "MIS", "price": 0,
     "trigger_price": 1350, "average_price": 0, "status": "TRIGGER PENDING", "variety": "regular"},
    {"order_id": "3", "tradingsymbol": "RELIANCE", "exchange": "NSE", "transaction_type": "BUY", "quantity": 5,
     "filled_quantity": 5, "status": "COMPLETE", "order_type": "MARKET", "product": "MIS", "variety": "regular"}]})
ods = z.orders()
assert [x.status for x in ods] == ["open", "trigger_pending", "complete"] and ods[0].product == "DELIVERY"
ROUTES[("PUT", "/orders/regular/1")] = (200, {"status": "success", "data": {"order_id": "1"}})
z.modify(ods[0], price=1310)
assert form(last("PUT", "/orders/regular/1"))["price"] == "1310"
ROUTES[("DELETE", "/orders/regular/1")] = (200, {"status": "success", "data": {"order_id": "1"}})
z.cancel(ods[0])
ROUTES[("GET", "/user/margins")] = (200, {"status": "success", "data": {
    "equity": {"net": 50000.5, "utilised": {"debits": 1200}}, "commodity": {"enabled": False, "net": 7}}})
fu = z.funds()
assert fu.available == 50000.5 and fu.used == 1200
# login: request token -> access token with the SHA-256 checksum
ROUTES[("POST", "/session/token")] = (200, {"status": "success", "data": {"access_token": "NEW", "user_id": "AB1"}})
z2 = mk("zerodha", {"api_key": "kk", "api_secret": "ss"}, [])
got = z2.complete_login({"request_token": "rt", "status": "success"})
import hashlib  # noqa: E402
assert form(last("POST", "/session/token"))["checksum"] == hashlib.sha256(b"kkrtss").hexdigest()
assert got["access_token"] == "NEW" and z2.token_expiry() > time.time()
assert "api_key=kk" in z2.login_url("", "abc") and "redirect_params=state%3Dabc" in z2.login_url("", "abc")
# candles: chunked by Kite's per-request limits, India time, no time zone
ROUTES[("GET", "/instruments/historical/738561/day")] = (200, {"status": "success", "data": {"candles": [
    ["2026-11-17T00:00:00+0530", 1, 2, 0.5, 1.5, 100], ["2026-11-18T00:00:00+0530", 1.5, 2.5, 1, 2, 200]]}})
df = z.history("RELIANCE", "1d", "1y")
assert list(df.columns) == ["Date", "Open", "High", "Low", "Close", "Volume"] and len(df) == 2
assert df["Date"].iloc[0] == pd.Timestamp("2026-11-17") and df["Date"].dt.tz is None

# ------------------------------------------------------------------ Upstox
u = mk("upstox", {"api_key": "uk", "api_secret": "us", "redirect_uri": "http://127.0.0.1:8767/callback",
                  "access_token": "ut"},
       [{**ROW, "token": "NSE_EQ|INE002A01018", "tsym": "RELIANCE", "name": "RELIANCE", "seg": "NSE_EQ"}])
ROUTES[("POST", "/v3/order/place")] = (200, {"status": "success", "data": {"order_ids": ["2611190001"]},
                                             "metadata": {"latency": 20}})
assert u.place(OrderRequest("RELIANCE", "SELL", 3, "SL-M", "DELIVERY", trigger=1300)) == "2611190001"
c = last("POST", "/v3/order/place")
js = json.loads(c["body"])
assert c["headers"]["Authorization"] == "Bearer ut"
assert js["instrument_token"] == "NSE_EQ|INE002A01018" and js["product"] == "D" and js["order_type"] == "SL-M"
assert js["trigger_price"] == 1300 and js["market_protection"] == -1 and js["price"] == 0
ROUTES[("POST", "/v3/order/place")] = (401, {"status": "error", "errors": [
    {"errorCode": "UDAPI100050", "message": "Invalid token used to access API"}]})
try:
    u.place(OrderRequest("RELIANCE", "BUY", 1))
    raise AssertionError
except brokers.AuthError:
    pass
ROUTES[("POST", "/v2/login/authorization/token")] = (200, {"access_token": "UPXNEW", "user_id": "U1"})
u2 = mk("upstox", {"api_key": "uk", "api_secret": "us", "redirect_uri": "http://127.0.0.1:8767/callback"}, [])
assert u2.complete_login({"code": "cc"})["access_token"] == "UPXNEW"
fb = form(last("POST", "/v2/login/authorization/token"))
assert fb == {"code": "cc", "client_id": "uk", "client_secret": "us", "redirect_uri": "http://127.0.0.1:8767/callback",
              "grant_type": "authorization_code"}
ROUTES[("GET", "/v3/market-quote/ltp")] = (200, {"status": "success", "data": {
    "NSE_EQ:RELIANCE": {"last_price": 1361.2, "instrument_token": "NSE_EQ|INE002A01018"}}})
assert u.ltp([u.resolve("RELIANCE")]) == {"NSE_EQ|INE002A01018": 1361.2}
assert "NSE_EQ%7CINE002A01018" in last("GET", "/v3/market-quote/ltp")["query"]
ROUTES[("GET", "/v3/feed/market-data-feed/authorize")] = (200, {"status": "success", "data": {
    "authorizedRedirectUri": "wss://example/feed?code=abc"}})
pr = feeds.proto_for(u)
pr.prepare()
assert pr.url() == "wss://example/feed?code=abc"
sub = pr.subscribe([pr.key(u.resolve("RELIANCE"))])
assert isinstance(sub[0], bytes) and json.loads(sub[0])["data"] == {"instrumentKeys": ["NSE_EQ|INE002A01018"],
                                                                    "mode": "full"}

# ------------------------------------------------------------------ Angel One
a = mk("angel", {"api_key": "ak", "client_code": "C1", "access_token": "jw", "feed_token": "ft"},
       [{**ROW, "token": "2885", "tsym": "RELIANCE-EQ", "name": "RELIANCE", "tick": 0.1, "seg": "NSE"}])
ROUTES[("POST", "/rest/secure/angelbroking/order/v1/placeOrder")] = (200, {"status": True, "message": "SUCCESS",
                                                                          "data": {"orderid": "A1", "script": "x"}})
assert a.place(OrderRequest("RELIANCE", "BUY", 2, "SL", "DELIVERY", price=1400, trigger=1399.95)) == "A1"
c = last("POST", "/rest/secure/angelbroking/order/v1/placeOrder")
js = json.loads(c["body"])
assert c["headers"]["X-PrivateKey"] == "ak" and c["headers"]["Authorization"] == "Bearer jw"
assert js["variety"] == "STOPLOSS" and js["ordertype"] == "STOPLOSS_LIMIT" and js["producttype"] == "DELIVERY"
assert js["symboltoken"] == "2885" and js["quantity"] == "2" and js["triggerprice"] == "1400.0"     # 0.1 tick
ROUTES[("POST", "/rest/secure/angelbroking/order/v1/placeOrder")] = (200, {"status": False,
                                                                          "message": "Invalid Token", "errorcode": "AG8001"})
try:
    a.place(OrderRequest("RELIANCE", "BUY", 1))
    raise AssertionError
except brokers.AuthError:
    pass
from chartlab.brokers.angel import totp_now  # noqa: E402
assert totp_now("JBSWY3DPEHPK3PXP", t=59) == "996554"                 # RFC 6238 test-style vector for this key
ROUTES[("POST", "/rest/auth/angelbroking/user/v1/loginByPassword")] = (200, {"status": True, "data": {
    "jwtToken": "Bearer J2", "refreshToken": "R2", "feedToken": "F2"}})
got = a.login_totp(pin="1234", code="123456")
assert got["access_token"] == "J2" and got["feed_token"] == "F2"
assert json.loads(last("POST", "/rest/auth/angelbroking/user/v1/loginByPassword")["body"]) == {
    "clientcode": "C1", "password": "1234", "totp": "123456"}
ROUTES[("GET", "/rest/secure/angelbroking/order/v1/getPosition")] = (200, {"status": True, "data": [
    {"tradingsymbol": "RELIANCE-EQ", "exchange": "NSE", "producttype": "INTRADAY", "netqty": "4", "avgnetprice": "1350.5",
     "ltp": "1360", "pnl": "38", "symboltoken": "2885", "buyqty": "4", "sellqty": "0"}]})
p = a.positions()[0]
assert (p.qty, p.avg, p.pnl, p.product) == (4, 1350.5, 38.0, "INTRADAY")

# ------------------------------------------------------------------ Fyers
fy = mk("fyers", {"app_id": "XB1-100", "api_secret": "sec", "access_token": "ftok"},
        [{**ROW, "token": "NSE:RELIANCE-EQ", "tsym": "NSE:RELIANCE-EQ", "name": "RELIANCE", "seg": "1010",
          "fytoken": "10100000002885", "extoken": "2885"}])
ROUTES[("POST", "/orders/sync")] = (200, {"s": "ok", "code": 1101, "message": "placed", "id": "F1"})
assert fy.place(OrderRequest("RELIANCE", "BUY", 1, "LIMIT", "DELIVERY", price=1300,
                             stop_loss=10, take_profit=20)) == "F1"
c = last("POST", "/orders/sync")
js = json.loads(c["body"])
assert c["headers"]["Authorization"] == "XB1-100:ftok" and c["headers"]["version"] == "3"
assert js["symbol"] == "NSE:RELIANCE-EQ" and js["type"] == 1 and js["side"] == 1 and js["productType"] == "CNC"
assert js["stopLoss"] == 10 and js["takeProfit"] == 20
ROUTES[("POST", "/validate-authcode")] = (200, {"s": "ok", "access_token": "FNEW", "refresh_token": "FR"})
got = fy.complete_login({"s": "ok", "code": "200", "auth_code": "AC"})
assert got["access_token"] == "FNEW"
assert json.loads(last("POST", "/validate-authcode")["body"])["appIdHash"] == hashlib.sha256(b"XB1-100:sec").hexdigest()
ROUTES[("GET", "/orders")] = (200, {"s": "ok", "orderBook": [
    {"id": "F1", "symbol": "NSE:RELIANCE-EQ", "side": -1, "qty": 2, "filledQty": 0, "remainingQuantity": 2,
     "type": 4, "productType": "INTRADAY", "limitPrice": 1290, "stopPrice": 1291, "status": 6}]})
ods = fy.orders()
assert ods[0].side == "SELL" and ods[0].order_type == "SL" and ods[0].status == "trigger_pending"
inst = fy.resolve("RELIANCE")
assert fy.hsm_topic(inst) == "sf|nse_cm|2885"

# ------------------------------------------------------------------ Dhan
d = mk("dhan", {"client_id": "1100", "access_token": "dt"},
       [{**ROW, "token": "2885", "tsym": "RELIANCE", "name": "RELIANCE", "tick": 0.1, "seg": "NSE_EQ"}])
ROUTES[("POST", "/orders")] = (200, {"orderId": "D1", "orderStatus": "TRANSIT"})
assert d.place(OrderRequest("RELIANCE", "BUY", 1, "MARKET", "INTRADAY", stop_loss=5, take_profit=10)) == "D1"
c = last("POST", "/orders")
js = json.loads(c["body"])
assert c["headers"]["access-token"] == "dt" and c["headers"]["client-id"] == "1100"
assert js["securityId"] == "2885" and js["exchangeSegment"] == "NSE_EQ" and js["productType"] == "BO"
assert js["boProfitValue"] == 10 and js["boStopLossValue"] == 5
ROUTES[("POST", "/orders")] = (400, {"errorType": "Order_Error", "errorCode": "DH-906",
                                     "errorMessage": "Order rejected: static IP not registered"})
try:
    d.place(OrderRequest("RELIANCE", "BUY", 1))
    raise AssertionError
except brokers.AccessError as e:
    assert "static IP" in str(e)

# ------------------------------------------------------------------ live-feed parsers
# Kite: one full packet (184 bytes) for token 738561 and one index packet (32 bytes)
pk = struct.pack(">IIIIIIIIIII", 738561, 136050, 5, 135000, 12345678, 1, 2, 135500, 137000, 134900, 135800)
pk += struct.pack(">IIIII", 1763540000, 0, 0, 0, 1763540001) + b"\0" * (184 - 64)
ix = struct.pack(">IIIIIIII", 256265, 2523450, 2530000, 2510000, 2515000, 2522000, 0, 1763540002)
frame = struct.pack(">H", 2) + struct.pack(">H", len(pk)) + pk + struct.pack(">H", len(ix)) + ix
kp = feeds.proto_for(z)
ev = kp.parse(frame)
ticks = [e for e in ev if e["kind"] == "tick"]
assert ticks[0] == {"kind": "tick", "key": ("KITE", "738561"), "ltp": 1360.5, "ltt": 1763540000.0, "vol": 12345678.0}
assert ticks[1]["ltp"] == 25234.5 and ticks[1]["ltt"] == 1763540002.0
assert {"kind": "prev", "key": ("KITE", "738561"), "prev": 1358.0} in ev
assert kp.parse(b"\x00") == [] and kp.parse('{"type":"order","data":{"order_id":"9"}}')[0]["data"]["order_id"] == "9"
assert json.loads(kp.subscribe([("KITE", "738561")])[1]) == {"a": "mode", "v": ["full", [738561]]}


# Upstox: hand-encoded protobuf FeedResponse with one marketFF entry
def vint(n):
    out = b""
    while True:
        b7 = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b7 | 0x80])
        else:
            return out + bytes([b7])


def fld(no, wt, payload):
    if wt == 0:
        return vint(no << 3) + vint(payload)
    if wt == 1:
        return vint(no << 3 | 1) + payload
    return vint(no << 3 | 2) + vint(len(payload)) + payload


ltpc = fld(1, 1, struct.pack("<d", 1361.25)) + fld(2, 0, 1763540000123) + fld(4, 1, struct.pack("<d", 1358.0))
market_ff = fld(1, 2, ltpc) + fld(6, 0, 4567890)
feed = fld(2, 2, fld(1, 2, market_ff))
entry = fld(1, 2, b"NSE_EQ|INE002A01018") + fld(2, 2, feed)
msg = fld(1, 0, 1) + fld(2, 2, entry) + fld(3, 0, 1763540000999)
ev = feeds.proto_for(u).parse(msg)
assert ev[0] == {"kind": "tick", "key": ("UPX", "NSE_EQ|INE002A01018"), "ltp": 1361.25, "ltt": 1763540000.123,
                 "vol": 4567890.0}
assert ev[1]["prev"] == 1358.0
# Angel: QUOTE packet (123 bytes) little-endian, prices in paise
ap = bytes([2, 1]) + b"2885".ljust(25, b"\0") + struct.pack("<qqq", 7, 1763540000000, 136025)
ap += struct.pack("<qqq", 10, 135900, 9876543) + struct.pack("<dd", 1.0, 2.0) + struct.pack("<qqqq", 1, 2, 3, 135800)
ev = feeds.proto_for(a).parse(ap)
assert ev[0] == {"kind": "tick", "key": ("1", "2885"), "ltp": 1360.25, "ltt": 1763540000.0, "vol": 9876543.0}
assert ev[1]["prev"] == 1358.0 and feeds.proto_for(a).ping() == "ping"
assert "feedToken=F2" in feeds.proto_for(a).url()      # the login above saved a new feed token
# Fyers: auth answer, then a snapshot and an update for sf|nse_cm|2885
fp = feeds.proto_for(fy)
fp.key(fy.resolve("RELIANCE"))
auth = struct.pack("!HB", 0, 1) + b"\x00\x01" + struct.pack("!H", 1) + b"K" + b"\x02" + struct.pack("!H", 4) \
    + struct.pack(">I", 0)
assert fp.parse(auth) == []
vals = [136050, 777, 1763540000, 1763540001] + [-2147483648] * 16 + [135800] + [-2147483648, -2147483648]
topic = b"sf|nse_cm|2885"
snap = bytes([83]) + struct.pack("<H", 7) + bytes([len(topic)]) + topic + bytes([len(vals)])
snap += b"".join(struct.pack(">i", v) for v in vals) + b"\0\0" + struct.pack(">H", 1) + bytes([2])
snap += bytes([3]) + b"NSE" + bytes([4]) + b"2885" + bytes([8]) + b"RELIANCE"
hdr = struct.pack("!HB", 0, 6) + struct.pack(">I", 1) + struct.pack("!H", 1)
ev = fp.parse(hdr + snap)
assert ev[0] == {"kind": "tick", "key": ("FY", "NSE:RELIANCE-EQ"), "ltp": 1360.5, "ltt": 1763540000.0, "vol": 777.0}
assert ev[1]["prev"] == 1358.0
upd = bytes([85]) + struct.pack("<H", 7) + bytes([2]) + struct.pack(">i", 136100) + struct.pack(">i", 800)
ev = fp.parse(hdr + upd)
assert ev[0]["ltp"] == 1361.0 and ev[0]["vol"] == 800.0
assert fp.on_open()[0][2:4] == bytes([1, 4]) and b"XB1-100:FNEW" in fp.on_open()[0]
assert b"sf|nse_cm|2885" in fp.subscribe([("FY", "NSE:RELIANCE-EQ")])[0]
# Dhan: a ticker packet + a disconnect code
dp = feeds.proto_for(d)
pkt = bytes([2]) + struct.pack("<H", 16) + bytes([1]) + struct.pack("<I", 2885) + struct.pack("<fI", 1360.5, 1763540000)
bye = bytes([50]) + struct.pack("<H", 10) + bytes([0]) + struct.pack("<I", 0) + struct.pack("<H", 807)
ev = dp.parse(pkt + bye)
assert ev[0]["key"] == ("NSE_EQ", "2885") and ev[0]["ltp"] == 1360.5
assert ev[1]["kind"] == "error" and ev[1]["fatal"] and "expired" in ev[1]["msg"]

# ------------------------------------------------------------------ paper trading
pdir = tempfile.mkdtemp()
pb = brokers.Paper({"name": "practice", "broker": "paper", "capital": "100000"}, folder=__import__("pathlib").Path(pdir))
try:
    pb.place(OrderRequest("RELIANCE", "BUY", 10))
    raise AssertionError("market order without a price was filled")
except brokers.Rejected:
    pass
brokers.Paper.set_price("RELIANCE", 1000.0)
o1 = pb.place(OrderRequest("RELIANCE", "BUY", 10))                     # fills at 1000
o2 = pb.place(OrderRequest("RELIANCE", "SELL", 4, "LIMIT", price=1010))  # waits
o3 = pb.place(OrderRequest("RELIANCE", "SELL", 6, "SL-M", trigger=990))  # waits for the stop
assert {x.id: x.status for x in pb.orders()} == {o1: "complete", o2: "open", o3: "trigger_pending"}
assert pb.on_price("RELIANCE", 1005) == []
assert pb.on_price("RELIANCE", 1012) == [o2]
pos = pb.positions()[0]
assert pos.qty == 6 and pos.realised == 4 * 12 and pos.avg == 1000
assert pb.on_price("RELIANCE", 989) == [o3]
pos = pb.positions()[0]
assert pos.qty == 0 and round(pos.realised, 6) == 4 * 12 - 6 * 11
f = pb.funds()
assert round(f.total, 6) == 100000 + 48 - 66 and f.used == 0
# bracket: entry fills, take-profit and stop are placed, the first to fill cancels the other
brokers.Paper.set_price("NIFTY FUT", 25000)
e = pb.place(OrderRequest("NIFTY FUT", "BUY", 65, stop_loss=50, take_profit=100))
kids = [x for x in pb.orders() if x.raw.get("parent") == e]
assert sorted(x.order_type for x in kids) == ["LIMIT", "SL-M"]
assert pb.on_price("NIFTY FUT", 25101) and all(
    x.status in ("complete", "cancelled") for x in pb.orders() if x.raw.get("parent") == e)
assert [x.status for x in pb.orders() if x.raw.get("parent") == e and x.order_type == "SL-M"] == ["cancelled"]
assert pb.positions()[1].qty == 0 and round(pb.positions()[1].realised) == 101 * 65
# survives a restart (a new object reads the same file)
pb2 = brokers.Paper({"name": "practice", "broker": "paper"}, folder=__import__("pathlib").Path(pdir))
assert len(pb2.orders()) == len(pb.orders())
# an index can be paper traded at its price
brokers.Paper.set_price("NIFTY", 25200)
pb.place(OrderRequest("NIFTY", "SELL", 1, instrument=inst_idx))
# cancel / modify
o4 = pb.place(OrderRequest("RELIANCE", "BUY", 1, "LIMIT", price=900))
pb.modify(next(x for x in pb.orders() if x.id == o4), price=950)
assert next(x for x in pb.orders() if x.id == o4).price == 950
pb.cancel(next(x for x in pb.orders() if x.id == o4))
assert next(x for x in pb.orders() if x.id == o4).status == "cancelled"

# ------------------------------------------------------------------ one order to many accounts
ROUTES[("POST", "/orders/regular")] = (200, {"status": "success", "data": {"order_id": "Z9"}})
real = {"name": "t-zerodha", "broker": "zerodha", "api_key": "kk", "access_token": "tt", "live": True}
paper_acct = {"name": "Practice2", "broker": "paper"}
orig_get = brokers.get
brokers.get = lambda acct, http=None, base_url=None: z if acct.get("broker") == "zerodha" else orig_get(acct)
try:
    res = brokers.place_many([real, paper_acct, {**real, "name": "notlive", "live": False}],
                             OrderRequest("RELIANCE", "BUY", 1))
finally:
    brokers.get = orig_get
by = {r["account"]: r for r in res}
assert by["t-zerodha"]["ok"] and by["t-zerodha"]["order_id"] == "Z9" and not by["t-zerodha"]["paper"]
assert by["Practice2"]["ok"] and by["Practice2"]["paper"]
assert by["notlive"]["ok"] and by["notlive"]["paper"], "an account that is not LIVE reached the broker"

srv.shutdown()
print("BROKERS OK")
