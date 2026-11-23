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
import os
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

OHLCV = ["Open", "High", "Low", "Close", "Volume"]
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
USER_DIR = Path.home() / "ChartLab"


def use_certifi() -> str:
    """Python's own HTTPS (urllib: the Dhan API, the brokers, live prices) checks certificates against a store that
    a built app does not have - the paths it knows are the build machine's. Point it at the certificate bundle that
    ships inside the app (certifi, the one 'requests' uses). Without this every such request fails with
    'certificate verify failed' in the built app while the same code works from source.
    -> 'env' (already set), 'system' (the computer's own store works), 'certifi', or '' (nothing could be done)."""
    import ssl
    cur = os.environ.get("SSL_CERT_FILE", "")
    if cur and os.path.exists(cur):
        return "env"
    try:
        if not getattr(sys, "frozen", False):
            v = ssl.get_default_verify_paths()
            if (v.cafile and os.path.exists(v.cafile)) or (v.capath and os.path.isdir(v.capath) and os.listdir(v.capath)):
                return "system"
        import certifi
        f = certifi.where()
        if os.path.exists(f):
            os.environ["SSL_CERT_FILE"] = f
            return "certifi"
    except Exception:  # noqa: BLE001 - leave things as they are
        pass
    return ""


CERTS = use_certifi()             # (before anything opens a connection)

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
        if time_col is None and len(df.columns) and (str(df.columns[0]).startswith("Unnamed") or not df.columns[0]):
            first = pd.to_datetime(df.iloc[:, 0].head(20), errors="coerce")     # a saved index without a name
            if first.notna().all():
                time_col = df.columns[0]
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
def _index_map() -> dict:
    """Every index name -> Yahoo's ticker for it (from chartlab/indices.py: NIFTY IT -> ^CNXIT ...)."""
    from . import indices as _ix
    out = {}
    for k, ix in _ix._BY.items():
        out[k] = ix.yahoo
    return out


_INDEX = _index_map()
_CRYPTO = re.compile(r"[A-Z0-9]+-(USD|USDT|EUR|INR|GBP|BTC|ETH)")

_PERIOD_DAYS = {"1d": 1, "5d": 5, "1mo": 30, "3mo": 90, "6mo": 180, "1y": 365,
                "2y": 730, "5y": 1825, "10y": 3650, "max": 10**6}
_INTRADAY_LIMIT = {"1m": 7, "2m": 60, "5m": 60, "15m": 60, "30m": 60, "60m": 730, "90m": 60, "1h": 730}
# how far back Yahoo goes at all: 1-minute bars for the last 30 days, asked for in pieces of up to 7 days
_INTRADAY_REACH = {"1m": 29}


RENAMED = {"TATAMOTORS": "TMPV"}       # NSE symbols that changed (Tata Motors demerger, Oct 2025) ...


def _all_renames() -> None:
    """... and every other change NSE lists whose old symbol no longer trades (ZOMATO -> ETERNAL, MCDOWELL-N ->
    UNITDSPR, GMRINFRA -> GMRAIRPORT ...: an old symbol would otherwise load the old, frozen chart)."""
    try:
        from .symbols import renamed
        for k, v in renamed().items():
            RENAMED.setdefault(k, v)
    except Exception:  # noqa: BLE001
        pass


_all_renames()


def normalize_symbol(text: str) -> str:
    """RELIANCE -> RELIANCE.NS, BSE:TCS -> TCS.BO, NIFTY -> ^NSEI, US:AAPL -> AAPL, BTC-USD -> BTC-USD."""
    s = text.strip().upper().replace(" ", "")
    ex = None
    if ":" in s:
        ex, s = s.split(":", 1)
    if ex in (None, "NSE", "BSE"):
        s = RENAMED.get(s, s)
    if ex in (None, "NSE", "BSE", "INDEX"):
        from . import indices as _ix
        hit = _ix.get(s)
        if hit is not None:
            return hit.yahoo
    if ex == "BSE":
        return s + ".BO"
    if ex == "NSE":
        return s + ".NS"
    if ex:
        return s
    if _CRYPTO.fullmatch(s) or any(ch in s for ch in ".^="):
        return s
    return s + ".NS"


def period_days(period: str) -> int:
    """'2y' -> 730, '60d' -> 60 (0: unknown)."""
    p = str(period)
    if p in _PERIOD_DAYS:
        return _PERIOD_DAYS[p]
    if p.endswith("d") and p[:-1].isdigit():
        return int(p[:-1])
    return 0


def clamp_period(interval: str, period: str) -> str:
    """Yahoo limits how far back intraday data goes: the furthest it allows, but no further than asked
    (5-minute bars: 60 days, not the 1 month this used to stop at; 1-minute bars: 29 days)."""
    reach = _INTRADAY_REACH.get(interval) or _INTRADAY_LIMIT.get(interval)
    if reach is None or period_days(period) <= reach:
        return period
    same = [p for p, d in _PERIOD_DAYS.items() if d == reach]
    return same[0] if same else f"{reach}d"


def _yahoo_1m(yf, sym: str, days: int):
    """Up to 29 days of 1-minute bars: Yahoo gives at most 7 days per request, so the days come in pieces, asked
    for all at once (half a second for a month). The newest piece must come; an older one that fails is left out."""
    from concurrent.futures import ThreadPoolExecutor
    now = pd.Timestamp.now(tz="UTC").floor("min")
    spans, d = [], 0
    while d < days:
        w = min(7, days - d)
        end = now + pd.Timedelta(days=1) if d == 0 else now - pd.Timedelta(days=d)
        spans.append((now - pd.Timedelta(days=d + w), end))
        d += w

    def get(span):
        a, b = span
        return yf.Ticker(sym).history(start=a.to_pydatetime(), end=b.to_pydatetime(), interval="1m",
                                      auto_adjust=ADJUST_DIVIDENDS)

    with ThreadPoolExecutor(max_workers=min(5, len(spans))) as ex:
        futs = [ex.submit(get, sp) for sp in spans]
        parts = [futs[0].result()]                          # (the newest: its error goes up)
        for f in futs[1:]:
            try:
                parts.append(f.result())
            except Exception:  # noqa: BLE001 - an older piece missing only means less history
                pass
    parts = [x for x in parts if x is not None and len(x)]
    if not parts:
        return pd.DataFrame()
    raw = pd.concat(parts[::-1]).sort_index()
    return raw[~raw.index.duplicated(keep="last")]


def drop_frozen(df: pd.DataFrame) -> pd.DataFrame:
    """Remove bars that are not trades but a stuck feed: open = high = low = close, no volume, and the same price
    as the bar before or after. Yahoo sends NIFTY's last 15 minutes like that (one price repeated each minute) and
    holidays as a copy of the previous close. TradingView simply has no bar there - now neither do we."""
    if df is None or len(df) < 3:
        return df
    o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    v = df["Volume"].to_numpy(float) if "Volume" in df else np.zeros(len(df))
    flat = (o == h) & (h == l) & (l == c) & ~(v > 0)
    prev_c = np.r_[np.nan, c[:-1]]
    next_c = np.r_[c[1:], np.nan]
    next_flat = np.r_[flat[1:], False]
    frozen = flat & ((c == prev_c) | (next_flat & (c == next_c)))
    return df[~frozen] if frozen.any() else df


def fill_last_session(raw: pd.DataFrame, meta: dict) -> bool:
    """Yahoo leaves the newest daily bar unfinished for hours after the close (over a weekend, all weekend): its
    open, high, low and volume are there, its close is empty - and a bar without a close is dropped, so Friday went
    missing from every daily chart. The chart's meta has that session's last price (the close), high, low and
    volume: finish the bar from it. Only when the meta is about that same day and the open is known."""
    if raw is None or raw.empty or "Close" not in raw.columns or pd.notna(raw["Close"].iloc[-1]):
        return False
    px, when = (meta or {}).get("regularMarketPrice"), (meta or {}).get("regularMarketTime")
    if not px or when is None:
        return False
    last = raw.index[-1]
    try:
        wd = pd.Timestamp(when, unit="s", tz="UTC") if isinstance(when, (int, float)) else pd.Timestamp(when)
        if getattr(last, "tzinfo", None) is not None and wd.tzinfo is not None:
            wd = wd.tz_convert(last.tzinfo)
        if wd.date() != last.date():
            return False
    except Exception:  # noqa: BLE001
        return False
    o = raw["Open"].iloc[-1]
    if not np.isfinite(o):
        return False
    px = float(px)
    hi = [x for x in (raw["High"].iloc[-1], meta.get("regularMarketDayHigh"), px, o) if x is not None and np.isfinite(x)]
    lo = [x for x in (raw["Low"].iloc[-1], meta.get("regularMarketDayLow"), px, o) if x is not None and np.isfinite(x)]
    raw.loc[last, "High"] = max(hi)
    raw.loc[last, "Low"] = min(lo)
    raw.loc[last, "Close"] = px
    if "Adj Close" in raw.columns:
        raw.loc[last, "Adj Close"] = px                 # (the newest bar has nothing after it to adjust for)
    if "Volume" in raw.columns and not (raw["Volume"].iloc[-1] > 0) and meta.get("regularMarketVolume"):
        raw.loc[last, "Volume"] = float(meta["regularMarketVolume"])
    return True


def _yahoo_daily(yf, sym: str, period: str) -> pd.DataFrame:
    """Daily bars as TradingView shows them: adjusted for splits (Yahoo's own Close is), and - with
    ADJUST_DIVIDENDS - for dividends too, exactly as yfinance's auto_adjust does (O, H, L times Adj Close / Close;
    Close = Adj Close). Done here, after the newest session is finished (fill_last_session): with auto_adjust
    Yahoo's unfinished bar comes back blank."""
    t = yf.Ticker(sym)
    raw = t.history(period=period, interval="1d", auto_adjust=False)
    if raw is None or raw.empty:
        return raw
    raw = raw.copy()
    try:
        meta = t.history_metadata or {}
    except Exception:  # noqa: BLE001
        meta = {}
    fill_last_session(raw, meta)
    if "Adj Close" in raw.columns and not ADJUST_DIVIDENDS:
        raw = raw.drop(columns=["Adj Close"])
    if "Adj Close" in raw.columns:
        ratio = (raw["Adj Close"] / raw["Close"]).astype(float)
        ratio = ratio.where(np.isfinite(ratio) & (ratio > 0), 1.0)
        for k in ("Open", "High", "Low"):
            raw[k] = raw[k] * ratio
        raw["Close"] = raw["Adj Close"].where(raw["Adj Close"].notna(), raw["Close"])
        raw = raw.drop(columns=["Adj Close"])
    return raw


# TradingView's 'ADJ' (adjust data for dividends): off by default there - its prices are adjusted for splits only,
# and so are ChartLab's (a year of RELIANCE before its dividend is otherwise ~0.4 % lower, ITC's ~5 %). On: also for
# dividends, as yfinance's auto_adjust.
ADJUST_DIVIDENDS = False


def _thin_index(ticker: str):
    """The index behind a Yahoo ticker when Yahoo has no daily bars of it (else None)."""
    try:
        from . import indices as _ix
        hit = _ix.by_yahoo(ticker)
        return hit if hit is not None and not hit.daily else None
    except Exception:  # noqa: BLE001
        return None


def _yahoo_daily_from_hours(yf, sym: str, period: str) -> pd.DataFrame:
    """Daily bars for an index Yahoo keeps no daily bars of (Nifty MidCap Select, Private Bank, Media, Healthcare
    ...: its daily history is one day long) - built from its hourly bars, which go back two years. Each day's close
    is exact (the day's last value is the official close - checked against TradingView); the open, high and low
    are the hours' own, which can sit a few points off the official ones. The newest day takes Yahoo's daily row
    for it, which has the official open, high, low and close."""
    days = period_days(period) or 730
    t = yf.Ticker(sym)
    h = t.history(period=f"{max(7, min(days + 7, 729))}d", interval="1h", auto_adjust=False)
    if h is None or h.empty:
        return h
    h = h.dropna(subset=["Close"])
    idx = h.index
    loc = idx.tz_convert("Asia/Kolkata") if idx.tz is not None else idx
    g = h.groupby(loc.normalize()).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last",
                                        "Volume": "sum"})
    g.index = pd.DatetimeIndex(g.index)
    try:
        d = t.history(period="5d", interval="1d", auto_adjust=False)
        meta = getattr(t, "history_metadata", None) or {}
        if d is not None and len(d):
            fill_last_session(d, meta)
            d = d.dropna(subset=["Close"])
        if d is not None and len(d):
            k = d.index[-1]
            k = (k.tz_convert("Asia/Kolkata") if k.tzinfo is not None else k).normalize()
            if g.index.tz is None and k.tzinfo is not None:
                k = k.tz_localize(None)
            row = d.iloc[-1]
            if all(pd.notna(row[c]) for c in ("Open", "High", "Low", "Close")):
                g.loc[k, ["Open", "High", "Low", "Close"]] = [float(row["Open"]), float(row["High"]),
                                                              float(row["Low"]), float(row["Close"])]
                if pd.isna(g.loc[k, "Volume"]):
                    g.loc[k, "Volume"] = float(row.get("Volume") or 0.0)
                g = g.sort_index()
                # ... and the day before it takes Yahoo's previous close, which is the official one (the hours'
                # last value misses it by a few points on most of these): today's change is then exact too
                pc = meta.get("chartPreviousClose")
                before = g.index[g.index < k]
                if pc and len(before):
                    b = before[-1]
                    g.loc[b, "Close"] = float(pc)
                    g.loc[b, "High"] = max(float(g.loc[b, "High"]), float(pc))
                    g.loc[b, "Low"] = min(float(g.loc[b, "Low"]), float(pc))
    except Exception:  # noqa: BLE001 - the hours alone then
        pass
    return g


def load_yahoo(symbol: str, interval: str = "1d", period: str = "2y") -> tuple[pd.DataFrame, str]:
    try:
        import yfinance as yf
    except ImportError as e:  # pragma: no cover
        raise ImportError("yfinance is not installed. Run: pip install yfinance") from e
    sym = normalize_symbol(symbol)
    from . import timeframes as TF
    if interval in ("1wk", "1mo") and _thin_index(sym) is not None:   # no weekly / monthly bars there either
        df, _label = load_yahoo(symbol, "1d", period)
        return TF.resample(df, interval), f"{sym} \u00b7 {interval}"
    if interval not in TF.YAHOO and TF.valid(interval):       # 3m, 45m, 4h, 3D ... built from what Yahoo has
        base = TF.base_for(interval, TF.YAHOO)
        df, _label = load_yahoo(symbol, base, period)
        return TF.resample(df, interval), f"{sym} \u00b7 {interval}"
    period = clamp_period(interval, period)
    cache = _cache_file(sym, interval, period)
    try:
        if interval == "1m" and period_days(period) > _INTRADAY_LIMIT["1m"]:
            raw = _yahoo_1m(yf, sym, period_days(period))
        elif interval == "1d":
            raw = (_yahoo_daily_from_hours(yf, sym, period) if _thin_index(sym) is not None
                   else _yahoo_daily(yf, sym, period))
        else:
            raw = yf.Ticker(sym).history(period=period, interval=interval, auto_adjust=ADJUST_DIVIDENDS)
        if raw is None or raw.empty:
            raise ValueError(f"No data returned for '{sym}' ({interval}, {period}). Check the symbol / internet.")
        df = no_index_volume(drop_frozen(standardise(raw)), sym)
    except Exception:  # noqa: BLE001 - fall back to the last good download
        if cache.exists():
            return no_index_volume(drop_frozen(pd.read_parquet(cache)), sym), f"{sym} · {interval} (offline copy)"
        raise
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cache)
    except Exception:  # noqa: BLE001 - cache is best-effort
        pass
    return df, f"{sym} · {interval}"


def no_index_volume(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """An index has no volume of its own. Yahoo sends numbers for some days that are not TradingView's (NIFTY's in
    thousands, Nifty Auto's a hundredth, 0 for today and for every intraday bar): show none rather than wrong ones -
    the legend then leaves 'Vol' out, as it does for an index."""
    try:
        from . import indices as _ix
        if df is not None and len(df) and "Volume" in df.columns and _ix.by_yahoo(ticker) is not None:
            df = df.copy()
            df["Volume"] = 0.0
    except Exception:  # noqa: BLE001
        pass
    return df


def peek_cache(symbol: str, interval: str = "1d", period: str = "2y"):
    """The last good download of this chart, if there is one - shown at once while the fresh bars are fetched."""
    sym = normalize_symbol(symbol)
    f = _cache_file(sym, interval, clamp_period(interval, period))
    if not f.exists():
        return None
    df = no_index_volume(drop_frozen(pd.read_parquet(f)), sym)
    return (df, f"{sym} \u00b7 {interval}") if len(df) else None


def _cache_file(sym: str, interval: str, period: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{sym}_{interval}_{period}" + ("_div" if ADJUST_DIVIDENDS else "_spl"))
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


# --------------------------------------------------------------------------- "Auto" source + my data folder
DATA_DIR = USER_DIR / "data"       # drop RELIANCE.csv / NIFTY.parquet / a NIFTY folder here: it is used first


def _plain(symbol: str) -> str:
    return re.sub(r"[^A-Z0-9&-]", "", symbol.upper().split(":")[-1])


def find_local(symbol: str, folder: Path | None = None) -> Path | None:
    """The file (or folder of files) in My data whose name matches the symbol, e.g. NIFTY.csv, nifty_5m.parquet."""
    folder = Path(folder) if folder else DATA_DIR
    if not folder.exists():
        return None
    want = _plain(symbol)
    best = None
    for p in sorted(folder.iterdir()):
        if p.name.startswith("."):
            continue
        if p.is_file() and p.suffix.lower() not in (".csv", ".tsv", ".txt", ".parquet"):
            continue
        stem = _plain(p.stem)
        if stem == want:
            return p
        if best is None and re.match(rf"^{re.escape(want)}[-_ ]", p.stem.upper()):
            best = p
    return best


def load_auto(symbol: str, interval: str = "1d", period: str = "2y", api_loaders=()) -> tuple[pd.DataFrame, str]:
    """Best available data without choosing a source: your own file in My data, else a connected broker API,
    else Yahoo. Each failure falls through to the next one; the label says where the bars came from."""
    errors = []
    p = find_local(symbol)
    if p is not None:
        try:
            raw, label = load_file(p)
            df, note = to_timeframe(raw, interval)
            if not note:                                   # the file can really give this interval
                return df, f"{symbol.upper()} \u00b7 {interval} \u00b7 my data"
            errors.append(f"my data: {note}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"my data: {e}")
    for name, fn in api_loaders:
        try:
            return fn(symbol, interval, period)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
    try:
        return load_yahoo(symbol, interval, period)
    except Exception as e:  # noqa: BLE001
        errors.append(f"Yahoo: {e}")
    raise RuntimeError("No source could load it  \u2014  " + "  |  ".join(str(x)[:90] for x in errors))


def save_daily(symbol: str, df: pd.DataFrame, interval: str = "1d") -> Path:
    """Keep a copy of downloaded bars in ~/ChartLab/data/downloads (merged with what is already there)."""
    out = USER_DIR / "data" / "downloads"
    out.mkdir(parents=True, exist_ok=True)
    f = out / (re.sub(r"[^A-Za-z0-9_.&-]", "_", f"{symbol.upper()}_{interval}") + ".parquet")
    if f.exists():
        try:
            old = pd.read_parquet(f)
            df = pd.concat([old[~old.index.isin(df.index)], df]).sort_index()
        except Exception:  # noqa: BLE001 - unreadable old copy: replace it
            pass
    tmp = f.with_suffix(".part")
    df.to_parquet(tmp)
    os.replace(tmp, f)
    return f
