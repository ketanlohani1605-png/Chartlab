"""Qt-free Dhan helpers shared by the app and by user scripts (copied next to the script as chartlab_dhan.py)."""
import json, os, time, urllib.error, urllib.request
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


def _master() -> pd.DataFrame:
    cache = USER_DIR / "cache" / "dhan_master.csv"
    if not cache.exists() or time.time() - cache.stat().st_mtime > 7 * 86400:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(_http(MASTER_URL, None, {}, timeout=120))
    return pd.read_csv(cache, low_memory=False)


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




def place_order(client_id, access_token, symbol, side, qty, order_type="MARKET", price=None, product="INTRADAY",
                exchange="NSE", live=False, tag=""):
    """BUY/SELL on one account. Paper mode (live=False) only prints what WOULD be sent."""
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    order_type = order_type.upper()
    if order_type == "LIMIT" and not price:
        raise ValueError("LIMIT order needs a price")
    sid, seg, _inst, _ = resolve_symbol(symbol if ":" in symbol else f"{exchange}:{symbol}")
    body = {"dhanClientId": client_id, "transactionType": side, "exchangeSegment": seg, "productType": product.upper(),
            "orderType": order_type, "validity": "DAY", "securityId": sid, "quantity": int(qty),
            "price": float(price) if order_type == "LIMIT" else 0, "correlationId": (tag or "chartlab")[:25]}
    if not live:
        print(f"[PAPER] {side} {qty} {symbol} {order_type} (no order sent; tick Live orders for this account to send)")
        return {"paper": True, "request": body}
    r = json.loads(_http(f"{DHAN}/orders", body, {"access-token": access_token, "client-id": client_id,
                                                    "Accept": "application/json"}))
    print(f"[LIVE] {side} {qty} {symbol} {order_type} -> {r}")
    return r
