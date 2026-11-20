"""The option archive: every day, the 1-minute history (price, volume, open interest) of the option contracts around
the money is saved, one file per contract, from the day the contract was listed until it expires. Files of expired
contracts stay, so after a few weeks there is a growing library of complete contract histories - what a model or a
machine-learning indicator needs, and what brokers no longer hand out once a contract has expired.

    <folder>/NIFTY/2026-11-24/NIFTY261124C25000.parquet     Date, Open, High, Low, Close, Volume, OI
    <folder>/NIFTY/spot_1m.parquet                          the index / stock itself, same minutes
    <folder>/index.json                                     what is stored: contract -> first bar, last bar, rows

The folder is ~/ChartLab/options, or a folder of your choice: pick one inside iCloud Drive / Google Drive / Dropbox
and the computer's own sync keeps a copy in the cloud.

Data comes from the connected Dhan account (its candles carry open interest); nothing here places orders."""
from __future__ import annotations

import datetime as dt
import json
import os
import queue
import re
import threading
import time
from pathlib import Path

import pandas as pd

from . import data as datamod
from . import dhan_core

DEFAULT_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")   # every index that has options
DEFAULT_WIDTH = 10            # strikes each side of the money (0 = every strike)
DEFAULT_EXPIRIES = 2          # the nearest two expiries (0 = every listed expiry)
PAUSE = 0.3                   # between two requests of one account: Dhan allows 5 a second, charts need some too
CHUNK_DAYS = 85               # Dhan gives about 90 days of 1-minute candles per request
MAX_BACK_CHUNKS = 4           # a first download goes back up to ~11 months, until the contract has no older bars
COLS = ["Open", "High", "Low", "Close", "Volume", "OI"]


def icloud_dir() -> Path | None:
    p = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    return p if p.is_dir() else None


def default_root() -> Path:
    return datamod.USER_DIR / "options"


def root(chosen: str = "") -> Path:
    """The archive folder: the chosen one if it can be written, else ~/ChartLab/options."""
    for cand in ([Path(chosen).expanduser()] if chosen else []) + [default_root()]:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".write_test"
            probe.write_text("ok")
            probe.unlink()
            return cand
        except Exception:  # noqa: BLE001 - not writable (cloud folder gone, no permission): next one
            continue
    return default_root()


def contract_path(base: Path, c: dict) -> Path:
    return base / c["und"] / str(c["expiry"]) / f"{c['symbol']}.parquet"


def _frame(js: dict) -> pd.DataFrame:
    ts = pd.Series(js.get("timestamp") or [], dtype="float64")
    if ts.empty:
        return pd.DataFrame(columns=COLS)
    t = pd.to_datetime(ts, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    n = len(t)
    oi = js.get("open_interest") or js.get("oi") or [0] * n
    df = pd.DataFrame({"Open": js["open"], "High": js["high"], "Low": js["low"], "Close": js["close"],
                       "Volume": js.get("volume", [0] * n), "OI": list(oi)[:n] + [0] * max(0, n - len(oi))},
                      index=pd.DatetimeIndex(t, name="Date")).astype("float64")
    return df[~df.index.duplicated(keep="last")].sort_index()


def fetch_minutes(acct: dict, sid, seg: str, inst: str, start: dt.date, end: dt.date, post=None) -> pd.DataFrame:
    """1-minute candles with open interest from Dhan for one instrument, start..end (at most CHUNK_DAYS apart)."""
    post = post or (lambda body: json.loads(dhan_core._http(f"{dhan_core.DHAN}/charts/intraday", body,
                                                            dhan_core._hdr(acct))))
    body = {"securityId": str(sid), "exchangeSegment": seg, "instrument": inst, "interval": "1", "oi": True,
            "fromDate": f"{start} 09:00:00", "toDate": f"{end} 15:30:00"}
    return _frame(post(body) or {})


def _busy(e: Exception) -> bool:
    m = str(e)
    return any(k in m for k in ("429", "DH-904", "Rate", "rate limit", "Too many"))


def _dead(e: Exception) -> bool:
    """The account itself cannot give data right now (login gone, no data plan), whatever the contract."""
    m = str(e)
    return any(k in m for k in ("401", "403", "DH-901", "DH-902", "DH-903", "nvalid", "xpired", "Could not reach"))


def _fetch(acct, sid, seg, inst, start, end, post=None) -> pd.DataFrame:
    try:
        return fetch_minutes(acct, sid, seg, inst, start, end, post)
    except Exception as e:  # noqa: BLE001
        if not _busy(e):
            raise
        time.sleep(1.6)                                      # too many requests: let the second pass, ask once more
        return fetch_minutes(acct, sid, seg, inst, start, end, post)


def read(path: Path) -> pd.DataFrame:
    try:
        if not Path(path).exists():                          # iCloud may hold only a placeholder of an old file
            from . import store as _store
            _store.ensure_local(Path(path))
        df = pd.read_parquet(path)
        if "Date" in df.columns:
            df = df.set_index("Date")
        return df
    except Exception:  # noqa: BLE001 - missing or half-written file
        return pd.DataFrame(columns=COLS)


def _write(path: Path, df: pd.DataFrame):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    df.to_parquet(tmp)
    os.replace(tmp, path)                                    # never a half-written file, also in a synced folder


def update_one(acct: dict, path: Path, sid, seg: str, inst: str, today: dt.date | None = None, post=None,
               pause: float = PAUSE) -> int:
    """Bring one instrument's file up to date. A new file gets everything back to the listing day. -> rows added."""
    today = today or dt.date.today()
    old = read(path)
    end = today + dt.timedelta(days=1)
    frames = []
    if len(old):
        start = max(old.index[-1].date() - dt.timedelta(days=1), end - dt.timedelta(days=CHUNK_DAYS))
        frames.append(_fetch(acct, sid, seg, inst, start, end, post))
    else:
        cur_end = end
        for _ in range(MAX_BACK_CHUNKS):
            start = cur_end - dt.timedelta(days=CHUNK_DAYS)
            try:
                f = _fetch(acct, sid, seg, inst, start, cur_end, post)
            except Exception:  # noqa: BLE001 - before the listing day Dhan answers with an error: that is the start
                if frames:
                    break
                raise
            if not len(f):
                break
            frames.append(f)
            if f.index[0].date() > start + dt.timedelta(days=4):
                break                                        # the contract's first bar is inside this piece
            cur_end = start
            time.sleep(pause)
    new = pd.concat([x for x in frames if len(x)] or [pd.DataFrame(columns=COLS)])
    if not len(new):
        return 0
    df = pd.concat([old, new]) if len(old) else new
    df = df[~df.index.duplicated(keep="last")].sort_index()
    added = len(df) - len(old)
    if added > 0 or not path.exists():
        df.index.name = "Date"
        _write(path, df[COLS] if all(c in df.columns for c in COLS) else df)
    return max(added, 0)


def plan(underlyings, spot: dict, width: int = DEFAULT_WIDTH, n_exp: int = DEFAULT_EXPIRIES,
         today: dt.date | None = None) -> list:
    """The contracts to keep: for each name, the nearest `n_exp` expiries (0 = every listed one), `width` strikes
    each side of the money (0 = every strike), calls and puts. The most useful come first - the nearest expiry of
    every name, from the money outwards - so a run that is cut short has saved what matters."""
    today = today or dt.date.today()
    out = []
    t = dhan_core._options_table()
    for u_no, und in enumerate(underlyings):
        px = spot.get(und)
        if not px:
            continue
        tu = t[(t["und"] == und) & (t["expiry"] >= today)]
        exps = sorted(set(tu["expiry"]))
        for e_no, exp in enumerate(exps[:n_exp] if n_exp else exps):
            te = tu[tu["expiry"] == exp]
            strikes = sorted(set(float(x) for x in te["strike"]))
            if not strikes:
                continue
            i = min(range(len(strikes)), key=lambda k: abs(strikes[k] - px))
            rank = {k: abs(n - i) for n, k in enumerate(strikes)}
            keep = set(strikes[max(0, i - width): i + width + 1]) if width else set(strikes)
            for _, r in te[te["strike"].astype(float).isin(keep)].iterrows():
                out.append(((e_no, rank[float(r["strike"])], u_no), dhan_core._contract(r)))
    out.sort(key=lambda x: x[0])
    return [c for _k, c in out]


_TV = re.compile(r"^([A-Z][A-Z0-9&\-]*?)(\d{2})(\d{2})(\d{2})([CP])(\d+(?:\.\d+)?)$")


def describe(name: str, entry: dict | None = None) -> dict:
    """What a stored name is: {'und', 'expiry', 'strike', 'kind'} of a contract, {'und', 'spot': True} of an index."""
    e = entry or {}
    if e.get("und") and (e.get("spot") or e.get("expiry")):
        return {k: e[k] for k in ("und", "expiry", "strike", "kind", "lot", "spot") if k in e}
    m = _TV.match(str(name).upper())
    if not m:
        return {"und": str(name).upper(), "spot": True}
    return {"und": m[1], "expiry": f"20{m[2]}-{m[3]}-{m[4]}", "strike": float(m[6]), "kind": "CE" if m[5] == "C" else "PE"}


def _save_index(base: Path, index: dict):
    tmp = base / "index.part"
    tmp.write_text(json.dumps(index, indent=1, sort_keys=True))
    os.replace(tmp, base / "index.json")


def run(acct, base: Path, underlyings=DEFAULT_UNDERLYINGS, width: int = DEFAULT_WIDTH,
        n_exp: int = DEFAULT_EXPIRIES, stop=lambda: False, progress=None, post=None, spot: dict | None = None,
        today: dt.date | None = None, pause: float = PAUSE, stamp: str = "") -> dict:
    """One archive pass. `acct` is one Dhan account or a list of them: several accounts share the work, each at
    its own request rate, and one that stops answering hands its contracts to the others.
    `stamp` names this pass ('2026-11-20-close'): a contract already saved under the same stamp is not asked for
    again, so a pass that was cut short carries on where it stopped.
    -> {'contracts', 'updated', 'rows', 'errors': [...], 'folder', 'accounts', 'skipped'}"""
    today = today or dt.date.today()
    accts = [a for a in (acct if isinstance(acct, (list, tuple)) else [acct]) if a]
    base = Path(base)
    unds = list(dict.fromkeys(u.strip().upper() for u in underlyings if u and u.strip()))
    refs = {}
    for u in unds:
        try:
            sid, seg = dhan_core.underlying_ref(u)
            refs[u] = (str(sid), seg, "INDEX" if seg == "IDX_I" else "EQUITY")
        except Exception:  # noqa: BLE001
            continue
    res = {"contracts": 0, "updated": 0, "rows": 0, "errors": [], "folder": str(base), "accounts": len(accts),
           "skipped": 0}
    if not accts:
        res["errors"].append("no account")
        return res
    if spot is None:
        spot, last = {}, None
        for a in accts:                                      # the first account that answers gives the prices
            try:
                got = dhan_core.ltp(a, [(seg, sid) for sid, seg, _i in refs.values()])
                spot = {u: got.get((seg, sid)) for u, (sid, seg, _i) in refs.items()}
                break
            except Exception as e:  # noqa: BLE001
                last = e
        else:
            raise last if last else RuntimeError("no prices")
    jobs = [(base / u / "spot_1m.parquet", sid, seg, inst, u, {"und": u, "spot": True})
            for u, (sid, seg, inst) in refs.items()]
    todo = plan(list(refs), spot, width, n_exp, today)
    jobs += [(contract_path(base, c), c["security_id"], c["segment"], c["instrument"], c["symbol"],
              {"und": c["und"], "expiry": str(c["expiry"]), "strike": c["strike"], "kind": c["kind"], "lot": c["lot"]})
             for c in todo]
    res["contracts"] = len(todo)
    index = load_index(base)
    lock = threading.Lock()
    q: queue.Queue = queue.Queue()
    for j in jobs:
        q.put((j, 0))
    done = [0]
    alive = [len(accts)]

    def work(a):
        bad = good = 0
        while not stop():
            try:
                job, tries = q.get_nowait()
            except queue.Empty:
                return
            path, sid, seg, inst, name, meta = job
            with lock:
                if stamp and (index.get(name) or {}).get("stamp") == stamp:
                    res["skipped"] += 1
                    done[0] += 1
                    continue
            try:
                added = update_one(a, path, sid, seg, inst, today, post, pause)
                good += 1
                bad = 0
                with lock:
                    if added:
                        res["updated"] += 1
                        res["rows"] += added
                    entry = index.get(name)
                    if added or entry is None or "und" not in entry:
                        df = read(path)
                        if len(df):
                            entry = {"file": str(path.relative_to(base)), "first": str(df.index[0]),
                                     "last": str(df.index[-1]), "rows": int(len(df))}
                            entry.update(meta)
                            index[name] = entry
                    if stamp and name in index:
                        index[name]["stamp"] = stamp
                    done[0] += 1
                    if done[0] % 60 == 0:                    # a pass that is cut short keeps what it has done
                        try:
                            _save_index(base, index)
                        except Exception:  # noqa: BLE001
                            pass
            except Exception as e:  # noqa: BLE001 - one contract must not stop the rest
                bad += 1
                with lock:
                    others = alive[0] > 1
                    if _dead(e) and others and tries < len(accts):
                        q.put((job, tries + 1))              # this account cannot: another one takes the contract
                    else:
                        res["errors"].append(f"{name}: {str(e)[:120]}")
                        done[0] += 1
                    quit_now = (bad >= 8 and good == 0) or (_dead(e) and bad >= 3)
                    if quit_now:
                        alive[0] -= 1                        # nothing works on this account (login gone, no data plan)
                if quit_now:
                    return
            if progress:
                progress(done[0], len(jobs))
            time.sleep(pause)

    if len(accts) == 1:
        work(accts[0])
    else:
        threads = [threading.Thread(target=work, args=(a,), daemon=True) for a in accts]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    try:
        _save_index(base, index)
    except Exception as e:  # noqa: BLE001
        res["errors"].append(f"index: {e}")
    return res


def contracts(base: Path, underlying: str = "", expiry=None, kind: str = "") -> list:
    """What the archive holds, as rows: {'symbol', 'und', 'expiry', 'strike', 'kind', 'first', 'last', 'rows', 'file'}
    (the index itself appears as symbol = its name, 'spot': True). Filters are optional."""
    out = []
    und = underlying.strip().upper()
    exp = str(pd.Timestamp(expiry).date()) if expiry else ""
    k = {"C": "CE", "P": "PE", "CALL": "CE", "PUT": "PE"}.get(kind.strip().upper(), kind.strip().upper())
    for name, e in sorted(load_index(base).items()):
        d = describe(name, e)
        if und and d.get("und") != und:
            continue
        if exp and str(d.get("expiry", "")) != exp:
            continue
        if k and d.get("kind") != k:
            continue
        row = {"symbol": name, "first": e.get("first"), "last": e.get("last"), "rows": e.get("rows"), "file": e.get("file")}
        row.update(d)
        out.append(row)
    return out


def load_index(base: Path) -> dict:
    try:
        return json.loads((base / "index.json").read_text())
    except Exception:  # noqa: BLE001
        return {}


def load(symbol: str, base: Path | None = None) -> pd.DataFrame:
    """A stored contract's whole history, e.g. load('NIFTY261124C25000'); the name alone ('NIFTY') gives its spot."""
    base = base or root()
    idx = load_index(base)
    key = symbol.strip().upper()
    hit = idx.get(key)
    if hit is None and dhan_core.is_option(key):
        try:
            key = dhan_core.resolve_option(key)["symbol"]
            hit = idx.get(key)
        except Exception:  # noqa: BLE001
            hit = None
    if hit is None:
        raise KeyError(f"'{symbol}' is not in the option archive ({base})")
    df = read(base / hit["file"])
    df.attrs["symbol"] = key
    return df
