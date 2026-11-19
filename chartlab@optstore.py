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
import time
from pathlib import Path

import pandas as pd

from . import data as datamod
from . import dhan_core

DEFAULT_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "SENSEX")
DEFAULT_WIDTH = 10            # strikes each side of the money
DEFAULT_EXPIRIES = 2          # the nearest two expiries
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


def read(path: Path) -> pd.DataFrame:
    try:
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
               pause: float = 0.22) -> int:
    """Bring one instrument's file up to date. A new file gets everything back to the listing day. -> rows added."""
    today = today or dt.date.today()
    old = read(path)
    end = today + dt.timedelta(days=1)
    frames = []
    if len(old):
        start = max(old.index[-1].date() - dt.timedelta(days=1), end - dt.timedelta(days=CHUNK_DAYS))
        frames.append(fetch_minutes(acct, sid, seg, inst, start, end, post))
    else:
        cur_end = end
        for _ in range(MAX_BACK_CHUNKS):
            start = cur_end - dt.timedelta(days=CHUNK_DAYS)
            try:
                f = fetch_minutes(acct, sid, seg, inst, start, cur_end, post)
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
    """The contracts to keep: for each name, the nearest `n_exp` expiries, `width` strikes each side of the money,
    calls and puts."""
    today = today or dt.date.today()
    out = []
    t = dhan_core._options_table()
    for und in underlyings:
        px = spot.get(und)
        if not px:
            continue
        tu = t[(t["und"] == und) & (t["expiry"] >= today)]
        for exp in sorted(set(tu["expiry"]))[:n_exp]:
            te = tu[tu["expiry"] == exp]
            strikes = sorted(set(float(x) for x in te["strike"]))
            if not strikes:
                continue
            i = min(range(len(strikes)), key=lambda k: abs(strikes[k] - px))
            keep = set(strikes[max(0, i - width): i + width + 1])
            for _, r in te[te["strike"].astype(float).isin(keep)].iterrows():
                out.append(dhan_core._contract(r))
    return out


def run(acct: dict, base: Path, underlyings=DEFAULT_UNDERLYINGS, width: int = DEFAULT_WIDTH,
        n_exp: int = DEFAULT_EXPIRIES, stop=lambda: False, progress=None, post=None, spot: dict | None = None,
        today: dt.date | None = None, pause: float = 0.22) -> dict:
    """One archive pass. -> {'contracts', 'updated', 'rows', 'errors': [...], 'folder'}"""
    today = today or dt.date.today()
    unds = [u.strip().upper() for u in underlyings if u and u.strip()]
    refs = {}
    for u in unds:
        try:
            sid, seg = dhan_core.underlying_ref(u)
            refs[u] = (str(sid), seg, "INDEX" if seg == "IDX_I" else "EQUITY")
        except Exception:  # noqa: BLE001
            continue
    if spot is None:
        got = dhan_core.ltp(acct, [(seg, sid) for sid, seg, _i in refs.values()])
        spot = {u: got.get((seg, sid)) for u, (sid, seg, _i) in refs.items()}
    res = {"contracts": 0, "updated": 0, "rows": 0, "errors": [], "folder": str(base)}
    jobs = [(base / u / "spot_1m.parquet", sid, seg, inst, u) for u, (sid, seg, inst) in refs.items()]
    todo = plan(list(refs), spot, width, n_exp, today)
    jobs += [(contract_path(base, c), c["security_id"], c["segment"], c["instrument"], c["symbol"]) for c in todo]
    res["contracts"] = len(todo)
    index = load_index(base)
    for k, (path, sid, seg, inst, name) in enumerate(jobs):
        if stop():
            break
        try:
            added = update_one(acct, path, sid, seg, inst, today, post, pause)
            if added:
                res["updated"] += 1
                res["rows"] += added
            if added or name not in index:
                df = read(path)
                if len(df):
                    index[name] = {"file": str(path.relative_to(base)), "first": str(df.index[0]),
                                   "last": str(df.index[-1]), "rows": int(len(df))}
        except Exception as e:  # noqa: BLE001 - one contract must not stop the rest
            res["errors"].append(f"{name}: {str(e)[:120]}")
            if len(res["errors"]) >= 8 and res["updated"] == 0:
                break                                        # nothing works (login gone, no data plan): stop asking
        if progress:
            progress(k + 1, len(jobs))
        time.sleep(pause)                                    # Dhan's data API allows 5 requests a second
    try:
        tmp = base / "index.part"
        tmp.write_text(json.dumps(index, indent=1, sort_keys=True))
        os.replace(tmp, base / "index.json")
    except Exception as e:  # noqa: BLE001
        res["errors"].append(f"index: {e}")
    return res


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
    return read(base / hit["file"])
