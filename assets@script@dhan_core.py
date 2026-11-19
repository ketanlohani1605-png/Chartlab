"""Qt-free Dhan helpers shared by the app and by user scripts (copied next to the script as chartlab_dhan.py)."""
from __future__ import annotations

import http.client, json, os, re, threading, time, urllib.error, urllib.request
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
_OPT = USER_DIR / "cache" / "dhan_options2.pkl"


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


_OPT_MEM: dict = {}


def _options_table() -> pd.DataFrame:
    """Every listed option contract: inst, und, sid, lot, expiry, strike, type, seg. Built once a day."""
    fresh = _OPT.exists() and date.fromtimestamp(_OPT.stat().st_mtime) == date.today()
    if fresh:
        key = _OPT.stat().st_mtime
        if _OPT_MEM.get("key") == key:
            return _OPT_MEM["df"]
        try:
            t = pd.read_pickle(_OPT)
            if "seg" in t.columns:
                _OPT_MEM.update(key=key, df=t)
                return t
        except Exception:  # noqa: BLE001 - unreadable cache: rebuild it below
            pass
    m = _master()
    c = {x.upper(): x for x in m.columns}
    need = ["INSTRUMENT", "UNDERLYING_SYMBOL", "SECURITY_ID", "LOT_SIZE", "SM_EXPIRY_DATE", "STRIKE_PRICE", "OPTION_TYPE"]
    miss = [n for n in need if n not in c]
    if miss:
        raise RuntimeError("Dhan instrument list changed (missing " + ", ".join(miss) + "). Update ChartLab.")
    rows = m[m[c["INSTRUMENT"]].isin(["OPTIDX", "OPTSTK"])]
    t = rows[[c[n] for n in need]].copy()
    t.columns = ["inst", "und", "sid", "lot", "expiry", "strike", "type"]
    t["expiry"] = pd.to_datetime(t["expiry"], errors="coerce").dt.date
    t["und"] = t["und"].astype(str).str.upper()
    t["type"] = t["type"].astype(str).str.upper()
    t["strike"] = pd.to_numeric(t["strike"], errors="coerce")
    ex = rows[c["EXCH_ID"]].astype(str).str.upper() if "EXCH_ID" in c else "NSE"
    t["seg"] = "NSE_FNO"
    if "EXCH_ID" in c:
        t.loc[(ex == "BSE").to_numpy(), "seg"] = "BSE_FNO"
    t = t.dropna(subset=["expiry", "strike"]).reset_index(drop=True)
    _OPT.parent.mkdir(parents=True, exist_ok=True)
    tmp = _OPT.with_suffix(".part")
    t.to_pickle(tmp)
    os.replace(tmp, _OPT)
    _OPT_MEM.update(key=_OPT.stat().st_mtime, df=t)
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
    return _contract(r)


def _contract(r) -> dict:
    und, exp, strike, kind = str(r["und"]), r["expiry"], float(r["strike"]), str(r["type"])
    return {"security_id": str(int(r["sid"])), "segment": str(r.get("seg", "NSE_FNO")), "instrument": str(r["inst"]),
            "lot": int(r["lot"]), "name": f"{und} {exp} {strike:g} {kind}",
            "und": und, "expiry": exp, "strike": strike, "kind": kind,
            "symbol": option_symbol(und, exp, strike, kind)}


# ------------------------------------------------------------------ option symbols
_MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
_MONS = "|".join(_MON)
_TV_OPT = re.compile(r"^([A-Z][A-Z0-9&\-]*?)(\d{2})(\d{2})(\d{2})([CP])(\d+(?:\.\d+)?)$")
_KIND = {"CE": "CE", "C": "CE", "CALL": "CE", "PE": "PE", "P": "PE", "PUT": "PE"}


def option_symbol(und: str, expiry, strike: float, kind: str) -> str:
    """One-word contract name, the way TradingView writes it: NIFTY261124C25000."""
    return f"{und.upper()}{pd.Timestamp(expiry):%y%m%d}{'C' if kind.upper().startswith('C') else 'P'}{float(strike):g}"


def option_name(und: str, expiry, strike: float, kind: str) -> str:
    return f"{und.upper()} {pd.Timestamp(expiry):%d %b %Y} {float(strike):g} {kind.upper()}".replace(" 0", " ", 1)


def _expiry_spec(mid: str):
    """'' -> None (nearest) | '24NOV', '24 NOV 26', '2026-11-24', '24-11-2026' -> ('d', y|None, m, d) | 'NOV', 'NOV26' -> ('m', y|None, m).
    Returns False when the text is not a date at all."""
    mid = mid.strip().strip(",")
    if not mid:
        return None
    yr = lambda y: None if not y else (int(y) if len(y) == 4 else 2000 + int(y))  # noqa: E731
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", mid)
    if m:
        return ("d", int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})(?:[-/](\d{4}|\d{2}))?", mid)
    if m:
        return ("d", yr(m[3]), int(m[2]), int(m[1]))
    m = re.fullmatch(rf"(\d{{1,2}})\s*[-]?\s*({_MONS})[A-Z]*\s*[-']?\s*(\d{{4}}|\d{{2}})?", mid)
    if m:
        return ("d", yr(m[3]), _MON[m[2]], int(m[1]))
    m = re.fullmatch(rf"({_MONS})[A-Z]*\s*[-']?\s*(\d{{4}}|\d{{2}})?", mid)
    if m:
        return ("m", yr(m[2]), _MON[m[1]])
    return False


_TAIL_OPT = re.compile(r"^(.*?)\s*(\d+(?:\.\d+)?)\s*(CE|PE|CALL|PUT|C|P)$")


def _option_candidates(text: str):
    """Every sensible reading of the text, best first; the contract list then decides which one exists."""
    s = re.sub(r"\s+", " ", str(text).strip().upper())
    if ":" in s:
        s = s.split(":", 1)[1].strip()
    m = _TV_OPT.match(s.replace(" ", ""))
    if m:
        try:
            d = date(2000 + int(m[2]), int(m[3]), int(m[4]))
            yield m[1], ("d", d.year, d.month, d.day), float(m[6]), _KIND[m[5]]
        except ValueError:
            pass
    m = _TAIL_OPT.match(s)
    if not m or not m[1] or not float(m[2]):
        return
    head, strike, kind = m[1].strip(), m[2], _KIND[m[3]]
    if not re.match(r"[A-Z0-9]", head) or not re.search(r"[A-Z]", head):
        return
    splits = []
    if " " in head:
        und, mid = head.split(" ", 1)
        splits.append((und, mid))
    else:
        for pat in (rf"^(.+?)(\d{{1,2}}(?:{_MONS})\d{{0,4}})$", rf"^(.+?)((?:{_MONS})\d{{0,4}})$"):
            m2 = re.match(pat, head)
            if m2 and re.search(r"[A-Z]", m2[1]):
                splits.append((m2[1], m2[2]))
        splits.append((head, ""))
    for und, mid in splits:
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9&\-]*", und):
            continue
        spec = _expiry_spec(mid)
        if spec is not False:
            yield und, spec, float(strike), kind
        # "NIFTY24NOV2625000CE": the year ran into the strike
        if mid and " " not in s and len(strike) > 3:
            spec2 = _expiry_spec(mid + strike[:2])
            if spec2 and float(strike[2:] or 0):
                yield und, spec2, float(strike[2:]), kind


def parse_option(text: str):
    """'NIFTY 25000 CE', 'NIFTY 24NOV 25000 PE', 'BANKNIFTY NOV 52000 CE', 'NIFTY261124C25000'
    -> (underlying, expiry spec or None, strike, 'CE'|'PE'); None when the text is not an option."""
    return next(iter(_option_candidates(text)), None)


def is_option(text: str) -> bool:
    return parse_option(text) is not None


def _pick_expiry(t: pd.DataFrame, spec):
    today = date.today()
    exps = sorted(set(t["expiry"]))
    live = [e for e in exps if e >= today]
    if spec is None:
        return live[0] if live else None
    if spec[0] == "d":
        _, y, mo, d = spec
        hit = [e for e in (live if y is None else exps) if e.month == mo and e.day == d and (y is None or e.year == y)]
        return hit[0] if hit else None
    _, y, mo = spec
    hit = [e for e in (live if y is None else exps) if e.month == mo and (y is None or e.year == y)]
    if not hit:
        return None
    first = hit[0]
    return max(e for e in hit if (e.year, e.month) == (first.year, first.month))     # the monthly contract


def resolve_option(text: str) -> dict:
    """Any way of writing an option -> the contract (security id, segment, lot, expiry, canonical 'symbol')."""
    cands = list(_option_candidates(text))
    if not cands:
        raise RuntimeError(f"'{text}' is not an option. Write it like NIFTY 25000 CE or NIFTY 24NOV 25000 PE.")
    tab = _options_table()
    why = ""
    for und, spec, strike, kind in cands:
        t = tab[tab["und"] == und]
        if t.empty:
            why = why or f"{und} has no options on Dhan"
            continue
        exp = _pick_expiry(t, spec)
        if exp is None:
            nxt = ", ".join(f"{e:%d %b}" for e in sorted(set(t[t['expiry'] >= date.today()]['expiry']))[:4])
            why = f"no {und} expiry matches that date. Upcoming: {nxt}"
            continue
        r = t[(t["expiry"] == exp) & (t["type"] == kind) & ((t["strike"] - strike).abs() < 1e-6)]
        if r.empty:
            ks = sorted(set(t[t["expiry"] == exp]["strike"]))
            near = sorted(ks, key=lambda k: abs(k - strike))[:4]
            why = f"no {und} {strike:g} {kind} for {exp:%d %b %Y}. Nearest strikes: " + ", ".join(f"{k:g}" for k in sorted(near))
            continue
        return _contract(r.iloc[0])
    raise RuntimeError(why[0].upper() + why[1:] + ".")


def expiries(underlying: str) -> list:
    t = _options_table()
    t = t[(t["und"] == underlying.upper()) & (t["expiry"] >= date.today())]
    return sorted(set(t["expiry"]))


def strikes(underlying: str, expiry) -> list:
    t = _options_table()
    t = t[(t["und"] == underlying.upper()) & (t["expiry"] == pd.Timestamp(expiry).date())]
    return sorted(set(float(x) for x in t["strike"]))


def contracts(underlying: str, expiry) -> dict:
    """(strike, 'CE'|'PE') -> contract dict for one expiry."""
    t = _options_table()
    t = t[(t["und"] == underlying.upper()) & (t["expiry"] == pd.Timestamp(expiry).date())]
    return {(float(r["strike"]), str(r["type"])): _contract(r) for _, r in t.iterrows()}


def underlyings() -> list:
    """Names that have options, indices first."""
    t = _options_table()
    idx = [u for u in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX")
           if (t["und"] == u).any()]
    rest = sorted(set(t[t["inst"] == "OPTSTK"]["und"]))
    return idx + rest


def options_ready() -> bool:
    """True when the contract list can be read without downloading anything (safe to use from the UI thread)."""
    if _OPT_MEM.get("df") is not None:
        return True
    try:
        return _OPT.exists() and date.fromtimestamp(_OPT.stat().st_mtime) == date.today()
    except OSError:
        return False


def search_options(text: str, limit: int = 40) -> list:
    """Contracts for what was typed so far: 'NIFTY' -> strikes around the middle of the nearest expiry,
    'NIFTY 25000' -> that strike's CE and PE for the next expiries, 'NIFTY 24NOV 25000 CE' -> that contract."""
    s = re.sub(r"\s+", " ", str(text).strip().upper())
    if ":" in s:
        s = s.split(":", 1)[1].strip()
    if not s:
        return []
    tab = _options_table()
    today = date.today()
    out, seen = [], set()

    def add(rows):
        for _, r in rows.iterrows():
            c = _contract(r)
            if c["symbol"] not in seen:
                seen.add(c["symbol"])
                out.append(c)

    for und, spec, strike, kind in _option_candidates(s):
        t = tab[(tab["und"] == und) & (tab["type"] == kind) & ((tab["strike"] - strike).abs() < 1e-6)]
        if t.empty:
            continue
        if spec is None:
            t = t[t["expiry"] >= today].sort_values("expiry").head(6)
        else:
            exp = _pick_expiry(tab[tab["und"] == und], spec)
            t = t[t["expiry"] == exp] if exp else t.iloc[0:0]
        add(t)
    if out:
        return out[:limit]
    tok = s.split(" ")
    und, rest = tok[0], tok[1:]
    strike = None
    if rest and re.fullmatch(r"\d+(?:\.\d+)?", rest[-1]):
        strike, rest = float(rest[-1]), rest[:-1]
    spec = _expiry_spec(" ".join(rest))
    if spec is False:
        return []
    if (tab["und"] == und).any():
        names = [und]
    elif strike is None and not rest:
        names = [u for u in underlyings() if u.startswith(und)][:3]
    else:
        names = []
    for u in names:
        t = tab[tab["und"] == u]
        if strike is not None:
            t = t[(t["strike"] - strike).abs() < 1e-6]
            if spec is None:
                exps = sorted(set(t[t["expiry"] >= today]["expiry"]))[:4]
            else:
                e = _pick_expiry(tab[tab["und"] == u], spec)
                exps = [e] if e else []
            add(t[t["expiry"].isin(exps)].sort_values(["expiry", "type"]))
            continue
        exp = _pick_expiry(t, spec)
        if exp is None:
            continue
        t = t[t["expiry"] == exp]
        ks = sorted(set(t["strike"]))
        mid = len(ks) // 2
        near = ks[max(0, mid - 4): mid + 5] if len(names) == 1 else ks[mid:mid + 1]
        add(t[t["strike"].isin(near)].sort_values(["strike", "type"]))
    return out[:limit]


def underlying_ref(underlying: str) -> tuple[int, str]:
    """(security id, segment) of the thing the options are written on, as the option-chain API wants it."""
    u = underlying.upper()
    if u in INDEX_IDS:
        return int(INDEX_IDS[u]), "IDX_I"
    ins = instrument(u)
    return int(ins["security_id"]), ins["segment"]


def resolve_any(symbol: str) -> dict:
    """Index, stock or option -> {'security_id','segment','instrument','lot', ...}. Raises when Dhan has no such thing."""
    if is_option(symbol):
        return resolve_option(symbol)
    s = symbol.strip().upper()
    ex, base = (s.split(":", 1) if ":" in s else ("NSE", s))
    if base in INDEX_IDS:
        return {"security_id": INDEX_IDS[base], "segment": "IDX_I", "instrument": "INDEX", "lot": 1}
    if ex not in ("NSE", "BSE") or not re.fullmatch(r"[A-Z0-9&\-_]+", base):
        raise RuntimeError(f"'{symbol}' is not an NSE/BSE symbol")
    return instrument(base, ex)


# ------------------------------------------------------------------ live prices / option chain (Data API)
def _hdr(acct: dict) -> dict:
    return {"access-token": acct["access_token"], "client-id": str(acct["client_id"]), "Accept": "application/json"}


def ltp(acct: dict, instruments) -> dict:
    """[(segment, security_id), ...] -> {(segment, security_id): last price}. One call, up to 1000 instruments,
    Dhan allows one such call a second."""
    body: dict = {}
    for seg, sid in instruments:
        body.setdefault(seg, []).append(int(sid))
    if not body:
        return {}
    js = json.loads(_http(f"{DHAN}/marketfeed/ltp", body, _hdr(acct), timeout=8))
    out = {}
    for seg, rows in (js.get("data") or {}).items():
        for sid, v in (rows or {}).items():
            px = (v or {}).get("last_price")
            if px:
                out[(seg, str(sid))] = float(px)
    return out


def expiry_list(acct: dict, underlying: str) -> list:
    scrip, seg = underlying_ref(underlying)
    js = json.loads(_http(f"{DHAN}/optionchain/expirylist", {"UnderlyingScrip": scrip, "UnderlyingSeg": seg},
                          _hdr(acct), timeout=10))
    return [pd.Timestamp(d).date() for d in (js.get("data") or [])]


def option_chain(acct: dict, underlying: str, expiry) -> dict:
    """-> {'spot': float, 'rows': [{'strike', 'ce': {...}, 'pe': {...}}]} sorted by strike. Each side has
    ltp, oi, volume, iv, prev (previous close), bid, ask and delta when Dhan sends them. One call per 3 seconds."""
    scrip, seg = underlying_ref(underlying)
    js = json.loads(_http(f"{DHAN}/optionchain", {"UnderlyingScrip": scrip, "UnderlyingSeg": seg,
                                                  "Expiry": str(pd.Timestamp(expiry).date())}, _hdr(acct), timeout=10))
    d = js.get("data") or {}

    def side(x):
        if not x:
            return None
        g = x.get("greeks") or {}
        return {"ltp": float(x.get("last_price") or 0), "oi": float(x.get("oi") or 0),
                "volume": float(x.get("volume") or 0), "iv": float(x.get("implied_volatility") or 0),
                "prev": float(x.get("previous_close_price") or 0), "prev_oi": float(x.get("previous_oi") or 0),
                "bid": float(x.get("top_bid_price") or 0), "ask": float(x.get("top_ask_price") or 0),
                "delta": float(g.get("delta") or 0), "sid": str(x.get("security_id") or "")}
    rows = []
    for k, v in (d.get("oc") or {}).items():
        try:
            rows.append({"strike": float(k), "ce": side((v or {}).get("ce")), "pe": side((v or {}).get("pe"))})
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda r: r["strike"])
    return {"spot": float(d.get("last_price") or 0), "rows": rows}


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
