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
    # plain Python floats: this loop is ~10x faster than indexing numpy arrays one element at a time
    n_ = len(c)
    nan = float("nan")
    A, C, U, D = a.tolist(), c.tolist(), up.tolist(), dn.tolist()
    st, d = [nan] * n_, [1.0] * n_
    for k in range(1, n_):
        ak = A[k]
        if ak != ak:                                         # NaN: ATR not ready yet
            continue
        u1, d1, c1 = U[k - 1], D[k - 1], C[k - 1]
        if u1 == u1 and c1 > u1 and u1 > U[k]:
            U[k] = u1
        if d1 == d1 and c1 < d1 and d1 < D[k]:
            D[k] = d1
        prev = d[k - 1]
        if prev < 0 and C[k] > d1:
            d[k] = 1.0
        elif prev > 0 and C[k] < u1:
            d[k] = -1.0
        else:
            d[k] = prev
        st[k] = U[k] if d[k] > 0 else D[k]
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
