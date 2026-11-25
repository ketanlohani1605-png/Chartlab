"""Fyers - API v3 (https://myapi.fyers.in/docsv3).

Login: the Fyers login page in the browser -> auth_code on the redirect -> access token (valid for the day). With the
PIN saved, the 15-day refresh token renews the login without the browser."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import time
import urllib.parse

import pandas as pd

from .base import (IST, AccessError, AuthError, Broker, BrokerError, Funds, Holding, Instrument, NetError, Order,
                   OrderRequest, Position, RateLimited, Rejected, candles_frame, chunks, classify, fetch_bytes, inum,
                   ip_hint, jwt_expiry, limiter, master, num, resample, span)

PRODUCT_OUT = {"INTRADAY": "INTRADAY", "DELIVERY": "CNC", "MARGIN": "MARGIN", "MTF": "MTF"}
PRODUCT_IN = {"INTRADAY": "INTRADAY", "CNC": "DELIVERY", "MARGIN": "MARGIN", "MTF": "MTF", "CO": "INTRADAY",
              "BO": "INTRADAY"}
TYPE_OUT = {"LIMIT": 1, "MARKET": 2, "SL-M": 3, "SL": 4}
TYPE_IN = {v: k for k, v in TYPE_OUT.items()}
STATUS_IN = {1: "cancelled", 2: "complete", 3: "open", 4: "pending", 5: "rejected", 6: "open", 7: "rejected"}
RES = {"1m": ("1", 100), "2m": ("2", 100), "3m": ("3", 100), "5m": ("5", 100), "10m": ("10", 100),
       "15m": ("15", 100), "30m": ("30", 100), "45m": ("45", 100), "1h": ("60", 100), "2h": ("120", 100),
       "3h": ("180", 100), "4h": ("240", 100), "1d": ("1D", 366)}
FILES = {"NSE_CM": "NSE", "NSE_FO": "NFO", "BSE_CM": "BSE", "BSE_FO": "BFO", "MCX_COM": "MCX"}
HSM_SEG = {"1010": "nse_cm", "1011": "nse_fo", "1120": "mcx_fo", "1210": "bse_cm", "1012": "cde_fo",
           "1211": "bse_fo", "1212": "bcs_fo", "1020": "nse_com"}
HSM_INDEX = {"NSE:NIFTY50-INDEX": "Nifty 50", "NSE:NIFTYBANK-INDEX": "Nifty Bank",
             "NSE:FINNIFTY-INDEX": "Nifty Fin Service", "NSE:MIDCPNIFTY-INDEX": "NIFTY MID SELECT",
             "NSE:INDIAVIX-INDEX": "India VIX", "BSE:SENSEX-INDEX": "SENSEX", "BSE:BANKEX-INDEX": "BANKEX",
             "NSE:NIFTYNXT50-INDEX": "Nifty Next 50"}


class Fyers(Broker):
    id = "fyers"
    name = "Fyers"
    base = "https://api-t1.fyers.in/api/v3"
    data_base = "https://api-t1.fyers.in/data"
    caps = frozenset({"login_oauth", "token_paste", "renew", "history", "quotes", "stream", "orders", "modify",
                      "cancel", "positions", "holdings", "funds", "amo", "bracket"})
    fields = [("app_id", "App ID", "text", True, "Like XB12345-100, from myapi.fyers.in/dashboard"),
              ("api_secret", "Secret key", "secret", True, ""),
              ("redirect_uri", "Redirect URL", "url", True, "Must match the app; ChartLab listens on it"),
              ("pin", "PIN", "pin", False, "Optional, kept only on this computer: renews the login without the browser"),
              ("access_token", "Access token", "secret", False, "Filled in by 'Log in with Fyers'")]
    products = ("INTRADAY", "DELIVERY", "MARGIN", "MTF")
    exchanges = ("NSE", "BSE", "NFO", "BFO", "MCX")
    docs = "https://myapi.fyers.in/dashboard"
    index_names = {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY", "FINNIFTY": "FINNIFTY", "MIDCPNIFTY": "MIDCPNIFTY",
                   "INDIAVIX": "INDIAVIX", "SENSEX": "SENSEX", "BANKEX": "BANKEX", "NIFTYNXT50": "NIFTYNXT50"}

    def _hdr(self) -> dict:
        h = {"Content-Type": "application/json", "version": "3"}
        if self.acct.get("access_token"):
            h["Authorization"] = f"{self.acct.get('app_id', '')}:{self.acct['access_token']}"
        return h

    def _call(self, method, url, *, params=None, json_body=None, kind="api", order=False):
        rate = {"api": (10, 10), "order": (10, 10), "hist": (5, 5), "quote": (5, 5)}[kind]
        lim = limiter(f"fyers:{self.acct.get('app_id')}:{kind}", *rate)
        r = self.http.send(method, url if url.startswith("http") else self.base + url, params=params,
                           json_body=json_body, headers=self._hdr(), safe=not order and method == "GET", limit=lim)
        js = r.json()
        if r.status >= 400 or not isinstance(js, dict) or js.get("s") not in ("ok", None) or \
                (js.get("s") is None and "code" in js and inum(js.get("code")) < 0):
            msg = (js or {}).get("message") if isinstance(js, dict) else r.text[:200]
            code = inum((js or {}).get("code")) if isinstance(js, dict) else 0
            msg = f"Fyers: {msg or f'HTTP {r.status}'}"
            cls = classify(msg, r.status, Rejected if order else BrokerError)
            if code in (-8, -15, -16, -17, -300) or r.status == 401:
                cls = AuthError
            if cls is AccessError and order and "ip" in msg.lower():
                msg += ". " + ip_hint("Fyers")
            raise cls(msg + (" - log in again" if cls is AuthError else ""), code or r.status, js)
        return js

    # ------------------------------------------------------------------ login
    def _hash(self) -> str:
        return hashlib.sha256(f"{self.acct.get('app_id', '')}:{self.acct.get('api_secret', '')}".encode()).hexdigest()

    def login_url(self, redirect: str = "", state: str = "chartlab") -> str:
        if not self.acct.get("app_id"):
            raise BrokerError("Fyers: fill in the App ID first")
        q = {"client_id": self.acct["app_id"], "redirect_uri": redirect or self.acct.get("redirect_uri", ""),
             "response_type": "code", "state": state}
        return f"{self.base}/generate-authcode?{urllib.parse.urlencode(q)}"

    def complete_login(self, params: dict, redirect: str = "") -> dict:
        code = (params.get("auth_code") or "").strip()
        if not code or params.get("s") not in (None, "", "ok"):
            raise AuthError(f"Fyers login did not finish ({params.get('message') or params.get('s') or 'no auth_code'})")
        js = self._call("POST", "/validate-authcode", order=True,
                        json_body={"grant_type": "authorization_code", "appIdHash": self._hash(), "code": code})
        tok = js.get("access_token")
        if not tok:
            raise AuthError("Fyers answered without an access token")
        self.acct["access_token"] = tok
        return self._done({"access_token": tok, "refresh_token": js.get("refresh_token", ""), "login_time": time.time()})

    def renew(self) -> dict:
        if not (self.acct.get("refresh_token") and self.acct.get("pin")):
            raise AuthError("Fyers: renewing needs the refresh token and the PIN - log in again")
        js = self._call("POST", "/validate-refresh-token", order=True,
                        json_body={"grant_type": "refresh_token", "appIdHash": self._hash(),
                                   "refresh_token": self.acct["refresh_token"], "pin": self.acct["pin"]})
        tok = js.get("access_token")
        if not tok:
            raise AuthError("Fyers did not renew the login")
        self.acct["access_token"] = tok
        return self._done({"access_token": tok})

    def token_expiry(self) -> float | None:
        return jwt_expiry(self.acct.get("access_token", ""))

    # ------------------------------------------------------------------ account
    def profile(self) -> dict:
        d = self._call("GET", "/profile").get("data") or {}
        return {"client": d.get("fy_id", ""), "name": d.get("name", ""), "email": d.get("email_id", "")}

    def funds(self) -> Funds:
        rows = self._call("GET", "/funds").get("fund_limit") or []
        by = {str(r.get("title", "")).lower(): num(r.get("equityAmount")) + num(r.get("commodityAmount")) for r in rows}
        avail = by.get("available balance", by.get("clear balance", 0.0))
        used = by.get("utilized amount", 0.0)
        return Funds(available=avail, used=used, total=by.get("total balance", avail + used), raw={"fund_limit": rows})

    def positions(self) -> list[Position]:
        out = []
        for p in self._call("GET", "/positions").get("netPositions") or []:
            out.append(Position(symbol=p.get("symbol", ""), exchange=str(p.get("symbol", "")).split(":")[0],
                                product=PRODUCT_IN.get(p.get("productType"), "INTRADAY"), qty=inum(p.get("netQty")),
                                avg=num(p.get("netAvg")), ltp=num(p.get("ltp")), pnl=num(p.get("pl")),
                                realised=num(p.get("realized_profit")), unrealised=num(p.get("unrealized_profit")),
                                buy_qty=inum(p.get("buyQty")), sell_qty=inum(p.get("sellQty")),
                                buy_avg=num(p.get("buyAvg")), sell_avg=num(p.get("sellAvg")),
                                token=p.get("symbol", ""), raw=p))
        return out

    def holdings(self) -> list[Holding]:
        out = []
        for h in self._call("GET", "/holdings").get("holdings") or []:
            out.append(Holding(symbol=h.get("symbol", ""), exchange=str(h.get("symbol", "")).split(":")[0],
                               qty=inum(h.get("quantity")), avg=num(h.get("costPrice")), ltp=num(h.get("ltp")),
                               pnl=num(h.get("pl")), isin=h.get("isin", ""), token=h.get("symbol", ""), raw=h))
        return out

    def orders(self) -> list[Order]:
        out = []
        for o in self._call("GET", "/orders").get("orderBook") or []:
            st = STATUS_IN.get(inum(o.get("status")), "open")
            if st == "open" and inum(o.get("filledQty")) > 0:
                st = "partial"
            if st == "open" and inum(o.get("type")) in (3, 4):
                st = "trigger_pending"
            out.append(Order(id=str(o.get("id")), symbol=o.get("symbol", ""), exchange=str(o.get("symbol", "")).split(":")[0],
                             side="BUY" if inum(o.get("side")) == 1 else "SELL", qty=inum(o.get("qty")),
                             filled=inum(o.get("filledQty")), pending=inum(o.get("remainingQuantity")),
                             order_type=TYPE_IN.get(inum(o.get("type")), ""),
                             product=PRODUCT_IN.get(o.get("productType"), "INTRADAY"), price=num(o.get("limitPrice")),
                             trigger=num(o.get("stopPrice")), avg_price=num(o.get("tradedPrice")), status=st,
                             message=o.get("message") or "", time=str(o.get("orderDateTime") or ""),
                             token=o.get("symbol", ""), tag=o.get("orderTag") or "", raw=o))
        return out

    def trades(self) -> list[dict]:
        return self._call("GET", "/tradebook").get("tradeBook") or []

    # ------------------------------------------------------------------ instruments
    def instruments(self) -> pd.DataFrame:
        def build():
            rows = []
            for f, ex in FILES.items():
                try:
                    raw = fetch_bytes(f"https://public.fyers.in/sym_details/{f}.csv", timeout=180)
                except NetError:
                    if f in ("NSE_CM", "NSE_FO"):
                        raise
                    continue
                for r in csv.reader(io.StringIO(raw.decode("utf8", "replace"))):
                    if len(r) < 18:
                        continue
                    tick, it, opt = r[9], r[2], r[16]
                    if tick.endswith("-INDEX"):
                        kind = "IDX"
                    elif f.endswith("_CM"):
                        if it not in ("0", "9"):
                            continue
                        kind = "EQ"
                    elif tick.endswith("FUT"):
                        kind = "FUT"
                    elif opt in ("CE", "PE"):
                        kind = opt
                    else:
                        continue
                    exp = dt.datetime.fromtimestamp(int(r[8]), IST).date() if r[8].isdigit() else None
                    pri = 0 if tick.endswith("-EQ") or ex != "NSE" else 1
                    rows.append((tick, ex, tick, r[13], kind, exp, num(r[15]) if kind in ("CE", "PE") else 0.0,
                                 inum(r[3], 1), num(r[4], 0.05), r[0][:4], r[0], r[12], pri))
            df = pd.DataFrame(rows, columns=["token", "exchange", "tsym", "name", "kind", "expiry", "strike", "lot",
                                             "tick", "seg", "fytoken", "extoken", "_pri"])
            return df.sort_values("_pri", kind="stable").drop(columns="_pri")
        return master("fyers", build)

    # ------------------------------------------------------------------ trading
    def place(self, req: OrderRequest) -> str:
        r = self.prepare(req)
        body = {"symbol": r.instrument.token, "qty": r.qty, "type": TYPE_OUT[r.order_type],
                "side": 1 if r.side == "BUY" else -1, "productType": PRODUCT_OUT[r.product],
                "limitPrice": r.price if r.order_type in ("LIMIT", "SL") else 0,
                "stopPrice": r.trigger if r.order_type in ("SL", "SL-M") else 0, "validity": r.validity,
                "disclosedQty": r.disclosed or 0, "offlineOrder": bool(r.amo), "orderTag": r.tag}
        if r.stop_loss or r.take_profit:
            body.update(stopLoss=r.stop_loss or 0, takeProfit=r.take_profit or 0, legType=1)
        js = self._call("POST", "/orders/sync", json_body=body, kind="order", order=True)
        oid = js.get("id")
        if not oid:
            raise Rejected(f"Fyers did not return an order id: {js.get('message')}")
        return str(oid)

    def modify(self, order: Order, qty=None, price=None, trigger=None, order_type=None) -> str:
        body = {"id": order.id, "type": TYPE_OUT.get(order_type or order.order_type, 1)}
        if qty is not None:
            body["qty"] = qty
        if price is not None:
            body["limitPrice"] = price
        if trigger is not None:
            body["stopPrice"] = trigger
        js = self._call("PATCH", "/orders/sync", json_body=body, kind="order", order=True)
        return str(js.get("id") or order.id)

    def cancel(self, order: Order) -> str:
        js = self._call("DELETE", "/orders/sync", json_body={"id": order.id}, kind="order", order=True)
        return str(js.get("id") or order.id)

    # ------------------------------------------------------------------ data
    def history(self, symbol, interval: str, period: str) -> pd.DataFrame:
        inst = self.resolve(symbol)
        base_iv = interval if interval in RES else {"1wk": "1d", "1mo": "1d"}.get(interval)
        if base_iv is None:
            raise BrokerError(f"Fyers has no {interval} candles")
        res, per = RES[base_iv]
        intraday = base_iv != "1d"
        start, end = span(period, intraday, 3650)
        frames = []
        for a, b in chunks(start, end, per):
            js = self._call("GET", f"{self.data_base}/history", kind="hist",
                            params={"symbol": inst.token, "resolution": res, "date_format": 1,
                                    "range_from": str(a), "range_to": str(b), "cont_flag": 1})
            frames.append(candles_frame(js.get("candles") or [], "s"))
        df = pd.concat([f for f in frames if len(f)] or [candles_frame([])])
        df = df.drop_duplicates("Date", keep="last").sort_values("Date").reset_index(drop=True)
        return resample(df, interval) if base_iv != interval else df

    def ltp(self, instruments: list[Instrument]) -> dict:
        out = {}
        toks = [i.token for i in instruments]
        for k in range(0, len(toks), 50):
            js = self._call("GET", f"{self.data_base}/quotes", params={"symbols": ",".join(toks[k:k + 50])},
                            kind="quote")
            for q in js.get("d") or []:
                v = q.get("v") or {}
                if q.get("n") and v.get("lp") is not None:
                    out[q["n"]] = num(v.get("lp"))
        return out

    def hsm_topic(self, inst: Instrument) -> str:
        """The data-socket name of an instrument: sf|nse_cm|2885 (scrip) or if|nse_cm|Nifty 50 (index)."""
        ftok = str(inst.extra.get("fytoken") or "")
        seg = HSM_SEG.get(ftok[:4] or inst.seg, "nse_cm")
        if inst.kind == "IDX":
            return f"if|{seg}|{HSM_INDEX.get(inst.token, inst.token.split(':')[-1].replace('-INDEX', ''))}"
        return f"sf|{seg}|{inst.extra.get('extoken') or ftok[10:]}"
