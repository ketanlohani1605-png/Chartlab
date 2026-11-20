"""Run a user's own standalone Python script (their own data loader, parameter grids, ...) in a separate
process with THEIR Python, and bring its trades / equity back into the chart.

Inside the script:  import chartlab
    df = chartlab.data()                     # the OHLCV of the active chart (DataFrame), optional
    df = chartlab.history("NIFTY", "5m")     # anything in the data store: any symbol, any interval, option contracts
    chartlab.contracts("NIFTY")              # the option archive as a table; chartlab.option_data(symbol) reads one
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

def option_data(symbol):
    """A contract's stored 1-minute history from ChartLab's option archive (Settings -> Data): Date index, Open, High,
    Low, Close, Volume, OI. option_data("NIFTY261124C25000"); option_data("NIFTY") is the index itself."""
    import pandas as pd
    base = os.environ.get("CHARTLAB_OPTIONS", "")
    idx = option_index()
    hit = idx.get(str(symbol).strip().upper())
    if not hit:
        raise KeyError(f"{symbol} is not in the option archive ({base})")
    df = pd.read_parquet(os.path.join(base, hit["file"]))
    return df.set_index("Date") if "Date" in df.columns else df

def option_index():
    """Everything in the option archive: {contract: {'file', 'first', 'last', 'rows'}}."""
    base = os.environ.get("CHARTLAB_OPTIONS", "")
    try:
        return json.load(open(os.path.join(base, "index.json")))
    except Exception:
        return {}

def contracts(underlying="", expiry=None, kind=""):
    """The option archive as a table (one row per stored contract): symbol, und, expiry, strike, kind, first, last,
    rows. contracts("NIFTY"), contracts("NIFTY", "2026-11-24", "CE"). Feed a symbol to option_data()."""
    import re
    import pandas as pd
    und = str(underlying).strip().upper()
    exp = str(pd.Timestamp(expiry).date()) if expiry else ""
    k = {"C": "CE", "P": "PE", "CALL": "CE", "PUT": "PE"}.get(str(kind).strip().upper(), str(kind).strip().upper())
    pat = re.compile(r"^([A-Z][A-Z0-9&\\-]*?)(\\d{2})(\\d{2})(\\d{2})([CP])(\\d+(?:\\.\\d+)?)$")
    rows = []
    for name, e in sorted(option_index().items()):
        m = pat.match(name)
        if e.get("spot") or not (m or e.get("expiry")):
            continue
        d = {"symbol": name, "und": e.get("und") or m[1], "expiry": str(e.get("expiry") or f"20{m[2]}-{m[3]}-{m[4]}"),
             "strike": float(e.get("strike") or m[6]), "kind": e.get("kind") or ("CE" if m[5] == "C" else "PE"),
             "first": e.get("first"), "last": e.get("last"), "rows": e.get("rows")}
        if (und and d["und"] != und) or (exp and d["expiry"] != exp) or (k and d["kind"] != k):
            continue
        rows.append(d)
    return pd.DataFrame(rows, columns=["symbol", "und", "expiry", "strike", "kind", "first", "last", "rows"])

def expiries(underlying):
    """The expiries of this name that the option archive holds (expired ones too), oldest first."""
    return sorted(set(contracts(underlying)["expiry"]))

_IDX = {"NIFTY50": "NIFTY", "^NSEI": "NIFTY", "NIFTYBANK": "BANKNIFTY", "^NSEBANK": "BANKNIFTY", "^BSESN": "SENSEX",
        "^INDIAVIX": "INDIAVIX"}

def _key(symbol):
    import re
    s = str(symbol).strip().upper().replace(" ", "")
    ex, s = (s.split(":", 1) if ":" in s else ("", s))
    if s.endswith(".NS"):
        s = s[:-3]
    elif s.endswith(".BO"):
        s, ex = s[:-3], "BSE"
    s = _IDX.get(s, s)
    if ex == "BSE":
        s = "BSE_" + s
    elif ex and ex != "NSE":
        s = ex + "_" + s
    return re.sub(r"[^A-Z0-9&_.\\-]", "_", s.lstrip("^")) or "_"

def _bars(folder):
    import glob
    import pandas as pd
    parts = [pd.read_parquet(f) for f in sorted(glob.glob(os.path.join(folder, "*.parquet")))]
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts)
    if "Date" in df.columns:
        df = df.set_index("Date")
    return df[~df.index.duplicated(keep="last")].sort_index()

def _resample(df, interval):
    """Bigger bars from smaller ones; minutes / hours start at each day's first bar (09:15 on NSE)."""
    import re
    import numpy as np
    import pandas as pd
    m = re.fullmatch(r"(\\d+)(m|h|d)", str(interval).strip().lower())
    if not m or not len(df):
        return df
    n, u = int(m[1]), m[2]
    how = {c: a for c, a in (("Open", "first"), ("High", "max"), ("Low", "min"), ("Close", "last"),
                             ("Volume", "sum"), ("OI", "last")) if c in df.columns}
    day = df.index.normalize()
    if u == "d":
        key = day
    else:
        step = n * (3600 if u == "h" else 60)
        tod = (df.index - day).total_seconds().values
        a = float(tod.min())
        key = day + pd.to_timedelta(a + np.floor((tod - a) / step) * step, unit="s")
    out = df.groupby(np.asarray(key)).agg(how)
    out.index = pd.DatetimeIndex(out.index, name="Date")
    return out

def history(symbol, interval="1m", start=None, end=None):
    """Bars from ChartLab's data store (Settings -> Data): everything the charts, the watchlist's daily job and the
    option archive have saved - usually far more than a broker or Yahoo hands out in one go.
        history("RELIANCE", "5m")                       any interval: built from the saved 1-minute bars
        history("NIFTY", "1d", start="2025-01-01")
        history("NIFTY261124C25000")                    an option contract (with OI), expired ones too
    -> DataFrame: Date index, Open, High, Low, Close, Volume (and OI for options). KeyError when nothing is saved."""
    import re
    import pandas as pd
    name = str(symbol).strip().upper()
    iv = str(interval).strip()
    saved = option_index()
    if name in saved and re.match(r"^[A-Z][A-Z0-9&\\-]*?\\d{6}[CP]\\d", name):      # a contract of the option archive
        df = option_data(name)
        if iv != "1m":
            df = _resample(df, iv)
    else:
        root = os.environ.get("CHARTLAB_CANDLES", "")
        folder = os.path.join(root, _key(symbol))
        df = _bars(os.path.join(folder, iv))
        if iv != "1m" and iv[-1:] in ("m", "h", "d"):
            one = _bars(os.path.join(folder, "1m"))
            if len(one) and iv[-1:] != "d":
                built = _resample(one, iv)
                df = built if not len(df) else pd.concat([built[built.index < df.index[0]], df])
            elif len(one) and not len(df):
                df = _resample(one, iv)
        if not len(df) and name in saved:                    # an index the option archive follows: its 1-minute bars
            df = _resample(option_data(name), iv) if iv != "1m" else option_data(name)
        if not len(df):
            raise KeyError(f"{symbol} ({iv}) is not in ChartLab's data store ({root}). Open its chart once or put it "
                           "in the watchlist: it is saved from then on.")
    if start is not None:
        df = df[df.index >= pd.Timestamp(start)]
    if end is not None:
        df = df[df.index <= pd.Timestamp(end)]
    return df

def stored():
    """What the data store holds: {'candles': {symbol: [intervals]}, 'options': number of contracts and indices}."""
    root = os.environ.get("CHARTLAB_CANDLES", "")
    out = {}
    if os.path.isdir(root):
        for k in sorted(os.listdir(root)):
            d = os.path.join(root, k)
            if os.path.isdir(d):
                ivs = sorted(i for i in os.listdir(d) if os.path.isdir(os.path.join(d, i)))
                if ivs:
                    out[k] = ivs
    return {"candles": out, "options": len(option_index())}

def account():
    """Credentials of the broker account this copy of the script runs on (separate-copies mode), or {}."""
    if not os.environ.get("CHARTLAB_ACCOUNT"):
        return {}
    return {k[len("CHARTLAB_"):].lower(): v for k, v in os.environ.items()
            if k.startswith("CHARTLAB_") and k not in ("CHARTLAB_OUT", "CHARTLAB_DATA", "CHARTLAB_PYTHON",
                                                     "CHARTLAB_ACCOUNTS_FILE", "CHARTLAB_OPTIONS",
                                                     "CHARTLAB_CANDLES", "CHARTLAB_HOME")}

def accounts():
    """All accounts this run trades on: every ticked account (one-script mode) or just this one."""
    f = os.environ.get("CHARTLAB_ACCOUNTS_FILE")
    if f:
        return json.load(open(f))
    a = account()
    if not a:
        return []
    acct = {k: v for k, v in a.items() if k not in ("account", "live")}
    acct.update({"name": a["account"], "broker": a.get("broker", "dhan"), "live": a.get("live") == "1", "qty_mult": 1})
    return [acct]

def warmup():
    """Open all broker connections now so the first order is instant. Call once at start."""
    import chartlab_dhan
    r = chartlab_dhan.warmup(accounts())
    print("[chartlab] connections ready:", r)
    return r

def option(underlying, strike, kind, expiry=None):
    """Option contract, e.g. chartlab.option('NIFTY', 24500, 'CE'); expiry None = nearest."""
    import chartlab_dhan
    return chartlab_dhan.option(underlying, strike, kind, expiry)

def order(symbol, side, qty=0, order_type="MARKET", price=None, product="INTRADAY", exchange="NSE", lots=0,
          trigger=None):
    """Order on ALL accounts of this run at the same instant (in parallel), whatever their broker (Dhan, Zerodha,
    Upstox, Angel One, Fyers). symbol: 'RELIANCE', 'NIFTY 24NOV 25000 CE', 'BANKNIFTY FUT' ... or an option()
    contract. For F&O use lots=1 (quantity = lots x lot size, scaled by each account's multiplier).
    order_type: MARKET, LIMIT (price=), SL (price= and trigger=), SL-M (trigger=).
    Paper accounts only print. Returns one result per account with latency in ms."""
    accts = accounts()
    if not accts:
        raise RuntimeError("chartlab.order() needs accounts: use Accounts -> Run script on multiple accounts")
    if all(a.get("broker", "dhan") == "dhan" for a in accts) and str(order_type).upper() in ("MARKET", "LIMIT"):
        import chartlab_dhan
        return chartlab_dhan.place_many(accts, symbol, side, qty, order_type, price, product, exchange, lots)
    import chartlab_brokers as B
    if isinstance(symbol, dict):
        symbol = symbol.get("symbol") or symbol.get("name") or ""
    sym = str(symbol)
    if exchange and exchange.upper() not in ("NSE", "") and ":" not in sym:
        sym = f"{exchange.upper()}:{sym}"
    req = B.OrderRequest(sym, side, qty=int(qty or 0), lots=int(lots or 0), order_type=order_type,
                         price=float(price or 0), trigger=float(trigger or 0), product=product, tag="chartlab")
    live = [a for a in accts if a.get("live")]
    res = B.place_many(live, req) if live else []
    for r in res:
        if r["ok"]:
            print(f"[LIVE] {r['account']} ({r.get('broker')}): {side} {qty or str(lots) + ' lot'} {sym} {order_type}"
                  f" -> order {r.get('order_id')} ({r['ms']} ms)")
        else:
            print(f"[FAILED] {r['account']}: {r['error']}")
    for a in accts:
        if not a.get("live"):
            print(f"[PAPER] {a['name']}: {side} {qty or str(lots) + ' lot'} {sym} {order_type}")
            res.append({"account": a["name"], "ok": True, "paper": True, "ms": 0})
    return res

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


def helper_sources() -> dict:
    """Source text of the modules a script can import (chartlab_dhan, chartlab_brokers): {'dhan_core.py': ...,
    'brokers/base.py': ...}. Run from source they are read from the package itself; the built Mac app has no .py
    files of its own, so it carries copies in assets/script (tests keep the copies identical)."""
    here = Path(__file__).parent
    out = {}
    if (here / "dhan_core.py").exists() and (here / "brokers" / "base.py").exists():
        out["dhan_core.py"] = (here / "dhan_core.py").read_text()
        for f in sorted((here / "brokers").glob("*.py")):
            out["brokers/" + f.name] = f.read_text()
        return out
    root = Path(getattr(sys, "_MEIPASS", here.parent)) / "assets" / "script"
    if not (root / "dhan_core.py").exists():
        raise RuntimeError("The script helper files are missing from this copy of ChartLab (assets/script).")
    out["dhan_core.py"] = (root / "dhan_core.py").read_text()
    for f in sorted((root / "brokers").glob("*.py")):
        out["brokers/" + f.name] = f.read_text()
    return out


def prepare(df: pd.DataFrame | None) -> Path:
    d = Path(tempfile.mkdtemp(prefix="chartlab_run_"))
    (d / "chartlab.py").write_text(HELPER)
    (d / "_runner.py").write_text(RUNNER)
    src = helper_sources()
    (d / "chartlab_dhan.py").write_text(src["dhan_core.py"])
    pkg = d / "chartlab_brokers"                       # every broker adapter, for orders on any broker
    pkg.mkdir()
    for name, text in src.items():
        if name.startswith("brokers/"):
            (pkg / name.split("/", 1)[1]).write_text(text)
    if df is not None:
        df.to_parquet(d / "data.parquet")
    return d


def environment(workdir: Path, df) -> dict:
    env = dict(os.environ)
    env["CHARTLAB_OUT"] = str(workdir)
    env["CHARTLAB_DATA"] = str(workdir / "data.parquet") if df is not None else ""
    env["PYTHONUNBUFFERED"] = "1"
    try:
        from PySide6 import QtCore as _qc
        from . import optstore, store
        store.flush()                                        # the script reads the files: write what is still waiting
        opts = store.STATE["root"] or optstore.root(
            str(_qc.QSettings("ChartLab", "ChartLab").value("opt_folder", "") or ""))
        env["CHARTLAB_OPTIONS"] = str(opts)
        env["CHARTLAB_CANDLES"] = str(store.candles_root(opts))
    except Exception:  # noqa: BLE001
        env["CHARTLAB_OPTIONS"] = str(Path.home() / "ChartLab" / "options")
        env["CHARTLAB_CANDLES"] = str(Path.home() / "ChartLab" / "candles")
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
