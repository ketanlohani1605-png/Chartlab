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


def rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's moving average (TradingView ta.rma): RSI, ATR and ADX use it."""
    return s.ewm(alpha=1 / max(int(n), 1), adjust=False).mean()


def wma(s: pd.Series, n: int) -> pd.Series:
    """Weighted moving average: the newest bar weighs n, the oldest 1 (TradingView ta.wma)."""
    n = max(int(n), 1)
    x = s.to_numpy(float)
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        w = np.arange(n, 0, -1, dtype=float)                 # newest first
        conv = np.convolve(np.nan_to_num(x), w, mode="valid") / w.sum()
        bad = np.convolve(np.isnan(x).astype(float), np.ones(n), mode="valid") > 0
        conv[bad] = np.nan
        out[n - 1:] = conv
    return pd.Series(out, index=s.index)


def hma(s: pd.Series, n: int = 9) -> pd.Series:
    """Hull moving average."""
    n = max(int(n), 2)
    return wma(2 * wma(s, max(n // 2, 1)) - wma(s, n), max(int(np.sqrt(n)), 1))


def stoch(df: pd.DataFrame, k: int = 14, smooth_k: int = 1, d: int = 3) -> pd.DataFrame:
    """Stochastic oscillator: %K and %D, 0..100 (TradingView defaults 14, 1, 3)."""
    ll, hh = df["Low"].rolling(k).min(), df["High"].rolling(k).max()
    raw = 100 * (df["Close"] - ll) / (hh - ll).replace(0, np.nan)
    kk = sma(raw, max(int(smooth_k), 1))
    return pd.DataFrame({"k": kk, "d": sma(kk, max(int(d), 1))})


def stoch_rsi(s: pd.Series, rsi_len: int = 14, stoch_len: int = 14, k: int = 3, d: int = 3) -> pd.DataFrame:
    r = rsi(s, rsi_len)
    ll, hh = r.rolling(stoch_len).min(), r.rolling(stoch_len).max()
    kk = sma(100 * (r - ll) / (hh - ll).replace(0, np.nan), max(int(k), 1))
    return pd.DataFrame({"k": kk, "d": sma(kk, max(int(d), 1))})


def cci(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """Commodity Channel Index on (high + low + close) / 3."""
    tp = ((df["High"] + df["Low"] + df["Close"]) / 3).to_numpy(float)
    n = max(int(n), 2)
    out = np.full(len(tp), np.nan)
    if len(tp) >= n:
        win = np.lib.stride_tricks.sliding_window_view(tp, n)
        m = win.mean(axis=1)
        md = np.abs(win - m[:, None]).mean(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            out[n - 1:] = (tp[n - 1:] - m) / (0.015 * md)
    return pd.Series(out, index=df.index)


def mfi(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Money Flow Index (needs volume)."""
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    mf = tp * df["Volume"].astype(float)
    ch = tp.diff()
    pos = mf.where(ch > 0, 0.0).rolling(n).sum()
    neg = mf.where(ch < 0, 0.0).rolling(n).sum()
    out = 100 - 100 / (1 + pos / neg.replace(0, np.nan))
    return out.where(~((neg == 0) & (pos > 0)), 100.0)


def williams_r(df: pd.DataFrame, n: int = 14) -> pd.Series:
    hh, ll = df["High"].rolling(n).max(), df["Low"].rolling(n).min()
    return -100 * (hh - df["Close"]) / (hh - ll).replace(0, np.nan)


def dmi(df: pd.DataFrame, n: int = 14, adx_n: int = 14) -> pd.DataFrame:
    """Directional Movement Index: +DI, -DI and ADX (Wilder smoothing, like TradingView)."""
    up, dn = df["High"].diff(), -df["Low"].diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    pc = df["Close"].shift()
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    tr_r = rma(tr, n).replace(0, np.nan)
    plus = 100 * rma(plus_dm, n) / tr_r
    minus = 100 * rma(minus_dm, n) / tr_r
    dx = 100 * (plus - minus).abs() / (plus + minus).replace(0, np.nan)
    return pd.DataFrame({"plus": plus, "minus": minus, "adx": rma(dx.fillna(0), adx_n)})


def obv(df: pd.DataFrame) -> pd.Series:
    """On Balance Volume."""
    sign = np.sign(df["Close"].diff().fillna(0.0))
    return (sign * df["Volume"].astype(float)).cumsum()


def roc(s: pd.Series, n: int = 9) -> pd.Series:
    return 100 * (s / s.shift(n) - 1)


def momentum(s: pd.Series, n: int = 10) -> pd.Series:
    return s - s.shift(n)


def psar(df: pd.DataFrame, start: float = 0.02, inc: float = 0.02, maximum: float = 0.2) -> pd.Series:
    """Parabolic SAR (stop and reverse), one value per bar."""
    h, l = df["High"].to_numpy(float).tolist(), df["Low"].to_numpy(float).tolist()
    n = len(h)
    out = [float("nan")] * n
    if n < 3:
        return pd.Series(out, index=df.index)
    up = h[1] >= h[0]
    af = start
    ep = h[1] if up else l[1]
    sar = l[0] if up else h[0]
    out[1] = sar
    for i in range(2, n):
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, l[i - 1], l[i - 2])
            if l[i] < sar:                                    # reversal to a down-trend
                up, sar, ep, af = False, ep, l[i], start
            elif h[i] > ep:
                ep, af = h[i], min(af + inc, maximum)
        else:
            sar = max(sar, h[i - 1], h[i - 2])
            if h[i] > sar:                                    # reversal to an up-trend
                up, sar, ep, af = True, ep, h[i], start
            elif l[i] < ep:
                ep, af = l[i], min(af + inc, maximum)
        out[i] = sar
    return pd.Series(out, index=df.index)


def donchian(df: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    hh, ll = df["High"].rolling(n).max(), df["Low"].rolling(n).min()
    return pd.DataFrame({"upper": hh, "lower": ll, "basis": (hh + ll) / 2})


def keltner(df: pd.DataFrame, n: int = 20, mult: float = 2.0, atr_n: int = 10) -> pd.DataFrame:
    basis = ema(df["Close"], n)
    rng = atr(df, atr_n)
    return pd.DataFrame({"basis": basis, "upper": basis + mult * rng, "lower": basis - mult * rng})


def ichimoku(df: pd.DataFrame, conv: int = 9, base: int = 26, span_b: int = 52) -> pd.DataFrame:
    """Ichimoku lines, not yet displaced (the chart shifts the leading spans forward and the lagging span back)."""
    mid = lambda k: (df["High"].rolling(k).max() + df["Low"].rolling(k).min()) / 2  # noqa: E731
    tenkan, kijun = mid(conv), mid(base)
    return pd.DataFrame({"conversion": tenkan, "base": kijun, "span_a": (tenkan + kijun) / 2, "span_b": mid(span_b),
                         "lagging": df["Close"]})


def pivots(df: pd.DataFrame, period: str = "auto") -> pd.DataFrame:
    """Standard floor pivots from the previous period's high / low / close: P, R1-R3, S1-S3 for every bar.
    period: 'D', 'W', 'M', 'Y' or 'auto' (TradingView: <= 15m charts use daily, other intraday weekly,
    daily monthly, weekly and slower yearly)."""
    idx = df.index
    if period == "auto":
        step = float(np.median(np.diff(idx.values[-60:]).astype("timedelta64[s]").astype(float))) if len(idx) > 2 else 86400
        period = "D" if step <= 900 else "W" if step < 86400 * 0.9 else "M" if step < 86400 * 3 else "Y"
    if period == "D":
        key = idx.normalize()
    elif period == "W":
        key = (idx - pd.to_timedelta(idx.dayofweek, unit="D")).normalize()
    elif period == "M":
        key = idx.to_period("M").to_timestamp()
    else:
        key = idx.to_period("Y").to_timestamp()
    g = df.groupby(key)
    agg = pd.DataFrame({"h": g["High"].max(), "l": g["Low"].min(), "c": g["Close"].last()}).shift(1)
    p = (agg["h"] + agg["l"] + agg["c"]) / 3
    lv = pd.DataFrame({"P": p, "R1": 2 * p - agg["l"], "S1": 2 * p - agg["h"], "R2": p + (agg["h"] - agg["l"]),
                       "S2": p - (agg["h"] - agg["l"]), "R3": agg["h"] + 2 * (p - agg["l"]),
                       "S3": agg["l"] - 2 * (agg["h"] - p)})
    out = lv.reindex(key)
    out.index = idx
    out["period"] = np.asarray(key)
    return out


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
