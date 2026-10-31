"""Qt-free Dhan helpers shared by the app and by user scripts (copied next to the script as chartlab_dhan.py)."""
from __future__ import annotations

import http.client, json, os, threading, time, urllib.error, urllib.request
import concurrent.futures as cf
from datetime import date
from pathlib import Path
import pandas as pd

USER_DIR = Path.home() / "ChartLab"
DHAN = "https://api.dhan.co/v2"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
INDEX_IDS = {"NIFTY": "13", "NIFTY50": "13", "BANKNIFTY": "25", "NIFTYBANK": "25", "FINNIFTY": "27",
             "SENSEX": "51", "MIDCPNIFTY": "442"}


def _http(url: str, body: dict | None, headers: dict, timeout=30) -> bytes:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **headers},
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Dhan API error {e.code}: {e.read().decode('utf8', 'replace')[:300]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach Dhan ({e.reason}). Check your internet.") from None


_MASTER_MEM: dict = {}
_MASTER_LOCK = threading.Lock()


def _master() -> pd.DataFrame:
    """Dhan's instrument list: downloaded once a week, parsed once per app session (it is large)."""
    cache = USER_DIR / "cache" / "dhan_master.csv"
    with _MASTER_LOCK:
        if not cache.exists() or time.time() - cache.stat().st_mtime > 7 * 86400:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".part")
            tmp.write_bytes(_http(MASTER_URL, None, {}, timeout=120))
            os.replace(tmp, cache)
        key = cache.stat().st_mtime
        if _MASTER_MEM.get("key") != key:
            _MASTER_MEM["df"] = pd.read_csv(cache, low_memory=False)
            _MASTER_MEM["key"] = key
        return _MASTER_MEM["df"]


def resolve_symbol(symbol: str) -> tuple[str, str, str, str]:
    """-> (securityId, exchangeSegment, instrument, display). Accepts RELIANCE, NSE:RELIANCE, BSE:SBIN, NIFTY."""
    s = symbol.strip().upper()
    exch = "NSE"
    if ":" in s:
        exch, s = s.split(":", 1)
    if s in INDEX_IDS:
        return INDEX_IDS[s], "IDX_I", "INDEX", s
    m = _master()
    col = {c.upper(): c for c in m.columns}
    ex, seg = col.get("EXCH_ID"), col.get("SEGMENT")
    sid, sym = col.get("SECURITY_ID"), col.get("SYMBOL_NAME") or col.get("UNDERLYING_SYMBOL")
    tsym, inst = col.get("DISPLAY_NAME"), col.get("INSTRUMENT")
    ser = col.get("SERIES")
    if not (ex and sid and inst):
        raise RuntimeError("Dhan instrument list has an unexpected format; update ChartLab.")
    q = m[(m[ex] == exch) & (m[inst] == "EQUITY")]
    if seg:
        q = q[q[seg] == "E"]
    if ser:
        q = q[q[ser].isin(["EQ", "BE", "SM"]) | q[ser].isna()]
    hit = q[q[sym].astype(str).str.upper() == s] if sym else q.iloc[0:0]
    if hit.empty and tsym:
        hit = q[q[tsym].astype(str).str.upper() == s]
    if hit.empty:
        raise RuntimeError(f"'{symbol}' not found on Dhan {exch} equity. Try NSE:RELIANCE or BSE:SBIN.")
    return str(int(hit.iloc[0][sid])), f"{exch}_EQ", "EQUITY", s




# ------------------------------------------------------------------ instruments (fast, cached)
_IDS = USER_DIR / "cache" / "dhan_ids.json"
_OPT = USER_DIR / "cache" / "dhan_options.pkl"


def _ids_cache() -> dict:
    try:
        return json.loads(_IDS.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _ids_put(key, val):
    d = _ids_cache()
    d[key] = val
    _IDS.parent.mkdir(parents=True, exist_ok=True)
    _IDS.write_text(json.dumps(d))


def instrument(symbol: str, exchange: str = "NSE") -> dict:
    """Equity/index -> {'security_id','segment','instrument','lot'}; cached so only the first call is slow."""
    if isinstance(symbol, dict):
        return symbol
    key = "EQ:" + (symbol.upper() if ":" in symbol else f"{exchange}:{symbol.upper()}")
    hit = _ids_cache().get(key)
    if hit:
        return dict(zip(("security_id", "segment", "instrument", "lot"), hit))
    sid, seg, inst, _ = resolve_symbol(symbol if ":" in symbol else f"{exchange}:{symbol}")
    _ids_put(key, [sid, seg, inst, 1])
    return {"security_id": sid, "segment": seg, "instrument": inst, "lot": 1}


def _options_table() -> pd.DataFrame:
    fresh = _OPT.exists() and date.fromtimestamp(_OPT.stat().st_mtime) == date.today()
    if fresh:
        return pd.read_pickle(_OPT)
    m = _master()
    c = {x.upper(): x for x in m.columns}
    need = ["INSTRUMENT", "UNDERLYING_SYMBOL", "SECURITY_ID", "LOT_SIZE", "SM_EXPIRY_DATE", "STRIKE_PRICE", "OPTION_TYPE"]
    miss = [n for n in need if n not in c]
    if miss:
        raise RuntimeError("Dhan instrument list changed (missing " + ", ".join(miss) + "). Update ChartLab.")
    t = m[m[c["INSTRUMENT"]].isin(["OPTIDX", "OPTSTK"])][[c[n] for n in need]].copy()
    t.columns = ["inst", "und", "sid", "lot", "expiry", "strike", "type"]
    t["expiry"] = pd.to_datetime(t["expiry"], errors="coerce").dt.date
    t["und"] = t["und"].astype(str).str.upper()
    _OPT.parent.mkdir(parents=True, exist_ok=True)
    t.to_pickle(_OPT)
    return t


def option(underlying: str, strike: float, kind: str, expiry: str | None = None) -> dict:
    """Option contract, e.g. option("NIFTY", 24500, "CE"). expiry=None -> nearest upcoming expiry."""
    kind = kind.upper()
    if kind not in ("CE", "PE"):
        raise ValueError("kind must be CE or PE")
    t = _options_table()
    t = t[(t["und"] == underlying.upper()) & (t["type"] == kind) & (t["strike"].astype(float) == float(strike))]
    if expiry:
        t = t[t["expiry"] == pd.Timestamp(expiry).date()]
    else:
        t = t[t["expiry"] >= date.today()].sort_values("expiry")
    if t.empty:
        raise RuntimeError(f"No {underlying} {strike} {kind} contract found" + (f" for {expiry}" if expiry else ""))
    r = t.iloc[0]
    return {"security_id": str(int(r["sid"])), "segment": "NSE_FNO", "instrument": str(r["inst"]),
            "lot": int(r["lot"]), "name": f"{underlying.upper()} {r['expiry']} {strike:g} {kind}"}


# ------------------------------------------------------------------ fast order routing
class Session:
    """One kept-alive HTTPS connection per account: no TLS handshake on the order path."""
    HOST = "api.dhan.co"

    def __init__(self, client_id: str, token: str):
        self.hdr = {"access-token": token, "client-id": client_id, "Content-Type": "application/json",
                    "Accept": "application/json"}
        self.conn = None
        self.lock = threading.Lock()

    def warm(self):
        with self.lock:
            self.conn = http.client.HTTPSConnection(self.HOST, timeout=10)
            self.conn.connect()

    def request(self, method: str, path: str, body: dict | None = None):
        payload = json.dumps(body) if body is not None else None
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.conn is None:
                        self.conn = http.client.HTTPSConnection(self.HOST, timeout=10)
                    self.conn.request(method, path, payload, self.hdr)
                    break                                   # the request was fully sent
                except (http.client.HTTPException, OSError):
                    self.conn = None                        # stale connection: nothing reached the server, retry once
                    if attempt:
                        raise
            try:
                r = self.conn.getresponse()
                data = r.read()
            except (http.client.HTTPException, OSError) as e:
                self.conn = None
                raise RuntimeError("Order sent but no answer received - CHECK your Dhan order book before retrying "
                                   f"({e})") from None
        if r.status >= 400:
            raise RuntimeError(f"Dhan API error {r.status}: {data.decode('utf8', 'replace')[:300]}")
        return json.loads(data or b"{}")


_sessions: dict = {}


def session(acct: dict) -> Session:
    k = acct["client_id"]
    if k not in _sessions:
        _sessions[k] = Session(acct["client_id"], acct["access_token"])
    return _sessions[k]


def warmup(accts: list) -> dict:
    """Open every account's connection in parallel (call once before trading starts)."""
    def one(a):
        t = time.perf_counter()
        try:
            session(a).warm()
            return a["name"], round((time.perf_counter() - t) * 1000)
        except Exception as e:  # noqa: BLE001
            return a["name"], f"failed: {e}"
    with cf.ThreadPoolExecutor(max(1, len(accts))) as ex:
        return dict(ex.map(one, accts))


def _body(a, inst, side, qty, order_type, price, product, lots, tag):
    unit = int(inst.get("lot", 1)) if lots else 1
    base = lots if lots else qty
    q = max(1, int(round(float(base) * float(a.get("qty_mult") or 1)))) * unit
    return {"dhanClientId": a["client_id"], "transactionType": side, "exchangeSegment": inst["segment"],
            "productType": product.upper(), "orderType": order_type, "validity": "DAY",
            "securityId": inst["security_id"], "quantity": q,
            "price": float(price) if order_type == "LIMIT" else 0, "correlationId": (tag or "chartlab")[:25]}


def place_many(accts: list, symbol, side: str, qty: int = 0, order_type="MARKET", price=None, product="INTRADAY",
               exchange="NSE", lots: int = 0, tag: str = "") -> list:
    """Send ONE order to every account at the same instant (threads released together by a barrier).
    Instrument lookup happens once, before the barrier. One account failing never blocks the others.
    Paper accounts (live != true) only print. Returns [{account, ms, ok, response|error}]."""
    side, order_type = side.upper(), order_type.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    if order_type == "LIMIT" and not price:
        raise ValueError("LIMIT order needs a price")
    if not qty and not lots:
        raise ValueError("give qty= (units) or lots=")
    inst = instrument(symbol, exchange)
    for a in accts:
        if a.get("live") and a["client_id"] not in _sessions:
            session(a)                                       # created now, connection opens lazily
    bar = threading.Barrier(max(1, len(accts)))
    name = inst.get("name") or (symbol if isinstance(symbol, str) else inst["security_id"])

    def one(a):
        try:
            body = _body(a, inst, side, qty, order_type, price, product, lots, tag or a["name"])
        except Exception as e:  # noqa: BLE001
            bar.wait()
            return {"account": a["name"], "ok": False, "error": str(e), "ms": 0}
        bar.wait()
        t = time.perf_counter()
        if not a.get("live"):
            return {"account": a["name"], "ok": True, "paper": True, "ms": 0, "request": body}
        try:
            r = session(a).request("POST", "/v2/orders", body)
            return {"account": a["name"], "ok": True, "response": r, "ms": round((time.perf_counter() - t) * 1000),
                    "request": body}
        except Exception as e:  # noqa: BLE001
            return {"account": a["name"], "ok": False, "error": str(e), "ms": round((time.perf_counter() - t) * 1000)}

    with cf.ThreadPoolExecutor(max(1, len(accts))) as ex:
        res = list(ex.map(one, accts))
    for r in res:
        if r.get("paper"):
            print(f"[PAPER] {r['account']}: {side} {r['request']['quantity']} {name} {order_type}")
        elif r["ok"]:
            print(f"[LIVE] {r['account']}: {side} {r['request']['quantity']} {name} {order_type} -> {r['response']} ({r['ms']} ms)")
        else:
            print(f"[FAILED] {r['account']}: {r['error']}")
    return res


def place_order(client_id, access_token, symbol, side, qty, order_type="MARKET", price=None, product="INTRADAY",
                exchange="NSE", live=False, tag=""):
    """Single-account convenience wrapper around place_many."""
    a = {"name": tag or client_id, "client_id": client_id, "access_token": access_token, "live": live}
    r = place_many([a], symbol, side, qty, order_type, price, product, exchange, tag=tag)[0]
    if not r["ok"]:
        raise RuntimeError(r["error"])
    return r if r.get("paper") else r["response"]
