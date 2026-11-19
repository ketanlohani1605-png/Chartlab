"""Upstox - API v2 / v3 (https://upstox.com/developer/api-documentation).

Login: the Upstox login page in the browser -> code on the redirect -> access token (valid until 03:30 IST next
morning). Orders go to the low-latency host api-hft.upstox.com (v3 order API, automatic market protection)."""
from __future__ import annotations

import datetime as dt
import gzip
import json
import time
import urllib.parse

import pandas as pd

from .base import (IST, AccessError, AuthError, Broker, BrokerError, Funds, Holding, Instrument, NetError, Order,
                   OrderRequest, Position, RateLimited, Rejected, candles_frame, chunks, classify, fetch_bytes, inum,
                   ip_hint, jwt_expiry, limiter, master, next_ist, num, resample, span)

PRODUCT_OUT = {"INTRADAY": "I", "DELIVERY": "D", "MARGIN": "D", "MTF": "MTF"}
PRODUCT_IN = {"I": "INTRADAY", "D": "DELIVERY", "MTF": "MTF", "CO": "INTRADAY", "OCO": "INTRADAY"}
SEG_EXCH = {"NSE_EQ": "NSE", "BSE_EQ": "BSE", "NSE_FO": "NFO", "BSE_FO": "BFO", "MCX_FO": "MCX", "NSE_INDEX": "NSE",
            "BSE_INDEX": "BSE", "NCD_FO": "CDS", "BCD_FO": "BCD", "NSE_COM": "NCO"}
# (unit, interval, days per request) - Upstox v3 historical candles
INTERVALS = {"1m": ("minutes", 1, 30), "2m": ("minutes", 2, 30), "3m": ("minutes", 3, 30), "5m": ("minutes", 5, 30),
             "10m": ("minutes", 10, 30), "15m": ("minutes", 15, 30), "30m": ("minutes", 30, 90),
             "45m": ("minutes", 45, 90), "1h": ("hours", 1, 90), "2h": ("hours", 2, 90), "3h": ("hours", 3, 90),
             "4h": ("hours", 4, 90), "1d": ("days", 1, 3650), "1wk": ("weeks", 1, 3650), "1mo": ("months", 1, 3650)}


class Upstox(Broker):
    id = "upstox"
    name = "Upstox"
    base = "https://api.upstox.com"
    order_base = "https://api-hft.upstox.com"
    caps = frozenset({"login_oauth", "token_paste", "history", "quotes", "stream", "orders", "modify", "cancel",
                      "positions", "holdings", "funds", "margin", "amo", "order_stream"})
    fields = [("api_key", "API key", "text", True, "From account.upstox.com/developer/apps"),
              ("api_secret", "API secret", "secret", True, ""),
              ("redirect_uri", "Redirect URL", "url", True, "Must match the app; ChartLab listens on it"),
              ("access_token", "Access token", "secret", False, "Filled in by 'Log in with Upstox'")]
    products = ("INTRADAY", "DELIVERY", "MARGIN", "MTF")
    exchanges = ("NSE", "BSE", "NFO", "BFO", "MCX")
    docs = "https://account.upstox.com/developer/apps"
    index_names = {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY", "FINNIFTY": "FINNIFTY", "MIDCPNIFTY": "MIDCPNIFTY",
                   "INDIAVIX": "INDIA VIX", "SENSEX": "SENSEX", "BANKEX": "BANKEX", "NIFTYNXT50": "NIFTYNXT50"}

    def _hdr(self) -> dict:
        h = {"Accept": "application/json", "Api-Version": "2.0"}
        if self.acct.get("access_token"):
            h["Authorization"] = f"Bearer {self.acct['access_token']}"
        return h

    def _call(self, method, url, *, params=None, form=None, json_body=None, kind="api", order=False, auth=True):
        rate = {"api": (20, 20), "order": (10, 10), "hist": (10, 10), "quote": (10, 10)}[kind]
        lim = limiter(f"upstox:{self.acct.get('api_key')}:{kind}", *rate)
        hdr = self._hdr() if auth else {"Accept": "application/json", "Api-Version": "2.0"}
        r = self.http.send(method, url if url.startswith("http") else self.base + url, params=params, form=form,
                           json_body=json_body, headers=hdr, safe=not order and method == "GET", limit=lim)
        js = r.json()
        if r.status >= 400 or not isinstance(js, dict) or js.get("status") == "error":
            errs = (js or {}).get("errors") if isinstance(js, dict) else None
            e0 = (errs or [{}])[0] if isinstance(errs, list) else {}
            msg = e0.get("message") or (js or {}).get("message") if isinstance(js, dict) else None
            code = e0.get("errorCode") or e0.get("error_code") or ""
            msg = f"Upstox: {msg or r.text[:200] or f'HTTP {r.status}'}"
            cls = classify(msg, r.status, Rejected if order else BrokerError)
            if code in ("UDAPI100050", "UDAPI100016", "UDAPI100067") or r.status == 401:
                cls = AuthError
            if cls is AccessError and order and "ip" in msg.lower():
                msg += ". " + ip_hint("Upstox")
            if cls is AuthError:
                msg += " - log in again"
            raise cls(msg, code or r.status, js)
        return js.get("data")

    # ------------------------------------------------------------------ login
    def login_url(self, redirect: str = "", state: str = "chartlab") -> str:
        if not self.acct.get("api_key"):
            raise BrokerError("Upstox: fill in the API key first")
        q = {"response_type": "code", "client_id": self.acct["api_key"],
             "redirect_uri": redirect or self.acct.get("redirect_uri", ""), "state": state}
        return f"{self.base}/v2/login/authorization/dialog?{urllib.parse.urlencode(q)}"

    def complete_login(self, params: dict, redirect: str = "") -> dict:
        code = (params.get("code") or "").strip()
        if not code:
            raise AuthError(f"Upstox login did not finish ({params.get('error') or 'no code'})")
        r = self.http.send("POST", f"{self.base}/v2/login/authorization/token",
                           form={"code": code, "client_id": self.acct.get("api_key", ""),
                                 "client_secret": self.acct.get("api_secret", ""),
                                 "redirect_uri": redirect or self.acct.get("redirect_uri", ""),
                                 "grant_type": "authorization_code"},
                           headers={"Accept": "application/json", "Api-Version": "2.0"}, safe=False)
        js = r.json() or {}
        tok = js.get("access_token") or (js.get("data") or {}).get("access_token")
        if r.status >= 400 or not tok:
            errs = js.get("errors") or [{}]
            raise AuthError(f"Upstox login failed: {(errs[0] or {}).get('message') or r.text[:200]}")
        self.acct["access_token"] = tok
        return self._done({"access_token": tok, "user_id": js.get("user_id", ""), "login_time": time.time()})

    def token_expiry(self) -> float | None:
        e = jwt_expiry(self.acct.get("access_token", ""))
        if e:
            return e
        lt = self.acct.get("login_time")
        return next_ist(3, 30, float(lt)) if lt else None

    # ------------------------------------------------------------------ account
    def profile(self) -> dict:
        d = self._call("GET", "/v2/user/profile") or {}
        return {"client": d.get("user_id", ""), "name": d.get("user_name", ""), "email": d.get("email", ""),
                "exchanges": d.get("exchanges", []), "products": d.get("products", [])}

    def funds(self) -> Funds:
        d = self._call("GET", "/v2/user/get-funds-and-margin") or {}
        eq, co = d.get("equity") or {}, d.get("commodity") or {}
        avail = num(eq.get("available_margin")) + num(co.get("available_margin"))
        used = num(eq.get("used_margin")) + num(co.get("used_margin"))
        return Funds(available=avail, used=used, total=avail + used, raw=d)

    def positions(self) -> list[Position]:
        out = []
        for p in self._call("GET", "/v2/portfolio/short-term-positions") or []:
            out.append(Position(symbol=p.get("trading_symbol") or p.get("tradingsymbol", ""),
                                exchange=p.get("exchange", ""), product=PRODUCT_IN.get(p.get("product"), "INTRADAY"),
                                qty=inum(p.get("quantity")), avg=num(p.get("average_price")),
                                ltp=num(p.get("last_price")), pnl=num(p.get("pnl")), realised=num(p.get("realised")),
                                unrealised=num(p.get("unrealised")),
                                buy_qty=inum(p.get("day_buy_quantity")) + inum(p.get("overnight_buy_quantity")),
                                sell_qty=inum(p.get("day_sell_quantity")) + inum(p.get("overnight_sell_quantity")),
                                buy_avg=num(p.get("buy_price")), sell_avg=num(p.get("sell_price")),
                                token=str(p.get("instrument_token", "")), raw=p))
        return out

    def holdings(self) -> list[Holding]:
        out = []
        for h in self._call("GET", "/v2/portfolio/long-term-holdings") or []:
            out.append(Holding(symbol=h.get("trading_symbol") or h.get("tradingsymbol", ""),
                               exchange=h.get("exchange", ""), qty=inum(h.get("quantity")),
                               avg=num(h.get("average_price")), ltp=num(h.get("last_price")), pnl=num(h.get("pnl")),
                               day_change=num(h.get("day_change_percentage")), isin=h.get("isin", ""),
                               token=str(h.get("instrument_token", "")), raw=h))
        return out

    def orders(self) -> list[Order]:
        out = []
        for o in self._call("GET", "/v2/order/retrieve-all") or []:
            out.append(Order(id=str(o.get("order_id")), symbol=o.get("trading_symbol") or o.get("tradingsymbol", ""),
                             exchange=o.get("exchange", ""), side=o.get("transaction_type", ""),
                             qty=inum(o.get("quantity")), filled=inum(o.get("filled_quantity")),
                             pending=inum(o.get("pending_quantity")), order_type=o.get("order_type", ""),
                             product=PRODUCT_IN.get(o.get("product"), "INTRADAY"), price=num(o.get("price")),
                             trigger=num(o.get("trigger_price")), avg_price=num(o.get("average_price")),
                             status=self._status_of(o.get("status")), message=o.get("status_message") or "",
                             time=str(o.get("order_timestamp") or ""), token=str(o.get("instrument_token", "")),
                             tag=o.get("tag") or "", raw=o))
        return out

    def trades(self) -> list[dict]:
        return self._call("GET", "/v2/order/trades/get-trades-for-day") or []

    # ------------------------------------------------------------------ instruments
    def instruments(self) -> pd.DataFrame:
        def build():
            raw = json.loads(fetch_bytes(
                "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"))
            rows = []
            for x in raw:
                seg = x.get("segment", "")
                ex = SEG_EXCH.get(seg)
                if not ex:
                    continue
                it = str(x.get("instrument_type", "")).upper()
                if seg.endswith("_INDEX"):
                    kind, name = "IDX", x.get("trading_symbol", "")
                elif seg.endswith("_EQ"):
                    kind, name = "EQ", x.get("trading_symbol", "")
                elif it in ("FUT", "CE", "PE"):
                    kind, name = it, x.get("underlying_symbol") or x.get("asset_symbol") or x.get("name", "")
                else:
                    continue
                e = x.get("expiry")
                exp = dt.datetime.fromtimestamp(e / 1000, IST).date() if e else None
                lot = int(x.get("lot_size") or 1)
                if seg == "MCX_FO":
                    lot = 1                      # Upstox takes commodity quantity in lots
                rows.append((x.get("instrument_key", ""), ex, x.get("trading_symbol", ""), name, kind, exp,
                             x.get("strike_price") or 0, lot, num(x.get("tick_size"), 5.0) / 100.0, seg,
                             x.get("isin", ""), 0 if it in ("EQ", "") else 1))
            df = pd.DataFrame(rows, columns=["token", "exchange", "tsym", "name", "kind", "expiry", "strike", "lot",
                                             "tick", "seg", "isin", "_series"])
            return df.sort_values("_series", kind="stable").drop(columns="_series")
        return master("upstox", build)

    # ------------------------------------------------------------------ trading
    def _qty(self, r: OrderRequest) -> int:
        return r.qty

    def place(self, req: OrderRequest) -> str:
        r = self.prepare(req)
        body = {"quantity": self._qty(r), "product": PRODUCT_OUT[r.product], "validity": r.validity,
                "price": r.price if r.order_type in ("LIMIT", "SL") else 0, "tag": r.tag,
                "instrument_token": r.instrument.token, "order_type": r.order_type, "transaction_type": r.side,
                "disclosed_quantity": r.disclosed or 0,
                "trigger_price": r.trigger if r.order_type in ("SL", "SL-M") else 0,
                "is_amo": bool(r.amo), "slice": False}
        if r.order_type in ("MARKET", "SL-M"):
            body["market_protection"] = -1
        d = self._call("POST", f"{self.order_base}/v3/order/place", json_body=body, kind="order", order=True) or {}
        ids = d.get("order_ids") or ([d.get("order_id")] if d.get("order_id") else [])
        if not ids:
            raise Rejected(f"Upstox did not return an order id: {d}")
        return str(ids[0])

    def modify(self, order: Order, qty=None, price=None, trigger=None, order_type=None) -> str:
        body = {"order_id": order.id, "quantity": qty if qty is not None else order.qty,
                "validity": (order.raw or {}).get("validity") or "DAY",
                "price": price if price is not None else order.price,
                "order_type": order_type or order.order_type,
                "disclosed_quantity": inum((order.raw or {}).get("disclosed_quantity")),
                "trigger_price": trigger if trigger is not None else order.trigger}
        if body["order_type"] in ("MARKET", "SL-M"):
            body["market_protection"] = -1
        d = self._call("PUT", f"{self.order_base}/v3/order/modify", json_body=body, kind="order", order=True) or {}
        return str(d.get("order_id") or order.id)

    def cancel(self, order: Order) -> str:
        d = self._call("DELETE", f"{self.order_base}/v3/order/cancel", params={"order_id": order.id}, kind="order",
                       order=True) or {}
        return str(d.get("order_id") or order.id)

    def margin(self, req: OrderRequest) -> dict | None:
        r = self.prepare(req)
        d = self._call("POST", "/v2/charges/margin", json_body={"instruments": [{
            "instrument_key": r.instrument.token, "quantity": r.qty, "transaction_type": r.side,
            "product": PRODUCT_OUT[r.product], "price": r.price or 0}]}) or {}
        if d:
            return {"required": num(d.get("final_margin") or d.get("required_margin"))}
        return None

    # ------------------------------------------------------------------ data
    def history(self, symbol, interval: str, period: str) -> pd.DataFrame:
        inst = self.resolve(symbol)
        if interval not in INTERVALS:
            raise BrokerError(f"Upstox has no {interval} candles")
        unit, n, per = INTERVALS[interval]
        intraday = unit in ("minutes", "hours")
        start, end = span(period, intraday, 3650)
        key = urllib.parse.quote(inst.token, safe="")
        frames = []
        for a, b in chunks(start, end, per):
            d = self._call("GET", f"/v3/historical-candle/{key}/{unit}/{n}/{b - dt.timedelta(days=1) if b > a else b}/{a}",
                           kind="hist") or {}
            frames.append(candles_frame(d.get("candles") or []))
        if intraday:
            try:                                          # today's bars come from a separate endpoint
                d = self._call("GET", f"/v3/historical-candle/intraday/{key}/{unit}/{n}", kind="hist") or {}
                frames.append(candles_frame(d.get("candles") or []))
            except BrokerError:
                pass
        df = pd.concat([f for f in frames if len(f)] or [candles_frame([])])
        return df.drop_duplicates("Date", keep="last").sort_values("Date").reset_index(drop=True)

    def ltp(self, instruments: list[Instrument]) -> dict:
        out = {}
        toks = [i.token for i in instruments]
        for k in range(0, len(toks), 500):
            d = self._call("GET", "/v3/market-quote/ltp", params={"instrument_key": ",".join(toks[k:k + 500])},
                           kind="quote") or {}
            for v in d.values():
                t = v.get("instrument_token")
                if t:
                    out[str(t)] = num(v.get("last_price"))
        return out

    def feed_url(self) -> str:
        d = self._call("GET", "/v3/feed/market-data-feed/authorize") or {}
        u = d.get("authorizedRedirectUri") or d.get("authorized_redirect_uri")
        if not u:
            raise BrokerError("Upstox did not give a live-feed address")
        return u
