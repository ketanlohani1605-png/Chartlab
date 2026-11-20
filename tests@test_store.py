"""The data store: everything a chart or the daily job downloads is kept in small files (one per month for
intraday bars) in a folder that can sit inside a cloud drive; charts get the stored older bars in front of the fresh
ones; a split rescales what is stored; nothing is written while the store is off."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from chartlab import optstore, store  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="chartlab_store_"))

# ---- the cloud folders of a computer are found by themselves
home = tmp / "home"
for p in ("Library/Mobile Documents/com~apple~CloudDocs", "Library/CloudStorage/GoogleDrive-a@b.com/My Drive",
          "Library/CloudStorage/Dropbox", "Library/CloudStorage/OneDrive-Personal"):
    (home / p).mkdir(parents=True)
found = store.cloud_folders(home)
assert [n for n, _p in found] == ["iCloud Drive", "Google Drive", "Dropbox", "OneDrive"], found
assert found[1][1].name == "My Drive"
name, target = store.pick_cloud(home)
assert name == "iCloud Drive" and target == home / "Library/Mobile Documents/com~apple~CloudDocs/ChartLab/options"
assert store.candles_root(target) == target.parent / "candles"
assert store.cloud_folders(tmp / "nobody") == [] and store.pick_cloud(tmp / "nobody") is None

# ---- one folder per instrument, however it was typed
for typed, k in (("reliance", "RELIANCE"), ("NSE:RELIANCE", "RELIANCE"), ("RELIANCE.NS", "RELIANCE"), ("BSE:TCS", "BSE_TCS"),
                 ("NIFTY", "NIFTY"), ("^NSEI", "NIFTY"), ("banknifty", "BANKNIFTY"), ("US:AAPL", "US_AAPL"),
                 ("BTC-USD", "BTC-USD"), ("M&M", "M&M")):
    assert store.key_for(typed) == k, (typed, store.key_for(typed))


def bars(start, days, step="1min", per_day=375, base=100.0, seed=1):
    rng = np.random.default_rng(seed)
    idx = []
    for d in pd.bdate_range(start, periods=days):
        idx += list(pd.date_range(d + pd.Timedelta(hours=9, minutes=15), periods=per_day, freq=step))
    idx = pd.DatetimeIndex(idx, name="Date")
    c = base + np.cumsum(rng.normal(0, 0.05, len(idx)))
    return pd.DataFrame({"Open": c, "High": c + 0.2, "Low": c - 0.2, "Close": c + 0.05, "Volume": 1000.0}, index=idx)


# ---- off: nothing happens, nothing is written
root = tmp / "cloud" / "ChartLab" / "options"
fresh = bars("2026-11-10", 5)
assert not store.active()
out, label = store.keep("RELIANCE", "1m", "2y", fresh, "x")
assert out is fresh and store.saved("RELIANCE", "1m", "2y") is None and not (tmp / "cloud").exists()

# ---- on: the first load is stored, one file per month
store.enable(root)
old = bars("2026-10-01", 20)                                  # 1 Oct .. 28 Oct
out, _l = store.keep("RELIANCE", "1m", "2y", old, "x")
assert len(out) == len(old)
cdir = root.parent / "candles" / "RELIANCE" / "1m"
assert sorted(f.name for f in cdir.glob("*.parquet")) == ["2026-10.parquet"], list(cdir.glob("*"))
# a later load only has the last days (Yahoo: 7 days of 1-minute bars): the chart still gets everything
out, _l = store.keep("NSE:RELIANCE", "1m", "2y", fresh, "x")
assert len(out) == len(old) + len(fresh) and out.index[0] == old.index[0] and out.index[-1] == fresh.index[-1]
assert out.index.is_monotonic_increasing and not out.index.has_duplicates
# ... but that second write waits (a sync service uploads the whole file again each time): it is in memory only
assert sorted(f.name for f in cdir.glob("*.parquet")) == ["2026-10.parquet"]
store.flush()
assert sorted(f.name for f in cdir.glob("*.parquet")) == ["2026-10.parquet", "2026-11.parquet"]
oct_file = cdir / "2026-10.parquet"
stamp = oct_file.stat().st_mtime_ns
# the same bars again: no file is touched; newer numbers for today's bars rewrite only this month's file
store.merge("RELIANCE", "1m", fresh, force=True)
later = fresh.copy()
later.iloc[-1, later.columns.get_loc("Close")] += 1.0
assert store.merge("RELIANCE", "1m", later, force=True) == 0
assert oct_file.stat().st_mtime_ns == stamp
assert abs(pd.read_parquet(cdir / "2026-11.parquet")["Close"].iloc[-1] - later["Close"].iloc[-1]) < 1e-9
# the chosen range cuts what a chart gets; the store keeps all of it
out, _l = store.keep("RELIANCE", "1m", "5d", fresh, "x")
assert out.index[0] >= fresh.index[-1].normalize() - pd.Timedelta(days=5) and len(out) <= len(fresh)
assert len(store.read("RELIANCE", "1m")) == len(old) + len(fresh)
# a restart reads it back from the files
store.disable()
assert store.saved("RELIANCE", "1m", "2y") is None
store.enable(root)
back, lab = store.saved("RELIANCE", "1m", "max")
assert len(back) == len(old) + len(fresh) and lab == "RELIANCE · 1m"

# ---- one stored 1-minute history feeds every intraday chart
five = fresh.resample("5min", origin="start_day", offset="15min").agg(
    {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
five = five[five.index >= "2026-11-13"]                       # the source's own 5-minute bars: two days only
out, _l = store.keep("RELIANCE", "5m", "2y", five, "x")
assert out.index[0] == pd.Timestamp("2026-10-01 09:15") and out.index[-1] == five.index[-1], (out.index[0], out.index[-1])
assert len(out) > len(five) * 5 and not out.index.has_duplicates and out.index.is_monotonic_increasing
day1 = out[out.index.normalize() == pd.Timestamp("2026-10-01")]
assert len(day1) == 75 and abs(day1["Volume"].iloc[0] - 5000.0) < 1e-6      # 375 minutes -> 75 bars of 5
got, lab = store.saved("RELIANCE", "45m", "max")              # a custom interval comes from the stored bars too
assert lab.endswith("45m") and got.index[0] == pd.Timestamp("2026-10-01 09:15") and len(got) > 100

# ---- hourly bars: one file per year; daily bars: one file
hr = bars("2025-12-20", 20, step="1h", per_day=7)
store.merge("NIFTY", "1h", hr, force=True)
assert sorted(f.name for f in (root.parent / "candles" / "NIFTY" / "1h").glob("*.parquet")) == ["2025.parquet", "2026.parquet"]
days = bars("2024-01-01", 400, step="1D", per_day=1, base=1000.0)
days.index = days.index.normalize()
store.merge("TCS", "1d", days, force=True)
assert [f.name for f in (root.parent / "candles" / "TCS" / "1d").glob("*.parquet")] == ["all.parquet"]

# ---- a 1:2 split: the source's whole history is halved; the stored older bars follow it
recent = days.iloc[-120:].copy()
recent[["Open", "High", "Low", "Close"]] *= 0.5
recent["Volume"] *= 2.0
out, _l = store.keep("TCS", "1d", "max", recent, "x")
assert len(out) == len(days)
seam = out["Close"].iloc[len(days) - 121] / out["Close"].iloc[len(days) - 120]
assert 0.9 < seam < 1.1, seam                                 # no cliff where stored and fresh bars meet
assert abs(out["Close"].iloc[0] - days["Close"].iloc[0] * 0.5) < 1e-6
assert abs(out["Volume"].iloc[0] - days["Volume"].iloc[0] * 2.0) < 1e-6
store.flush()
disk = pd.read_parquet(root.parent / "candles" / "TCS" / "1d" / "all.parquet")
assert abs(disk["Close"].iloc[0] - days["Close"].iloc[0] * 0.5) < 1e-6      # and it is what is on disk now
# ordinary noise (a few different closes) never rescales anything
noisy = recent.copy()
noisy.iloc[3, noisy.columns.get_loc("Close")] *= 1.3
before = store.read("TCS", "1d")["Close"].iloc[0]
store.keep("TCS", "1d", "max", noisy, "x")
assert store.read("TCS", "1d")["Close"].iloc[0] == before

# ---- never more bars than a chart can carry
store.MAX_BARS, keep_max = 3000, store.MAX_BARS
out, _l = store.keep("RELIANCE", "1m", "max", fresh, "x")
assert len(out) == 3000 and out.index[-1] == fresh.index[-1]
store.MAX_BARS = keep_max

# ---- the daily job: the whole watchlist, daily and 1-minute bars, options are left to the option archive
asked = []


def loader(sym, iv, rng):
    asked.append((sym, iv, rng))
    if sym == "BAD":
        raise RuntimeError("no such symbol")
    return (days if iv == "1d" else fresh), "label"


res = store.save_watchlist(["INFY", "BAD", "NIFTY 25000 CE", "SBIN"], loader)
assert res["saved"] == 2 and res["symbols"] == 4 and len(res["errors"]) == 2 and res["bars"] == 2 * (len(days) + len(fresh))
assert ("INFY", "1d", "2y") in asked and ("INFY", "1m", "3mo") in asked and not any("25000" in a[0] for a in asked)
cat = store.catalog()
assert set(cat["INFY"]) == {"1d", "1m"} and "SBIN" in cat and "BAD" not in cat and "RELIANCE" in cat
size, files = store.folder_size(root.parent)
assert files >= 8 and size > 10000 and "MB" in store.nice_size(5 * 1024 * 1024)

# ---- an expired option contract opens from the option archive, at any interval
opt = bars("2026-11-10", 3)
opt["OI"] = 5000.0
f = root / "NIFTY" / "2026-11-17" / "NIFTY261117C25000.parquet"
optstore._write(f, opt)
(root / "index.json").write_text('{"NIFTY261117C25000": {"file": "NIFTY/2026-11-17/NIFTY261117C25000.parquet", '
                                 '"first": "x", "last": "y", "rows": 1125}}')
got, lab = store.saved("NIFTY261117C25000", "1m", "max")
assert len(got) == len(opt) and lab == "NIFTY261117C25000 · 1m · saved"
got, lab = store.option_bars("NIFTY261117C25000", "15m")
assert len(got) == 3 * 25 and lab.endswith("15m · saved")
got, lab = store.option_bars("NIFTY261117C25000", "1d")
assert len(got) == 3 and abs(got["Volume"].iloc[0] - 375000.0) < 1e-6
assert store.option_bars("NIFTY261117C99999", "1m") is None

# ---- moving an older local store into the cloud folder: files go across, what is already there stays
src = tmp / "old" / "options"
(src / "NIFTY").mkdir(parents=True)
(src / "NIFTY" / "a.parquet").write_text("A")
(src / "index.json").write_text("{}")                          # the new folder already has its own index
n = store.move_into(src, root)
assert n == 1 and (root / "NIFTY" / "a.parquet").read_text() == "A" and "NIFTY261117C25000" in (root / "index.json").read_text()
assert store.move_into(src, root) == 0
assert store.cloud_of(tmp) == ""
store.disable()
shutil.rmtree(tmp, ignore_errors=True)
print("STORE OK")
