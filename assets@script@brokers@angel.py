"""Angel One - SmartAPI (https://smartapi.angelbroking.com/docs).

Login, either way:
  * in ChartLab: client code + PIN + the 6-digit TOTP code from your authenticator (or a saved TOTP key);
  * in the browser: Angel's publisher login page -> tokens come back on the redirect.
The session (JWT) lasts the trading day; a refresh token renews it without logging in again."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import socket
import struct
import time
import urllib.parse
import uuid

import pandas as pd

from .base import (IST, AccessError, AuthError, Broker, BrokerError, Funds, Holding, Instrument, NetError, Order,
                   OrderRequest, Position, RateLimited, Rejected, candles_frame, chunks, classify, fetch_bytes, inum,
                   ip_hint, jwt_expiry, limiter, master, num, resample, span)

PRODUCT_OUT = {"INTRADAY": "INTRADAY", "DELIVERY": "DELIVERY", "MARGIN": "CARRYFORWARD", "MTF": "MARGIN"}
PRODUCT_IN = {"INTRADAY": "INTRADAY", "DELIVERY": "DELIVERY", "CARRYFORWARD": "MARGIN", "MARGIN": "MTF",
              "BO": "INTRADAY", "CO": "INTRADAY"}
TYPE_OUT = {"MARKET": "MARKET", "LIMIT": "LIMIT", "SL": "STOPLOSS_LIMIT", "SL-M": "STOPLOSS_MARKET"}
TYPE_IN = {v: k for k, v in TYPE_OUT.items()}
WS_EXCH = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5, "NCO": 7, "CDS": 13}
INTERVALS = {"1m": ("ONE_MINUTE", 30), "3m": ("THREE_MINUTE", 60), "5m": ("FIVE_MINUTE", 100),
             "10m": ("TEN_MINUTE", 100), "15m": ("FIFTEEN_MINUTE", 200), "30m": ("THIRTY_MINUTE", 200),
             "1h": ("ONE_HOUR", 400), "1d": ("ONE_DAY", 2000)}


def totp_now(secret: str, t: float | None = None, digits: int = 6, step: int = 30) -> str:
    """RFC 6238 time-based code from a base32 TOTP key (what authenticator apps show)."""
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    counter = int((time.time() if t is None else t) // step)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def _local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


class Angel(Broker):
    id = "angel"
    name = "Angel One"
    base = "https://apiconnect.angelone.in"
    login_base = "https://smartapi.angelone.in/publisher-login"
    caps = frozenset({"login_totp", "login_oauth", "token_paste", "renew", "history", "quotes", "stream", "orders",
                      "modify", "cancel", "positions", "holdings", "funds", "margin", "amo", "bracket"})
    fields = [("api_key", "API key", "text", True, "From smartapi.angelbroking.com -> My apps"),
              ("client_code", "Client code", "text", True, "Your Angel One login id"),
              ("pin", "PIN", "pin", False, "Kept only on this Mac. Leave empty to type it at each login"),
              ("totp_secret", "TOTP key", "totp", False, "Optional: the key behind your authenticator, for one-click login"),
              ("access_token", "Session token", "secret", False, "Filled in by Log in")]
    products = ("INTRADAY", "DELIVERY", "MARGIN", "MTF")
    exchanges = ("NSE", "BSE", "NFO", "BFO", "MCX", "CDS")
    docs = "https://smartapi.angelbroking.com/"
    index_names = {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY", "FINNIFTY": "FINNIFTY", "MIDCPNIFTY": "MIDCPNIFTY",
                   "INDIAVIX": "INDIA VIX", "SENSEX": "SENSEX", "BANKEX": "BANKEX", "NIFTYNXT50": "NIFTYNXT50"}

    def _hdr(self) -> dict:
        ip = _local_ip()
        mac = ":".join(f"{(uuid.getnode() >> s) & 0xff:02x}" for s in range(40, -1, -8))
        h = {"Content-Type": "application/json", "Accept": "application/json", "X-UserType": "USER",
             "X-SourceID": "WEB", "X-ClientLocalIP": ip, "X-ClientPublicIP": ip, "X-MACAddress": mac,
             "X-PrivateKey": self.acct.get("api_key", "")}
        if self.acct.get("access_token"):
            h["Authorization"] = f"Bearer {self.acct['access_token']}"
        return h

    def _call(self, method, path, *, json_body=None, kind="api", order=False, auth=True):
        rate = {"api": (10, 10), "order": (10, 10), "hist": (3, 3), "quote": (10, 10)}[kind]
        lim = limiter(f"angel:{self.acct.get('api_key')}:{kind}", *rate)
        hdr = self._hdr()
        if not auth:
            hdr.pop("Authorization", None)
        r = self.http.send(method, self.base + path, json_body=json_body, headers=hdr,
                           safe=not order and method == "GET", limit=lim)
        js = r.json()
        if r.status >= 400 or not isinstance(js, dict) or js.get("status") in (False, "false"):
            msg = (js or {}).get("message") if isinstance(js, dict) else r.text[:200]
            code = (js or {}).get("errorcode", "") if isinstance(js, dict) else ""
            msg = f"Angel One: {msg or f'HTTP {r.status}'}" + (f" ({code})" if code else "")
            cls = classify(msg, r.status, Rejected if order else BrokerError)
            if code in ("AG8001", "AG8002", "AG8003", "AB1010", "AB8050", "AB8051") or r.status == 401:
                cls = AuthError
            if cls is AccessError and order and "ip" in msg.lower():
                msg += ". " + ip_hint("Angel One")
            raise cls(msg + (" - log in again" if cls is AuthError else ""), code or r.status, js)
        return js.get("data")

    # ------------------------------------------------------------------ login
    def login_url(self, redirect: str = "", state: str = "chartlab") -> str:
        if not self.acct.get("api_key"):
            raise BrokerError("Angel One: fill in the API key first")
        return f"{self.login_base}?{urllib.parse.urlencode({'api_key': self.acct['api_key'], 'state': state})}"

    def complete_login(self, params: dict, redirect: str = "") -> dict:
        tok = params.get("auth_token") or params.get("jwtToken")
        if not tok:
            raise AuthError("Angel One login did not finish (no auth_token on the redirect)")
        tok = tok.replace("Bearer ", "")
        self.acct["access_token"] = tok
        return self._done({"access_token": tok, "feed_token": params.get("feed_token", ""),
                           "refresh_token": params.get("refresh_token", ""), "login_time": time.time()})

    def login_totp(self, pin: str = "", code: str = "") -> dict:
        """Log in with client code + PIN + TOTP (typed, or made from the saved TOTP key)."""
        pin = pin or self.acct.get("pin", "")
        if not code and self.acct.get("totp_secret"):
            code = totp_now(self.acct["totp_secret"])
        if not (self.acct.get("client_code") and pin and code):
            raise AuthError("Angel One needs the client code, PIN and the 6-digit TOTP code")
        d = self._call("POST", "/rest/auth/angelbroking/user/v1/loginByPassword", auth=False, order=True,
                       json_body={"clientcode": self.acct["client_code"], "password": pin, "totp": code}) or {}
        tok = (d.get("jwtToken") or "").replace("Bearer ", "")
        if not tok:
            raise AuthError("Angel One answered without a session token")
        self.acct["access_token"] = tok
        return self._done({"access_token": tok, "refresh_token": d.get("refreshToken", ""),
                           "feed_token": d.get("feedToken", ""), "login_time": time.time()})

    def renew(self) -> dict:
        if not self.acct.get("refresh_token"):
            raise AuthError("Angel One: no refresh token saved - log in again")
        d = self._call("POST", "/rest/auth/angelbroking/jwt/v1/generateTokens", order=True,
                       json_body={"refreshToken": self.acct["refresh_token"]}) or {}
        tok = (d.get("jwtToken") or "").replace("Bearer ", "")
        if not tok:
            raise AuthError("Angel One did not renew the session - log in again")
        self.acct["access_token"] = tok
        return self._done({"access_token": tok, "refresh_token": d.get("refreshToken", self.acct["refresh_token"]),
                           "feed_token": d.get("feedToken", self.acct.get("feed_token", ""))})

    def token_expiry(self) -> float | None:
        return jwt_expiry(self.acct.get("access_token", ""))

    # ------------------------------------------------------------------ account
    def profile(self) -> dict:
        d = self._call("GET", "/rest/secure/angelbroking/user/v1/getProfile") or {}
        return {"client": d.get("clientcode", ""), "name": d.get("name", ""), "email": d.get("email", ""),
                "exchanges": d.get("exchanges", []), "products": d.get("products", [])}

    def funds(self) -> Funds:
        d = self._call("GET", "/rest/secure/angelbroking/user/v1/getRMS") or {}
        avail = num(d.get("net") or d.get("availablecash"))
        used = num(d.get("utiliseddebits"))
        return Funds(available=avail, used=used, total=avail + used, raw=d)

    def positions(self) -> list[Position]:
        out = []
        for p in self._call("GET", "/rest/secure/angelbroking/order/v1/getPosition") or []:
            q = inum(p.get("netqty"))
            avg = num(p.get("avgnetprice") or p.get("netprice"))
            ltp = num(p.get("ltp"))
            realised = num(p.get("realised"))
            unreal = num(p.get("unrealised"))
            pnl = num(p.get("pnl"), realised + unreal)
            out.append(Position(symbol=p.get("tradingsymbol", ""), exchange=p.get("exchange", ""),
                                product=PRODUCT_IN.get(str(p.get("producttype", "")).upper(), "INTRADAY"),
                                qty=q, avg=avg, ltp=ltp, pnl=pnl, realised=realised, unrealised=unreal,
                                buy_qty=inum(p.get("buyqty")), sell_qty=inum(p.get("sellqty")),
                                buy_avg=num(p.get("buyavgprice")), sell_avg=num(p.get("sellavgprice")),
                                token=str(p.get("symboltoken", "")), raw=p))
        return out

    def holdings(self) -> list[Holding]:
        d = self._call("GET", "/rest/secure/angelbroking/portfolio/v1/getAllHolding") or {}
        rows = d.get("holdings") if isinstance(d, dict) else d
        out = []
        for h in rows or []:
            out.append(Holding(symbol=h.get("tradingsymbol", ""), exchange=h.get("exchange", ""),
                               qty=inum(h.get("quantity")) + inum(h.get("t1quantity")), avg=num(h.get("averageprice")),
                               ltp=num(h.get("ltp")), pnl=num(h.get("profitandloss")),
                               day_change=num(h.get("pnlpercentage")), isin=h.get("isin", ""),
                               token=str(h.get("symboltoken", "")), raw=h))
        return out

    def orders(self) -> list[Order]:
        out = []
        for o in self._call("GET", "/rest/secure/angelbroking/order/v1/getOrderBook") or []:
            out.append(Order(id=str(o.get("orderid")), symbol=o.get("tradingsymbol", ""), exchange=o.get("exchange", ""),
                             side=o.get("transactiontype", ""), qty=inum(o.get("quantity")),
                             filled=inum(o.get("filledshares")), pending=inum(o.get("unfilledshares")),
                             order_type=TYPE_IN.get(o.get("ordertype"), o.get("ordertype", "")),
                             product=PRODUCT_IN.get(str(o.get("producttype", "")).upper(), "INTRADAY"),
                             price=num(o.get("price")), trigger=num(o.get("triggerprice")),
                             avg_price=num(o.get("averageprice")), status=self._status_of(o.get("status")),
                             message=o.get("text") or "", time=str(o.get("updatetime") or ""),
                             token=str(o.get("symboltoken", "")), tag=o.get("ordertag") or "", raw=o))
        return out

    def trades(self) -> list[dict]:
        return self._call("GET", "/rest/secure/angelbroking/order/v1/getTradeBook") or []

    # ------------------------------------------------------------------ instruments
    def instruments(self) -> pd.DataFrame:
        def build():
            raw = json.loads(fetch_bytes(
                "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json", timeout=240))
            rows = []
            for x in raw:
                ex, it, sym = x.get("exch_seg", ""), x.get("instrumenttype", ""), x.get("symbol", "")
                if it == "AMXIDX":
                    kind = "IDX"
                elif it == "":
                    if ex == "NSE" and not (sym.endswith("-EQ") or sym.endswith("-BE") or sym.endswith("-SM")
                                            or sym.endswith("-BZ") or sym.endswith("-ST")):
                        continue
                    if ex not in ("NSE", "BSE"):
                        continue
                    kind = "EQ"
                elif it.startswith("FUT"):
                    kind = "FUT"
                elif it.startswith("OPT"):
                    kind = "CE" if sym.endswith("CE") else "PE" if sym.endswith("PE") else ""
                    if not kind:
                        continue
                else:
                    continue
                e = x.get("expiry") or ""
                try:
                    exp = dt.datetime.strptime(e, "%d%b%Y").date() if e else None
                except ValueError:
                    exp = None
                pri = 0 if sym.endswith("-EQ") or ex != "NSE" else 1
                rows.append((x.get("token", ""), ex, sym, x.get("name", ""), kind, exp,
                             num(x.get("strike")) / 100.0 if kind in ("CE", "PE") else 0.0,
                             inum(x.get("lotsize"), 1), num(x.get("tick_size"), 5.0) / 100.0, ex, pri))
            df = pd.DataFrame(rows, columns=["token", "exchange", "tsym", "name", "kind", "expiry", "strike", "lot",
                                             "tick", "seg", "_pri"])
            return df.sort_values("_pri", kind="stable").drop(columns="_pri")
        return master("angel", build)

    # ------------------------------------------------------------------ trading
    def place(self, req: OrderRequest) -> str:
        r = self.prepare(req)
        i = r.instrument
        variety = "AMO" if r.amo else ("STOPLOSS" if r.order_type in ("SL", "SL-M") else "NORMAL")
        product = PRODUCT_OUT[r.product]
        body = {"variety": variety, "tradingsymbol": i.tsym, "symboltoken": i.token, "transactiontype": r.side,
                "exchange": i.exchange, "ordertype": TYPE_OUT[r.order_type], "producttype": product,
                "duration": r.validity, "price": str(r.price or 0), "triggerprice": str(r.trigger or 0),
                "squareoff": "0", "stoploss": "0", "quantity": str(r.qty), "ordertag": r.tag}
        if (r.stop_loss or r.take_profit) and "bracket" in self.caps and r.product == "INTRADAY" and not r.amo:
            body.update(variety="ROBO", producttype="BO", squareoff=str(r.take_profit or 0),
                        stoploss=str(r.stop_loss or 0))
        d = self._call("POST", "/rest/secure/angelbroking/order/v1/placeOrder", json_body=body, kind="order",
                       order=True) or {}
        oid = d.get("orderid") or d.get("uniqueorderid")
        if not oid:
            raise Rejected(f"Angel One did not return an order id: {d}")
        return str(oid)

    def modify(self, order: Order, qty=None, price=None, trigger=None, order_type=None) -> str:
        raw = order.raw or {}
        body = {"variety": raw.get("variety") or "NORMAL", "orderid": order.id,
                "ordertype": TYPE_OUT.get(order_type or order.order_type, raw.get("ordertype", "LIMIT")),
                "producttype": raw.get("producttype") or PRODUCT_OUT.get(order.product, "INTRADAY"),
                "duration": raw.get("duration") or "DAY",
                "price": str(price if price is not None else order.price),
                "quantity": str(qty if qty is not None else order.qty),
                "triggerprice": str(trigger if trigger is not None else order.trigger),
                "tradingsymbol": order.symbol, "symboltoken": order.token, "exchange": order.exchange}
        d = self._call("POST", "/rest/secure/angelbroking/order/v1/modifyOrder", json_body=body, kind="order",
                       order=True) or {}
        return str(d.get("orderid") or order.id)

    def cancel(self, order: Order) -> str:
        d = self._call("POST", "/rest/secure/angelbroking/order/v1/cancelOrder", kind="order", order=True,
                       json_body={"variety": (order.raw or {}).get("variety") or "NORMAL", "orderid": order.id}) or {}
        return str(d.get("orderid") or order.id)

    def margin(self, req: OrderRequest) -> dict | None:
        r = self.prepare(req)
        d = self._call("POST", "/rest/secure/angelbroking/margin/v1/batch", json_body={"positions": [{
            "exchange": r.instrument.exchange, "qty": r.qty, "price": r.price or 0,
            "productType": PRODUCT_OUT[r.product], "token": r.instrument.token, "tradeType": r.side,
            "orderType": TYPE_OUT[r.order_type]}]}) or {}
        if d:
            return {"required": num(d.get("totalMarginRequired"))}
        return None

    # ------------------------------------------------------------------ data
    def history(self, symbol, interval: str, period: str) -> pd.DataFrame:
        inst = self.resolve(symbol)
        base_iv = interval if interval in INTERVALS else {"2m": "1m", "45m": "15m", "2h": "1h", "3h": "1h",
                                                          "4h": "1h", "1wk": "1d", "1mo": "1d"}.get(interval)
        if base_iv is None:
            raise BrokerError(f"Angel One has no {interval} candles")
        name, per = INTERVALS[base_iv]
        intraday = base_iv != "1d"
        start, end = span(period, intraday, 2000)
        frames = []
        for a, b in chunks(start, end, per):
            d = self._call("POST", "/rest/secure/angelbroking/historical/v1/getCandleData", kind="hist",
                           json_body={"exchange": inst.exchange, "symboltoken": inst.token, "interval": name,
                                      "fromdate": f"{a} 09:00", "todate": f"{b} 15:30"}) or []
            frames.append(candles_frame(d))
        df = pd.concat([f for f in frames if len(f)] or [candles_frame([])])
        df = df.drop_duplicates("Date", keep="last").sort_values("Date").reset_index(drop=True)
        return resample(df, interval) if base_iv != interval else df

    def ltp(self, instruments: list[Instrument]) -> dict:
        out = {}
        for k in range(0, len(instruments), 50):
            ex: dict = {}
            for i in instruments[k:k + 50]:
                ex.setdefault(i.exchange, []).append(i.token)
            d = self._call("POST", "/rest/secure/angelbroking/market/v1/quote/", kind="quote",
                           json_body={"mode": "LTP", "exchangeTokens": ex}) or {}
            for f in d.get("fetched") or []:
                out[str(f.get("symbolToken"))] = num(f.get("ltp"))
        return out
