"""Dhan - DhanHQ API v2 (https://dhanhq.co/docs/v2/).

Login, any of:
  * paste the 24-hour access token made on web.dhan.co (the way ChartLab always worked);
  * API key + secret: Dhan's consent page in the browser -> tokenId on the redirect -> access token;
  * client id + PIN + TOTP code, straight from ChartLab.
A valid token made on Dhan Web can be renewed for another 24 hours (RenewToken)."""
from __future__ import annotations

import datetime as dt
import io
import time

import pandas as pd

from .base import (IST, AccessError, AuthError, Broker, BrokerError, Funds, Holding, Instrument, NetError, Order,
                   OrderRequest, Position, RateLimited, Rejected, candles_frame, chunks, classify, fetch_bytes,
                   find_jwt, inum, ip_hint, jwt_expiry, limiter, master, num, resample, span)

AUTH = "https://auth.dhan.co"
SEGMENTS = {("NSE", "E"): "NSE_EQ", ("BSE", "E"): "BSE_EQ", ("NSE", "D"): "NSE_FNO", ("BSE", "D"): "BSE_FNO",
            ("MCX", "M"): "MCX_COMM", ("NSE", "C"): "NSE_CURRENCY", ("BSE", "C"): "BSE_CURRENCY",
            ("NSE", "I"): "IDX_I", ("BSE", "I"): "IDX_I"}
EXCH = {("NSE", "E"): "NSE", ("BSE", "E"): "BSE", ("NSE", "D"): "NFO", ("BSE", "D"): "BFO", ("MCX", "M"): "MCX",
        ("NSE", "C"): "CDS", ("BSE", "C"): "BCD", ("NSE", "I"): "NSE", ("BSE", "I"): "BSE"}
PRODUCT_OUT = {"INTRADAY": "INTRADAY", "DELIVERY": "CNC", "MARGIN": "MARGIN", "MTF": "MTF"}
PRODUCT_IN = {"INTRADAY": "INTRADAY", "CNC": "DELIVERY", "MARGIN": "MARGIN", "MTF": "MTF", "CO": "INTRADAY",
              "BO": "INTRADAY"}
TYPE_OUT = {"MARKET": "MARKET", "LIMIT": "LIMIT", "SL": "STOP_LOSS", "SL-M": "STOP_LOSS_MARKET"}
TYPE_IN = {v: k for k, v in TYPE_OUT.items()}
INTRA = {"1m": 1, "5m": 5, "15m": 15, "25m": 25, "1h": 60}
INSTR = {"EQ": "EQUITY", "IDX": "INDEX", "FUT": "FUTIDX", "CE": "OPTIDX", "PE": "OPTIDX"}


class Dhan(Broker):
    id = "dhan"
    name = "Dhan"
    base = "https://api.dhan.co/v2"
    caps = frozenset({"token_paste", "login_oauth", "login_totp", "renew", "history", "quotes", "stream", "orders",
                      "modify", "cancel", "positions", "holdings", "funds", "margin", "amo", "bracket",
                      "option_chain", "order_stream"})
    fields = [("client_id", "Client ID", "text", True, "Your Dhan client id"),
              ("access_token", "Access token", "secret", False, "From web.dhan.co -> DhanHQ Trading APIs, or Log in"),
              ("app_id", "API key", "text", False, "Optional: for 'Log in with Dhan' in the browser"),
              ("app_secret", "API secret", "secret", False, "Optional, with the API key"),
              ("pin", "PIN", "pin", False, "Optional, kept only on this Mac: log in with PIN + TOTP"),
              ("totp_secret", "TOTP key", "totp", False, "Optional: the key behind your authenticator")]
    products = ("INTRADAY", "DELIVERY", "MARGIN", "MTF")
    exchanges = ("NSE", "BSE", "NFO", "BFO", "MCX")
    docs = "https://web.dhan.co"
    index_names = {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY", "FINNIFTY": "FINNIFTY", "MIDCPNIFTY": "MIDCPNIFTY",
                   "SENSEX": "SENSEX", "BANKEX": "BANKEX", "INDIAVIX": "INDIA VIX", "NIFTYNXT50": "NIFTY NEXT 50"}

    def _hdr(self) -> dict:
        return {"access-token": self.acct.get("access_token", ""), "client-id": str(self.acct.get("client_id", "")),
                "Accept": "application/json"}

    def _call(self, method, path, *, json_body=None, kind="api", order=False):
        rate = {"api": (10, 10), "order": (10, 10), "hist": (5, 5), "quote": (1, 1)}[kind]
        lim = limiter(f"dhan:{self.acct.get('client_id')}:{kind}", *rate)
        r = self.http.send(method, self.base + path, json_body=json_body, headers=self._hdr(),
                           safe=not order and method == "GET", limit=lim)
        js = r.json()
        if r.status >= 400 or (isinstance(js, dict) and js.get("status") == "failure"):
            d = js if isinstance(js, dict) else {}
            inner = d.get("data") if isinstance(d.get("data"), dict) else {}
            code = str(d.get("errorCode") or inner.get("errorCode") or "")
            msg = d.get("errorMessage") or inner.get("errorMessage") or d.get("remarks") or r.text[:200] \
                or f"HTTP {r.status}"
            msg = f"Dhan: {msg}" + (f" ({code})" if code else "")
            cls = classify(msg, r.status, Rejected if order else BrokerError)
            if code in ("DH-901", "807", "808", "809") or r.status == 401:
                cls = AuthError
            elif code in ("DH-902", "806"):
                cls = AccessError
            elif code == "DH-904" or r.status == 429:
                cls = RateLimited
            if cls is AccessError and order and "ip" in msg.lower():
                msg += ". " + ip_hint("Dhan")
            raise cls(msg + (" - log in again" if cls is AuthError else ""), code or r.status, js)
        return js

    # ------------------------------------------------------------------ login
    def login_url(self, redirect: str = "", state: str = "chartlab") -> str:
        if not (self.acct.get("app_id") and self.acct.get("app_secret") and self.acct.get("client_id")):
            raise BrokerError("Dhan browser login needs the client id, API key and API secret")
        r = self.http.send("POST", f"{AUTH}/app/generate-consent", params={"client_id": self.acct["client_id"]},
                           headers={"app_id": self.acct["app_id"], "app_secret": self.acct["app_secret"]}, safe=False)
        js = r.json() or {}
        cid = js.get("consentAppId")
        if r.status >= 400 or not cid:
            raise AuthError(f"Dhan did not start the login: {js.get('message') or r.text[:160]}")
        return f"{AUTH}/login/consentApp-login?consentAppId={cid}"

    def complete_login(self, params: dict, redirect: str = "") -> dict:
        tid = (params.get("tokenId") or params.get("tokenid") or "").strip()
        if not tid:
            raise AuthError("Dhan login did not finish (no tokenId on the redirect)")
        r = self.http.send("GET", f"{AUTH}/app/consumeApp-consent", params={"tokenId": tid},
                           headers={"app_id": self.acct.get("app_id", ""), "app_secret": self.acct.get("app_secret", "")})
        js = r.json() or {}
        tok = js.get("accessToken") or js.get("access_token")
        if r.status >= 400 or not tok:
            raise AuthError(f"Dhan login failed: {js.get('message') or r.text[:160]}")
        self.acct["access_token"] = tok
        out = {"access_token": tok, "login_time": time.time()}
        if js.get("dhanClientId"):
            out["client_id"] = str(js["dhanClientId"])
        return self._done(out)

    def login_totp(self, pin: str = "", code: str = "") -> dict:
        from .angel import totp_now
        pin = pin or self.acct.get("pin", "")
        if not code and self.acct.get("totp_secret"):
            code = totp_now(self.acct["totp_secret"])
        if not (self.acct.get("client_id") and pin and code):
            raise AuthError("Dhan needs the client id, PIN and the 6-digit TOTP code")
        r = self.http.send("POST", f"{AUTH}/app/generateAccessToken", safe=False,
                           params={"dhanClientId": self.acct["client_id"], "pin": pin, "totp": code})
        js = r.json() or {}
        tok = js.get("accessToken") or js.get("access_token")
        if r.status >= 400 or not tok:
            raise AuthError(f"Dhan login failed: {js.get('message') or js.get('errorMessage') or r.text[:160]}")
        self.acct["access_token"] = tok
        return self._done({"access_token": tok, "login_time": time.time()})

    def renew(self) -> dict:
        r = self.http.send("GET", f"{self.base}/RenewToken", safe=False,
                           headers={"access-token": self.acct.get("access_token", ""),
                                    "dhanClientId": str(self.acct.get("client_id", ""))})
        tok = find_jwt(r.json() if r.json() is not None else r.text)
        if r.status >= 400 or not tok:
            js = r.text
            raise AuthError(f"Dhan did not renew the token: {str(js)[:160]}")
        self.acct["access_token"] = tok
        return self._done({"access_token": tok})

    def token_expiry(self) -> float | None:
        return jwt_expiry(self.acct.get("access_token", ""))

    # ------------------------------------------------------------------ account
    def profile(self) -> dict:
        js = self._call("GET", "/profile") or {}
        return {"client": js.get("dhanClientId", ""), "validity": js.get("tokenValidity", ""),
                "segments": js.get("activeSegment", ""), "data_plan": js.get("dataPlan", "")}

    def funds(self) -> Funds:
        js = self._call("GET", "/fundlimit") or {}
        avail = num(js.get("availabelBalance", js.get("availableBalance")))
        used = num(js.get("utilizedAmount"))
        return Funds(available=avail, used=used, total=num(js.get("sodLimit"), avail + used), raw=js)

    def positions(self) -> list[Position]:
        out = []
        for p in self._call("GET", "/positions") or []:
            q = inum(p.get("netQty"))
            avg = num(p.get("costPrice")) or (num(p.get("buyAvg")) if q > 0 else num(p.get("sellAvg")))
            out.append(Position(symbol=p.get("tradingSymbol", ""), exchange=str(p.get("exchangeSegment", "")),
                                product=PRODUCT_IN.get(p.get("productType"), "INTRADAY"), qty=q, avg=avg,
                                pnl=num(p.get("realizedProfit")) + num(p.get("unrealizedProfit")),
                                realised=num(p.get("realizedProfit")), unrealised=num(p.get("unrealizedProfit")),
                                buy_qty=inum(p.get("buyQty")), sell_qty=inum(p.get("sellQty")),
                                buy_avg=num(p.get("buyAvg")), sell_avg=num(p.get("sellAvg")),
                                token=str(p.get("securityId", "")), raw=p))
        return out

    def holdings(self) -> list[Holding]:
        js = self._call("GET", "/holdings")
        out = []
        for h in js if isinstance(js, list) else []:
            out.append(Holding(symbol=h.get("tradingSymbol", ""), exchange=h.get("exchange", ""),
                               qty=inum(h.get("totalQty")), avg=num(h.get("avgCostPrice")),
                               ltp=num(h.get("lastTradedPrice")), isin=h.get("isin", ""),
                               token=str(h.get("securityId", "")), raw=h))
        return out

    def orders(self) -> list[Order]:
        out = []
        for o in self._call("GET", "/orders") or []:
            st = str(o.get("orderStatus", "")).upper()
            status = {"TRADED": "complete", "PART_TRADED": "partial", "CANCELLED": "cancelled", "REJECTED": "rejected",
                      "EXPIRED": "rejected", "TRANSIT": "pending", "PENDING": "open", "TRIGGERED": "open",
                      "CLOSED": "complete"}.get(st, "open")
            if status == "open" and o.get("orderType") in ("STOP_LOSS", "STOP_LOSS_MARKET") and st == "PENDING":
                status = "trigger_pending"
            out.append(Order(id=str(o.get("orderId")), symbol=o.get("tradingSymbol", ""),
                             exchange=str(o.get("exchangeSegment", "")), side=o.get("transactionType", ""),
                             qty=inum(o.get("quantity")), filled=inum(o.get("filledQty")),
                             pending=inum(o.get("remainingQuantity")),
                             order_type=TYPE_IN.get(o.get("orderType"), o.get("orderType", "")),
                             product=PRODUCT_IN.get(o.get("productType"), "INTRADAY"), price=num(o.get("price")),
                             trigger=num(o.get("triggerPrice")), avg_price=num(o.get("averageTradedPrice")),
                             status=status, message=o.get("omsErrorDescription") or "",
                             time=str(o.get("updateTime") or o.get("createTime") or ""),
                             token=str(o.get("securityId", "")), tag=o.get("correlationId") or "", raw=o))
        return out

    def trades(self) -> list[dict]:
        js = self._call("GET", "/trades")
        return js if isinstance(js, list) else []

    # ------------------------------------------------------------------ instruments
    def instruments(self) -> pd.DataFrame:
        def build():
            cols = ["EXCH_ID", "SEGMENT", "SECURITY_ID", "ISIN", "INSTRUMENT", "UNDERLYING_SYMBOL", "SYMBOL_NAME",
                    "DISPLAY_NAME", "SERIES", "LOT_SIZE", "SM_EXPIRY_DATE", "STRIKE_PRICE", "OPTION_TYPE", "TICK_SIZE"]
            raw = pd.read_csv(io.BytesIO(fetch_bytes("https://images.dhan.co/api-data/api-scrip-master-detailed.csv",
                                                     timeout=240)), low_memory=False, usecols=cols)
            ins = raw["INSTRUMENT"].astype(str)
            kind = pd.Series("", index=raw.index)
            kind[ins == "EQUITY"] = "EQ"
            kind[ins == "INDEX"] = "IDX"
            kind[ins.str.startswith("FUT")] = "FUT"
            opt = ins.str.startswith("OPT")
            kind[opt & (raw["OPTION_TYPE"] == "CE")] = "CE"
            kind[opt & (raw["OPTION_TYPE"] == "PE")] = "PE"
            eqok = (kind != "EQ") | (raw["EXCH_ID"] != "NSE") | raw["SERIES"].isin(["EQ", "BE", "SM", "ST", "BZ"])
            key = list(zip(raw["EXCH_ID"], raw["SEGMENT"]))
            df = pd.DataFrame({
                "token": raw["SECURITY_ID"].astype("Int64").astype(str),
                "exchange": [EXCH.get(k, "") for k in key],
                "tsym": raw["UNDERLYING_SYMBOL"].where(kind == "EQ", raw["DISPLAY_NAME"])
                        .fillna(raw["SYMBOL_NAME"]).astype(str),
                "name": raw["UNDERLYING_SYMBOL"].fillna(raw["SYMBOL_NAME"]).astype(str),
                "kind": kind, "expiry": pd.to_datetime(raw["SM_EXPIRY_DATE"], errors="coerce"),
                "strike": raw["STRIKE_PRICE"], "lot": raw["LOT_SIZE"], "tick": raw["TICK_SIZE"] / 100.0,
                "seg": [SEGMENTS.get(k, "") for k in key], "instrument": ins,
                "_pri": (raw["SERIES"] != "EQ").astype(int)})
            df = df[eqok & (df["exchange"] != "") & (df["kind"] != "")]
            df.loc[df["kind"] == "IDX", "expiry"] = pd.NaT
            df.loc[df["kind"].isin(["EQ", "IDX"]), "expiry"] = pd.NaT
            return df.sort_values("_pri", kind="stable").drop(columns="_pri")
        return master("dhan", build)

    # ------------------------------------------------------------------ trading
    def place(self, req: OrderRequest) -> str:
        r = self.prepare(req)
        product = PRODUCT_OUT[r.product]
        body = {"dhanClientId": str(self.acct.get("client_id", "")), "correlationId": r.tag[:25],
                "transactionType": r.side, "exchangeSegment": r.instrument.seg, "productType": product,
                "orderType": TYPE_OUT[r.order_type], "validity": r.validity, "securityId": r.instrument.token,
                "quantity": r.qty, "disclosedQuantity": r.disclosed or 0,
                "price": r.price if r.order_type in ("LIMIT", "SL") else 0,
                "triggerPrice": r.trigger if r.order_type in ("SL", "SL-M") else 0, "afterMarketOrder": bool(r.amo)}
        if r.amo:
            body["amoTime"] = "OPEN"
        if (r.stop_loss or r.take_profit) and r.product == "INTRADAY" and not r.amo:
            body.update(productType="BO", boProfitValue=r.take_profit or 0, boStopLossValue=r.stop_loss or 0)
        js = self._call("POST", "/orders", json_body=body, kind="order", order=True) or {}
        oid = js.get("orderId")
        if not oid:
            raise Rejected(f"Dhan did not return an order id: {js}")
        return str(oid)

    def modify(self, order: Order, qty=None, price=None, trigger=None, order_type=None) -> str:
        raw = order.raw or {}
        body = {"dhanClientId": str(self.acct.get("client_id", "")), "orderId": order.id,
                "orderType": TYPE_OUT.get(order_type or order.order_type, raw.get("orderType", "LIMIT")),
                "legName": raw.get("legName") or "ENTRY_LEG",
                "quantity": qty if qty is not None else order.qty,
                "price": price if price is not None else order.price,
                "disclosedQuantity": inum(raw.get("disclosedQuantity")),
                "triggerPrice": trigger if trigger is not None else order.trigger,
                "validity": raw.get("validity") or "DAY"}
        js = self._call("PUT", f"/orders/{order.id}", json_body=body, kind="order", order=True) or {}
        return str(js.get("orderId") or order.id)

    def cancel(self, order: Order) -> str:
        js = self._call("DELETE", f"/orders/{order.id}", kind="order", order=True) or {}
        return str(js.get("orderId") or order.id)

    def margin(self, req: OrderRequest) -> dict | None:
        r = self.prepare(req)
        js = self._call("POST", "/margincalculator", json_body={
            "dhanClientId": str(self.acct.get("client_id", "")), "exchangeSegment": r.instrument.seg,
            "transactionType": r.side, "quantity": r.qty, "productType": PRODUCT_OUT[r.product],
            "securityId": r.instrument.token, "price": r.price or 0, "triggerPrice": r.trigger or 0}) or {}
        if js:
            return {"required": num(js.get("totalMargin")), "available": num(js.get("availableBalance")),
                    "short": num(js.get("insufficientBalance"))}
        return None

    # ------------------------------------------------------------------ data
    def history(self, symbol, interval: str, period: str) -> pd.DataFrame:
        try:                                     # inside the app: the cached, incremental loader
            from .. import accounts as _acc
            return _acc.fetch_dhan(self.acct, str(symbol), interval, period)
        except ImportError:
            pass
        inst = self.resolve(symbol)
        instrument = inst.extra.get("instrument") or INSTR.get(inst.kind, "EQUITY")
        if interval in INTRA:
            start, end = span(period, True, 365)
            frames = []
            for a, b in chunks(start, end, 85):
                js = self._call("POST", "/charts/intraday", kind="hist", json_body={
                    "securityId": inst.token, "exchangeSegment": inst.seg, "instrument": instrument,
                    "interval": str(INTRA[interval]), "oi": False, "fromDate": f"{a} 09:00:00", "toDate": f"{b} 15:30:00"})
                frames.append(_dhan_frame(js))
            return pd.concat([f for f in frames if len(f)] or [candles_frame([])]).drop_duplicates("Date") \
                .sort_values("Date").reset_index(drop=True)
        start, end = span(period, False)
        js = self._call("POST", "/charts/historical", kind="hist", json_body={
            "securityId": inst.token, "exchangeSegment": inst.seg, "instrument": instrument, "expiryCode": 0,
            "oi": False, "fromDate": str(start), "toDate": str(end)})
        df = _dhan_frame(js)
        return resample(df, interval) if interval in ("1wk", "1mo") else df

    def ltp(self, instruments: list[Instrument]) -> dict:
        body: dict = {}
        for i in instruments:
            body.setdefault(i.seg, []).append(int(i.token))
        if not body:
            return {}
        js = self._call("POST", "/marketfeed/ltp", json_body=body, kind="quote") or {}
        out = {}
        for seg, rows in (js.get("data") or {}).items():
            for sid, v in (rows or {}).items():
                if (v or {}).get("last_price"):
                    out[str(sid)] = num(v["last_price"])
        return out


def _dhan_frame(js) -> pd.DataFrame:
    js = js or {}
    ts = js.get("timestamp") or []
    if not ts:
        return candles_frame([])
    rows = list(zip(ts, js.get("open", []), js.get("high", []), js.get("low", []), js.get("close", []),
                    js.get("volume", [0] * len(ts))))
    return candles_frame(rows, "s")
