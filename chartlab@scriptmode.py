"""Run a user's own standalone Python script (their own data loader, parameter grids, ...) in a separate
process with THEIR Python, and bring its trades / equity back into the chart.

Inside the script:  import chartlab
    df = chartlab.data()                     # the OHLCV of the active chart (DataFrame), optional
    chartlab.report(trades=trades_df, equity=equity_series, plots={"EMA": series})
If the script never calls report(), a DataFrame that looks like trades (has entry/exit time + pnl) and a Series/
DataFrame called equity/equity_curve are picked up automatically from the script's globals."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

HELPER = '''"""chartlab helper injected into your script run (do not edit)."""
import os, json
_OUT = os.environ.get("CHARTLAB_OUT", "")
_DATA = os.environ.get("CHARTLAB_DATA", "")
_state = {}

def data():
    import pandas as pd
    if not _DATA or not os.path.exists(_DATA):
        raise RuntimeError("No chart data is loaded in ChartLab")
    return pd.read_parquet(_DATA)

def account():
    """Credentials of the broker account this copy of the script runs on (multi-account mode), or {}."""
    if not os.environ.get("CHARTLAB_ACCOUNT"):
        return {}
    return {k[len("CHARTLAB_"):].lower(): v for k, v in os.environ.items()
            if k.startswith("CHARTLAB_") and k not in ("CHARTLAB_OUT", "CHARTLAB_DATA", "CHARTLAB_PYTHON")}

def order(symbol, side, qty, order_type="MARKET", price=None, product="INTRADAY", exchange="NSE"):
    """Place an order on THIS account (multi-account mode). Paper unless the account is set to Live orders."""
    a = account()
    if not a:
        raise RuntimeError("chartlab.order() needs an account: use Accounts -> Run script on multiple accounts")
    import chartlab_dhan
    return chartlab_dhan.place_order(a["client_id"], a["access_token"], symbol, side, qty, order_type, price,
                                     product, exchange, live=a.get("live") == "1", tag=a["account"])

def report(trades=None, equity=None, plots=None):
    import pandas as pd
    if trades is not None:
        pd.DataFrame(trades).to_parquet(os.path.join(_OUT, "trades.parquet"))
        _state["trades"] = True
    if equity is not None:
        e = equity if isinstance(equity, pd.Series) else pd.DataFrame(equity).iloc[:, 0]
        e.rename("equity").to_frame().to_parquet(os.path.join(_OUT, "equity.parquet"))
        _state["equity"] = True
    if plots:
        pd.DataFrame({str(k): pd.Series(v) for k, v in plots.items()}).to_parquet(os.path.join(_OUT, "plots.parquet"))
'''

RUNNER = '''
import os, sys, runpy, traceback
script = sys.argv[1]
out = os.environ["CHARTLAB_OUT"]
sys.path.insert(0, out)
sys.path.insert(0, os.path.dirname(script))
os.chdir(os.path.dirname(script))
try:
    g = runpy.run_path(script, run_name="__main__")
except SystemExit:
    g = {}
except BaseException:
    traceback.print_exc()
    sys.exit(1)
try:
    import pandas as pd, chartlab
    if not chartlab._state.get("trades"):
        for k, v in g.items():
            if isinstance(v, pd.DataFrame) and not k.startswith("_"):
                cols = {str(c).lower() for c in v.columns}
                if "pnl" in cols or ({"entry_price", "exit_price"} <= cols):
                    chartlab.report(trades=v); print("[chartlab] trades picked up from variable '%s'" % k); break
    if not chartlab._state.get("equity"):
        for k in ("equity", "equity_curve", "eq", "equity_series"):
            v = g.get(k)
            if isinstance(v, (pd.Series, pd.DataFrame)) and len(v) > 2:
                chartlab.report(equity=v); print("[chartlab] equity picked up from variable '%s'" % k); break
except Exception:
    traceback.print_exc()
'''


def _has_pandas(exe: str) -> bool:
    try:
        r = subprocess.run([exe, "-c", "import pandas, numpy, pyarrow"], capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def find_python(saved: str = "") -> tuple[str, list[str]]:
    """Return (interpreter, notes). Prefers the user's saved choice, then CHARTLAB_PYTHON, PATH and common installs."""
    cands = [saved, os.environ.get("CHARTLAB_PYTHON", "")]
    for name in ("python3", "python"):
        w = shutil.which(name)
        if w:
            cands.append(w)
    home = Path.home()
    cands += ["/opt/homebrew/bin/python3", "/usr/local/bin/python3",
              str(home / "anaconda3/bin/python"), str(home / "miniconda3/bin/python"),
              str(home / "miniforge3/bin/python"), "/opt/anaconda3/bin/python",
              "/Library/Frameworks/Python.framework/Versions/Current/bin/python3"]
    pyenv = home / ".pyenv" / "versions"
    if pyenv.exists():
        cands += [str(p / "bin" / "python") for p in sorted(pyenv.iterdir(), reverse=True)]
    if not getattr(sys, "frozen", False):
        cands.append(sys.executable)
    seen, notes = set(), []
    for c in cands:
        if not c or c in seen or not Path(c).exists() or c == "/usr/bin/python3":
            continue
        seen.add(c)
        if _has_pandas(c):
            return c, notes
        notes.append(f"{c}: pandas/numpy/pyarrow missing")
    raise RuntimeError(
        "No Python with pandas + numpy + pyarrow was found. Set your Python in Strategy -> Script Python... "
        "(the one you normally run your scripts with). Checked: " + ("; ".join(notes) or "nothing"))


def prepare(df: pd.DataFrame | None) -> Path:
    d = Path(tempfile.mkdtemp(prefix="chartlab_run_"))
    (d / "chartlab.py").write_text(HELPER)
    (d / "_runner.py").write_text(RUNNER)
    (d / "chartlab_dhan.py").write_text((Path(__file__).parent / "dhan_core.py").read_text())
    if df is not None:
        df.to_parquet(d / "data.parquet")
    return d


def environment(workdir: Path, df) -> dict:
    env = dict(os.environ)
    env["CHARTLAB_OUT"] = str(workdir)
    env["CHARTLAB_DATA"] = str(workdir / "data.parquet") if df is not None else ""
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _col(df, *names):
    low = {str(c).lower().replace(" ", "_"): c for c in df.columns}
    for n in names:
        if n in low:
            return low[n]
    return None


def load_results(workdir: Path, chart_df: pd.DataFrame, capital: float):
    """Turn the files a script reported into a backtest.Result (or None if it reported nothing)."""
    from . import backtest as bt
    tp, ep = workdir / "trades.parquet", workdir / "equity.parquet"
    if not tp.exists() and not ep.exists():
        return None, {}
    idx = chart_df.index
    ts_ns = idx.values.astype("datetime64[ns]").astype("int64")

    def where(t):
        t = pd.Timestamp(t)
        if idx.tz is not None and t.tzinfo is None:
            t = t.tz_localize(idx.tz)
        if idx.tz is None and t.tzinfo is not None:
            t = t.tz_convert(None)
        i = int(np.searchsorted(ts_ns, t.value))
        return min(max(i, 0), len(idx) - 1)

    rows = []
    if tp.exists():
        t = pd.read_parquet(tp)
        c_et = _col(t, "entry_time", "entry_date", "entry_datetime", "entry", "entrytime", "open_time")
        c_xt = _col(t, "exit_time", "exit_date", "exit_datetime", "exit", "exittime", "close_time")
        c_ep = _col(t, "entry_price", "entryprice", "buy_price", "entry_px")
        c_xp = _col(t, "exit_price", "exitprice", "sell_price", "exit_px")
        c_q = _col(t, "qty", "quantity", "size", "shares")
        c_pnl = _col(t, "pnl", "profit", "net_pnl", "pl", "p&l")
        c_side = _col(t, "side", "direction", "type", "position")
        if c_et is None or c_xt is None:
            raise ValueError("Trades table needs entry and exit time columns (entry_time / exit_time). Found: "
                             + ", ".join(map(str, t.columns)))
        for r in t.to_dict("records"):
            ei, xi = where(r[c_et]), where(r[c_xt])
            epx = float(r[c_ep]) if c_ep and pd.notna(r[c_ep]) else float(chart_df["Close"].iloc[ei])
            xpx = float(r[c_xp]) if c_xp and pd.notna(r[c_xp]) else float(chart_df["Close"].iloc[xi])
            q = float(r[c_q]) if c_q and pd.notna(r[c_q]) else 1.0
            sd = str(r[c_side]).lower() if c_side else "long"
            short = sd.startswith(("s", "-")) or sd in ("sell",)
            pnl = float(r[c_pnl]) if c_pnl and pd.notna(r[c_pnl]) else ((epx - xpx) if short else (xpx - epx)) * q
            notional = abs(epx * q) or 1.0
            rows.append({"entry_idx": ei, "exit_idx": xi, "entry_time": idx[ei], "exit_time": idx[xi],
                         "entry_price": epx, "exit_price": xpx, "qty": q, "pnl": pnl, "pnl_pct": pnl / notional,
                         "bars": xi - ei, "open": False, "side": "short" if short else "long",
                         "reason": "script", "fees": 0.0})
    trades = pd.DataFrame(rows, columns=bt.TRADE_COLUMNS).sort_values("entry_idx").reset_index(drop=True)

    if ep.exists():
        e = pd.read_parquet(ep)["equity"]
        if not isinstance(e.index, pd.DatetimeIndex):
            e.index = idx[: len(e)] if len(e) <= len(idx) else pd.RangeIndex(len(e))
        e = e.astype(float)
        eq = e.reindex(idx, method="ffill") if isinstance(e.index, pd.DatetimeIndex) else e
        eq = eq.bfill().fillna(capital)
        if eq.abs().max() < capital * 0.01 and len(trades):      # looks like cumulative P&L, not equity
            eq = eq + capital
    else:
        step = np.zeros(len(idx))
        np.add.at(step, trades.exit_idx.to_numpy(int), trades.pnl.to_numpy(float))
        eq = pd.Series(capital + np.cumsum(step), index=idx)
    eq.name = "Strategy"
    c = chart_df["Close"].to_numpy(float)
    bh = pd.Series(c / c[0] * capital, index=idx, name="Buy & hold")
    res = bt.Result(trades, eq, bh, bt._metrics(chart_df, eq, bh, trades, capital))
    res.metrics["exposure"] = float("nan")
    res.metrics["fees_paid"] = 0.0
    plots = {}
    pp = workdir / "plots.parquet"
    if pp.exists():
        p = pd.read_parquet(pp)
        for col in p.columns:
            s = p[col]
            if isinstance(s.index, pd.DatetimeIndex):
                s = s.reindex(idx, method="ffill")
            plots[col] = pd.Series(s.to_numpy(float)[: len(idx)], index=idx[: len(s)]).reindex(idx)
    return res, plots
