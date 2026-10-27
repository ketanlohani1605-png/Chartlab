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


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate to a bigger timeframe, e.g. '15min', '1h', 'D', 'W-FRI', 'ME'."""
    out = df.resample(rule).agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    )
    return out.dropna(subset=["Open", "Close"])


# --------------------------------------------------------------------------- local files
def load_file(path: str | Path) -> tuple[pd.DataFrame, str]:
    p = Path(path)
    if p.suffix.lower() in (".parquet", ".pq"):
        raw = pd.read_parquet(p)
    else:
        raw = pd.read_csv(p, sep=None, engine="python")
    return standardise(raw), p.name


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
    raw = yf.Ticker(sym).history(period=period, interval=interval, auto_adjust=True)
    if raw is None or raw.empty:
        raise ValueError(f"No data returned for '{sym}' ({interval}, {period}). Check the symbol / internet.")
    return standardise(raw), f"{sym} · {interval}"


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
