"""Broker accounts: saved credentials, API data fetching (Dhan) and the account manager dialog.

Credentials live in ~/ChartLab/accounts.json (file mode 600, readable only by you). Nothing is sent anywhere
except to the broker's own API host when you load data or test a connection."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pandas as pd
from PySide6 import QtCore, QtGui, QtWidgets

from . import data as datamod
from . import theme

PATH = datamod.USER_DIR / "accounts.json"
from . import brokers as _brokers  # noqa: E402

BROKERS = {k: _brokers.NAMES[k] for k in _brokers.ORDER}
# what a script running on an account gets in its environment (never the PIN or the TOTP key)
FIELDS = {k: [(f[0], f[1]) for f in _brokers.REGISTRY[k].fields if f[2] not in ("pin", "totp")]
          for k in _brokers.ORDER}


# ---------------------------------------------------------------------- store
def load_accounts(path: Path | None = None) -> list[dict]:
    p = path or PATH
    try:
        return json.loads(p.read_text()).get("accounts", [])
    except Exception:  # noqa: BLE001
        return []


def save_accounts(accts: list[dict], path: Path | None = None):
    p = path or PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"accounts": accts}, f, indent=2)
    os.replace(tmp, p)


def get_account(name: str, path: Path | None = None) -> dict:
    for a in load_accounts(path):
        if a["name"] == name:
            return a
    raise KeyError(f"No account named '{name}'. Add it in Accounts -> Manage accounts.")


def account_env(a: dict) -> dict:
    """Environment variables handed to a script that runs on this account."""
    env = {"CHARTLAB_ACCOUNT": a["name"], "CHARTLAB_BROKER": a["broker"],
           "CHARTLAB_LIVE": "1" if a.get("live") else "0"}
    for k, _ in FIELDS.get(a["broker"], []):
        env["CHARTLAB_" + k.upper()] = str(a.get(k, ""))
    return env


# ---------------------------------------------------------------------- Dhan
_EPOCH_1980 = 315532800


from .dhan_core import DHAN, INDEX_IDS, MASTER_URL, _http, _master, place_order, resolve_symbol  # noqa: F401


_DAYS = {"5d": 5, "1mo": 31, "3mo": 93, "6mo": 186, "1y": 366, "2y": 731, "5y": 1827, "10y": 3653, "max": 3653}
_MIN = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}             # the candle sizes Dhan serves directly
_VIA = {"3m": ("1m", 3), "30m": ("15m", 30)}               # built here from a smaller one


def _frame(js: dict) -> pd.DataFrame:
    legacy = not js.get("timestamp") and bool(js.get("start_Time"))     # old API counts seconds from 1980
    ts = pd.Series(js.get("timestamp") or js.get("start_Time") or [], dtype="float64")
    if ts.empty:
        return pd.DataFrame()
    if legacy:
        ts = ts + _EPOCH_1980
    t = pd.to_datetime(ts, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return pd.DataFrame({"Date": t, "Open": js["open"], "High": js["high"], "Low": js["low"],
                         "Close": js["close"], "Volume": js.get("volume", [0] * len(t))})


_CANDLE_DIR = datamod.USER_DIR / "cache" / "dhan_candles"
_MEM: dict = {}                       # (sid, seg, interval, period) -> (time, DataFrame): instant re-open


def _post_candles(path: str, body: dict, hdr: dict) -> pd.DataFrame:
    return _frame(json.loads(_http(f"{DHAN}/charts/{path}", body, hdr)))


def fetch_dhan(acct: dict, symbol: str, interval: str, period: str) -> pd.DataFrame:
    """Candles from Dhan. Fast paths: cached security-id lookup, intraday chunks fetched in parallel, and a
    per-symbol disk cache so re-opening a chart only downloads the bars that are new since last time."""
    import concurrent.futures as cf
    import time as _time
    from .dhan_core import instrument, is_option, resolve_option
    hdr = {"access-token": acct["access_token"], "client-id": acct["client_id"], "Accept": "application/json"}
    s = symbol.strip().upper()
    ex, base = (s.split(":", 1) if ":" in s else ("NSE", s))
    tag = ""
    if is_option(s):                                        # NIFTY 25000 CE, NIFTY261124C25000, ...
        ins = resolve_option(s)
        sid, seg, inst = ins["security_id"], ins["segment"], ins["instrument"]
        tag = f"_{ins['expiry']:%y%m%d}"                    # ids are re-used after expiry: keep caches apart
    elif base in INDEX_IDS:
        sid, seg, inst = INDEX_IDS[base], "IDX_I", "INDEX"
    else:
        ins = instrument(base, ex)
        sid, seg, inst = ins["security_id"], ins["segment"], ins["instrument"]
    if interval in _VIA:                                    # Dhan has no 3m / 30m candles: build them from 1m / 15m
        base_iv, mins = _VIA[interval]
        df = fetch_dhan(acct, symbol, base_iv, period)
        if not len(df):
            return df
        return (df.resample(f"{mins}min", on="Date", offset=f"{(9 * 60 + 15) % mins}min")
                .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
                .dropna(subset=["Open"]).reset_index())
    mkey = (sid, seg, interval, period)
    hit = _MEM.get(mkey)
    if hit and _time.time() - hit[0] < 20:                  # same chart re-opened within seconds
        return hit[1].copy()
    end = dt.date.today() + dt.timedelta(days=1)
    start = end - dt.timedelta(days=_DAYS.get(period, 731))
    intraday = interval in _MIN
    if intraday:
        start = max(start, end - dt.timedelta(days=370))
    cfile = _CANDLE_DIR / f"{seg}_{sid}{tag}_{'m' + str(_MIN[interval]) if intraday else 'd'}.pkl"
    cached = None
    try:
        cached = pd.read_pickle(cfile)
    except Exception:  # noqa: BLE001 - no / unreadable cache: full download
        cached = None
    fetch_from = start
    if cached is not None and len(cached) and cached["Date"].iloc[0].date() <= start + dt.timedelta(days=5):
        fetch_from = max(start, cached["Date"].iloc[-1].date() - dt.timedelta(days=3))   # only the new bars
    if intraday:
        chunks, cur = [], fetch_from
        while cur < end:
            nxt = min(cur + dt.timedelta(days=85), end)     # Dhan allows ~90 days of intraday per request
            chunks.append({"securityId": sid, "exchangeSegment": seg, "instrument": inst,
                           "interval": str(_MIN[interval]), "oi": False,
                           "fromDate": f"{cur} 09:00:00", "toDate": f"{nxt} 15:30:00"})
            cur = nxt
        with cf.ThreadPoolExecutor(max_workers=min(4, len(chunks) or 1)) as pool:    # Dhan data API: 5 req/s
            frames = list(pool.map(lambda b: _post_candles("intraday", b, hdr), chunks))
        new = pd.concat([f for f in frames if len(f)] or [pd.DataFrame()])
    else:
        body = {"securityId": sid, "exchangeSegment": seg, "instrument": inst, "expiryCode": 0, "oi": False,
                "fromDate": str(fetch_from), "toDate": str(end)}
        new = _post_candles("historical", body, hdr)
    parts = [x for x in (cached, new) if x is not None and len(x)]
    df = pd.concat(parts) if parts else pd.DataFrame()
    if df.empty:
        raise RuntimeError(f"Dhan returned no candles for {symbol} ({interval}).")
    df = df.drop_duplicates("Date", keep="last").sort_values("Date").reset_index(drop=True)
    try:
        _CANDLE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cfile.with_suffix(".part")                     # write-then-rename: a quit mid-write never
        df.to_pickle(tmp)                                    # leaves a half-written cache behind
        os.replace(tmp, cfile)
    except Exception:  # noqa: BLE001 - cache is optional
        pass
    if cached is not None:                                    # trim old cached bars to the asked range
        df = df[df["Date"] >= pd.Timestamp(start)].reset_index(drop=True)
    if not intraday and interval in ("1wk", "1mo") and len(df):
        df = df.set_index("Date").resample("W-FRI" if interval == "1wk" else "ME").agg(
            {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna().reset_index()
    _MEM[mkey] = (_time.time(), df)
    return df.copy()


def test_account(acct: dict) -> str:
    """Cheap authenticated call; returns a human message or raises."""
    if acct["broker"] not in ("dhan",):
        prof = _brokers.get(acct).profile()
        return f"Connected to {BROKERS.get(acct['broker'])}. Client {prof.get('client') or prof.get('name') or '?'}"
    if acct["broker"] == "dhan":
        raw = _http(f"{DHAN}/profile", None, {"access-token": acct["access_token"],
                                              "client-id": acct["client_id"]})
        js = json.loads(raw)
        return f"Connected. Client {js.get('dhanClientId', acct['client_id'])}, token valid until " \
               f"{js.get('tokenValidity', '?')}"
    raise RuntimeError("Unsupported broker")


STATUS: dict[str, dict] = {}          # account name -> last status (shared by the dialog, toolbar dot, delegate)


def _parse_validity(s) -> float | None:
    """Dhan tokenValidity like '30/10/2026 15:30' -> days left (float) or None."""
    if not s:
        return None
    for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return (dt.datetime.strptime(str(s)[:19], fmt) - dt.datetime.now()).total_seconds() / 86400
        except ValueError:
            continue
    return None


def _generic_status(acct: dict) -> dict:
    """Status of a Zerodha / Upstox / Angel One / Fyers / paper account, in the shape the dialog shows."""
    st = {"state": "error", "msg": "", "client": acct.get("client_id") or acct.get("client_code") or "",
          "validity": "", "days_left": None, "funds": None, "segments": "", "plan": "", "data": None, "open": None,
          "pnl": None, "ts": dt.datetime.now().strftime("%H:%M:%S")}
    try:
        b = _brokers.get(acct)
        got = b.status()
    except Exception as e:  # noqa: BLE001 - a status check never breaks the app
        st["msg"] = str(e)[:160]
        return st
    st.update({k: got.get(k) for k in ("state", "msg", "funds", "open", "pnl") if k in got})
    st["client"] = got.get("client") or st["client"]
    exp = got.get("expires")
    if exp:
        st["validity"] = dt.datetime.fromtimestamp(exp).strftime("%d/%m/%Y %H:%M")
        st["days_left"] = (exp - dt.datetime.now().timestamp()) / 86400
    st["plan"] = getattr(b, "data_note", "") or ""
    st["broker"] = BROKERS.get(acct.get("broker"), acct.get("broker"))
    return st


def get_status(acct: dict) -> dict:
    """Never raises. {state: ok|expiring|expired|auth|offline|error, msg, client, validity, days_left, funds, segments, plan}"""
    st = {"state": "error", "msg": "", "client": acct.get("client_id", ""), "validity": "", "days_left": None,
          "funds": None, "segments": "", "plan": "", "data": None, "open": None, "pnl": None,
          "ts": dt.datetime.now().strftime("%H:%M:%S")}
    if acct.get("broker") != "dhan":
        return _generic_status(acct)
    hdr = {"access-token": acct.get("access_token", ""), "client-id": acct.get("client_id", "")}
    try:
        js = json.loads(_http(f"{DHAN}/profile", None, hdr, timeout=12))
    except RuntimeError as e:
        m = str(e)
        if "Could not reach" in m:
            st.update(state="offline", msg="No internet / Dhan unreachable")
        elif any(k in m for k in ("401", "403", "DH-901", "DH-902", "Invalid", "invalid", "expired")):
            st.update(state="auth", msg="Token invalid or expired - paste a fresh access token")
        else:
            st.update(state="error", msg=m[:160])
        return st
    st["client"] = js.get("dhanClientId", st["client"])
    st["validity"] = str(js.get("tokenValidity", ""))
    st["days_left"] = _parse_validity(js.get("tokenValidity"))
    st["segments"] = str(js.get("activeSegment", "")).replace(",", ", ")
    st["plan"] = ("Data API active" if str(js.get("dataPlan", "")).lower() == "active" else
                  f"Data plan: {js.get('dataPlan', '-')}")
    st["data"] = str(js.get("dataPlan", "")).lower() == "active"
    try:
        fl = json.loads(_http(f"{DHAN}/fundlimit", None, hdr, timeout=12))
        st["funds"] = float(fl.get("availabelBalance", fl.get("availableBalance", 0)) or 0)
    except Exception:  # noqa: BLE001 - funds are a bonus, never fail the check for them
        pass
    try:
        pos = json.loads(_http(f"{DHAN}/positions", None, hdr, timeout=12))
        if isinstance(pos, list):
            st["open"] = sum(1 for x in pos if str(x.get("positionType", "")).upper() != "CLOSED"
                             and float(x.get("netQty") or 0) != 0)
            st["pnl"] = sum(float(x.get("realizedProfit") or 0) + float(x.get("unrealizedProfit") or 0) for x in pos)
    except Exception:  # noqa: BLE001 - positions are a bonus too
        pass
    d = st["days_left"]
    if d is not None and d <= 0:
        st.update(state="expired", msg="Token has expired - paste a fresh access token")
    elif d is not None and d * 24 < EXPIRING_HOURS:      # Dhan tokens live 24 hours: warn in the last few
        st.update(state="expiring", msg=f"Connected - token expires in {max(d, 0) * 24:.1f} h")
    else:
        st.update(state="ok", msg="Connected" + (f" - token good for {d * 24:.0f} h" if d is not None and d < 2 else ""))
    return st


EXPIRING_HOURS = 4.0


def _find_token(obj):
    """The new access token inside Dhan's answer, whatever the field is called."""
    import re
    pat = re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")
    if isinstance(obj, dict):
        for k in ("accessToken", "access_token", "token", "jwtToken"):
            v = obj.get(k)
            if isinstance(v, str) and pat.match(v.strip()):
                return v.strip()
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            t = _find_token(v) if isinstance(v, (dict, list)) else (v.strip() if isinstance(v, str) and pat.match(v.strip()) else None)
            if t:
                return t
    return None


def renew_token(acct: dict) -> str:
    """Ask Dhan for a fresh 24-hour token in exchange for the current one (which must still be valid and must have
    been generated on Dhan Web). Returns the new token. Dhan switches the old one off when it answers."""
    if acct.get("broker") != "dhan":
        raise RuntimeError("Only Dhan tokens can be renewed")
    raw = _http(f"{DHAN}/RenewToken", None, {"access-token": acct.get("access_token", ""),
                                             "dhanClientId": str(acct.get("client_id", ""))}, timeout=15)
    try:
        js = json.loads(raw)
    except ValueError:
        js = raw.decode("utf8", "replace")
    tok = _find_token(js if not isinstance(js, str) else [js.strip()])
    if not tok:
        raise RuntimeError("Dhan answered, but without a new token: " + str(js)[:160])
    return tok


def renew_saved(name: str, path: Path | None = None) -> str:
    """Renew one saved account's token and write it to accounts.json straight away. -> message for the user."""
    accts = load_accounts(path)
    a = next((x for x in accts if x["name"] == name), None)
    if a is None:
        raise KeyError(name)
    tok = renew_token(a)
    accts = load_accounts(path)                               # re-read: do not undo an edit made meanwhile
    for x in accts:
        if x["name"] == name:
            x["access_token"] = tok
    save_accounts(accts, path)
    return f"{name}: token renewed for another 24 hours"


def parse_bulk(text: str) -> tuple[list[dict], list[str]]:
    """Many accounts pasted at once, one per line: 'name, client id, token' (commas, tabs or spaces; 'client id, token'
    alone is fine - the client id becomes the name). -> (accounts, lines that could not be read)."""
    import re
    out, bad, seen = [], [], set()
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip().strip('"').strip("'") for x in re.split(r"[,;\t|]+|\s{1,}", line) if x.strip()]
        if parts and parts[0].lower() in ("name", "account", "client", "clientid", "client_id") and len(out) == 0 and \
                not any(len(x) > 60 for x in parts):
            continue                                          # a header row
        tok = next((x for x in parts if len(x) > 60), None)
        if tok is None:
            bad.append(raw.strip()[:60])
            continue
        rest = [x for x in parts if x != tok]
        cid = next((x for x in rest if x.isdigit() and 6 <= len(x) <= 14), None)
        if cid is None:
            bad.append(raw.strip()[:60])
            continue
        words = [x for x in rest if x != cid and x.lower() not in ("live", "paper", "dhan")]
        name = " ".join(words) or cid
        k = 2
        base = name
        while name in seen:
            name = f"{base} {k}"
            k += 1
        seen.add(name)
        out.append({"name": name, "broker": "dhan", "client_id": cid, "access_token": tok, "live": False,
                    "qty_mult": 1.0})
    return out, bad


STATE_COLOR = {"ok": "#089981", "expiring": "#ff9800", "expired": "#f23645", "auth": "#f23645",
               "offline": "#787b86", "error": "#f23645", None: "#4a4e59"}


class StatusPoller(QtCore.QObject):
    """Checks accounts in background threads; emits updated(name) on the GUI thread. Never blocks the UI."""
    updated = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy: set[str] = set()

    def refresh(self, accts: list[dict] | None = None, force: bool = True):
        import threading
        for a in (accts if accts is not None else load_accounts()):
            n = a["name"]
            if n in self._busy:
                continue
            if not force and n in STATUS and (dt.datetime.now() - STATUS[n].get("_at", dt.datetime.min)).total_seconds() < 60:
                continue
            self._busy.add(n)
            threading.Thread(target=self._run, args=(dict(a),), daemon=True).start()

    def _run(self, a: dict):
        st = get_status(a)
        st["_at"] = dt.datetime.now()
        STATUS[a["name"]] = st
        self._busy.discard(a["name"])
        try:
            self.updated.emit(a["name"])
        except RuntimeError:                  # object deleted while quitting
            pass


def overall_state(accts: list[dict] | None = None) -> tuple[str | None, str]:
    """-> (state for the toolbar dot, tooltip text)."""
    accts = accts if accts is not None else load_accounts()
    if not accts:
        return None, "No accounts yet - click to connect a broker"
    lines, worst = [], "ok"
    rank = {"ok": 0, "offline": 1, "expiring": 2, "expired": 3, "auth": 3, "error": 3}
    for a in accts:
        st = STATUS.get(a["name"])
        s = st["state"] if st else None
        lines.append(f"{a['name']}: " + (st["msg"] if st else "checking..."))
        if s and rank.get(s, 0) > rank.get(worst, 0):
            worst = s
    if all(STATUS.get(a["name"]) is None for a in accts):
        worst = None
    return worst, "\n".join(lines)


def load_api(account: str, symbol: str, interval: str, period: str):
    """Entry point used by the chart panel: -> (DataFrame cleaned like other sources, label)."""
    a = get_account(account)
    if a["broker"] == "paper":
        raise RuntimeError("A paper account has no market data of its own: choose a broker account or Auto")
    if a["broker"] != "dhan":
        bk = _brokers.get(a)
        if not bk.has_token():
            raise RuntimeError(f"{BROKERS.get(a['broker'])} account '{a['name']}' is not logged in: Accounts -> Log in")
        inst = bk.resolve(symbol)
        raw = bk.history(inst, interval, period)
        if raw is None or not len(raw):
            raise RuntimeError(f"{bk.name} returned no candles for {symbol} ({interval})")
        return datamod.standardise(raw), f"{inst.display} \u00b7 {interval} \u00b7 {a['name']}"
    from .dhan_core import is_option, resolve_option
    raw = fetch_dhan(a, symbol, interval, period)
    name = symbol.strip().upper()
    if is_option(name):
        name = resolve_option(name)["symbol"]               # one word, TradingView style: NIFTY261124C25000
    return datamod.standardise(raw), f"{name} · {interval} · {a['name']}"


# ---------------------------------------------------------------------- which account gives the candles
_DATA_FAIL: dict = {}             # account name -> when a data request on it last failed for the account's own reasons
DATA_COOLDOWN = 180.0             # ... and for how long charts then go to another account / Yahoo first


def _account_trouble(e: Exception) -> bool:
    """True when the failure is the account's (login gone, no data plan, too many requests, no network), not the
    symbol's - a symbol this broker does not have says nothing about the next symbol."""
    m = str(e)
    return any(k in m for k in ("401", "403", "429", "DH-90", "nvalid", "xpired", "not logged in", "Could not reach",
                                "timed out", "Rate", "rate limit"))


def data_accounts(path: Path | None = None, include_failed: bool = True) -> list[dict]:
    """The accounts a chart can get candles from, the best first: logged in and not paper; one whose login is known
    to be gone is left out; one that just failed goes to the back (or is left out: include_failed=False)."""
    import time as _t
    now = _t.time()
    out = []
    for a in load_accounts(path):
        b = a.get("broker")
        if b == "paper":
            continue
        try:
            ok = bool(a.get("access_token")) if b == "dhan" else bool(_brokers.get(a).has_token())
        except Exception:  # noqa: BLE001
            ok = False
        if not ok or (STATUS.get(a["name"]) or {}).get("state") in ("expired", "auth"):
            continue
        failed = now - _DATA_FAIL.get(a["name"], 0.0) < DATA_COOLDOWN
        if failed and not include_failed:
            continue
        good = (STATUS.get(a["name"]) or {}).get("state") in ("ok", "expiring")
        out.append(((1 if failed else 0, 0 if good else 1), a))
    out.sort(key=lambda x: x[0])                             # (a stable sort: your own order decides between equals)
    return [a for _k, a in out]


def data_loaders(path: Path | None = None, include_failed: bool = True) -> list:
    """[(label, fn(symbol, interval, range) -> (DataFrame, label)), ...] for the Auto source: every usable account in
    turn, whatever its broker. The next one is asked when one fails."""
    import time as _t
    out = []
    for a in data_accounts(path, include_failed):
        def fn(s, i, r, n=a["name"]):
            try:
                got = load_api(n, s, i, r)
            except Exception as e:  # noqa: BLE001
                if _account_trouble(e):
                    _DATA_FAIL[n] = _t.time()
                raise
            _DATA_FAIL.pop(n, None)
            return got
        out.append((f"API {a['name']}", fn))
    return out


# ---------------------------------------------------------------------- UI
class _AccountCard(QtWidgets.QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QtCore.QSize(option.rect.width(), 62)

    def paint(self, p, option, index):
        a = index.data(QtCore.Qt.ItemDataRole.UserRole)
        p.save()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(4, 3, -4, -3)
        sel = bool(option.state & QtWidgets.QStyle.StateFlag.State_Selected)
        hov = bool(option.state & QtWidgets.QStyle.StateFlag.State_MouseOver)
        p.setPen(QtGui.QPen(QtGui.QColor("#868993" if sel else "#2a2a2a"), 1.3 if sel else 1))
        p.setBrush(QtGui.QColor("#262626" if sel else ("#202020" if hov else "#1a1a1a")))
        p.drawRoundedRect(r, 10, 10)
        av = QtCore.QRect(r.left() + 12, r.center().y() - 16, 32, 32)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(theme.ACCENT))
        p.drawEllipse(av)
        f = QtGui.QFont(option.font)
        f.setBold(True)
        f.setPointSize(13)
        p.setFont(f)
        p.setPen(QtGui.QColor("white"))
        p.drawText(av, QtCore.Qt.AlignmentFlag.AlignCenter, (a["name"][:1] or "?").upper())
        f.setPointSize(12)
        p.setFont(f)
        tx = r.adjusted(54, 9, -74, 0)
        p.drawText(tx, QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft, a["name"])
        f.setBold(False)
        f.setPointSize(10)
        p.setFont(f)
        p.setPen(QtGui.QColor(theme.MUTED))
        cid = str(a.get("client_id") or a.get("client_code") or a.get("user_id") or a.get("app_id") or "")
        p.drawText(r.adjusted(54, 0, -74, -9), QtCore.Qt.AlignmentFlag.AlignBottom | QtCore.Qt.AlignmentFlag.AlignLeft,
                   f"{BROKERS.get(a['broker'], a['broker'])}  ·  {cid[:4]}…{cid[-3:]}" if len(cid) > 8 else
                   f"{BROKERS.get(a['broker'], a['broker'])}" + (f"  ·  {cid}" if cid else ""))
        st = STATUS.get(a["name"])
        dc = QtGui.QColor(STATE_COLOR.get(st["state"] if st else None))
        p.setPen(QtGui.QPen(QtGui.QColor("#1a1a1a"), 2))
        p.setBrush(dc)
        p.drawEllipse(QtCore.QPoint(av.right() - 2, av.bottom() - 2), 6, 6)
        live = bool(a.get("live"))
        col = QtGui.QColor(theme.DOWN if live else "#787b86")
        badge = QtCore.QRect(r.right() - 64, r.center().y() - 10, 54, 20)
        fill = QtGui.QColor(col)
        fill.setAlpha(50)
        p.setBrush(fill)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.drawRoundedRect(badge, 10, 10)
        f.setBold(True)
        f.setPointSize(9)
        p.setFont(f)
        p.setPen(col)
        p.drawText(badge, QtCore.Qt.AlignmentFlag.AlignCenter, "LIVE" if live else "PAPER")
        p.restore()


class AccountsDialog(QtWidgets.QDialog):
    """Every broker account in one place. The form follows the broker: its own fields, its own way of logging in
    (browser login, PIN + TOTP, or a pasted token) and its own renewal."""

    def __init__(self, parent=None, path: Path | None = None):
        super().__init__(parent)
        self.path = path
        self.setWindowTitle("Broker accounts")
        self.resize(900, 640)
        self.setStyleSheet(f"QDialog {{ background: #1e1e1e; }} QLabel#h {{ font-size: 15px; font-weight: 700; color: white; }} "
                           f"QLabel#sub {{ color: {theme.MUTED}; font-size: 12px; }} QLabel#lbl {{ color: {theme.MUTED}; font-size: 11px; }} "
                           f"QLineEdit, QDoubleSpinBox, QComboBox {{ background: #121212; border: 1px solid #3a3a3a; "
                           f"border-radius: 8px; padding: 7px 10px; min-height: 18px; }} "
                           f"QLineEdit:focus, QDoubleSpinBox:focus {{ border-color: {theme.ACCENT}; }} "
                           f"QPushButton#danger {{ background: transparent; color: {theme.DOWN}; }} "
                           f"QPushButton#danger:hover {{ background: rgba(242,54,69,40); }} "
                           f"QPushButton#login {{ background: #2a2e39; border: 1px solid #434651; border-radius: 6px; "
                           f"padding: 6px 12px; color: white; font-weight: 600; }} QPushButton#login:hover {{ border-color: {theme.ACCENT}; }}")
        self.accts = load_accounts(path)
        self.editing = -1
        self.inputs: dict = {}
        self._catcher = None
        self._login_job = None
        self.poller = StatusPoller(self)
        self.poller.updated.connect(self._status_updated)
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        left = QtWidgets.QFrame()
        left.setFixedWidth(300)
        left.setObjectName("leftpane")
        left.setStyleSheet(f"QFrame#leftpane {{ background: #161616; border-right: 1px solid #2a2a2a; }} QLabel {{ border: 0; background: transparent; }} "
                           "QListWidget { border: 0; background: transparent; }")
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(14, 16, 14, 14)
        head = QtWidgets.QHBoxLayout()
        t = QtWidgets.QLabel("Accounts")
        t.setObjectName("h")
        head.addWidget(t)
        head.addStretch(1)
        add = QtWidgets.QPushButton("+  Add")
        add.setObjectName("primary")
        add.clicked.connect(self._new)
        head.addWidget(add)
        lv.addLayout(head)
        self.list = QtWidgets.QListWidget()
        self.list.setItemDelegate(_AccountCard(self.list))
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.currentRowChanged.connect(self._select)
        lv.addWidget(self.list, 1)
        self.empty = QtWidgets.QLabel("No accounts yet.\nClick  + Add  to connect your\nfirst broker account.")
        self.empty.setObjectName("sub")
        self.empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        lv.addWidget(self.empty, 1)
        many = QtWidgets.QHBoxLayout()
        bm = QtWidgets.QPushButton("Add many…")
        bm.setToolTip("Paste a list of Dhan accounts (name, client id, token) and connect them all in one go")
        bm.clicked.connect(self.add_many)
        bo = QtWidgets.QPushButton("All accounts")
        bo.setToolTip("Every account in one table: connection, token time left, balance, positions, today's P&L")
        bo.clicked.connect(self.overview)
        many.addWidget(bm)
        many.addWidget(bo)
        lv.addLayout(many)
        root.addWidget(left)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        right = QtWidgets.QWidget()
        scroll.setWidget(right)
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(28, 20, 28, 18)
        rv.setSpacing(6)
        self.title = QtWidgets.QLabel("New account")
        self.title.setObjectName("h")
        rv.addWidget(self.title)
        sub = QtWidgets.QLabel("Live prices, candles, the Trading Panel and scripts use these accounts. Saved only on "
                               "this Mac (~/ChartLab/accounts.json, private to your user); sent only to the broker itself.")
        sub.setObjectName("sub")
        sub.setWordWrap(True)
        rv.addWidget(sub)
        rv.addSpacing(4)
        self.card = QtWidgets.QLabel("")
        self.card.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.card.setWordWrap(True)
        self.card.setStyleSheet("background:#121212;border:1px solid #2a2a2a;border-radius:10px;padding:10px 12px;")
        rv.addWidget(self.card)
        rv.addSpacing(6)

        def lab(text):
            x = QtWidgets.QLabel(text.upper())
            x.setObjectName("lbl")
            return x
        self.name = QtWidgets.QLineEdit()
        self.name.setPlaceholderText("e.g. Main, Papa, Fund 2")
        rv.addWidget(lab("Account name"))
        rv.addWidget(self.name)
        rv.addSpacing(4)
        self.broker = QtWidgets.QComboBox()
        for k, tt in BROKERS.items():
            self.broker.addItem(tt, k)
        self.broker.currentIndexChanged.connect(lambda _i: self._build_fields())
        rv.addWidget(lab("Broker"))
        rv.addWidget(self.broker)
        rv.addSpacing(4)
        self.fields_box = QtWidgets.QWidget()
        self.fields_lay = QtWidgets.QVBoxLayout(self.fields_box)
        self.fields_lay.setContentsMargins(0, 0, 0, 0)
        self.fields_lay.setSpacing(6)
        rv.addWidget(self.fields_box)
        self.more = QtWidgets.QToolButton()
        self.more.setText("More ways to log in ▸")
        self.more.setCheckable(True)
        self.more.setStyleSheet(f"QToolButton {{ border: 0; color: {theme.ACCENT}; padding: 2px 0; }}")
        self.more.toggled.connect(self._more_toggled)
        rv.addWidget(self.more)
        self.more_box = QtWidgets.QWidget()
        self.more_lay = QtWidgets.QVBoxLayout(self.more_box)
        self.more_lay.setContentsMargins(0, 0, 0, 0)
        self.more_lay.setSpacing(6)
        rv.addWidget(self.more_box)
        self.show_tok = QtWidgets.QCheckBox("Show secrets")
        self.show_tok.toggled.connect(self._echo)
        rv.addWidget(self.show_tok)
        self.redirect_row = QtWidgets.QWidget()
        rr = QtWidgets.QHBoxLayout(self.redirect_row)
        rr.setContentsMargins(0, 2, 0, 2)
        self.redirect_lbl = QtWidgets.QLabel("")
        self.redirect_lbl.setObjectName("sub")
        self.redirect_lbl.setWordWrap(True)
        self.redirect_lbl.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        rr.addWidget(self.redirect_lbl, 1)
        cp = QtWidgets.QPushButton("Copy")
        cp.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(self._redirect()))
        rr.addWidget(cp)
        rv.addWidget(self.redirect_row)
        lrow = QtWidgets.QHBoxLayout()
        self.login_btn = QtWidgets.QPushButton("Log in")
        self.login_btn.setObjectName("login")
        self.login_btn.clicked.connect(self.login_browser)
        lrow.addWidget(self.login_btn)
        self.totp_btn = QtWidgets.QPushButton("Log in with PIN + TOTP")
        self.totp_btn.setObjectName("login")
        self.totp_btn.clicked.connect(self.login_totp)
        lrow.addWidget(self.totp_btn)
        lrow.addStretch(1)
        rv.addLayout(lrow)
        self.paste_row = QtWidgets.QWidget()
        pr = QtWidgets.QHBoxLayout(self.paste_row)
        pr.setContentsMargins(0, 0, 0, 0)
        self.paste = QtWidgets.QLineEdit()
        self.paste.setPlaceholderText("…or paste the address the browser landed on after logging in")
        pr.addWidget(self.paste, 1)
        pb = QtWidgets.QPushButton("Use it")
        pb.clicked.connect(self._use_pasted)
        pr.addWidget(pb)
        self.paste_row.hide()
        rv.addWidget(self.paste_row)
        rv.addSpacing(4)
        self.mult = QtWidgets.QDoubleSpinBox()
        self.mult.setRange(0.1, 1000)
        self.mult.setDecimals(2)
        self.mult.setValue(1.0)
        self.mult.setToolTip("2 = double the quantity/lots a script or a broadcast order sends on this account")
        rv.addWidget(lab("Quantity multiplier"))
        rv.addWidget(self.mult)
        rv.addSpacing(4)
        self.live = QtWidgets.QCheckBox("  Live orders  -  REAL money")
        self.live.setStyleSheet(f"QCheckBox {{ color: {theme.DOWN}; font-weight: 600; }}")
        self.live.setToolTip("Off = paper: orders fill on ChartLab's paper broker, nothing reaches the broker")
        rv.addWidget(self.live)
        note = QtWidgets.QLabel("Leave off to test safely: while it is off, orders on this account are paper orders.")
        note.setObjectName("sub")
        note.setWordWrap(True)
        rv.addWidget(note)
        rv.addStretch(1)
        self.msg = QtWidgets.QLabel("")
        self.msg.setWordWrap(True)
        self.msg.setTextFormat(QtCore.Qt.TextFormat.RichText)
        rv.addWidget(self.msg)
        btns = QtWidgets.QHBoxLayout()
        self.del_btn = d = QtWidgets.QPushButton("Delete")
        d.setObjectName("danger")
        d.clicked.connect(self.delete)
        btns.addWidget(d)
        btns.addStretch(1)
        self.rbtn = rb = QtWidgets.QPushButton("Renew token")
        rb.setToolTip("Swap the saved login for a fresh one while it is still valid (Dhan: 24 hours more; Angel One / "
                      "Fyers: with the saved refresh token)")
        rb.clicked.connect(self.renew)
        btns.addWidget(rb)
        self.tbtn = tb = QtWidgets.QPushButton("Test connection")
        tb.clicked.connect(self.test)
        btns.addWidget(tb)
        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self.test)
        sb = QtWidgets.QPushButton("Save")
        sb.setObjectName("primary")
        sb.setDefault(True)
        sb.clicked.connect(self.save)
        btns.addWidget(sb)
        rv.addLayout(btns)
        root.addWidget(scroll, 1)
        self._poll = QtCore.QTimer(self)
        self._poll.setInterval(300)
        self._poll.timeout.connect(self._check_catch)
        self._build_fields()
        self._fill()
        self.poller.refresh(self.accts, force=False)

    # ------------------------------------------------------------------ the broker's own form
    @property
    def client(self):
        return self.inputs.get("client_id") or self.inputs.get("client_code") or self._spare("client")

    @property
    def token(self):
        return self.inputs.get("access_token") or self._spare("token")

    def _spare(self, key):
        w = getattr(self, "_spares", {}).get(key)
        if w is None:
            self._spares = getattr(self, "_spares", {})
            w = self._spares[key] = QtWidgets.QLineEdit()
        return w

    def _cls(self):
        return _brokers.cls_of(self.broker.currentData() or "dhan")

    def _build_fields(self, values: dict | None = None):
        keep = {k: w.text() for k, w in self.inputs.items()}
        if values is not None:
            keep = {k: str(v) for k, v in values.items() if v is not None}
        for lay in (self.fields_lay, self.more_lay):
            while lay.count():
                it = lay.takeAt(0)
                if it.widget():
                    it.widget().deleteLater()
        self.inputs = {}
        c = self._cls()
        extra = c.id == "dhan"                                 # Dhan: the token is enough, the rest is optional
        for key, label, kind, req, helptext in c.fields:
            w = QtWidgets.QLineEdit()
            w.setPlaceholderText(helptext or label)
            w.setToolTip(helptext)
            if kind in ("secret", "pin", "totp"):
                w.setEchoMode(QtWidgets.QLineEdit.EchoMode.Normal if self.show_tok.isChecked()
                              else QtWidgets.QLineEdit.EchoMode.Password)
            if key == "redirect_uri" and not keep.get(key):
                from .brokers.oauth import DEFAULT_REDIRECT
                w.setText(DEFAULT_REDIRECT)
            if keep.get(key):
                w.setText(keep[key])
                w.setCursorPosition(0)
            if key == "access_token":
                w.textChanged.connect(lambda *_: self._token_edited())
            x = QtWidgets.QLabel((label + ("" if req else "  (optional)")).upper())
            x.setObjectName("lbl")
            lay = self.more_lay if (extra and key in ("app_id", "app_secret", "pin", "totp_secret")) else self.fields_lay
            lay.addWidget(x)
            lay.addWidget(w)
            self.inputs[key] = w
        self.more.setVisible(extra)
        self.more_box.setVisible(extra and self.more.isChecked())
        caps = c.caps
        self.login_btn.setVisible("login_oauth" in caps)
        self.login_btn.setText(f"Log in with {c.name}")
        self.totp_btn.setVisible("login_totp" in caps and (c.id != "dhan" or self.more.isChecked()))
        self.redirect_row.setVisible("login_oauth" in caps)
        self.redirect_lbl.setText(f"In your {c.name} API app, set the redirect URL to <b>{self._redirect()}</b>")
        self.redirect_lbl.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.rbtn.setVisible("renew" in caps and self.editing >= 0)
        self.tbtn.setVisible(c.id != "paper")
        if c.id == "paper":
            self.live.setChecked(False)
        self.live.setVisible(c.id != "paper")
        if self.editing < 0:
            hint = {"paper": "A practice account: set the starting cash and trade on the prices on your charts.",
                    "dhan": "Fill in the Client ID and paste the access token (or use More ways to log in).",
                    "angel": "Fill in the API key and client code, then Log in with PIN + TOTP.",
                    }.get(c.id, f"Fill in the {c.name} app details, then Log in with {c.name}.")
            self.card.setText(f"<span style='color:#787b86'>{hint}</span>")

    def _more_toggled(self, on):
        self.more.setText("More ways to log in ▾" if on else "More ways to log in ▸")
        self.more_box.setVisible(on)
        self.totp_btn.setVisible("login_totp" in self._cls().caps and (self._cls().id != "dhan" or on))
        self.login_btn.setVisible("login_oauth" in self._cls().caps and (self._cls().id != "dhan" or on))

    def _echo(self, on):
        c = self._cls()
        kinds = {f[0]: f[2] for f in c.fields}
        for k, w in self.inputs.items():
            if kinds.get(k) in ("secret", "pin", "totp"):
                w.setEchoMode(QtWidgets.QLineEdit.EchoMode.Normal if on else QtWidgets.QLineEdit.EchoMode.Password)

    def _redirect(self) -> str:
        from .brokers.oauth import DEFAULT_REDIRECT
        w = self.inputs.get("redirect_uri")
        return (w.text().strip() if w is not None else "") or DEFAULT_REDIRECT

    def _token_edited(self):
        t = self.token.text().strip()
        if len(t) > 40 and (self.broker.currentData() != "dhan" or self.client.text().strip()):
            self._debounce.start(700)

    # ------------------------------------------------------------------ status
    def _status_updated(self, name: str):
        self.list.viewport().update()
        if (0 <= self.editing < len(self.accts) and self.accts[self.editing]["name"] == name) or (self.editing < 0 and name in ("_new", self.name.text().strip())):
            self._show_status(STATUS.get(name))
        self.tbtn.setEnabled(True)
        self.tbtn.setText("Test connection")

    def _show_status(self, st):
        if not st:
            self.card.setText("<span style='color:#787b86'>Not checked yet</span>")
            return
        c = STATE_COLOR.get(st["state"])
        rows = [f"<b style='color:{c}'>● {st['msg'] or st['state']}</b>"]
        if st.get("client"):
            rows.append(f"Client <b>{st['client']}</b>")
        if st.get("validity"):
            d = st.get("days_left")
            left = "" if d is None or d <= 0 else (f"  ({d * 24:.0f} hours left)" if d < 2 else f"  ({d:.1f} days left)")
            rows.append(f"Login valid until <b>{st['validity']}</b>" + left)
        if st.get("segments"):
            rows.append(f"Segments: {st['segments']}")
        if st.get("plan"):
            rows.append(st["plan"])
        if st.get("funds") is not None:
            rows.append(f"Available balance <b>₹{st['funds']:,.2f}</b>")
        if st.get("open") is not None:
            pc = theme.UP if (st.get("pnl") or 0) >= 0 else theme.DOWN
            rows.append(f"Open positions <b>{st['open']}</b> &nbsp; today's P&amp;L "
                        f"<b style='color:{pc}'>₹{st.get('pnl') or 0:+,.2f}</b>")
        rows.append(f"<span style='color:#787b86'>checked {st.get('ts', '')}</span>")
        self.card.setText("<br>".join(rows))

    def _say(self, text: str, ok: bool | None = None):
        col = theme.UP if ok else (theme.DOWN if ok is False else theme.MUTED)
        self.msg.setText(f"<span style='color:{col}'>{text}</span>")

    # ------------------------------------------------------------------ list
    def _fill(self, keep: str = ""):
        self.list.blockSignals(True)
        self.list.clear()
        for a in self.accts:
            it = QtWidgets.QListWidgetItem()
            it.setData(QtCore.Qt.ItemDataRole.UserRole, a)
            self.list.addItem(it)
        self.list.blockSignals(False)
        self.empty.setVisible(not self.accts)
        self.list.setVisible(bool(self.accts))
        if self.accts:
            i = next((k for k, a in enumerate(self.accts) if a["name"] == keep), 0)
            self.list.setCurrentRow(i)
            self._select(i)
        else:
            self._new()

    def _select(self, i):
        if 0 <= i < len(self.accts):
            self.editing = i
            self.del_btn.show()
            a = self.accts[i]
            self.title.setText(a["name"])
            self.name.setText(a["name"])
            self.broker.blockSignals(True)
            self.broker.setCurrentIndex(max(0, self.broker.findData(a.get("broker", "dhan"))))
            self.broker.blockSignals(False)
            self._build_fields({k: a.get(k, "") for k in (f[0] for f in self._cls().fields)})
            self.live.setChecked(bool(a.get("live")))
            self.mult.setValue(float(a.get("qty_mult", 1) or 1))
            self._say("")
            self._show_status(STATUS.get(a["name"]))
            self.poller.refresh([a], force=False)

    def _new(self):
        self.editing = -1
        self.del_btn.hide()                                  # nothing saved yet -> nothing to delete
        self.list.blockSignals(True)
        self.list.setCurrentRow(-1)
        self.list.blockSignals(False)
        self.title.setText("New account")
        self.name.clear()
        self._build_fields({})
        self.live.setChecked(False)
        self.mult.setValue(1.0)
        self._say("")
        self.name.setFocus()

    def _current(self) -> dict:
        a = {"name": self.name.text().strip(), "broker": self.broker.currentData(),
             "live": self.live.isChecked() and self.broker.currentData() != "paper", "qty_mult": self.mult.value()}
        for k, w in self.inputs.items():
            a[k] = w.text().strip()
        old = self.accts[self.editing] if 0 <= self.editing < len(self.accts) else {}
        for k in ("feed_token", "refresh_token", "login_time", "user_id"):     # kept from the last login
            if old.get(k) and old.get("broker") == a["broker"]:
                a.setdefault(k, old[k])
        return a

    def _missing(self, a: dict) -> list:
        c = _brokers.cls_of(a["broker"])
        need = [f[1] for f in c.fields if f[3] and not a.get(f[0])]
        if a["broker"] == "dhan" and not a.get("access_token") and not (a.get("app_id") or a.get("pin")):
            need.append("Access token")
        return need

    def save(self, quiet: bool = False):
        a = self._current()
        need = self._missing(a)
        if not a["name"] or need:
            self._say("Fill in: " + ", ".join((["Account name"] if not a["name"] else []) + need), False)
            return False
        dup = [i for i, x in enumerate(self.accts) if x["name"] == a["name"]]
        if self.editing >= 0:
            self.accts[self.editing] = a
        elif dup:
            self.accts[dup[0]] = a
        else:
            self.accts.append(a)
        save_accounts(self.accts, self.path)
        self._fill(a["name"])
        if not quiet:
            self._say("Saved.", True)
        self.saved = a["name"]
        if a["broker"] != "paper":
            self.poller.refresh([a], force=True)
        return True

    def delete(self):
        if 0 <= self.editing < len(self.accts):
            del self.accts[self.editing]
            save_accounts(self.accts, self.path)
            self._fill()
            self._say("Deleted.", True)

    def test(self):
        cur = self._current()
        if cur["broker"] == "dhan" and (not cur.get("client_id") or not cur.get("access_token")):
            self._say("Enter Client ID and access token first.", False)
            return
        if cur["broker"] != "dhan" and not cur.get("access_token") and cur["broker"] != "paper":
            self._say(f"Log in to {BROKERS.get(cur['broker'])} first.", False)
            return
        self.tbtn.setEnabled(False)
        self.tbtn.setText("Testing…")
        self.card.setText(f"<span style='color:#787b86'>Checking with {BROKERS.get(cur['broker'])}…</span>")
        key = cur["name"] or "_new"
        cur["name"] = key
        if self.editing < 0:
            self._pending_new = key
        self.poller.refresh([cur], force=True)

    # ------------------------------------------------------------------ logging in
    def _login_values(self, got: dict):
        """A login finished: put what it returned into the form and save the account."""
        for k, v in got.items():
            if k in self.inputs and v:
                self.inputs[k].setText(str(v))
                self.inputs[k].setCursorPosition(0)
        a = self._current()
        a.update({k: v for k, v in got.items() if v not in (None, "")})
        if not a["name"]:
            a["name"] = f"{BROKERS.get(a['broker'])} {a.get('client_id') or a.get('client_code') or a.get('user_id') or ''}".strip()
            self.name.setText(a["name"])
        dup = [i for i, x in enumerate(self.accts) if x["name"] == a["name"]]
        if self.editing >= 0:
            self.accts[self.editing] = a
        elif dup:
            self.accts[dup[0]] = a
        else:
            self.accts.append(a)
        save_accounts(self.accts, self.path)
        self.saved = a["name"]
        self._fill(a["name"])
        self._say(f"Logged in to {BROKERS.get(a['broker'])}. Saved.", True)
        self.poller.refresh([a], force=True)

    def login_browser(self):
        from .brokers import oauth
        import secrets
        cur = self._current()
        need = [f[1] for f in self._cls().fields if f[3] and f[0] != "access_token" and not cur.get(f[0])]
        if cur["broker"] == "dhan":
            need = [lbl for k, lbl in (("client_id", "Client ID"), ("app_id", "API key"), ("app_secret", "API secret"))
                    if not cur.get(k)]
        if need:
            self._say("Fill in first: " + ", ".join(need), False)
            return
        b = _brokers.cls_of(cur["broker"])(cur)
        redirect = self._redirect()
        state = secrets.token_urlsafe(8)
        problem = oauth.check_redirect(redirect)
        self._stop_catch()
        if not problem:
            try:
                self._catcher = oauth.Catcher(redirect, state if cur["broker"] in ("zerodha", "upstox", "fyers") else "").start()
            except OSError as e:
                problem = f"ChartLab could not listen on {redirect} ({e})."
        self.login_btn.setEnabled(False)
        self._say(f"Opening the {b.name} login page…")

        def got_url(url):
            self.login_btn.setEnabled(True)
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))
            self.paste_row.show()
            self._login_b, self._login_redirect = b, redirect
            if self._catcher is not None:
                self._say(f"Log in on the {b.name} page in your browser. ChartLab is waiting for it to come back "
                          f"(to {redirect})…")
                self._poll_until = QtCore.QDateTime.currentSecsSinceEpoch() + 300
                self._poll.start()
            else:
                self._say(problem + " After logging in, paste the address you land on below.", None)

        def failed(msg):
            self.login_btn.setEnabled(True)
            self._stop_catch()
            self._say(f"Could not start the {b.name} login: {msg[:200]}", False)
        job = _Job(lambda: b.login_url(redirect, state), self)
        job.done.connect(got_url)
        job.failed.connect(failed)
        self._login_job = job
        job.start()

    def _check_catch(self):
        c = self._catcher
        if c is None:
            self._poll.stop()
            return
        if c.params is not None:
            params = c.params
            self._stop_catch()
            self._finish_login(params)
        elif QtCore.QDateTime.currentSecsSinceEpoch() > getattr(self, "_poll_until", 0):
            self._stop_catch()
            self._say("No answer from the login page in 5 minutes. Try again, or paste the address you landed on.",
                      False)

    def _stop_catch(self):
        self._poll.stop()
        if self._catcher is not None:
            self._catcher.stop()
            self._catcher = None

    def _use_pasted(self):
        from .brokers import oauth
        params = oauth.parse_pasted(self.paste.text())
        if not params:
            self._say("That is not an address with a login result in it.", False)
            return
        self._stop_catch()
        self._finish_login(params)

    def _finish_login(self, params: dict):
        b = getattr(self, "_login_b", None) or _brokers.cls_of(self.broker.currentData())(self._current())
        redirect = getattr(self, "_login_redirect", self._redirect())
        self._say(f"Finishing the {b.name} login…")
        job = _Job(lambda: b.complete_login(params, redirect), self)
        job.done.connect(lambda got: (self.paste_row.hide(), self.paste.clear(), self._login_values(got)))
        job.failed.connect(lambda msg: self._say(f"{b.name} login failed: {msg[:220]}", False))
        self._login_job = job
        job.start()

    def login_totp(self):
        cur = self._current()
        c = self._cls()
        who = cur.get("client_code") or cur.get("client_id")
        if not who or (c.id == "angel" and not cur.get("api_key")):
            self._say("Fill in " + ("the API key and client code" if c.id == "angel" else "the Client ID") + " first.",
                      False)
            return
        pin, code = cur.get("pin", ""), ""
        if not pin or not cur.get("totp_secret"):
            d = QtWidgets.QDialog(self)
            d.setWindowTitle(f"{c.name} login")
            f = QtWidgets.QFormLayout(d)
            pin_e = QtWidgets.QLineEdit(pin)
            pin_e.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
            code_e = QtWidgets.QLineEdit()
            code_e.setPlaceholderText("6 digits from your authenticator app")
            code_e.setMaxLength(6)
            if not pin:
                f.addRow("PIN", pin_e)
            if not cur.get("totp_secret"):
                f.addRow("TOTP code", code_e)
            bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                            QtWidgets.QDialogButtonBox.StandardButton.Cancel)
            bb.accepted.connect(d.accept)
            bb.rejected.connect(d.reject)
            f.addRow(bb)
            if not d.exec():
                return
            pin, code = pin_e.text().strip(), code_e.text().strip()
        b = c(cur)
        self.totp_btn.setEnabled(False)
        self._say(f"Logging in to {c.name}…")
        job = _Job(lambda: b.login_totp(pin, code), self)

        def done(got):
            self.totp_btn.setEnabled(True)
            self._login_values(got)

        def failed(msg):
            self.totp_btn.setEnabled(True)
            self._say(f"{c.name} login failed: {msg[:220]}", False)
        job.done.connect(done)
        job.failed.connect(failed)
        self._login_job = job
        job.start()

    def reject(self):
        self._stop_catch()
        super().reject()

    def accept(self):
        self._stop_catch()
        super().accept()

    # ------------------------------------------------------------------ many accounts
    def renew(self):
        if not 0 <= self.editing < len(self.accts):
            return
        a = self.accts[self.editing]
        name = a["name"]
        self.rbtn.setEnabled(False)
        self._say(f"Asking {BROKERS.get(a.get('broker'))} for a fresh login…")
        if a.get("broker") == "dhan":
            job = _Job(lambda: renew_saved(name, self.path), self)
        else:
            b = _brokers.cls_of(a["broker"])(a)
            job = _Job(lambda: (_save_fields(name, b.renew(), self.path), f"{name}: login renewed")[1], self)

        def done(msg):
            self.rbtn.setEnabled(True)
            self.accts = load_accounts(self.path)
            self._fill(name)
            self._say(msg, True)
            self.poller.refresh([x for x in self.accts if x["name"] == name], force=True)

        def failed(msg):
            self.rbtn.setEnabled(True)
            self._say("Could not renew: " + msg[:200] + "  (an expired login cannot be renewed: log in again)", False)
        job.done.connect(done)
        job.failed.connect(failed)
        job.start()

    def add_many(self):
        d = BulkAddDialog(self, {a["name"] for a in self.accts})
        if d.exec() and d.result_accounts:
            by = {a["name"]: i for i, a in enumerate(self.accts)}
            for a in d.result_accounts:
                if a["name"] in by:
                    self.accts[by[a["name"]]] = a
                else:
                    self.accts.append(a)
            save_accounts(self.accts, self.path)
            self.saved = d.result_accounts[0]["name"]
            self._fill(self.saved)
            self._say(f"Added {len(d.result_accounts)} accounts (all PAPER until you tick Live orders).", True)
            self.poller.refresh(self.accts, force=False)

    def overview(self):
        OverviewDialog(self, self.path).exec()
        self.accts = load_accounts(self.path)                 # tokens may have been renewed there
        keep = self.accts[self.editing]["name"] if 0 <= self.editing < len(self.accts) else ""
        self._fill(keep)


def _save_fields(name: str, fields: dict, path: Path | None = None):
    """Write fields returned by a login / renewal into a saved account (re-read first: never undo another edit)."""
    accts = load_accounts(path)
    for x in accts:
        if x["name"] == name:
            x.update({k: v for k, v in fields.items() if v not in (None, "")})
    save_accounts(accts, path)


def auto_login_way(a: dict) -> str:
    """How this saved account can get a fresh login by itself: 'renew', 'totp', or '' (the broker wants a login in
    the browser every day: Zerodha, Upstox, or missing PIN / TOTP key)."""
    bid = a.get("broker")
    if bid in (None, "paper"):
        return ""
    try:
        caps = _brokers.cls_of(bid).caps
    except Exception:  # noqa: BLE001
        return ""
    if "login_totp" in caps and a.get("pin") and a.get("totp_secret") and (a.get("client_id") or a.get("client_code")):
        return "totp"
    if "renew" in caps and a.get("access_token"):
        if bid == "fyers" and not (a.get("refresh_token") and a.get("pin")):
            return ""
        if bid == "angel" and not a.get("refresh_token"):
            return ""
        return "renew"
    return ""


def auto_login(name: str, path: Path | None = None, expired: bool = False) -> str:
    """A fresh login for one saved account without anyone typing: the broker's own renewal while the login still
    works, else PIN + the saved TOTP key (Dhan, Angel One). Saves the new token. -> message for the user."""
    a = next((x for x in load_accounts(path) if x["name"] == name), None)
    if a is None:
        raise KeyError(name)
    way = auto_login_way(a)
    if not way:
        raise RuntimeError(f"{BROKERS.get(a.get('broker'), a.get('broker'))} needs a login in the browser: Accounts \u2192 Log in")
    errors = []
    if not expired and "renew" in _brokers.cls_of(a["broker"]).caps and a.get("access_token"):
        try:
            if a["broker"] == "dhan":
                return renew_saved(name, path)
            _save_fields(name, _brokers.cls_of(a["broker"])(dict(a)).renew(), path)
            return f"{name}: login renewed"
        except Exception as e:  # noqa: BLE001 - the renewal was refused: the TOTP way is next
            errors.append(str(e)[:120])
    if way == "totp":
        b = _brokers.cls_of(a["broker"])(dict(a))
        _save_fields(name, b.login_totp(a.get("pin", ""), ""), path)
        return f"{name}: logged in again with the saved TOTP key"
    raise RuntimeError(errors[0] if errors else "this login cannot be renewed")


class _Job(QtCore.QThread):
    done = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


def check_many(accts: list[dict]) -> dict:
    """Check every account at the same time (one thread each). -> {name: status}"""
    import concurrent.futures as cf
    if not accts:
        return {}
    with cf.ThreadPoolExecutor(max_workers=min(8, len(accts))) as pool:
        sts = list(pool.map(get_status, accts))
    out = {}
    for a, st in zip(accts, sts):
        st["_at"] = dt.datetime.now()
        out[a["name"]] = st
    return out


_DLG = ("QDialog { background: #1e1e1e; } QLabel#h { font-size: 16px; font-weight: 700; color: white; } "
        "QLabel#sub { color: #868993; font-size: 12px; } "
        "QPlainTextEdit { background: #121212; border: 1px solid #3a3a3a; border-radius: 8px; padding: 8px; "
        "font-family: Menlo, monospace; font-size: 12px; } "
        "QTableWidget { background: #161616; border: 1px solid #2a2a2a; border-radius: 8px; gridline-color: #262626; } "
        "QHeaderView::section { background: #161616; color: #868993; border: 0; border-bottom: 1px solid #2a2a2a; "
        "padding: 6px 8px; font-size: 11px; }")


class BulkAddDialog(QtWidgets.QDialog):
    """Paste many accounts, check them all with Dhan at once, add the ones that connect."""

    def __init__(self, parent=None, existing: set | None = None):
        super().__init__(parent)
        self.setWindowTitle("Add many accounts")
        self.resize(760, 560)
        self.setStyleSheet(_DLG)
        self.existing = set(existing or ())
        self.result_accounts: list[dict] = []
        self._parsed: list[dict] = []
        self._status: dict = {}
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(22, 18, 22, 16)
        v.setSpacing(8)
        h = QtWidgets.QLabel("Add many accounts")
        h.setObjectName("h")
        v.addWidget(h)
        sub = QtWidgets.QLabel("One account per line:  <b>name, client id, access token</b>  (commas, tabs or spaces; a line "
                               "copied from Excel works). The token stays on this Mac. Every account is added as "
                               "<b>PAPER</b> - you switch on live orders yourself, one account at a time.")
        sub.setObjectName("sub")
        sub.setWordWrap(True)
        v.addWidget(sub)
        self.text = QtWidgets.QPlainTextEdit()
        self.text.setPlaceholderText("Papa, 1100000001, eyJ0eXAiOiJKV1QiLCJhbGciOi...\nFund 2, 1100000002, eyJ0eXAiOiJKV1QiLCJhbGciOi...")
        self.text.setFixedHeight(130)
        v.addWidget(self.text)
        row = QtWidgets.QHBoxLayout()
        self.file_btn = QtWidgets.QPushButton("Open a CSV / text file\u2026")
        self.file_btn.clicked.connect(self._open)
        row.addWidget(self.file_btn)
        row.addStretch(1)
        self.check_btn = QtWidgets.QPushButton("Check with Dhan")
        self.check_btn.clicked.connect(self.check)
        row.addWidget(self.check_btn)
        v.addLayout(row)
        self.tbl = QtWidgets.QTableWidget(0, 4)
        self.tbl.setHorizontalHeaderLabels(["Name", "Client ID", "Result", "Balance"])
        self.tbl.verticalHeader().hide()
        self.tbl.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.tbl.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        v.addWidget(self.tbl, 1)
        self.note = QtWidgets.QLabel("")
        self.note.setObjectName("sub")
        self.note.setWordWrap(True)
        v.addWidget(self.note)
        b = QtWidgets.QHBoxLayout()
        b.addStretch(1)
        c = QtWidgets.QPushButton("Cancel")
        c.clicked.connect(self.reject)
        b.addWidget(c)
        self.add_btn = QtWidgets.QPushButton("Add accounts")
        self.add_btn.setObjectName("primary")
        self.add_btn.setEnabled(False)
        self.add_btn.clicked.connect(self._add)
        b.addWidget(self.add_btn)
        v.addLayout(b)

    def _open(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Accounts list", str(Path.home()),
                                                        "Lists (*.csv *.txt *.tsv);;All files (*)")
        if path:
            try:
                self.text.setPlainText(Path(path).read_text(errors="replace"))
            except OSError as e:
                self.note.setText(f"<span style='color:{theme.DOWN}'>Could not read the file: {e}</span>")

    def check(self):
        accts, bad = parse_bulk(self.text.toPlainText())
        self._parsed, self._status = accts, {}
        self._show()
        msg = []
        if bad:
            msg.append(f"<span style='color:{theme.DOWN}'>{len(bad)} line(s) not understood "
                       f"(need a client id and a token): {', '.join(bad[:3])}</span>")
        if not accts:
            self.note.setText("<br>".join(msg) or "Paste at least one line first.")
            return
        self.note.setText("<br>".join(msg + [f"Checking {len(accts)} accounts with Dhan\u2026"]))
        self.check_btn.setEnabled(False)
        job = _Job(lambda: check_many(accts), self)
        job.done.connect(lambda res, m=msg: self._checked(res, m))
        job.failed.connect(lambda e: (self.check_btn.setEnabled(True), self.note.setText(e[:200])))
        job.start()

    def _checked(self, res, msg):
        self.check_btn.setEnabled(True)
        self._status = res
        STATUS.update(res)
        self._show()
        ok = [a for a in self._parsed if res.get(a["name"], {}).get("state") in ("ok", "expiring")]
        off = [a for a in self._parsed if res.get(a["name"], {}).get("state") == "offline"]
        self._ok = ok + off                                   # offline: cannot tell - add them, check later
        clash = [a["name"] for a in self._ok if a["name"] in self.existing]
        if clash:
            msg = msg + [f"Already saved and will be updated: {', '.join(clash[:5])}"]
        msg = msg + [f"<b>{len(ok)}</b> of {len(self._parsed)} connected" +
                     (f", {len(off)} could not be checked (no internet)" if off else "") + "."]
        self.note.setText("<br>".join(msg))
        self.add_btn.setEnabled(bool(self._ok))
        self.add_btn.setText(f"Add {len(self._ok)} account{'s' if len(self._ok) != 1 else ''}")

    def _show(self):
        t = self.tbl
        t.setRowCount(len(self._parsed))
        for r, a in enumerate(self._parsed):
            st = self._status.get(a["name"])
            cells = [a["name"], a["client_id"], (st["msg"] or st["state"]) if st else "checking\u2026",
                     f"\u20B9{st['funds']:,.0f}" if st and st.get("funds") is not None else ""]
            for c, txt in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(txt)
                if c == 2:
                    it.setForeground(QtGui.QColor(STATE_COLOR.get(st["state"] if st else None) if st else "#868993"))
                t.setItem(r, c, it)
        t.resizeColumnsToContents()
        t.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)

    def _add(self):
        self.result_accounts = list(getattr(self, "_ok", []))
        self.accept()


class OverviewDialog(QtWidgets.QDialog):
    """Every account in one table, checked at the same time; renew tokens for all with one button."""
    COLS = ["Account", "Client ID", "Orders", "Connection", "Token left", "Data API", "Balance", "Open pos.", "Today's P&L"]

    def __init__(self, parent=None, path: Path | None = None):
        super().__init__(parent)
        self.path = path
        self.setWindowTitle("All accounts")
        self.resize(980, 520)
        self.setStyleSheet(_DLG)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(22, 18, 22, 16)
        v.setSpacing(8)
        top = QtWidgets.QHBoxLayout()
        h = QtWidgets.QLabel("All accounts")
        h.setObjectName("h")
        top.addWidget(h)
        top.addStretch(1)
        self.renew_btn = QtWidgets.QPushButton("Renew all tokens")
        self.renew_btn.setToolTip("Swap every connected account's token for a fresh 24-hour one (Dhan Web tokens that are "
                                  "still valid). The old tokens stop working.")
        self.renew_btn.clicked.connect(self.renew_all)
        self.renew_btn.setAutoDefault(False)
        top.addWidget(self.renew_btn)
        self.check_btn = QtWidgets.QPushButton("Check all now")
        self.check_btn.setObjectName("primary")
        self.check_btn.setDefault(True)
        self.check_btn.clicked.connect(self.check)
        top.addWidget(self.check_btn)
        v.addLayout(top)
        self.tbl = QtWidgets.QTableWidget(0, len(self.COLS))
        self.tbl.setHorizontalHeaderLabels(self.COLS)
        self.tbl.verticalHeader().hide()
        self.tbl.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.tbl.setShowGrid(False)
        self.tbl.verticalHeader().setDefaultSectionSize(30)
        v.addWidget(self.tbl, 1)
        self.note = QtWidgets.QLabel("")
        self.note.setObjectName("sub")
        self.note.setWordWrap(True)
        self.note.setTextFormat(QtCore.Qt.TextFormat.RichText)
        v.addWidget(self.note)
        b = QtWidgets.QHBoxLayout()
        b.addStretch(1)
        c = QtWidgets.QPushButton("Close")
        c.clicked.connect(self.accept)
        b.addWidget(c)
        v.addLayout(b)
        self._show()
        QtCore.QTimer.singleShot(0, self.check)

    def check(self):
        accts = load_accounts(self.path)
        if not accts or not self.check_btn.isEnabled():
            return
        self.check_btn.setEnabled(False)
        self.check_btn.setText("Checking\u2026")
        job = _Job(lambda: check_many(accts), self)
        job.done.connect(self._checked)
        job.failed.connect(lambda e: self._checked({}))
        job.start()

    def _checked(self, res):
        STATUS.update(res)
        self.check_btn.setEnabled(True)
        self.check_btn.setText("Check all now")
        self._show()

    def renew_all(self):
        accts = [a for a in load_accounts(self.path)
                 if (STATUS.get(a["name"]) or {}).get("state") in ("ok", "expiring")]
        if not accts:
            self.note.setText("No connected account to renew. (An expired token cannot be renewed: paste a new one.)")
            return
        if QtWidgets.QMessageBox.question(
                self, "Renew tokens",
                f"Ask Dhan for a fresh 24-hour token for {len(accts)} account(s)?\n\nThe tokens saved now stop working "
                "at once - anything else that uses them (another app, a script with the token typed in) must be "
                "given the new token.") != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.renew_btn.setEnabled(False)
        path = self.path

        def work():
            out = []
            for a in accts:                                   # one after another: each writes accounts.json
                try:
                    out.append((True, renew_saved(a["name"], path)))
                except Exception as e:  # noqa: BLE001
                    out.append((False, f"{a['name']}: {str(e)[:120]}"))
            return out
        job = _Job(work, self)

        def done(res):
            self.renew_btn.setEnabled(True)
            self.note.setText("<br>".join(f"<span style='color:{theme.UP if ok else theme.DOWN}'>{m}</span>"
                                          for ok, m in res))
            self.check()
        job.done.connect(done)
        job.failed.connect(lambda e: (self.renew_btn.setEnabled(True), self.note.setText(e[:200])))
        job.start()

    def _show(self):
        accts = load_accounts(self.path)
        t = self.tbl
        t.setRowCount(len(accts) + (1 if accts else 0))
        A = QtCore.Qt.AlignmentFlag
        tot_f = tot_p = 0.0
        tot_o = n_ok = 0
        for r, a in enumerate(accts):
            st = STATUS.get(a["name"]) or {}
            d = st.get("days_left")
            left = "" if d is None else ("expired" if d <= 0 else (f"{d * 24:.1f} h" if d < 2 else f"{d:.0f} days"))
            funds, pnl, op = st.get("funds"), st.get("pnl"), st.get("open")
            n_ok += st.get("state") in ("ok", "expiring")
            tot_f += funds or 0
            tot_p += pnl or 0
            tot_o += op or 0
            cells = [a["name"], str(a.get("client_id", "")), "LIVE" if a.get("live") else "PAPER",
                     st.get("msg") or ("checking\u2026" if not st else st.get("state", "")), left,
                     "" if st.get("data") is None else ("active" if st["data"] else "not active"),
                     "" if funds is None else f"\u20B9{funds:,.2f}", "" if op is None else str(op),
                     "" if pnl is None else f"\u20B9{pnl:+,.2f}"]
            for c, txt in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(txt)
                if c >= 6:
                    it.setTextAlignment(A.AlignRight | A.AlignVCenter)
                if c == 2:
                    it.setForeground(QtGui.QColor(theme.DOWN if a.get("live") else "#868993"))
                elif c == 3:
                    it.setForeground(QtGui.QColor(STATE_COLOR.get(st.get("state"))))
                elif c == 4 and d is not None and d * 24 < EXPIRING_HOURS:
                    it.setForeground(QtGui.QColor(theme.DOWN if d <= 0 else "#ff9800"))
                elif c == 5 and st.get("data") is False:
                    it.setForeground(QtGui.QColor("#ff9800"))
                    it.setToolTip("Dhan's Data API plan is needed for candles, live prices and the option chain")
                elif c == 8 and pnl is not None:
                    it.setForeground(QtGui.QColor(theme.UP if pnl >= 0 else theme.DOWN))
                t.setItem(r, c, it)
        if accts:
            r = len(accts)
            bold = QtGui.QFont(t.font())
            bold.setBold(True)
            cells = [f"{n_ok} of {len(accts)} connected", "", "", "", "", "", f"\u20B9{tot_f:,.2f}", str(tot_o),
                     f"\u20B9{tot_p:+,.2f}"]
            for c, txt in enumerate(cells):
                it = QtWidgets.QTableWidgetItem(txt)
                it.setFont(bold)
                if c >= 6:
                    it.setTextAlignment(A.AlignRight | A.AlignVCenter)
                if c == 8:
                    it.setForeground(QtGui.QColor(theme.UP if tot_p >= 0 else theme.DOWN))
                t.setItem(r, c, it)
        t.resizeColumnsToContents()
        t.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Stretch)
        if not accts:
            self.note.setText("No accounts yet. Close this and use  + Add  or  Add many\u2026")


class PickAccountsDialog(QtWidgets.QDialog):
    """Choose which accounts a script should run on; shows a clear warning that it may place real orders."""

    def __init__(self, parent, accts: list[dict]):
        super().__init__(parent)
        self.setWindowTitle("Run script on accounts")
        self.setMinimumWidth(560)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(22, 18, 22, 16)
        v.setSpacing(10)
        v.addWidget(QtWidgets.QLabel("<span style='font-size:17px;font-weight:600;color:#fff'>Run script on accounts</span>"))
        info = QtWidgets.QLabel(
            "The script runs <b>once per ticked account, all at the same time</b>. Each copy can read its own "
            "credentials with <code>chartlab.account()</code>.<br>"
            f"<span style='color:{theme.DOWN}'>If your script places orders, they will be REAL orders on every "
            "ticked account.</span>")
        info.setWordWrap(True)
        v.addWidget(info)
        self.one = QtWidgets.QRadioButton("One script, orders sent to ALL ticked accounts at the same instant (fastest)")
        self.one.setChecked(True)
        self.sep = QtWidgets.QRadioButton("A separate copy of the script per account (independent runs)")
        v.addWidget(self.one)
        v.addWidget(self.sep)
        self.list = QtWidgets.QListWidget()
        for a in accts:
            it = QtWidgets.QListWidgetItem(f"{a['name']}  ({BROKERS.get(a['broker'], a['broker'])})  -  "
                                        + ("LIVE orders" if a.get("live") else "paper"))
            it.setFlags(it.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.CheckState.Unchecked)
            it.setSizeHint(QtCore.QSize(0, 34))
            it.setData(QtCore.Qt.ItemDataRole.UserRole, a["name"])
            self.list.addItem(it)
        v.addWidget(self.list)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        ok = bb.addButton("Run on selected", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setObjectName("primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def broadcast(self) -> bool:
        return self.one.isChecked()

    def chosen(self) -> list[str]:
        return [self.list.item(i).data(QtCore.Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == QtCore.Qt.CheckState.Checked]
