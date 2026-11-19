"""Any time interval, like TradingView's "Add custom interval": 2 / 7 / 45 minutes, 2 / 3 / 4 hours, 2 / 3 days,
2 weeks, 3 / 6 / 12 months ... A data source only has a few intervals of its own; every other one is built from the
nearest smaller one it has (7m from 1m, 45m from 15m, 4h from 1h, 3D from 1D, 3M from 1M), the way TradingView does.

Codes (what is stored and passed around): '<n>m' minutes, '<n>h' hours, '<n>d' days, '<n>wk' weeks, '<n>mo' months.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

LEGACY = ("1m", "3m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo")      # every source is asked for these directly
YAHOO = ("1m", "2m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo")       # what Yahoo itself has
BASES = ("1m", "5m", "15m", "1h", "1d", "1wk", "1mo")                    # what any source has: custom ones come from these
_CODE = re.compile(r"(\d+)(m|h|d|wk|mo)")
MAX = {"m": 1440, "h": 24, "d": 366, "wk": 52, "mo": 12}                 # TradingView's own limits
UNIT_NAMES = {"m": "minute", "h": "hour", "d": "day", "wk": "week", "mo": "month"}


def split(code: str) -> tuple[int, str]:
    m = _CODE.fullmatch(str(code).strip().lower())
    if not m:
        raise ValueError(f"Not a time interval: '{code}'")
    return int(m[1]), m[2]


def valid(code) -> bool:
    try:
        n, u = split(code)
    except ValueError:
        return False
    return 1 <= n <= MAX[u]


def canon(n: int, unit: str) -> str:
    """One spelling per interval: 60 minutes is '1h', 120 is '2h', 90 stays '90m' (TradingView shows 1h, 2h, 90m)."""
    n = int(n)
    if unit == "m" and n >= 60 and n % 60 == 0:
        n, unit = n // 60, "h"
    return f"{n}{unit}"


def parse(text: str):
    """What is typed on the chart, TradingView's way: a number alone = minutes ('45'), 'h' hours ('4h'), 'D' days,
    'W' weeks, capital 'M' months ('3M'); a small 'm' = minutes ('5m'). -> code, or None."""
    t = str(text).strip().replace(",", "").replace(" ", "")
    m = re.fullmatch(r"(\d*)([A-Za-z]*)", t)
    if not m or not (m[1] or m[2]):
        return None
    n = int(m[1] or 1)
    u = m[2]
    unit = {"": "m", "m": "m", "min": "m", "h": "h", "H": "h", "d": "d", "D": "d", "w": "wk", "W": "wk", "wk": "wk",
            "M": "mo", "mo": "mo", "MO": "mo"}.get(u)
    if unit is None:
        return None
    code = canon(n, unit)
    return code if valid(code) else None


def short(code: str) -> str:
    """The toolbar's text: 5m, 2h, D, 3D, W, 2W, M, 3M."""
    n, u = split(code)
    if u in ("m", "h"):
        return f"{n}{u}"
    letter = {"d": "D", "wk": "W", "mo": "M"}[u]
    return letter if n == 1 else f"{n}{letter}"


def legend(code: str) -> str:
    """As the chart's legend and menus write it: 1m, 45m, 2h, 1D, 3D, 1W, 3M."""
    n, u = split(code)
    return f"{n}{u}" if u in ("m", "h") else f"{n}{ {'d': 'D', 'wk': 'W', 'mo': 'M'}[u] }"


def name(code: str) -> str:
    """'45 minutes', '1 hour', '3 days'."""
    n, u = split(code)
    return f"{n} {UNIT_NAMES[u]}{'' if n == 1 else 's'}"


def minutes(code: str) -> float:
    n, u = split(code)
    return n * {"m": 1, "h": 60, "d": 1440, "wk": 10080, "mo": 43200}[u]


def sort_key(code: str):
    n, u = split(code)
    return ("m", "h", "d", "wk", "mo").index(u), n


def group(code: str) -> str:
    return {"m": "MINUTES", "h": "HOURS"}.get(split(code)[1], "DAYS")


def base_for(code: str, have=BASES) -> str:
    """The interval to load and build `code` from: the biggest one the source has that fits into it evenly."""
    n, u = split(code)
    if u in ("m", "h"):
        mins = n * (60 if u == "h" else 1)
        best = None
        for b in have:
            bn, bu = split(b)
            if bu not in ("m", "h"):
                continue
            bm = bn * (60 if bu == "h" else 1)
            if bm <= mins and mins % bm == 0 and (best is None or bm > best[0]):
                best = (bm, b)
        if best is None:
            raise ValueError(f"This data source cannot build a {name(code)} chart")
        return best[1]
    return {"d": "1d", "wk": "1wk", "mo": "1mo"}[u]


def resample(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """Build `code` bars from smaller ones.
    Minutes / hours start at the session's first bar every day (09:15 on NSE: 45m bars are 09:15, 10:00 ...).
    N days are N trading days counted from the year's first one; N weeks / months are counted inside the year
    (3M = calendar quarters, 6M = halves). The newest bar is the one still being built."""
    n, u = split(code)
    if df is None or not len(df):
        return df
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    cols = [c for c in agg if c in df.columns]
    idx = df.index
    if u in ("m", "h"):
        step = n * (3600 if u == "h" else 60)
        day = idx.normalize()
        tod = (idx - day).total_seconds().values
        a = float(tod.min())                                  # every day's bars are counted from the session's start
        start = day + pd.to_timedelta(a + np.floor((tod - a) / step) * step, unit="s")
        g = df[cols].groupby(start.values, sort=True)
        out = g.agg({c: agg[c] for c in cols})
        out.index = pd.DatetimeIndex(out.index)
        out = out.dropna(subset=["Open", "Close"])
    else:
        if u == "d":
            days = pd.Series(idx.normalize(), index=idx)
            ordinal = days.groupby(idx.year).transform(lambda s: pd.factorize(s)[0])
            key = idx.year.values.astype("int64") * 1000 + (ordinal.values // n)
        elif u == "wk":
            iso = idx.isocalendar()
            key = iso["year"].values.astype("int64") * 1000 + (iso["week"].values.astype("int64") - 1) // n
        else:
            key = idx.year.values.astype("int64") * 1000 + (idx.month.values - 1) // n
        g = df[cols].groupby(key, sort=True)
        out = g.agg({c: agg[c] for c in cols})
        out.index = pd.DatetimeIndex(pd.Series(idx, index=idx).groupby(key, sort=True).first().values)
        if u == "d":
            out.attrs["tf_days"] = int(pd.Series(idx.normalize()).groupby(key).nunique().iloc[-1])
            out.attrs["tf_last_day"] = idx[-1].normalize()
    if "Volume" in out.columns:
        out["Volume"] = out["Volume"].fillna(0.0)
    for c in df.columns:
        if c not in out.columns:
            out[c] = np.nan
    out = out[list(df.columns)]
    out.index.name = df.index.name or "Date"
    attrs = dict(out.attrs)
    out.attrs.update(df.attrs)
    out.attrs.update(attrs)
    return out


def load(fn, symbol: str, code: str, period: str, native=LEGACY, have=BASES):
    """Run a source's loader fn(symbol, interval, period) -> (DataFrame, label) for any interval."""
    if code in native or not valid(code):
        return fn(symbol, code, period)
    base = base_for(code, have)
    df, label = fn(symbol, base, period)
    out = resample(df, code)
    return out, str(label).replace(f"· {base}", f"· {code}", 1)


def anchor(index) -> float:
    """Seconds after midnight at which this intraday data's day starts (09:15 -> 33300)."""
    if index is None or not len(index):
        return 0.0
    tail = index[-2000:]
    return float((tail - tail.normalize()).min().total_seconds())


def bar_start(t: float, step: float, anchor_s: float) -> float:
    """Start of the intraday bar a trade at chart time t belongs to: bars are counted from the session's start."""
    day = (t // 86400.0) * 86400.0
    start = day + anchor_s
    if t < start:                                   # before the session (pre-open): that minute's own slot
        return start - np.ceil((start - t) / step) * step
    return start + ((t - start) // step) * step


def same_period(last: pd.Timestamp, now: pd.Timestamp, code: str, days_in_last: int = 1) -> bool:
    """Does a trade at `now` still belong to the daily-or-longer bar that starts at `last`?"""
    n, u = split(code)
    a, b = pd.Timestamp(last), pd.Timestamp(now)
    if b.normalize() <= a.normalize():
        return True
    if u == "d":
        return n > 1 and a.year == b.year and days_in_last < n
    if u == "wk":
        ia, ib = a.isocalendar(), b.isocalendar()
        return ia[0] == ib[0] and (ia[1] - 1) // n == (ib[1] - 1) // n
    return a.year == b.year and (a.month - 1) // n == (b.month - 1) // n
