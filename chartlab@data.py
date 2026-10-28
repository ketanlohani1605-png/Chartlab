"""Data loading for ChartLab.

Three kinds of sources:
  * Local files   - CSV / TSV / TXT / Parquet with Date, Open, High, Low, Close, Volume columns
  * Yahoo Finance - NSE (.NS), BSE (.BO), indices, US stocks, crypto, forex (via yfinance)
  * Providers     - any python file in ``providers/`` exposing ``fetch(symbol, interval, period)``
                    (use this to plug in Zerodha / Angel One / Upstox / Binance / your own API)

Every loader returns a DataFrame with a DatetimeIndex and float columns
Open, High, Low, Close, Volume (sorted, de-duplicated, timezone-naive).
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

OHLCV = ["Open", "High", "Low", "Close", "Volume"]
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
USER_DIR = Path.home() / "ChartLab"

_TIME_NAMES = ("datetime", "date", "time", "timestamp", "date_time", "dt")
_ALIASES = {
    "open": "Open", "o": "Open",
    "high": "High", "h": "High",
    "low": "Low", "l": "Low",
    "close": "Close", "c": "Close",
    "volume": "Volume", "vol": "Volume", "v": "Volume",
}


# --------------------------------------------------------------------------- cleaning
def standardise(raw: pd.DataFrame) -> pd.DataFrame:
    """Turn almost any OHLCV table into ChartLab's canonical format."""
    df = raw.copy()
    df.columns = [str(c).strip() for c in df.columns]
    lower = {c.lower(): c for c in df.columns}

    if "date" in lower and "time" in lower:  # broker exports with separate columns
        idx = pd.to_datetime(
            df[lower["date"]].astype(str) + " " + df[lower["time"]].astype(str), errors="coerce"
        )
        df = df.drop(columns=[lower["date"], lower["time"]])
        df.index = pd.DatetimeIndex(idx)
    else:
        time_col = next((lower[k] for k in _TIME_NAMES if k in lower), None)
        if time_col is not None:
            s = df[time_col]
            if pd.api.types.is_numeric_dtype(s):
                unit = "ms" if float(s.iloc[0]) > 1e11 else "s"
                idx = pd.to_datetime(s, unit=unit)
            else:
                idx = pd.to_datetime(s, errors="coerce", utc=False)
            df = df.drop(columns=[time_col])
            df.index = pd.DatetimeIndex(idx)
        elif not isinstance(df.index, pd.DatetimeIndex):
            raise ValueError(
                "No date/time column found. Expected a column named Date, Datetime, Time or Timestamp."
            )

    rename, taken = {}, set()
    for c in df.columns:
        target = _ALIASES.get(c.lower())
        if target and target not in taken:
            rename[c] = target
            taken.add(target)
    df = df.rename(columns=rename)

    missing = [c for c in ("Open", "High", "Low", "Close") if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns: {', '.join(missing)}. Found: {', '.join(map(str, df.columns))}")
    if "Volume" not in df.columns:
        df["Volume"] = 0.0

    df = df[OHLCV].apply(pd.to_numeric, errors="coerce")
    df = df[~df.index.isna()]
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    if df.index.tz is not None:  # keep exchange wall-clock time
        df.index = df.index.tz_localize(None)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df["Volume"] = df["Volume"].fillna(0.0)
    if df.empty:
        raise ValueError("No valid rows after cleaning the data.")
    df.index.name = "Date"
    return df.astype(float)


# --------------------------------------------------------------------------- timeframes
# Timeframe codes used everywhere in the app:  1m 2m 3m 5m 10m 15m 30m 1h 2h 4h  1d  1wk  1mo
_TF_RE = re.compile(r"^(\d+)\s*(m|min|h|d|w|wk|mo)$", re.I)


def parse_timeframe(tf: str) -> tuple[str, int]:
    """'3m' -> ('min', 3); '4h' -> ('min', 240); '1d' -> ('day', 1); '1wk' -> ('week', 1); '1mo' -> ('month', 1)."""
    t = str(tf).strip().lower()
    t = {"d": "1d", "w": "1wk", "m": "1mo", "day": "1d", "week": "1wk", "month": "1mo"}.get(t, t)
    m = _TF_RE.match(t)
    if not m:
        raise ValueError(f"Bad timeframe '{tf}'. Use e.g. 1m, 3m, 15m, 1h, 4h, 1d, 1wk, 1mo")
    n, unit = int(m.group(1)), m.group(2).lower()
    if n < 1:
        raise ValueError(f"Bad timeframe '{tf}'")
    if unit in ("m", "min"):
        return "min", n
    if unit == "h":
        return "min", n * 60
    if unit == "d":
        return "day", n
    if unit in ("w", "wk"):
        return "week", n
    return "month", n


def timeframe_minutes(tf: str) -> float:
    kind, n = parse_timeframe(tf)
    return {"min": n, "day": n * 1440, "week": n * 10080, "month": n * 43200}[kind]


def normalize_tf(tf: str) -> str:
    kind, n = parse_timeframe(tf)
    if kind == "min":
        return f"{n // 60}h" if n % 60 == 0 and n >= 60 else f"{n}m"
    return {"day": f"{n}d", "week": f"{n}wk", "month": f"{n}mo"}[kind]


def detect_base_minutes(df: pd.DataFrame) -> float:
    """Typical bar length of a dataset in minutes (1440 = daily)."""
    idx = df.index
    if len(idx) < 3:
        return 1440.0
    sample = idx[: min(len(idx), 5000)]
    d = np.diff(sample.values.astype("datetime64[s]").astype("int64"))
    d = d[d > 0]
    if len(d) == 0:
        return 1440.0
    return float(max(np.median(d) / 60.0, 1.0 / 60))


def resample_ohlcv(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Aggregate to a bigger timeframe. Intraday bars are aligned to the session open (09:15 for NSE, ...)."""
    kind, n = parse_timeframe(tf)
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    if kind == "min":
        tod = (df.index - df.index.normalize())
        offset = tod.min()
        out = df.resample(f"{n}min", origin="start_day", offset=offset).agg(agg)
    else:
        rule = {"day": f"{n}D", "week": f"W-FRI" if n == 1 else f"{n}W-FRI", "month": f"{n}ME"}[kind]
        out = df.resample(rule).agg(agg)
    out = out.dropna(subset=["Open", "Close"])
    out["Volume"] = out["Volume"].fillna(0.0)
    out.index.name = "Date"
    return out


def to_timeframe(df: pd.DataFrame, tf: str) -> tuple[pd.DataFrame, str]:
    """Convert data to timeframe ``tf`` if it is coarser than the data. Returns (data, note)."""
    base = detect_base_minutes(df)
    want = timeframe_minutes(tf)
    if want <= base * 1.01:
        if want < base * 0.99:
            return df, f"data is {base:g}-minute, cannot show {tf}"
        return df, ""
    return resample_ohlcv(df, tf), ""


# --------------------------------------------------------------------------- local files
DATA_SUFFIXES = (".parquet", ".pq", ".csv", ".tsv", ".txt")


def _read_one(p: Path) -> pd.DataFrame:
    if p.suffix.lower() in (".parquet", ".pq"):
        raw = pd.read_parquet(p)
    else:
        raw = pd.read_csv(p, sep=None, engine="python")
    return standardise(raw)


def list_data_files(folder: str | Path) -> list[Path]:
    files = [f for f in Path(folder).rglob("*") if f.is_file() and f.suffix.lower() in DATA_SUFFIXES
             and not f.name.startswith(".")]
    return sorted(files)


def load_file(path: str | Path, progress=None) -> tuple[pd.DataFrame, str]:
    """Load one CSV/Parquet file, or every data file inside a folder (e.g. one parquet per day) as one series."""
    p = Path(path).expanduser()
    if p.is_dir():
        files = list_data_files(p)
        if not files:
            raise ValueError(f"No .parquet / .csv files found in {p}")
        parts, bad = [], []
        for i, f in enumerate(files):
            try:
                parts.append(_read_one(f))
            except Exception as e:  # noqa: BLE001 - skip unreadable files but report how many
                bad.append(f"{f.name}: {e}")
            if progress and (i % 25 == 0 or i == len(files) - 1):
                progress(i + 1, len(files))
        if not parts:
            raise ValueError("None of the files could be read. First error: " + (bad[0] if bad else "?"))
        df = pd.concat(parts)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        note = f"{p.name} ({len(parts)} files" + (f", {len(bad)} skipped" if bad else "") + ")"
        return df, note
    return _read_one(p), p.name


# --------------------------------------------------------------------------- Yahoo Finance
_INDEX = {
    "NIFTY": "^NSEI", "NIFTY50": "^NSEI", "BANKNIFTY": "^NSEBANK", "NIFTYBANK": "^NSEBANK",
    "SENSEX": "^BSESN", "INDIAVIX": "^INDIAVIX",
}
_CRYPTO = re.compile(r"[A-Z0-9]+-(USD|USDT|EUR|INR|GBP|BTC|ETH)")

_PERIOD_DAYS = {"1d": 1, "5d": 5, "1mo": 30, "3mo": 90, "6mo": 180, "1y": 365,
                "2y": 730, "5y": 1825, "10y": 3650, "max": 10**6}
_INTRADAY_LIMIT = {"1m": 7, "2m": 60, "5m": 60, "15m": 60, "30m": 60, "60m": 730, "90m": 60, "1h": 730}


def normalize_symbol(text: str) -> str:
    """RELIANCE -> RELIANCE.NS, BSE:TCS -> TCS.BO, NIFTY -> ^NSEI, US:AAPL -> AAPL, BTC-USD -> BTC-USD."""
    s = text.strip().upper().replace(" ", "")
    ex = None
    if ":" in s:
        ex, s = s.split(":", 1)
    if s in _INDEX:
        return _INDEX[s]
    if ex == "BSE":
        return s + ".BO"
    if ex == "NSE":
        return s + ".NS"
    if ex:
        return s
    if _CRYPTO.fullmatch(s) or any(ch in s for ch in ".^="):
        return s
    return s + ".NS"


def clamp_period(interval: str, period: str) -> str:
    """Yahoo limits how far back intraday data goes; pick the largest allowed period."""
    limit = _INTRADAY_LIMIT.get(interval)
    if limit is None or _PERIOD_DAYS.get(period, 0) <= limit:
        return period
    ok = [p for p, d in _PERIOD_DAYS.items() if d <= limit]
    return max(ok, key=lambda p: _PERIOD_DAYS[p])


def load_yahoo(symbol: str, interval: str = "1d", period: str = "2y") -> tuple[pd.DataFrame, str]:
    try:
        import yfinance as yf
    except ImportError as e:  # pragma: no cover
        raise ImportError("yfinance is not installed. Run: pip install yfinance") from e
    sym = normalize_symbol(symbol)
    period = clamp_period(interval, period)
    cache = _cache_file(sym, interval, period)
    try:
        raw = yf.Ticker(sym).history(period=period, interval=interval, auto_adjust=True)
        if raw is None or raw.empty:
            raise ValueError(f"No data returned for '{sym}' ({interval}, {period}). Check the symbol / internet.")
        df = standardise(raw)
    except Exception:  # noqa: BLE001 - fall back to the last good download
        if cache.exists():
            return pd.read_parquet(cache), f"{sym} · {interval} (offline copy)"
        raise
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cache)
    except Exception:  # noqa: BLE001 - cache is best-effort
        pass
    return df, f"{sym} · {interval}"


def _cache_file(sym: str, interval: str, period: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{sym}_{interval}_{period}")
    return USER_DIR / "cache" / f"{safe}.parquet"


# --------------------------------------------------------------------------- custom providers
def provider_dirs() -> list[Path]:
    return [USER_DIR / "providers", ROOT / "providers"]


def list_providers() -> list[str]:
    names = set()
    for d in provider_dirs():
        if d.exists():
            names.update(p.stem for p in d.glob("*.py") if not p.name.startswith("_"))
    return sorted(names)


def load_provider(name: str, symbol: str, interval: str, period: str) -> tuple[pd.DataFrame, str]:
    for d in provider_dirs():
        f = d / f"{name}.py"
        if f.exists():
            spec = importlib.util.spec_from_file_location(f"chartlab_provider_{name}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if not hasattr(mod, "fetch"):
                raise ValueError(f"Provider '{name}' must define fetch(symbol, interval, period).")
            return standardise(mod.fetch(symbol, interval, period)), f"{symbol} · {interval} · {name}"
    raise FileNotFoundError(f"Provider '{name}' not found in {', '.join(map(str, provider_dirs()))}")
