"""The option archive: contracts around the money for the nearest expiries, each one's full 1-minute history with open
interest from its listing day, one file per contract, extended every day, kept after expiry. Fake Dhan answers."""
import datetime as dt
import os
import shutil
import sys
from pathlib import Path

os.environ["CHARTLAB_HOME"] = "/tmp/chartlab_test_opt"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import pandas as pd  # noqa: E402

from chartlab import dhan_core, optstore  # noqa: E402

base = Path("/tmp/chartlab_test_opt/archive")
shutil.rmtree(base, ignore_errors=True)
today = dt.date(2026, 11, 19)
rows = []
sid = 1000
for exp in (dt.date(2026, 11, 24), dt.date(2026, 12, 1), dt.date(2026, 12, 8)):
    for k in range(24000, 27001, 50):
        for typ in ("CE", "PE"):
            sid += 1
            rows.append({"inst": "OPTIDX", "und": "NIFTY", "sid": sid, "lot": 75, "expiry": exp, "strike": float(k),
                         "type": typ, "seg": "NSE_FNO"})
table = pd.DataFrame(rows)
dhan_core._options_table = lambda: table
LISTED = dt.datetime(2026, 10, 27, 9, 15)                 # the contracts' first bar
calls = []


def post(body):
    calls.append(body)
    a = max(pd.Timestamp(body["fromDate"]), pd.Timestamp(LISTED) if body["exchangeSegment"] != "IDX_I" else pd.Timestamp("2026-01-01"))
    b = min(pd.Timestamp(body["toDate"]), pd.Timestamp(now[0]))
    assert body["oi"] is True and body["interval"] == "1"
    days = pd.bdate_range(a.normalize(), b.normalize())
    ts = [d + pd.Timedelta(minutes=555 + 15 * i) for d in days for i in range(25)]
    ts = [t for t in ts if a <= t <= b]
    ep = [int(t.tz_localize("Asia/Kolkata").timestamp()) for t in ts]
    n = len(ep)
    return {"timestamp": ep, "open": [10.0] * n, "high": [11.0] * n, "low": [9.0] * n, "close": [10.5] * n,
            "volume": [100] * n, "open_interest": [5000 + i for i in range(n)]}


now = [dt.datetime(2026, 11, 19, 15, 30)]
acct = {"access_token": "T", "client_id": "1"}
todo = optstore.plan(["NIFTY"], {"NIFTY": 25510.0}, width=10, n_exp=2, today=today)
assert len(todo) == 2 * 21 * 2, len(todo)
ks = sorted({c["strike"] for c in todo})
assert ks[0] == 25000 and ks[-1] == 26000 and {str(c["expiry"]) for c in todo} == {"2026-11-24", "2026-12-01"}
res = optstore.run(acct, base, ["NIFTY"], width=2, n_exp=2, post=post, spot={"NIFTY": 25510.0}, today=today, pause=0)
assert res["contracts"] == 2 * 5 * 2 and not res["errors"] and res["updated"] == 21, res
f = base / "NIFTY" / "2026-11-24" / "NIFTY261124C25500.parquet"
assert f.exists() and (base / "NIFTY" / "spot_1m.parquet").exists()
df = optstore.read(f)
assert list(df.columns) == optstore.COLS and df.index[0] == pd.Timestamp(LISTED), df.index[0]   # from the listing day
assert df.index[-1] == pd.Timestamp("2026-11-19 15:15") and df["OI"].iloc[0] == 5000 and df.index.is_monotonic_increasing
n1 = len(df)
idx = optstore.load_index(base)
assert idx["NIFTY261124C25500"]["rows"] == n1 and idx["NIFTY261124C25500"]["first"].startswith("2026-10-27")
# the next day: only the new bars are asked for and added; nothing is lost
calls.clear()
now[0] = dt.datetime(2026, 11, 20, 15, 30)
res = optstore.run(acct, base, ["NIFTY"], width=2, n_exp=2, post=post, spot={"NIFTY": 25510.0},
                   today=dt.date(2026, 11, 20), pause=0)
df2 = optstore.read(f)
assert len(df2) == n1 + 25 and df2.index[0] == df.index[0] and res["rows"] == 21 * 25, (len(df2), n1, res)
assert all(pd.Timestamp(c["fromDate"]) >= pd.Timestamp("2026-11-18") for c in calls), "old bars were downloaded again"
# the money moves: new strikes join with their whole history, the old ones stay on disk
res = optstore.run(acct, base, ["NIFTY"], width=2, n_exp=2, post=post, spot={"NIFTY": 25710.0},
                   today=dt.date(2026, 11, 20), pause=0)
g = base / "NIFTY" / "2026-11-24" / "NIFTY261124P25800.parquet"
assert g.exists() and optstore.read(g).index[0] == pd.Timestamp(LISTED) and f.exists()
# after the expiry the file is still there and readable by name
res = optstore.run(acct, base, ["NIFTY"], width=2, n_exp=2, post=post, spot={"NIFTY": 25710.0},
                   today=dt.date(2026, 11, 26), pause=0)
assert f.exists() and len(optstore.load("NIFTY261124C25500", base)) == n1 + 25
assert (base / "NIFTY" / "2026-12-08").is_dir()                         # the next expiry came into range
assert len(optstore.load("NIFTY", base)) > 1000
# a broker error on one contract does not stop the rest; a dead login stops early
bad = {"n": 0}


def flaky(body):
    bad["n"] += 1
    if bad["n"] == 3:
        raise RuntimeError("DH-905 boom")
    return post(body)


now[0] = dt.datetime(2026, 11, 27, 15, 30)
res = optstore.run(acct, base, ["NIFTY"], width=1, n_exp=1, post=flaky, spot={"NIFTY": 25710.0},
                   today=dt.date(2026, 11, 27), pause=0)
assert len(res["errors"]) == 1 and res["updated"] >= 4, res
res = optstore.run(acct, base, ["NIFTY"], width=10, n_exp=2, post=lambda b: (_ for _ in ()).throw(RuntimeError("auth")),
                   spot={"NIFTY": 25710.0}, today=dt.date(2026, 11, 28), pause=0)
assert len(res["errors"]) == 8, len(res["errors"])
# an unwritable chosen folder falls back to the default one
assert optstore.root("/proc/nope/x") == optstore.default_root()
print("OPTSTORE OK")
