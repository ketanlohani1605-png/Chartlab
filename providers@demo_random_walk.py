"""Demo provider: offline random-walk data, so you can try ChartLab without internet.

HOW TO PLUG IN YOUR OWN API (Zerodha Kite, Angel One, Upstox, Binance, ...):
  1. Copy this file, e.g. to ~/ChartLab/providers/my_broker.py  (or into this folder).
  2. Replace the body of fetch() with calls to your API.
  3. Return a pandas DataFrame with a date/time column (or DatetimeIndex) and
     Open, High, Low, Close, Volume columns. ChartLab cleans the rest.
  4. Restart ChartLab -> it appears in the "Source" dropdown as "Provider - my_broker".

Arguments you receive from the toolbar:
  symbol   text typed in the symbol box (exactly as typed)
  interval "1m" "5m" "15m" "30m" "1h" "1d" "1wk" "1mo"
  period   "5d" "1mo" "3mo" "6mo" "1y" "2y" "5y" "10y" "max"
"""
import zlib

import numpy as np
import pandas as pd

_STEP = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}
_DAYS = {"5d": 5, "1mo": 21, "3mo": 63, "6mo": 126, "1y": 252, "2y": 504, "5y": 1260, "10y": 2520, "max": 2520}


def fetch(symbol, interval, period):
    rng = np.random.default_rng(zlib.crc32(symbol.encode()))
    days = _DAYS.get(period, 504)
    step = _STEP.get(interval)
    end = pd.Timestamp.today().normalize()
    if step:
        days = min(days, 60)
        per_day = 375 // step  # NSE session 09:15-15:30
        bdays = pd.bdate_range(end=end, periods=days)
        times = [d + pd.Timedelta(minutes=555 + step * k) for d in bdays for k in range(per_day)]
        vol_scale = 1 / np.sqrt(per_day)
    else:
        times = list(pd.bdate_range(end=end, periods=days))
        vol_scale = 1.0

    n = len(times)
    close = (500 + zlib.crc32(symbol.encode()) % 2500) * np.exp(np.cumsum(rng.normal(0.0003, 0.015 * vol_scale, n)))
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.002 * vol_scale, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004 * vol_scale, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004 * vol_scale, n)))
    volume = rng.lognormal(12, 0.5, n)
    df = pd.DataFrame({"Date": times, "Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume})

    if interval in ("1wk", "1mo"):
        rule = "W-FRI" if interval == "1wk" else "ME"
        df = (df.set_index("Date")
                .resample(rule)
                .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
                .dropna()
                .reset_index())
    return df
