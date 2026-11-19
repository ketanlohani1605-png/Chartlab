"""Zerodha - Kite Connect v3 (https://kite.trade/docs/connect/v3/).

Login: the Kite login page in the browser -> request_token on the redirect -> access token (valid until 06:00 IST
next morning). Orders, portfolio and funds are in every Kite Connect plan; candles and live prices need the paid
data add-on."""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import time
import urllib.parse

import pandas as pd

from .base import (IST, AccessError, AuthError, Broker, BrokerError, Funds, Holding, Instrument, NetError, Order,
                   OrderRequest, Position, RateLimited, Rejected, candles_frame, chunks, classify, fetch_bytes, inum,
                   ip_hint, limiter, master, next_ist, num, resample, span)

PRODUCT_OUT = {"INTRADAY": "MIS", "DELIVERY": "CNC", "MARGIN": "NRML", "MTF": "MTF"}
PRODUCT_IN = {v: k for k, v in PRODUCT_OUT.items()}
INTERVALS = {"1m": ("minute", 60), "3m": ("3minute", 100), "5m": ("5minute", 100), "10m": ("10minute", 100),
             "15m": ("15minute", 200), "30m": ("30minute", 200), "1h": ("60minute", 400), "1d": ("day", 2000)}


class Zerodha(Broker):
    id = "zerodha"
    name = "Zerodha"
    base = "https://api.kite.trade"
    login_base = "https://kite.zerodha.com/connect/login"
    caps = frozenset({"login_oauth", "token_paste", "history", "quotes", "stream", "orders", "modify", "cancel",
                      "positions", "holdings", "funds", "margin", "amo", "order_stream"})
    fields = [("api_key", "API key", "text", True, "From developers.kite.trade -> your app"),
              ("api_secret", "API secret", "secret", True, "Shown once when the app is created"),
              ("access_token", "Access token", "secret", False, "Filled in by 'Log in with Zerodha'")]
    products = ("INTRADAY", "DELIVERY", "MARGIN", "MTF")
    exchanges = ("NSE", "BSE", "NFO", "BFO", "MCX", "CDS")
    docs = "https://developers.kite.trade/apps"
    data_note = "Candles and live prices need the Kite Connect data plan; orders and portfolio work without it."
    index_names = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FIN SERVICE",
                   "MIDCPNIFTY": "NIFTY MID SELECT", "NIFTYNXT50": "NIFTY NEXT 50", "SENSEX": "SENSEX",
                   "BANKEX": "BANKEX", "INDIAVIX": "INDIA VIX", "SENSEX50": "SNSX50"}

    # ------------------------------------------------------------------ plumbing
    def _hdr(self) -> dict:
        h = {"X-Kite-Version": "3"}
        if self.acct.get("access_token"):
            h["Authorization"] = f"token {self.acct.get('api_key', '')}:{self.acct['access_token']}"
        return h

    def _call(self, method: str, path: str, *, params=None, form=None, json_body=None, kind="api", order=False):
        rate = {"api": (10, 10), "quote": (1, 1), "hist": (3, 3), "order": (10, 10)}[kind]
        lim = limiter(f"zerodha:{self.acct.get('api_key')}:{kind}", *rate)
        r = self.http.send(method, self.base + path, params=params, form=form, json_body=json_body,
                           headers=self._hdr(), safe=not order and method == "GET", limit=lim)
        js = r.json()
        if r.status >= 400 or not isinstance(js, dict) or js.get("status") == "error":
            msg = (js or {}).get("message") if isinstance(js, dict) else r.text[:200]
            et = (js or {}).get("error_type", "") if isinstance(js, dict) else ""
            msg = f"Zerodha: {msg or f'HTTP {r.status}'}"
            if et == "TokenException":
                raise AuthError(msg + " - log in again", r.status, js)
            if et == "PermissionException" or classify(msg, r.status) is AccessError:
                if order and any(w in msg.lower() for w in ("ip", "whitelist")):
                    msg += ". " + ip_hint("Zerodha")
                raise AccessError(msg, r.status, js)
            if et == "NetworkException":
                raise NetError(msg, r.status, js)
            if r.status == 429:
                raise RateLimited(msg, r.status, js)
            if order or et in ("OrderException", "InputException", "MarginException"):
                raise Rejected(msg, r.status, js)
            raise classify(msg, r.status)(msg, r.status, js)
        return js.get("data")

    # ------------------------------------------------------------------ login
    def login_url(self, redirect: str = "", state: str = "chartlab") -> str:
        if not self.acct.get("api_key"):
            raise BrokerError("Zerodha: fill in the API key first")
        q = {"v": "3", "api_key": self.acct["api_key"], "redirect_params": urllib.parse.urlencode({"state": state})}
        return f"{self.login_base}?{urllib.parse.urlencode(q)}"

    def complete_login(self, params: dict, redirect: str = "") -> dict:
        rt = (params.get("request_token") or "").strip()
        if params.get("status") not in (None, "", "success") or not rt:
            raise AuthError(f"Zerodha login did not finish ({params.get('status') or 'no request_token'})")
        key, secret = self.acct.get("api_key", ""), self.acct.get("api_secret", "")
        if not key or not secret:
            raise BrokerError("Zerodha: API key and API secret are both needed to finish the login")
        chk = hashlib.sha256((key + rt + secret).encode()).hexdigest()
        data = self._call("POST", "/session/token", form={"api_key": key, "request_token": rt, "checksum": chk})
        tok = (data or {}).get("access_token")
        if not tok:
            raise AuthError("Zerodha answered without an access token")
        self.acct["access_token"] = tok
        return self._done({"access_token": tok, "user_id": data.get("user_id", ""), "login_time": time.time()})

    def token_expiry(self) -> float | None:
        lt = self.acct.get("login_time")
        return next_ist(6, 0, float(lt)) if lt else None

    # ------------------------------------------------------------------ account
    def profile(self) -> dict:
        d = self._call("GET", "/user/profile") or {}
        return {"client": d.get("user_id", ""), "name": d.get("user_name", ""), "email": d.get("email", ""),
                "exchanges": d.get("exchanges", []), "products": d.get("products", [])}

    def funds(self) -> Funds:
        d = self._call("GET", "/user/margins") or {}
        eq, co = d.get("equity") or {}, d.get("commodity") or {}
        avail = num(eq.get("net")) + (num(co.get("net")) if co.get("enabled") else 0)
        used = num((eq.get("utilised") or {}).get("debits")) + num((co.get("utilised") or {}).get("debits"))
        return Funds(available=avail, used=used, total=avail + used, raw=d)

    def positions(self) -> list[Position]:
        d = self._call("GET", "/portfolio/positions") or {}
        out = []
        for p in d.get("net") or []:
            out.append(Position(symbol=p.get("tradingsymbol", ""), exchange=p.get("exchange", ""),
                                product=PRODUCT_IN.get(p.get("product"), p.get("product", "")),
                                qty=inum(p.get("quantity")), avg=num(p.get("average_price")),
                                ltp=num(p.get("last_price")), pnl=num(p.get("pnl")), realised=num(p.get("realised")),
                                unrealised=num(p.get("unrealised")), buy_qty=inum(p.get("buy_quantity")),
                                sell_qty=inum(p.get("sell_quantity")), buy_avg=num(p.get("buy_price")),
                                sell_avg=num(p.get("sell_price")), token=str(p.get("instrument_token", "")), raw=p))
        return out

    def holdings(self) -> list[Holding]:
        out = []
        for h in self._call("GET", "/portfolio/holdings") or []:
            qty = inum(h.get("quantity")) + inum(h.get("t1_quantity"))
            out.append(Holding(symbol=h.get("tradingsymbol", ""), exchange=h.get("exchange", ""), qty=qty,
                               avg=num(h.get("average_price")), ltp=num(h.get("last_price")), pnl=num(h.get("pnl")),
                               day_change=num(h.get("day_change_percentage")), isin=h.get("isin", ""),
                               token=str(h.get("instrument_token", "")), raw=h))
        return out

    def orders(self) -> list[Order]:
        out = []
        for o in self._call("GET", "/orders") or []:
            out.append(Order(id=str(o.get("order_id")), symbol=o.get("tradingsymbol", ""), exchange=o.get("exchange", ""),
                             side=o.get("transaction_type", ""), qty=inum(o.get("quantity")),
                             filled=inum(o.get("filled_quantity")), pending=inum(o.get("pending_quantity")),
                             order_type=o.get("order_type", ""),
                             product=PRODUCT_IN.get(o.get("product"), o.get("product", "")),
                             price=num(o.get("price")), trigger=num(o.get("trigger_price")),
                             avg_price=num(o.get("average_price")), status=self._status_of(o.get("status")),
                             message=o.get("status_message") or "", time=str(o.get("order_timestamp") or ""),
                             token=str(o.get("instrument_token", "")), tag=o.get("tag") or "", raw=o))
        return out

    def trades(self) -> list[dict]:
        return self._call("GET", "/trades") or []

    # ------------------------------------------------------------------ instruments
    def instruments(self) -> pd.DataFrame:
        def build():
            raw = pd.read_csv(io.BytesIO(fetch_bytes("https://api.kite.trade/instruments")), low_memory=False)
            idx = raw["segment"] == "INDICES"
            kind = raw["instrument_type"].where(~idx, "IDX")
            eq = kind == "EQ"
            return pd.DataFrame({
                "token": raw["instrument_token"].astype(str), "exchange": raw["exchange"], "tsym": raw["tradingsymbol"],
                "name": raw["tradingsymbol"].where(eq, raw["name"]).where(~idx, raw["tradingsymbol"]),
                "kind": kind, "expiry": raw["expiry"], "strike": raw["strike"], "lot": raw["lot_size"],
                "tick": raw["tick_size"], "seg": raw["segment"], "company": raw["name"]})
        return master("zerodha", build)

    # ------------------------------------------------------------------ trading
    def _order_form(self, r: OrderRequest) -> dict:
        i = r.instrument
        f = {"tradingsymbol": i.tsym, "exchange": i.exchange, "transaction_type": r.side, "order_type": r.order_type,
             "quantity": r.qty, "product": PRODUCT_OUT[r.product], "validity": r.validity, "tag": r.tag[:20]}
        if r.order_type in ("LIMIT", "SL"):
            f["price"] = r.price
        if r.order_type in ("SL", "SL-M"):
            f["trigger_price"] = r.trigger
        if r.order_type in ("MARKET", "SL-M"):
            f["market_protection"] = -1          # exchange rule for API market orders: automatic protection band
        if r.disclosed:
            f["disclosed_quantity"] = r.disclosed
        return f

    def place(self, req: OrderRequest) -> str:
        r = self.prepare(req)
        variety = "amo" if r.amo else "regular"
        d = self._call("POST", f"/orders/{variety}", form=self._order_form(r), kind="order", order=True) or {}
        oid = d.get("order_id")
        if not oid:
            raise Rejected(f"Zerodha did not return an order id: {d}")
        return str(oid)

    def modify(self, order: Order, qty=None, price=None, trigger=None, order_type=None) -> str:
        variety = (order.raw or {}).get("variety") or "regular"
        f = {"quantity": qty, "price": price, "trigger_price": trigger, "order_type": order_type,
             "validity": (order.raw or {}).get("validity") or "DAY"}
        d = self._call("PUT", f"/orders/{variety}/{order.id}", form={k: v for k, v in f.items() if v is not None},
                       kind="order", order=True) or {}
        return str(d.get("order_id") or order.id)

    def cancel(self, order: Order) -> str:
        variety = (order.raw or {}).get("variety") or "regular"
        d = self._call("DELETE", f"/orders/{variety}/{order.id}", kind="order", order=True) or {}
        return str(d.get("order_id") or order.id)

    def margin(self, req: OrderRequest) -> dict | None:
        r = self.prepare(req)
        body = [{"exchange": r.instrument.exchange, "tradingsymbol": r.instrument.tsym, "transaction_type": r.side,
                 "variety": "regular", "product": PRODUCT_OUT[r.product], "order_type": r.order_type,
                 "quantity": r.qty, "price": r.price, "trigger_price": r.trigger}]
        d = self._call("POST", "/margins/orders", json_body=body) or []
        if d:
            return {"required": num(d[0].get("total")), "charges": num((d[0].get("charges") or {}).get("total"))}
        return None

    # ------------------------------------------------------------------ data
    def history(self, symbol, interval: str, period: str) -> pd.DataFrame:
        inst = self.resolve(symbol)
        base_iv = interval
        if interval not in INTERVALS:
            base_iv = {"2m": "1m", "45m": "15m", "2h": "1h", "3h": "1h", "4h": "1h", "1wk": "1d", "1mo": "1d"}.get(interval)
            if base_iv is None:
                raise BrokerError(f"Zerodha has no {interval} candles")
        name, max_days = INTERVALS[base_iv]
        intraday = base_iv != "1d"
        start, end = span(period, intraday, 2000 if not intraday else 3650)
        frames = []
        for a, b in chunks(start, end, max_days):
            d = self._call("GET", f"/instruments/historical/{inst.token}/{name}",
                           params={"from": f"{a} 00:00:00", "to": f"{b} 23:59:59", "continuous": 0, "oi": 0},
                           kind="hist") or {}
            frames.append(candles_frame(d.get("candles") or []))
        df = pd.concat([f for f in frames if len(f)] or [candles_frame([])]).drop_duplicates("Date").reset_index(drop=True)
        return resample(df, interval) if base_iv != interval else df

    def ltp(self, instruments: list[Instrument]) -> dict:
        keys = [f"{i.exchange}:{i.tsym}" for i in instruments][:1000]
        if not keys:
            return {}
        d = self._call("GET", "/quote/ltp", params={"i": keys}, kind="quote") or {}
        by = {f"{i.exchange}:{i.tsym}": i.token for i in instruments}
        return {by[k]: num(v.get("last_price")) for k, v in d.items() if k in by}
