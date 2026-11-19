"""Broker-neutral core shared by every broker adapter.

Only the standard library and pandas are used (no Qt, no requests), because the same files are copied next to a user
script (Strategy -> Run on accounts), which runs in the user's own Python.

What lives here:
  * the common shapes every adapter returns (Instrument, Order, Position, Holding, Funds, OrderRequest);
  * the error types the app understands (log in again, IP not registered, rejected, sent-but-no-answer, ...);
  * Http: one kept-alive connection per host and thread, rate limits, and the rule that an order request is
    never sent twice (a retry could buy twice);
  * instrument lists: downloaded once a day per broker, turned into one common table, looked up by any way of
    writing a symbol (RELIANCE, NSE:SBIN, NIFTY, NIFTY 24NOV 25000 CE, NIFTY261124C25000, BANKNIFTY FUT, ...).
"""
from __future__ import annotations

import base64
import datetime as dt
import gzip
import http.client
import io
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

USER_DIR = Path(os.environ.get("CHARTLAB_HOME") or (Path.home() / "ChartLab"))
CACHE = USER_DIR / "cache" / "brokers"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

SIDES = ("BUY", "SELL")
ORDER_TYPES = ("MARKET", "LIMIT", "SL", "SL-M")          # SL = stop-limit, SL-M = stop-market
PRODUCTS = ("INTRADAY", "DELIVERY", "MARGIN", "MTF")     # MARGIN = F&O / commodity carried overnight (NRML)
VALIDITIES = ("DAY", "IOC")
OPEN_STATES = ("open", "pending", "trigger_pending", "partial")
DONE_STATES = ("complete", "cancelled", "rejected")


# ---------------------------------------------------------------------------------------------------- errors
class BrokerError(RuntimeError):
    """Anything a broker said no to. `kind` tells the app what to do about it."""
    kind = "error"

    def __init__(self, msg: str, code=None, raw=None):
        super().__init__(msg)
        self.code, self.raw = code, raw


class AuthError(BrokerError):            # token missing / expired / revoked: log in again
    kind = "auth"


class AccessError(BrokerError):          # IP not registered, segment not active, no data subscription, ...
    kind = "access"


class RateLimited(BrokerError):
    kind = "rate"


class Rejected(BrokerError):             # the broker or the exchange refused the order (margin, price band, ...)
    kind = "rejected"


class NetError(BrokerError):             # could not reach the broker: nothing happened
    kind = "network"


class SentNoAnswer(BrokerError):         # the order went out but no answer came back: CHECK the order book
    kind = "unknown"


_IP_WORDS = ("static ip", "ip not", "not whitelisted", "whitelist", "ip address", "unregistered ip", "invalid ip",
             "ip is not", "registered ip")


def classify(msg: str, status: int = 0, default=BrokerError):
    """Pick the error type from an HTTP status and the broker's words."""
    low = (msg or "").lower()
    if any(w in low for w in _IP_WORDS):
        return AccessError
    if status in (401,) or any(w in low for w in ("token expired", "invalid token", "token is invalid", "session expired",
                                                  "invalid session", "unauthorized", "not authorized", "login again",
                                                  "access token", "tokenexception", "invalid access", "jwt")):
        return AuthError
    if status == 429 or "too many" in low or "rate limit" in low:
        return RateLimited
    if status == 403:
        return AccessError
    return default


def ip_hint(broker_name: str) -> str:
    return (f"{broker_name} only accepts API orders from the static IP address registered in your {broker_name} "
            "API app (SEBI rule since 1 April 2026). Register this internet connection's IP there, or place the order "
            "on the broker's own app.")


# ---------------------------------------------------------------------------------------------------- shapes
@dataclass
class Instrument:
    broker: str
    token: str                 # the broker's own id (Kite instrument_token, Upstox instrument_key, Angel token, ...)
    exchange: str              # NSE BSE NFO BFO MCX CDS NCO BCD
    tsym: str                  # the broker's trading symbol
    name: str                  # underlying / company symbol: RELIANCE, NIFTY
    kind: str                  # EQ IDX FUT CE PE
    expiry: dt.date | None = None
    strike: float = 0.0
    lot: int = 1
    tick: float = 0.05
    seg: str = ""              # broker segment code needed by its order / feed APIs
    extra: dict = field(default_factory=dict)

    @property
    def derivative(self) -> bool:
        return self.kind in ("FUT", "CE", "PE")

    @property
    def tradable(self) -> bool:
        return self.kind != "IDX"

    @property
    def display(self) -> str:
        if self.kind in ("CE", "PE") and self.expiry:
            return f"{self.name} {self.expiry:%d %b %y} {self.strike:g} {self.kind}".upper()
        if self.kind == "FUT" and self.expiry:
            return f"{self.name} {self.expiry:%d %b %y} FUT".upper()
        return self.name if self.exchange in ("NSE", "") or self.kind == "IDX" else f"{self.exchange}:{self.name}"

    def round_price(self, px: float) -> float:
        t = self.tick if self.tick and self.tick > 0 else 0.05
        return round(round(float(px) / t) * t, 4)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expiry"] = self.expiry.isoformat() if self.expiry else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Instrument":
        d = dict(d)
        if d.get("expiry"):
            d["expiry"] = dt.date.fromisoformat(str(d["expiry"])[:10])
        return cls(**d)


@dataclass
class OrderRequest:
    symbol: str
    side: str
    qty: int = 0                  # units (shares, or lots x lot size for F&O)
    order_type: str = "MARKET"
    product: str = "INTRADAY"
    price: float = 0.0
    trigger: float = 0.0
    validity: str = "DAY"
    amo: bool = False
    tag: str = "chartlab"
    disclosed: int = 0
    lots: int = 0                 # alternative to qty for F&O: qty = lots x lot size
    stop_loss: float = 0.0        # bracket legs, as a price distance from the entry (brokers that support it)
    take_profit: float = 0.0
    instrument: Instrument | None = None

    def normalised(self) -> "OrderRequest":
        """Upper-case enums, qty from lots, and every check that does not need the broker. Raises ValueError."""
        r = OrderRequest(**{**self.__dict__})
        r.side, r.order_type = r.side.upper().strip(), r.order_type.upper().strip().replace("_", "-")
        r.order_type = {"SLM": "SL-M", "STOP": "SL-M", "STOPLIMIT": "SL", "STOP-LIMIT": "SL", "MKT": "MARKET",
                        "LMT": "LIMIT", "SL-L": "SL"}.get(r.order_type, r.order_type)
        r.product = {"MIS": "INTRADAY", "INTRA": "INTRADAY", "I": "INTRADAY", "CNC": "DELIVERY", "D": "DELIVERY",
                     "NRML": "MARGIN", "CARRYFORWARD": "MARGIN", "NORMAL": "MARGIN"}.get(r.product.upper(),
                                                                                      r.product.upper())
        r.validity = r.validity.upper()
        if r.side not in SIDES:
            raise ValueError("side must be BUY or SELL")
        if r.order_type not in ORDER_TYPES:
            raise ValueError(f"order type must be one of {', '.join(ORDER_TYPES)}")
        if r.product not in PRODUCTS:
            raise ValueError(f"product must be one of {', '.join(PRODUCTS)}")
        if r.validity not in VALIDITIES:
            raise ValueError("validity must be DAY or IOC")
        lot = r.instrument.lot if r.instrument and r.instrument.lot > 0 else 1
        if r.lots and not r.qty:
            r.qty = int(r.lots) * lot
        r.qty = int(r.qty or 0)
        if r.qty <= 0:
            raise ValueError("quantity must be more than 0")
        if r.instrument is not None:
            if not r.instrument.tradable:
                raise ValueError(f"{r.instrument.display} is an index: trade its futures or options instead")
            if r.instrument.derivative and lot > 1 and r.qty % lot:
                raise ValueError(f"{r.instrument.display} trades in lots of {lot}: {r.qty} is not a whole number of lots")
        if r.order_type in ("LIMIT", "SL") and not (r.price and r.price > 0):
            raise ValueError(f"a {r.order_type} order needs a price")
        if r.order_type in ("SL", "SL-M") and not (r.trigger and r.trigger > 0):
            raise ValueError(f"a {r.order_type} order needs a trigger price")
        if r.order_type in ("MARKET", "SL-M"):
            r.price = 0.0
        if r.order_type in ("MARKET", "LIMIT"):
            r.trigger = 0.0
        if r.instrument is not None:
            if r.price:
                r.price = r.instrument.round_price(r.price)
            if r.trigger:
                r.trigger = r.instrument.round_price(r.trigger)
        r.tag = re.sub(r"[^A-Za-z0-9_\-]", "", r.tag or "chartlab")[:20] or "chartlab"
        return r


@dataclass
class Order:
    id: str
    symbol: str
    exchange: str = ""
    side: str = ""
    qty: int = 0
    filled: int = 0
    pending: int = 0
    order_type: str = ""
    product: str = ""
    price: float = 0.0
    trigger: float = 0.0
    avg_price: float = 0.0
    status: str = "open"          # open pending trigger_pending partial complete cancelled rejected
    message: str = ""
    time: str = ""
    token: str = ""
    tag: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATES


@dataclass
class Position:
    symbol: str
    exchange: str = ""
    product: str = ""
    qty: int = 0                  # net, signed: +long / -short
    avg: float = 0.0
    ltp: float = 0.0
    pnl: float = 0.0
    realised: float = 0.0
    unrealised: float = 0.0
    buy_qty: int = 0
    sell_qty: int = 0
    buy_avg: float = 0.0
    sell_avg: float = 0.0
    token: str = ""
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class Holding:
    symbol: str
    exchange: str = ""
    qty: int = 0
    avg: float = 0.0
    ltp: float = 0.0
    pnl: float = 0.0
    day_change: float = 0.0
    isin: str = ""
    token: str = ""
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class Funds:
    available: float = 0.0
    used: float = 0.0
    total: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)


def num(x, default=0.0) -> float:
    try:
        v = float(x)
        return v if v == v else default             # NaN -> default
    except (TypeError, ValueError):
        return default


def inum(x, default=0) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------------------------------- rate limits
class Limiter:
    """Token bucket: at most `rate` calls a second on average, short bursts up to `burst`."""

    def __init__(self, rate: float, burst: int | None = None):
        self.rate, self.burst = float(rate), float(burst or max(1, int(rate)))
        self.tokens, self.t = self.burst, time.monotonic()
        self.lock = threading.Lock()

    def wait(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                need = (1 - self.tokens) / self.rate
            time.sleep(min(need, 1.0))


_LIMITERS: dict = {}
_LIM_LOCK = threading.Lock()


def limiter(key: str, rate: float, burst: int | None = None) -> Limiter:
    with _LIM_LOCK:
        if key not in _LIMITERS:
            _LIMITERS[key] = Limiter(rate, burst)
        return _LIMITERS[key]


# ---------------------------------------------------------------------------------------------------- HTTP
class Response:
    def __init__(self, status: int, headers: dict, body: bytes):
        self.status, self.headers, self.body = status, headers, body

    def json(self):
        try:
            return json.loads(self.body.decode("utf8", "replace") or "null")
        except ValueError:
            return None

    @property
    def text(self) -> str:
        return self.body.decode("utf8", "replace")


class Http:
    """Small HTTPS client with kept-alive connections (one per host per thread: no TLS handshake on the order path).

    `send` is the only method. An order request (`safe=False`) is retried only when it provably never left this
    computer; once it was written to the connection, a lost answer raises SentNoAnswer instead of trying again."""

    def __init__(self, name: str, timeout: float = 15.0):
        self.name, self.timeout = name, timeout
        self._local = threading.local()
        self._ctx = ssl.create_default_context()

    def _conn(self, scheme: str, host: str, port: int | None):
        pool = getattr(self._local, "pool", None)
        if pool is None:
            pool = self._local.pool = {}
        key = (scheme, host, port)
        c = pool.get(key)
        if c is None:
            if scheme == "https":
                c = http.client.HTTPSConnection(host, port, timeout=self.timeout, context=self._ctx)
            else:
                c = http.client.HTTPConnection(host, port, timeout=self.timeout)
            pool[key] = c
        return key, c

    def _drop(self, key):
        c = getattr(self._local, "pool", {}).pop(key, None)
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    def send(self, method: str, url: str, *, params: dict | None = None, json_body=None, form: dict | None = None,
             headers: dict | None = None, safe: bool | None = None, limit: Limiter | None = None,
             timeout: float | None = None) -> Response:
        method = method.upper()
        if safe is None:
            safe = method in ("GET", "HEAD")
        u = urllib.parse.urlsplit(url)
        path = u.path or "/"
        q = [x for x in [u.query, urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None},
                                                          doseq=True)] if x]
        if q:
            path += "?" + "&".join(q)
        hdr = {"User-Agent": "ChartLab", "Accept": "application/json", "Accept-Encoding": "gzip"}
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode()
            hdr["Content-Type"] = "application/json"
        elif form is not None:
            body = urllib.parse.urlencode({k: v for k, v in form.items() if v is not None}, doseq=True).encode()
            hdr["Content-Type"] = "application/x-www-form-urlencoded"
        hdr.update({k: v for k, v in (headers or {}).items() if v is not None})
        if body is not None or method in ("POST", "PUT", "PATCH"):
            hdr["Content-Length"] = str(len(body or b""))
        tries = 3 if safe else 2
        last = None
        for attempt in range(tries):
            if limit is not None:
                limit.wait()
            key, c = self._conn(u.scheme or "https", u.hostname or "", u.port)
            if timeout:
                c.timeout = timeout
            sent = False
            try:
                c.request(method, path, body, hdr)
                sent = True
                r = c.getresponse()
                data = r.read()
                if (r.getheader("Content-Encoding") or "").lower() == "gzip":
                    data = gzip.decompress(data)
                resp = Response(r.status, {k.lower(): v for k, v in r.getheaders()}, data)
                if r.getheader("Connection", "").lower() == "close":
                    self._drop(key)
            except (http.client.HTTPException, OSError, ssl.SSLError) as e:
                self._drop(key)
                last = e
                if sent and not safe:
                    raise SentNoAnswer(f"{self.name}: the order was sent but no answer came back ({e}). CHECK your "
                                       f"{self.name} order book before trying again.") from None
                if attempt + 1 < tries:
                    time.sleep(0.3 * (attempt + 1) if safe else 0)
                    continue
                if isinstance(e, (socket.timeout, TimeoutError)):
                    raise NetError(f"{self.name} did not answer in time. Check your internet.") from None
                raise NetError(f"Could not reach {self.name} ({e}). Check your internet.") from None
            if safe and resp.status in (429, 502, 503, 504) and attempt + 1 < tries:
                time.sleep(0.6 * (attempt + 1))
                continue
            return resp
        raise NetError(f"Could not reach {self.name} ({last})")


# ---------------------------------------------------------------------------------------------------- JWT helper
def jwt_claims(token: str) -> dict:
    """The payload of a JWT access token (not verified: only read for its expiry time)."""
    try:
        part = str(token).split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode())
    except Exception:  # noqa: BLE001
        return {}


_JWT = re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")


def find_jwt(obj):
    """The access token inside a broker's answer, whatever the field is called."""
    if isinstance(obj, str):
        s = obj.strip().strip('"')
        return s if _JWT.match(s) else None
    if isinstance(obj, dict):
        for k in ("accessToken", "access_token", "token", "jwtToken"):
            t = find_jwt(obj.get(k)) if isinstance(obj.get(k), str) else None
            if t:
                return t
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            t = find_jwt(v)
            if t:
                return t
    return None


def jwt_expiry(token: str) -> float | None:
    exp = jwt_claims(token).get("exp")
    try:
        return float(exp)
    except (TypeError, ValueError):
        return None


def next_ist(hour: int, minute: int = 0, after: float | None = None) -> float:
    """Unix time of the next hh:mm India time after `after` (the daily token reset of most Indian brokers)."""
    t = dt.datetime.fromtimestamp(after if after is not None else time.time(), IST)
    cut = t.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cut <= t:
        cut += dt.timedelta(days=1)
    return cut.timestamp()


# ---------------------------------------------------------------------------------------------------- downloads
def fetch_bytes(url: str, timeout: float = 120.0, headers: dict | None = None) -> bytes:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "ChartLab", "Accept-Encoding": "gzip", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                data = gzip.decompress(data)
            return data
    except Exception as e:  # noqa: BLE001
        raise NetError(f"Could not download {url.split('/')[2]} instrument list ({e})") from None


_MASTERS: dict = {}
_MASTER_LOCKS: dict = {}


def master(broker: str, build, max_age_h: float = 20.0) -> pd.DataFrame:
    """The broker's instrument list in the common columns, built by `build()` at most once a day (cached on disk as a
    pickle; kept in memory for the session). The day changes at 08:00 India time, after brokers publish new lists."""
    day = (dt.datetime.now(IST) - dt.timedelta(hours=8)).date().isoformat()
    lock = _MASTER_LOCKS.setdefault(broker, threading.Lock())
    with lock:
        hit = _MASTERS.get(broker)
        if hit is not None and hit[0] == day:
            return hit[1]
        f = CACHE / f"{broker}_master.pkl"
        df = None
        try:
            if f.exists() and (time.time() - f.stat().st_mtime) < max_age_h * 3600:
                old = pd.read_pickle(f)
                if getattr(old, "attrs", {}).get("day") == day:
                    df = old
        except Exception:  # noqa: BLE001 - unreadable cache: build again
            df = None
        if df is None:
            df = build()
            df = normalise_master(df, broker)
            df.attrs["day"] = day
            try:
                f.parent.mkdir(parents=True, exist_ok=True)
                tmp = f.with_suffix(".part")
                df.to_pickle(tmp)
                os.replace(tmp, f)
            except Exception:  # noqa: BLE001 - the cache is optional
                pass
        _MASTERS[broker] = (day, df)
        return df


MASTER_COLS = ["token", "exchange", "tsym", "name", "kind", "expiry", "strike", "lot", "tick", "seg"]


def normalise_master(df: pd.DataFrame, broker: str) -> pd.DataFrame:
    for c in MASTER_COLS:
        if c not in df.columns:
            df[c] = None
    df = df.copy()
    df["token"] = df["token"].astype(str)
    for c in ("exchange", "tsym", "name", "kind", "seg"):
        df[c] = df[c].fillna("").astype(str)
    df["name"] = df["name"].str.upper().str.strip()
    df["exchange"] = df["exchange"].str.upper().str.strip()
    df["kind"] = df["kind"].str.upper().str.strip()
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce").fillna(0.0).astype(float)
    df["lot"] = pd.to_numeric(df["lot"], errors="coerce").fillna(1).astype(int).clip(lower=1)
    df["tick"] = pd.to_numeric(df["tick"], errors="coerce").fillna(0.05).astype(float)
    df.loc[df["tick"] <= 0, "tick"] = 0.05
    ex = df["expiry"]
    first = ex.dropna().iloc[0] if ex.notna().any() else None
    if first is None or type(first) is not dt.date:            # Timestamps / strings -> plain dates
        df["expiry"] = pd.to_datetime(ex, errors="coerce").dt.date
    df["expiry"] = df["expiry"].astype(object).where(df["expiry"].notna(), None)
    df.loc[df["kind"].isin(["EQ", "IDX"]), "expiry"] = None
    df.loc[df["kind"] == "IDX", "tick"] = 0.05
    df.loc[~df["kind"].isin(["CE", "PE"]), "strike"] = 0.0
    df = df[df["kind"].isin(["EQ", "IDX", "FUT", "CE", "PE"])].reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------------------------------- symbols
INDEX_ALIASES = {
    "NIFTY": "NIFTY", "NIFTY50": "NIFTY", "NIFTY 50": "NIFTY", "^NSEI": "NIFTY",
    "BANKNIFTY": "BANKNIFTY", "NIFTYBANK": "BANKNIFTY", "NIFTY BANK": "BANKNIFTY", "^NSEBANK": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY", "NIFTYFIN": "FINNIFTY", "NIFTY FIN SERVICE": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY", "NIFTYMIDSELECT": "MIDCPNIFTY", "NIFTY MID SELECT": "MIDCPNIFTY",
    "NIFTYNXT50": "NIFTYNXT50", "NIFTY NEXT 50": "NIFTYNXT50",
    "SENSEX": "SENSEX", "^BSESN": "SENSEX", "BANKEX": "BANKEX", "SENSEX50": "SENSEX50",
    "INDIAVIX": "INDIAVIX", "INDIA VIX": "INDIAVIX", "VIX": "INDIAVIX",
}
BSE_INDICES = {"SENSEX", "BANKEX", "SENSEX50"}
MCX_NAMES = {"CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI", "GOLD", "GOLDM", "GOLDGUINEA", "GOLDPETAL", "GOLDTEN",
             "SILVER", "SILVERM", "SILVERMIC", "COPPER", "ZINC", "ZINCMINI", "ALUMINIUM", "ALUMINI", "LEAD", "LEADMINI",
             "NICKEL", "COTTONCNDY", "MENTHAOIL", "ELECDMBL"}

_MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
_MONS = "|".join(_MON)
_TV_OPT = re.compile(r"^([A-Z][A-Z0-9&\-]*?)(\d{2})(\d{2})(\d{2})([CP])(\d+(?:\.\d+)?)$")
_KIND = {"CE": "CE", "C": "CE", "CALL": "CE", "PE": "PE", "P": "PE", "PUT": "PE"}
_TAIL_OPT = re.compile(r"^(.*?)\s*(\d+(?:\.\d+)?)\s*(CE|PE|CALL|PUT|C|P)$")
_FUT = re.compile(rf"^([A-Z0-9][A-Z0-9&\-]*?)\s*((?:\d{{1,2}}\s*)?(?:{_MONS})[A-Z]*\s*'?\s*(?:\d{{4}}|\d{{2}})?|"
                  rf"(?:\d{{2}}(?:{_MONS})))?\s*(?:FUT|FUTURE|FUTURES|-I|1!)$")


def _expiry_spec(mid: str):
    """'' -> None (nearest) | '24NOV', '24 NOV 26', '2026-11-24' -> ('d', y|None, m, d) | 'NOV', 'NOV26' -> ('m', y|None, m).
    False when the text is not a date at all."""
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
    m = re.fullmatch(rf"(\d{{2}})({_MONS})", mid)                 # 26NOV (year then month, the Kite way)
    if m:
        return ("m", 2000 + int(m[1]), _MON[m[2]])
    return False


_KITE_WEEKLY = re.compile(r"^([A-Z][A-Z0-9&\-]*?)(\d{2})([1-9OND])(\d{2})(\d+(?:\.\d+)?)(CE|PE)$")
_WEEK_MON = {**{str(i): i for i in range(1, 10)}, "O": 10, "N": 11, "D": 12}


def _option_candidates(text: str):
    s = re.sub(r"\s+", " ", str(text).strip().upper())
    if ":" in s:
        s = s.split(":", 1)[1].strip()
    m = _TV_OPT.match(s.replace(" ", ""))
    if m:
        try:
            d = dt.date(2000 + int(m[2]), int(m[3]), int(m[4]))
            yield m[1], ("d", d.year, d.month, d.day), float(m[6]), _KIND[m[5]]
        except ValueError:
            pass
    m = _KITE_WEEKLY.match(s.replace(" ", ""))          # NIFTY26N2325250CE: year, month code, day (Kite weekly)
    if m:
        try:
            d = dt.date(2000 + int(m[2]), _WEEK_MON[m[3]], int(m[4]))
            yield m[1], ("d", d.year, d.month, d.day), float(m[5]), _KIND[m[6]]
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
        for pat in (rf"^(.+?)(\d{{1,2}}(?:{_MONS})\d{{0,4}})$", rf"^(.+?)((?:{_MONS})\d{{0,4}})$",
                    rf"^(.+?)(\d{{2}}(?:{_MONS}))$"):
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
        ym = re.fullmatch(rf"(\d{{2}})({_MONS})", mid.replace(" ", ""))
        if ym:                                           # NIFTY26NOV25250CE: '26NOV' may be year + month (Kite)
            yield und, ("m", 2000 + int(ym[1]), _MON[ym[2]]), float(strike), kind
        if mid and " " not in s and len(strike) > 3:
            spec2 = _expiry_spec(mid + strike[:2])
            if spec2 and float(strike[2:] or 0):
                yield und, spec2, float(strike[2:]), kind


@dataclass
class Spec:
    kind: str                       # EQ IDX FUT CE PE
    name: str
    exchange: str = ""              # '' = let the instrument list decide (NSE first)
    expiry: object = None           # None (nearest) or an _expiry_spec tuple
    strike: float = 0.0
    alts: list = field(default_factory=list)      # other readings of an option, tried in order


def parse_symbol(text: str) -> Spec:
    """Any way of writing a symbol -> what it means. Yahoo's RELIANCE.NS / .BO are understood too."""
    s = re.sub(r"\s+", " ", str(text or "").strip().upper())
    if not s:
        raise ValueError("empty symbol")
    for suf, ex in ((".NS", "NSE"), (".BO", "BSE")):
        if s.endswith(suf):
            s = f"{ex}:{s[:-len(suf)]}"
    exch = ""
    body = s
    if ":" in s:
        exch, body = s.split(":", 1)
        exch, body = exch.strip(), body.strip()
    if body in INDEX_ALIASES:
        idx = INDEX_ALIASES[body]
        return Spec("IDX", idx, "BSE" if idx in BSE_INDICES else "NSE")
    opts = list(_option_candidates(body))
    if opts:
        und, spec, strike, kind = opts[0]
        und = INDEX_ALIASES.get(und, und)
        return Spec(kind, und, exch if exch not in ("NSE", "BSE") else "", spec, strike,
                    alts=[(INDEX_ALIASES.get(u, u), sp, k, kd) for u, sp, k, kd in opts[1:]])
    m = _FUT.match(body)
    if m and m[1] not in ("", "NIFTY 50"):
        und = INDEX_ALIASES.get(m[1], m[1])
        spec = _expiry_spec(m[2] or "")
        return Spec("FUT", und, exch if exch not in ("", "NSE", "BSE") else ("MCX" if und in MCX_NAMES else ""),
                    None if spec is False else spec)
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9&\-_.]*", body):
        raise ValueError(f"'{text}' is not a symbol ChartLab knows how to send to a broker")
    if exch in ("MCX",) or body in MCX_NAMES:
        return Spec("FUT", body, "MCX", None)
    return Spec("EQ", body, exch or "NSE")


def pick_expiry(exps, spec, today: dt.date | None = None):
    today = today or dt.datetime.now(IST).date()
    exps = sorted({e for e in exps if e is not None})
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
    return max(e for e in hit if (e.year, e.month) == (first.year, first.month))        # the monthly contract


def lookup(m: pd.DataFrame, text: str, broker: str, index_names: dict | None = None) -> Instrument:
    """Find `text` in a normalised instrument list. Raises ValueError with a helpful message."""
    sp = parse_symbol(text)
    if sp.kind == "IDX":
        want = (index_names or {}).get(sp.name, sp.name)
        cand = m[(m["kind"] == "IDX") & ((m["tsym"].str.upper() == str(want).upper()) | (m["name"] == str(want).upper()))]
        if sp.exchange:
            c2 = cand[cand["exchange"] == sp.exchange]
            cand = c2 if len(c2) else cand
        if cand.empty:
            raise ValueError(f"{broker} has no index called {sp.name}")
        return row_instrument(cand.iloc[0], broker)
    if sp.kind == "EQ":
        q = m[(m["kind"] == "EQ") & (m["exchange"] == (sp.exchange or "NSE"))]
        hit = q[q["name"] == sp.name]
        if hit.empty:
            hit = q[q["tsym"].str.upper() == sp.name]
        if hit.empty:
            raise ValueError(f"'{text}' is not on {broker} {sp.exchange or 'NSE'}. Try NSE:SYMBOL or BSE:SYMBOL.")
        return row_instrument(hit.iloc[0], broker)
    if sp.kind == "FUT":
        q = m[(m["kind"] == "FUT") & (m["name"] == sp.name)]
        if sp.exchange:
            q2 = q[q["exchange"] == sp.exchange]
            q = q2 if len(q2) else q
        if q.empty:
            raise ValueError(f"{broker} has no futures on {sp.name}")
        exch = sorted(set(q["exchange"]), key=lambda e: ("NFO", "MCX", "BFO", "CDS", "NCO").index(e)
                      if e in ("NFO", "MCX", "BFO", "CDS", "NCO") else 9)[0]
        q = q[q["exchange"] == exch]
        exp = pick_expiry(q["expiry"], sp.expiry)
        if exp is None and sp.expiry and sp.expiry[0] == "d" and sp.expiry[1] is None:
            exp = pick_expiry(q["expiry"], ("m", 2000 + sp.expiry[3], sp.expiry[2]))   # NIFTY26NOVFUT: 26 = year
        if exp is None:
            nxt = ", ".join(f"{e:%d %b %y}" for e in sorted({e for e in q["expiry"] if e})[:4])
            raise ValueError(f"no {sp.name} futures expiry matches. Listed: {nxt}")
        return row_instrument(q[q["expiry"] == exp].iloc[0], broker)
    # options
    why = ""
    for und, spec, strike, kind in [(sp.name, sp.expiry, sp.strike, sp.kind)] + list(sp.alts):
        q = m[(m["kind"] == kind) & (m["name"] == und)]
        if sp.exchange:
            q2 = q[q["exchange"] == sp.exchange]
            q = q2 if len(q2) else q
        if q.empty:
            why = why or f"{broker} has no {und} options"
            continue
        exch = sorted(set(q["exchange"]), key=lambda e: ("NFO", "BFO", "MCX", "CDS", "NCO").index(e)
                      if e in ("NFO", "BFO", "MCX", "CDS", "NCO") else 9)[0]
        q = q[q["exchange"] == exch]
        exp = pick_expiry(q["expiry"], spec)
        if exp is None:
            nxt = ", ".join(f"{e:%d %b}" for e in sorted({e for e in q["expiry"] if e and e >= dt.date.today()})[:4])
            why = f"no {und} expiry matches that date. Upcoming: {nxt}"
            continue
        r = q[(q["expiry"] == exp) & ((q["strike"] - strike).abs() < 1e-6)]
        if r.empty:
            ks = sorted(set(q[q["expiry"] == exp]["strike"]))
            near = sorted(ks, key=lambda k: abs(k - strike))[:4]
            why = f"no {und} {strike:g} {kind} for {exp:%d %b %Y}. Nearest strikes: " + \
                  ", ".join(f"{k:g}" for k in sorted(near))
            continue
        return row_instrument(r.iloc[0], broker)
    raise ValueError((why or f"'{text}' was not found on {broker}")[0].upper() + (why or f"'{text}' was not found on {broker}")[1:])


def row_instrument(r, broker: str) -> Instrument:
    extra = {k: r[k] for k in r.index if k not in MASTER_COLS and pd.notna(r[k])} if hasattr(r, "index") else {}
    exp = r["expiry"]
    return Instrument(broker=broker, token=str(r["token"]), exchange=str(r["exchange"]), tsym=str(r["tsym"]),
                      name=str(r["name"]), kind=str(r["kind"]), expiry=exp if isinstance(exp, dt.date) else None,
                      strike=float(r["strike"] or 0), lot=int(r["lot"] or 1), tick=float(r["tick"] or 0.05),
                      seg=str(r["seg"]), extra={k: (v.item() if hasattr(v, "item") else v) for k, v in extra.items()})


def search(m: pd.DataFrame, text: str, limit: int = 30) -> list[dict]:
    """Instruments whose name or trading symbol starts with / contains the text (for the order ticket's search)."""
    t = str(text or "").strip().upper()
    if not t:
        return []
    a = m[m["name"].str.startswith(t) | m["tsym"].str.upper().str.startswith(t)]
    if len(a) < limit:
        b = m[m["tsym"].str.upper().str.contains(re.escape(t), regex=True)]
        a = pd.concat([a, b]).drop_duplicates("token")
    rank = {"EQ": 0, "IDX": 1, "FUT": 2, "CE": 3, "PE": 3}
    a = a.assign(_r=a["kind"].map(rank).fillna(9), _l=a["tsym"].str.len()).sort_values(["_r", "_l", "tsym"])
    return [{"tsym": r.tsym, "name": r.name, "kind": r.kind, "exchange": r.exchange,
             "expiry": r.expiry.isoformat() if isinstance(r.expiry, dt.date) else "", "strike": r.strike}
            for r in a.head(limit).itertuples()]


# ---------------------------------------------------------------------------------------------------- candles
INTERVAL_MIN = {"1m": 1, "2m": 2, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "45m": 45, "1h": 60,
                "2h": 120, "3h": 180, "4h": 240}
PERIOD_DAYS = {"1d": 2, "5d": 7, "1mo": 31, "3mo": 93, "6mo": 186, "ytd": 366, "1y": 366, "2y": 731, "5y": 1827,
               "10y": 3653, "max": 3653}


def span(period: str, intraday: bool, max_intraday_days: int = 365) -> tuple[dt.date, dt.date]:
    end = dt.datetime.now(IST).date() + dt.timedelta(days=1)
    days = PERIOD_DAYS.get(period, 731)
    if period == "ytd":
        days = (end - dt.date(end.year, 1, 1)).days + 1
    if intraday:
        days = min(days, max_intraday_days)
    return end - dt.timedelta(days=days), end


def chunks(start: dt.date, end: dt.date, days: int) -> list[tuple[dt.date, dt.date]]:
    out, cur = [], start
    while cur < end:
        nxt = min(cur + dt.timedelta(days=days), end)
        out.append((cur, nxt))
        cur = nxt
    return out


def candles_frame(rows, time_unit: str = "iso") -> pd.DataFrame:
    """[[time, o, h, l, c, v, ...], ...] -> Date (India wall clock, no time zone) / Open / High / Low / Close / Volume."""
    if not rows:
        return pd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    df = pd.DataFrame([list(r[:6]) + [0] * (6 - len(r[:6])) for r in rows],
                      columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    if time_unit == "iso":
        t = pd.to_datetime(df["Date"], utc=True, errors="coerce")
    else:
        t = pd.to_datetime(pd.to_numeric(df["Date"], errors="coerce"), unit=time_unit, utc=True)
    df["Date"] = t.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    for c in ("Open", "High", "Low", "Close", "Volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["Date", "Close"]).sort_values("Date").drop_duplicates("Date", keep="last") \
        .reset_index(drop=True)


def resample(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Build candles a broker does not serve (3m from 1m, 2h from 1h, weekly / monthly from daily)."""
    if df is None or not len(df):
        return df
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    if interval in ("1wk", "1mo"):
        out = df.set_index("Date").resample("W-FRI" if interval == "1wk" else "ME").agg(agg)
    else:
        mins = INTERVAL_MIN[interval]
        out = df.set_index("Date").resample(f"{mins}min", offset=f"{(9 * 60 + 15) % mins}min").agg(agg)
    return out.dropna(subset=["Open"]).reset_index()


# ---------------------------------------------------------------------------------------------------- the adapter base
class Broker:
    """One broker account. Adapters fill in the methods their broker offers and list them in `caps`."""
    id = "base"
    name = "Broker"
    caps: frozenset = frozenset()
    # caps: login_oauth login_totp token_paste renew history quotes stream orders modify cancel positions holdings
    #       funds margin bracket amo iceberg gtt option_chain order_stream
    fields: list = []                     # (key, label, kind: text|secret|url|pin|totp, required, help)
    products = ("INTRADAY", "DELIVERY", "MARGIN")
    order_types = ORDER_TYPES
    exchanges = ("NSE", "BSE", "NFO", "BFO", "MCX")
    docs = ""
    index_names: dict = {}
    data_note = ""                        # e.g. 'live prices need the Kite Connect paid plan'

    def __init__(self, acct: dict, http: Http | None = None, base_url: str | None = None):
        self.acct = dict(acct or {})
        self.http = http or Http(self.name)
        if base_url:
            self.base = base_url.rstrip("/")

    # --- identity / auth
    @property
    def label(self) -> str:
        return self.acct.get("name") or self.name

    @property
    def live(self) -> bool:
        return bool(self.acct.get("live"))

    def has_token(self) -> bool:
        return bool(self.acct.get("access_token"))

    def _done(self, d: dict) -> dict:
        """A login / renewal finished: this adapter uses the new values at once, the caller saves them."""
        self.acct.update({k: v for k, v in d.items() if v not in (None, "")})
        return d

    def login_url(self, redirect: str, state: str = "chartlab") -> str:
        raise BrokerError(f"{self.name}: no browser login - paste an access token instead")

    def complete_login(self, params: dict, redirect: str = "") -> dict:
        """-> fields to save into the account (access_token, ...). params = what the redirect brought back."""
        raise BrokerError(f"{self.name}: no browser login")

    def token_expiry(self) -> float | None:
        return jwt_expiry(self.acct.get("access_token", ""))

    def status(self) -> dict:
        """Never raises: {state: ok|expiring|expired|auth|access|offline|error, msg, client, funds, expires}."""
        st = {"state": "error", "msg": "", "client": "", "funds": None, "expires": None, "open": None, "pnl": None,
              "broker": self.name, "ts": dt.datetime.now().strftime("%H:%M:%S")}
        if not self.has_token():
            st.update(state="auth", msg=f"Not logged in to {self.name}")
            return st
        exp = self.token_expiry()
        st["expires"] = exp
        if exp is not None and exp <= time.time():
            st.update(state="expired", msg=f"{self.name} login has expired - log in again")
            return st
        try:
            prof = self.profile()
            st["client"] = str(prof.get("client") or prof.get("name") or "")
        except BrokerError as e:
            st.update(state={"auth": "auth", "network": "offline", "access": "access"}.get(e.kind, "error"),
                      msg=str(e)[:180])
            return st
        try:
            st["funds"] = self.funds().available
        except Exception:  # noqa: BLE001 - funds are a bonus
            pass
        try:
            pos = self.positions()
            st["open"] = sum(1 for p in pos if p.qty)
            st["pnl"] = sum(p.pnl for p in pos)
        except Exception:  # noqa: BLE001
            pass
        left = (exp - time.time()) / 3600 if exp else None
        if left is not None and left < 4:
            st.update(state="expiring", msg=f"Connected - login expires in {left:.1f} h")
        else:
            st.update(state="ok", msg="Connected")
        return st

    # --- account
    def profile(self) -> dict:
        raise BrokerError(f"{self.name}: profile not available")

    def funds(self) -> Funds:
        raise BrokerError(f"{self.name}: funds not available")

    def positions(self) -> list[Position]:
        raise BrokerError(f"{self.name}: positions not available")

    def holdings(self) -> list[Holding]:
        raise BrokerError(f"{self.name}: holdings not available")

    def orders(self) -> list[Order]:
        raise BrokerError(f"{self.name}: order book not available")

    def trades(self) -> list[dict]:
        return []

    # --- instruments
    def instruments(self) -> pd.DataFrame:
        raise BrokerError(f"{self.name}: instrument list not available")

    def resolve(self, symbol) -> Instrument:
        if isinstance(symbol, Instrument):
            return symbol
        try:
            return lookup(self.instruments(), str(symbol), self.name, self.index_names)
        except ValueError as e:
            raise BrokerError(str(e)) from None

    def search(self, text: str, limit: int = 30) -> list[dict]:
        return search(self.instruments(), text, limit)

    # --- trading
    def prepare(self, req: OrderRequest) -> OrderRequest:
        if req.instrument is None:
            req = OrderRequest(**{**req.__dict__, "instrument": self.resolve(req.symbol)})
        r = req.normalised()
        if r.product not in self.products:
            raise ValueError(f"{self.name} has no {r.product.lower()} product here "
                             f"(choose {', '.join(p.lower() for p in self.products)})")
        if r.order_type not in self.order_types:
            raise ValueError(f"{self.name} does not take {r.order_type} orders")
        if r.instrument.kind == "EQ" and r.product == "MARGIN":
            raise ValueError("MARGIN (carry forward) is for futures and options: use DELIVERY for shares")
        if r.instrument.derivative and r.product == "DELIVERY":
            r.product = "MARGIN"                    # F&O has no delivery product: carry forward it is
        return r

    def place(self, req: OrderRequest) -> str:
        raise BrokerError(f"{self.name}: placing orders is not available")

    def modify(self, order: Order, qty: int | None = None, price: float | None = None, trigger: float | None = None,
               order_type: str | None = None) -> str:
        raise BrokerError(f"{self.name}: changing orders is not available")

    def cancel(self, order: Order) -> str:
        raise BrokerError(f"{self.name}: cancelling orders is not available")

    def exit_position(self, pos: Position) -> str:
        if not pos.qty:
            raise BrokerError("nothing to close")
        req = OrderRequest(symbol=pos.symbol, side="SELL" if pos.qty > 0 else "BUY", qty=abs(int(pos.qty)),
                           order_type="MARKET", product=pos.product or "INTRADAY", tag="chartlab_exit")
        inst = None
        if pos.token:
            try:
                inst = self.instrument_by_token(pos.token)
            except Exception:  # noqa: BLE001
                inst = None
        if inst is not None:
            req.instrument = inst
        return self.place(self.prepare(req))

    def instrument_by_token(self, token: str) -> Instrument | None:
        m = self.instruments()
        hit = m[m["token"] == str(token)]
        return row_instrument(hit.iloc[0], self.name) if len(hit) else None

    # --- market data
    def history(self, symbol, interval: str, period: str) -> pd.DataFrame:
        raise BrokerError(f"{self.name}: candles are not available")

    def ltp(self, instruments: list[Instrument]) -> dict:
        """-> {token: last price}"""
        raise BrokerError(f"{self.name}: prices are not available")

    def margin(self, req: OrderRequest) -> dict | None:
        return None

    # --- helpers
    def _status_of(self, raw: str) -> str:
        s = str(raw or "").strip().lower().replace("_", " ")
        if s in ("complete", "completed", "traded", "filled", "executed", "2"):
            return "complete"
        if s in ("cancelled", "canceled", "cancelled amo", "1"):
            return "cancelled"
        if s in ("rejected", "5", "expired", "7", "failed"):
            return "rejected"
        if "trigger" in s:
            return "trigger_pending"
        if s in ("part traded", "partially filled", "partial", "partially executed"):
            return "partial"
        if s in ("open", "pending", "6", "after market order req received", "amo req received"):
            return "open"
        if s in ("transit", "4", "put order req received", "validation pending", "open pending", "modify pending",
                 "cancel pending", "modify validation pending", "not modified", "not cancelled"):
            return "pending"
        return "open"
