"""Indicators available inside strategies as ``ta`` (ta.sma, ta.rsi, ...). All take/return pandas objects."""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    out = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    out = out.where(dn != 0, 100.0)
    return out.where(d.notna() | (s.index != s.index[0]))


def macd(s: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(s, fast) - ema(s, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def bollinger(s: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(s, n)
    sd = s.rolling(n).std(ddof=0)
    return pd.DataFrame({"mid": mid, "upper": mid + k * sd, "lower": mid - k * sd})


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["Close"].shift()
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.0) -> pd.DataFrame:
    """Supertrend (ATR based). Returns columns: st (the line), dir (+1 up-trend / -1 down-trend)."""
    h, l, c = df["High"].to_numpy(float), df["Low"].to_numpy(float), df["Close"].to_numpy(float)
    a = atr(df, n).to_numpy(float)
    hl2 = (h + l) / 2
    up, dn = hl2 - mult * a, hl2 + mult * a
    st = np.full(len(c), np.nan)
    d = np.ones(len(c))
    for k in range(1, len(c)):
        if np.isnan(a[k]):
            continue
        if not np.isnan(up[k - 1]) and c[k - 1] > up[k - 1]:
            up[k] = max(up[k], up[k - 1])
        if not np.isnan(dn[k - 1]) and c[k - 1] < dn[k - 1]:
            dn[k] = min(dn[k], dn[k - 1])
        prev = d[k - 1]
        if prev < 0 and c[k] > dn[k - 1]:
            d[k] = 1
        elif prev > 0 and c[k] < up[k - 1]:
            d[k] = -1
        else:
            d[k] = prev
        st[k] = up[k] if d[k] > 0 else dn[k]
    return pd.DataFrame({"st": st, "dir": d}, index=df.index)


def vwap(df: pd.DataFrame) -> pd.Series:
    """Volume-weighted average price. Intraday data: resets every day (session VWAP); daily data: rolling 20."""
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    v = df["Volume"].astype(float).replace(0, np.nan)
    idx = df.index
    intraday = isinstance(idx, pd.DatetimeIndex) and len(idx) > 1 and bool(((idx.hour != 0) | (idx.minute != 0)).any())
    if intraday:
        day = idx.normalize()
        pv = (tp * v).groupby(day).cumsum()
        vv = v.groupby(day).cumsum()
        return (pv / vv).ffill()
    return ((tp * v).rolling(20).sum() / v.rolling(20).sum()).ffill()


def highest(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).max()


def lowest(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).min()


def _as_series(b, like: pd.Series) -> pd.Series:
    return b if isinstance(b, pd.Series) else pd.Series(b, index=like.index)


def crossover(a: pd.Series, b) -> pd.Series:
    """True on the bar where ``a`` crosses above ``b`` (b may be a number)."""
    b = _as_series(b, a)
    return (a > b) & (a.shift() <= b.shift())


def crossunder(a: pd.Series, b) -> pd.Series:
    """True on the bar where ``a`` crosses below ``b`` (b may be a number)."""
    b = _as_series(b, a)
    return (a < b) & (a.shift() >= b.shift())
