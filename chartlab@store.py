"""ChartLab's data store. TradingView keeps every chart's history on its servers; ChartLab keeps it in a folder of
yours - and when that folder lives inside iCloud Drive / Google Drive / Dropbox / OneDrive, that service copies it
to the cloud by itself. Whatever a chart, the watchlist or the option archive downloads is added to the store and
never thrown away, so history only grows: a 1-minute chart that Yahoo gives 7 days of shows months once the store
has them, and an expired option contract still opens.

    <folder>/options/NIFTY/2026-11-24/NIFTY261124C25000.parquet     the option archive (optstore.py)
    <folder>/candles/RELIANCE/1m/2026-11.parquet                    intraday bars, one file per month
    <folder>/candles/RELIANCE/1h/2026.parquet                       hourly bars, one file per year
    <folder>/candles/RELIANCE/1d/all.parquet                        daily bars

Small files on purpose: a sync service uploads a whole file again whenever it changes, so only the current month's
file of a symbol is ever rewritten, and at most every quarter of an hour.

Nothing here talks to a broker or places anything: it only keeps what the loaders already fetched."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import data as datamod
from . import optstore
from . import timeframes as TF

COLS = ["Open", "High", "Low", "Close", "Volume"]
KEEP = ("1m", "2m", "3m", "5m", "15m", "30m", "1h", "1d")       # what a source serves directly: these are stored
MAX_BARS = 150_000              # the most bars a chart is handed (1-minute NSE bars: about a year and a half)
WRITE_EVERY = 900.0             # seconds between two writes of the same symbol's file (the daily job writes at once)
STATE = {"root": None}          # the options folder, once the running app has switched the store on
_lock = threading.RLock()
_mem: dict = {}                 # folder of one symbol + interval -> {"df", "dirty", "wrote"}
_built: dict = {}               # (1-minute folder, interval) -> (the 1-minute frame it was built from, bars)
_INDEX_NAME = {"^NSEI": "NIFTY", "^NSEBANK": "BANKNIFTY", "^BSESN": "SENSEX", "^INDIAVIX": "INDIAVIX"}


# ------------------------------------------------------------------ where it lives
def cloud_folders(home: Path | None = None) -> list:
    """The cloud-synced folders on this computer: [('iCloud Drive', Path), ('Google Drive', Path), ...]."""
    home = Path(home) if home else Path.home()
    out, seen = [], set()

    def add(name, p):
        try:
            if p.is_dir() and name not in seen:
                seen.add(name)
                out.append((name, p))
        except OSError:
            pass

    add("iCloud Drive", home / "Library" / "Mobile Documents" / "com~apple~CloudDocs")       # macOS
    add("iCloud Drive", home / "iCloudDrive")                                                # iCloud for Windows
    cs = home / "Library" / "CloudStorage"                  # macOS keeps every other sync service's folder here
    try:
        kids = sorted(cs.iterdir()) if cs.is_dir() else []
    except OSError:
        kids = []
    for d in kids:
        n = d.name
        if n.startswith("GoogleDrive"):
            mine = next((d / x for x in ("My Drive", "Mi unidad", "Meu Drive", "Mon Drive", "Meine Ablage")
                         if (d / x).is_dir()), d)
            add("Google Drive", mine)
        elif n.startswith("Dropbox"):
            add("Dropbox", d)
        elif n.startswith("OneDrive"):
            add("OneDrive", d)
        elif n.startswith("Box"):
            add("Box", d)
    for name, rel in (("Google Drive", "Google Drive"), ("Google Drive", "My Drive"), ("Dropbox", "Dropbox"),
                      ("OneDrive", "OneDrive")):
        add(name, home / rel)
    one = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    if one:
        add("OneDrive", Path(one))
    if sys.platform == "win32":                             # Google Drive for desktop mounts itself as a drive
        for letter in "GHIJKLMNOPQRSTUVWXYZ":
            add("Google Drive", Path(f"{letter}:/My Drive"))
    order = ["iCloud Drive", "Google Drive", "Dropbox", "OneDrive", "Box"]
    return sorted(out, key=lambda x: order.index(x[0]))


def cloud_of(path) -> str:
    """'iCloud Drive' / 'Google Drive' / ... when this folder is inside a synced one, else ''."""
    try:
        p = Path(path).expanduser().resolve()
    except OSError:
        return ""
    for name, base in cloud_folders():
        try:
            b = base.resolve()
            if p == b or b in p.parents:
                return name
        except OSError:
            continue
    return ""


def _writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write_test"
        probe.write_text("ok")
        probe.unlink()
        return True
    except Exception:  # noqa: BLE001 - no permission, service not running, read-only
        return False


def cloud_target(base: Path) -> Path:
    return Path(base) / "ChartLab" / "options"


def pick_cloud(home: Path | None = None):
    """The first cloud folder that can be written: (service name, <cloud>/ChartLab/options), or None."""
    for name, base in cloud_folders(home):
        target = cloud_target(base)
        if _writable(target):
            return name, target
    return None


def candles_root(options_root) -> Path:
    """Chart data sits next to the option archive: .../ChartLab/options -> .../ChartLab/candles."""
    p = Path(options_root)
    return p.parent / "candles" if p.name.lower() == "options" else p / "_candles"


def move_into(src: Path, dst: Path) -> int:
    """Bring the files of an older store folder into the new one (what the new one already has is kept).
    -> files moved. The old folder is removed when nothing is left in it."""
    src, dst = Path(src), Path(dst)
    if not src.is_dir() or src.resolve() == dst.resolve():
        return 0
    n = 0
    for f in sorted(src.rglob("*")):
        if not f.is_file() or f.name.startswith(".") or f.suffix == ".part":
            continue
        t = dst / f.relative_to(src)
        if t.exists():
            continue
        try:
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, t)
            f.unlink()
            n += 1
        except Exception:  # noqa: BLE001 - that file stays where it was
            logging.exception("store: could not move %s", f)
    try:
        if not any(p.is_file() and not p.name.startswith(".") for p in src.rglob("*")):
            shutil.rmtree(src, ignore_errors=True)
    except OSError:
        pass
    return n


def folder_size(path) -> tuple:
    """(bytes, files) of everything under a folder."""
    total = files = 0
    try:
        for f in Path(path).rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
                    files += 1
            except OSError:
                continue
    except OSError:
        pass
    return total, files


def nice_size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit in ("bytes", "KB") else f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n:,.1f} GB"


def nice_path(path) -> str:
    """A folder as a person says it: 'iCloud Drive > ChartLab', '~/ChartLab'."""
    p = Path(path)
    for name, base in cloud_folders():
        try:
            rel = p.resolve().relative_to(base.resolve())
            return " › ".join([name] + list(rel.parts))
        except (ValueError, OSError):
            continue
    try:
        return "~/" + str(p.resolve().relative_to(Path.home().resolve()))
    except (ValueError, OSError):
        return str(p)


def ensure_local(path: Path, wait: float = 8.0) -> bool:
    """iCloud may keep only a placeholder of an old file on this Mac ('.name.icloud'): ask for the real file."""
    path = Path(path)
    if path.exists():
        return True
    stub = path.parent / f".{path.name}.icloud"
    if not stub.exists():
        return False
    try:
        subprocess.run(["brctl", "download", str(path)], capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        return False
    end = time.time() + wait
    while time.time() < end:
        if path.exists():
            return True
        time.sleep(0.25)
    return path.exists()


# ------------------------------------------------------------------ switching it on
def enable(options_root) -> Path:
    """The running app switches the store on (tests and scripts that never call this leave no files behind)."""
    with _lock:
        root = Path(options_root)
        if STATE["root"] is not None and Path(STATE["root"]) != root:
            flush()
            _mem.clear()
            _built.clear()
        STATE["root"] = root
    return root


def disable():
    with _lock:
        flush()
        _mem.clear()
        _built.clear()
        STATE["root"] = None


def active() -> bool:
    return STATE["root"] is not None


def _croot(root=None):
    r = root if root is not None else STATE["root"]
    return None if r is None else candles_root(r)


# ------------------------------------------------------------------ names and files
def key_for(symbol: str) -> str:
    """One folder name per instrument, whatever way it was typed: RELIANCE / NSE:RELIANCE / RELIANCE.NS -> RELIANCE,
    BSE:TCS -> BSE_TCS, NIFTY / ^NSEI -> NIFTY, US:AAPL -> US_AAPL, BTC-USD -> BTC-USD."""
    s = str(symbol).strip().upper().replace(" ", "")
    ex = s.split(":", 1)[0] if ":" in s else ""
    y = datamod.normalize_symbol(s)
    if y in _INDEX_NAME:
        k = _INDEX_NAME[y]
    elif y.endswith(".NS"):
        k = y[:-3]
    elif y.endswith(".BO"):
        k = "BSE_" + y[:-3]
    elif ex and ex not in ("NSE", "BSE"):
        k = f"{ex}_{y}"
    else:
        k = y.lstrip("^")
    return re.sub(r"[^A-Z0-9&_.\-]", "_", k) or "_"


def _part_names(index: pd.DatetimeIndex, interval: str) -> np.ndarray:
    n, u = TF.split(interval)
    if u == "m":
        return index.strftime("%Y-%m").to_numpy()
    if u == "h":
        return index.strftime("%Y").to_numpy()
    return np.full(len(index), "all", dtype=object)


def _dir(root: Path, key: str, interval: str) -> Path:
    return Path(root) / key / interval


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or not len(df):
        return pd.DataFrame(columns=COLS)
    out = df[[c for c in COLS if c in df.columns]].copy()
    if "Volume" not in out.columns:
        out["Volume"] = 0.0
    out = out[COLS].astype("float64")
    idx = pd.DatetimeIndex(out.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    out.index = idx
    out = out[~out.index.isna()].dropna(subset=["Open", "High", "Low", "Close"])
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out.index.name = "Date"
    return out


def _read_dir(d: Path) -> pd.DataFrame:
    frames = []
    try:
        files = sorted(d.glob("*.parquet")) if d.is_dir() else []
        for stub in (d.glob(".*.parquet.icloud") if d.is_dir() else []):     # only a placeholder is on this Mac
            real = d / stub.name[1:-len(".icloud")]
            if ensure_local(real) and real not in files:
                files.append(real)
    except OSError:
        files = []
    for f in sorted(files):
        try:
            x = pd.read_parquet(f)
            if "Date" in x.columns:
                x = x.set_index("Date")
            frames.append(x)
        except Exception:  # noqa: BLE001 - a half-synced file: the rest is still good
            logging.warning("store: could not read %s", f)
    if not frames:
        return pd.DataFrame(columns=COLS)
    return _clean(pd.concat(frames))


def read(symbol: str, interval: str, root=None) -> pd.DataFrame:
    """Everything stored for this symbol at exactly this interval (empty when nothing is)."""
    cr = _croot(root)
    if cr is None:
        return pd.DataFrame(columns=COLS)
    d = _dir(cr, key_for(symbol), interval)
    with _lock:
        hit = _mem.get(str(d))
        if hit is None:
            hit = _mem[str(d)] = {"df": _read_dir(d), "dirty": set(), "wrote": 0.0}
        return hit["df"]


def _rescaled(old: pd.DataFrame, new: pd.DataFrame):
    """A split, a bonus or a dividend adjustment changed the source's old prices: bring the stored older bars onto
    the new scale (what the data vendor did to its own history). -> (old, rescaled or not)."""
    common = old.index.intersection(new.index)
    if len(common) < 5 or common[0] <= old.index[0]:
        return old, False
    head = common[:40]
    a, b = old.loc[head, "Close"].to_numpy(float), new.loc[head, "Close"].to_numpy(float)
    ok = (a > 0) & (b > 0)
    if ok.sum() < 5:
        return old, False
    ratios = b[ok] / a[ok]
    r = float(np.median(ratios))
    if not (0.02 < r < 50) or abs(r - 1.0) < 0.002:
        return old, False
    if float(np.mean(np.abs(ratios - r) <= max(0.002, 0.15 * abs(r - 1.0)))) < 0.8:
        return old, False                                    # not one clean factor: leave the stored bars alone
    out = old.copy()
    older = out.index < common[0]
    out.loc[older, ["Open", "High", "Low", "Close"]] *= r
    if abs(r - 1.0) > 0.05:                                  # a split / bonus also changes the share count
        out.loc[older, "Volume"] /= r
    return out, True


def merge(symbol: str, interval: str, df: pd.DataFrame, force: bool = False, root=None) -> int:
    """Add bars to the store (newer numbers for the same bar replace the stored ones). -> bars that were new."""
    cr = _croot(root)
    if cr is None or interval not in KEEP:
        return 0
    new = _clean(df)
    if not len(new):
        return 0
    d = _dir(cr, key_for(symbol), interval)
    with _lock:
        old = read(symbol, interval, root)
        hit = _mem[str(d)]
        dirty = hit["dirty"]
        if len(old):
            old2, scaled = _rescaled(old, new)
            if scaled:
                dirty.update(_part_names(old2.index[old2.index < new.index[0]], interval))
                old = old2
            same = old.reindex(new.index)
            diff = ((same[COLS] - new[COLS]).abs() > 1e-9).any(axis=1) | same["Close"].isna()
            if not diff.any() and not scaled:
                if force and dirty:
                    _write(d, hit, interval)
                return 0
            added = int(same["Close"].isna().sum())
            dirty.update(_part_names(new.index[diff.to_numpy()], interval))
            both = pd.concat([old[~old.index.isin(new.index)], new]).sort_index()
        else:
            both, added = new, len(new)
            dirty.update(_part_names(new.index, interval))
        both.index.name = "Date"
        hit["df"] = both
        if force or time.time() - hit["wrote"] >= WRITE_EVERY:      # (the first write of a session is at once)
            _write(d, hit, interval)
        return added


def _write(d: Path, hit: dict, interval: str):
    df, dirty = hit["df"], hit["dirty"]
    if not dirty or not len(df):
        dirty.clear()
        return
    try:
        d.mkdir(parents=True, exist_ok=True)
        names = _part_names(df.index, interval)
        for part in sorted(dirty):
            rows = df[names == part]
            if not len(rows):
                continue
            tmp = d / f"{part}.part"
            rows.to_parquet(tmp)
            os.replace(tmp, d / f"{part}.parquet")           # never a half-written file, also in a synced folder
        dirty.clear()
        hit["wrote"] = time.time()
    except Exception:  # noqa: BLE001 - the folder went away (drive unplugged, service stopped): try again later
        logging.exception("store: could not write %s", d)


def flush(root=None):
    """Write whatever is still waiting (the app is closing, or the folder is about to change)."""
    with _lock:
        for path, hit in list(_mem.items()):
            if hit["dirty"]:
                _write(Path(path), hit, Path(path).name)


# ------------------------------------------------------------------ reading history back
def history(symbol: str, interval: str, root=None) -> pd.DataFrame:
    """All stored bars of this interval: the ones stored as such, and - for minutes / hours - older ones built
    from the stored 1-minute bars (so one saved 1-minute history feeds every intraday chart)."""
    direct = read(symbol, interval, root)
    try:
        n, u = TF.split(interval)
    except ValueError:
        return direct
    if u not in ("m", "h") or interval == "1m":
        return direct
    one = read(symbol, "1m", root)
    if not len(one):
        return direct
    with _lock:
        ck = (str(_dir(_croot(root), key_for(symbol), "1m")), interval)
        hit = _built.get(ck)
        if hit is None or hit[0] is not one:                 # built once per change of the 1-minute history
            built = _clean(TF.resample(one, interval))
            _built[ck] = hit = (one, built)
        built = hit[1]
    if not len(direct):
        return built
    if not len(built) or built.index[0] >= direct.index[0]:
        return direct                                        # nothing older than what is stored as such
    return pd.concat([built[built.index < direct.index[0]], direct])


def _window(df: pd.DataFrame, period: str) -> pd.DataFrame:
    days = datamod._PERIOD_DAYS.get(str(period))
    if days and days < 10 ** 5 and len(df):
        df = df[df.index >= df.index[-1].normalize() - pd.Timedelta(days=days)]
    return df.iloc[-MAX_BARS:] if len(df) > MAX_BARS else df


def extend(symbol: str, interval: str, period: str, fresh: pd.DataFrame, root=None) -> pd.DataFrame:
    """The bars a chart gets: what the source just gave, with the stored older bars in front of it (as far back
    as the chosen range asks for). The fresh bars are added to the store on the way."""
    if _croot(root) is None or interval not in KEEP or fresh is None or not len(fresh):
        return fresh
    merge(symbol, interval, fresh, root=root)
    hist = history(symbol, interval, root)
    new = _clean(fresh)
    older = hist[hist.index < new.index[0]]
    if not len(older):
        return fresh
    out = pd.concat([older[COLS], new[COLS]])
    out.index.name = fresh.index.name or "Date"
    out = _window(out, period)
    out.attrs.update(getattr(fresh, "attrs", {}) or {})
    return out


def keep(symbol: str, interval: str, period: str, df, label: str):
    """What every chart loader's answer goes through: -> (bars with the stored history in front, label)."""
    if not active() or df is None or interval not in KEEP:
        return df, label
    try:
        from .dhan_core import is_option
        if is_option(symbol):                                # contracts live in the option archive
            return df, label
        return extend(symbol, interval, period, df), label
    except Exception:  # noqa: BLE001 - the store must never stand between a chart and its data
        logging.exception("store: keep %s %s", symbol, interval)
        return df, label


def saved(symbol: str, interval: str, period: str):
    """The stored bars alone - shown at once while fresh ones load, and instead of them when no source answers.
    -> (DataFrame, label) or None."""
    if not active():
        return None
    try:
        from .dhan_core import is_option
        if is_option(symbol):
            return option_bars(symbol, interval)
        if interval in KEEP:
            df = history(symbol, interval)
        elif TF.valid(interval):                             # 45m, 4h ...: built from the stored base
            df = history(symbol, TF.base_for(interval))
            df = TF.resample(df, interval) if len(df) else df
        else:
            return None
        df = _window(df, period)
        if not len(df):
            return None
        return df, f"{key_for(symbol)} · {interval}"
    except Exception:  # noqa: BLE001
        logging.exception("store: saved %s %s", symbol, interval)
        return None


def option_bars(symbol: str, interval: str, root=None):
    """A contract from the option archive at any interval (expired ones too). -> (DataFrame, label) or None."""
    base = Path(root) if root is not None else STATE["root"]
    if base is None:
        return None
    try:
        df = optstore.load(symbol, base)
    except Exception:  # noqa: BLE001 - not in the archive
        return None
    if not len(df):
        return None
    name = str(getattr(df, "attrs", {}).get("symbol") or symbol).strip().upper()
    bars = _clean(df)
    if interval != "1m" and TF.valid(interval):
        n, u = TF.split(interval)
        if u in ("m", "h"):
            bars = TF.resample(bars, interval)
        else:
            day = bars.groupby(bars.index.normalize()).agg(
                {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
            day.index.name = "Date"
            bars = day if interval == "1d" else TF.resample(day, interval)
    if not len(bars):
        return None
    return bars, f"{name} · {interval} · saved"


def catalog(root=None) -> dict:
    """What chart data is stored: {symbol folder: {interval: files}} (read from the folder names only)."""
    cr = _croot(root)
    out: dict = {}
    if cr is None or not cr.is_dir():
        return out
    try:
        for k in sorted(p for p in cr.iterdir() if p.is_dir()):
            ivs = {iv.name: len(list(iv.glob("*.parquet"))) for iv in sorted(k.iterdir()) if iv.is_dir()}
            ivs = {a: b for a, b in ivs.items() if b}
            if ivs:
                out[k.name] = ivs
    except OSError:
        pass
    return out


def save_watchlist(symbols, loader, stop=lambda: False, progress=None, intraday_range: str = "3mo",
                   root=None, pause: float = 0.2) -> dict:
    """The daily job: every watchlist symbol's daily and 1-minute bars go into the store.
    loader(symbol, interval, range) -> (DataFrame, label). -> {'symbols', 'saved', 'bars', 'errors': [...]}"""
    res = {"symbols": len(symbols), "saved": 0, "bars": 0, "errors": []}
    if _croot(root) is None:
        return res
    from .dhan_core import is_option
    for k, s in enumerate(symbols):
        if stop():
            break
        if is_option(s):
            continue
        good = False
        for iv, rng in (("1d", "2y"), ("1m", intraday_range)):
            if stop():
                break
            try:
                df, _label = loader(s, iv, rng)
                res["bars"] += merge(s, iv, df, force=True, root=root)
                good = True
            except Exception as e:  # noqa: BLE001 - one symbol or interval must not stop the rest
                res["errors"].append(f"{s} {iv}: {str(e)[:100]}")
        res["saved"] += 1 if good else 0
        if progress:
            progress(k + 1, len(symbols))
        if pause:
            time.sleep(pause)                                # a source that is asked too fast stops answering
    return res
