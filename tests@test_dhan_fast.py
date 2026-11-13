"""Dhan data path is fast: cached id lookup, parallel intraday chunks, incremental disk cache."""
import os, sys, json, time, tempfile, threading, datetime as dt
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, ".")
from pathlib import Path
import pandas as pd
from chartlab import accounts as A
import chartlab.dhan_core as D

tmp = Path(tempfile.mkdtemp())
A._CANDLE_DIR = tmp / "candles"
D._IDS = tmp / "ids.json"
calls, lock = [], threading.Lock()


def fake_http(url, body, headers, timeout=30):
    time.sleep(0.25)                                   # network latency
    with lock:
        calls.append((url, dict(body or {})))
    f = dt.datetime.strptime(body["fromDate"][:10], "%Y-%m-%d")
    t = dt.datetime.strptime(body["toDate"][:10], "%Y-%m-%d")
    days = pd.bdate_range(f, t - dt.timedelta(days=1))
    ts = [int(pd.Timestamp(d).timestamp()) + 4 * 3600 for d in days]
    n = len(ts)
    return json.dumps({"open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n, "close": [100.5] * n,
                       "volume": [1000] * n, "timestamp": ts}).encode()


A._http = D._http = fake_http
resolves = []
D.resolve_symbol = lambda s: (resolves.append(s), ("1333", "NSE_EQ", "EQUITY", s))[1]
acct = {"client_id": "1", "access_token": "t"}

# 1) security id is looked up once, then cached
A.fetch_dhan(acct, "HDFCBANK", "1d", "1y")
A._MEM.clear()
A.fetch_dhan(acct, "HDFCBANK", "1d", "1y")
assert len(resolves) == 1, resolves

# 2) incremental: the second load only asks Dhan for the last few days
last = calls[-1][1]
assert (dt.date.today() - dt.date.fromisoformat(last["fromDate"][:10])).days <= 6, last

# 3) intraday chunks run in parallel: 5 chunks x 0.25 s must take well under 1.25 s
calls.clear()
t0 = time.perf_counter()
df = A.fetch_dhan(acct, "INFY", "5m", "1y")
el = time.perf_counter() - t0
n_chunks = sum(1 for c in calls if c[0].endswith("/intraday"))
assert n_chunks >= 4 and el < 0.25 * n_chunks * 0.7, (n_chunks, el)

# 4) re-opening the same chart within seconds is instant (no network)
calls.clear()
t0 = time.perf_counter()
A.fetch_dhan(acct, "INFY", "5m", "1y")
assert not calls and time.perf_counter() - t0 < 0.05
print(f"DHAN FAST OK  ({n_chunks} intraday chunks in {el:.2f}s, incremental + cached)")
