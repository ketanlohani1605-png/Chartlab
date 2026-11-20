"""Script mode end-to-end: run a user's standalone script in a subprocess through the real main window."""
import os, sys, tempfile
from pathlib import Path
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np, pandas as pd
from PySide6 import QtCore, QtWidgets
app = QtWidgets.QApplication([])
from chartlab.mainwindow import MainWindow
st = QtCore.QSettings(tempfile.mktemp(suffix=".ini"), QtCore.QSettings.Format.IniFormat)
w = MainWindow(st); w.show()
idx = pd.date_range("2024-01-01 09:15", periods=400, freq="min")
c = 100 + np.cumsum(np.random.default_rng(3).normal(0, .1, 400))
w.active.set_dataframe(pd.DataFrame({"Open": c, "High": c + .1, "Low": c - .1, "Close": c, "Volume": 1.0}, index=idx), "T")
w.editor.setPlainText('''
import pandas as pd, chartlab
df = chartlab.data()
print("hello from user script", len(df))
trades = pd.DataFrame({"entry_time": [df.index[5]], "exit_time": [df.index[60]], "entry_price": [df.Close.iloc[5]],
                       "exit_price": [df.Close.iloc[60]], "pnl": [1234.5]})
''')
w.run_backtest()
import time
t0 = time.time()
while time.time() - t0 < 60 and w.active.result is None:
    app.processEvents(); time.sleep(0.05)
assert w.active.result is not None, w.log_view.toPlainText()
assert w.active.result.metrics["trades"] == 1 and abs(w.active.result.metrics["net_profit"] - 1234.5) < 1e-6
assert "hello from user script" in w.log_view.toPlainText()
# failing script shows traceback, keeps app alive
w.active.result = None
w.editor.setPlainText("raise RuntimeError('kaboom')")
w.run_backtest()
t0 = time.time()
while time.time() - t0 < 30 and "kaboom" not in w.log_view.toPlainText():
    app.processEvents(); time.sleep(0.05)
assert "kaboom" in w.log_view.toPlainText()
print("SCRIPT MODE OK")

# the built app reads the script helpers from assets/script: the copies must be the package's own files
import filecmp as _fc
from pathlib import Path as _P
_root = _P(__file__).resolve().parent.parent
_pairs = [(_root / "chartlab" / "dhan_core.py", _root / "assets" / "script" / "dhan_core.py")]
_pairs += [(f, _root / "assets" / "script" / "brokers" / f.name) for f in (_root / "chartlab" / "brokers").glob("*.py")]
for _a, _b in _pairs:
    assert _b.exists() and _fc.cmp(_a, _b, shallow=False), f"assets/script copy differs: {_b.name} (copy it again)"
assert len(list((_root / "assets" / "script" / "brokers").glob("*.py"))) == len(_pairs) - 1
print("SCRIPT HELPER COPIES OK")

# ---- a script reads the data store: any symbol at any interval, the option archive as a table
import json as _json, shutil as _sh, subprocess as _sp, tempfile as _tf
import numpy as _np, pandas as _pd
from chartlab import optstore as _opt, scriptmode as _sm, store as _store
_tmp = _P(_tf.mkdtemp(prefix="chartlab_script_store_"))
_root_o = _tmp / "ChartLab" / "options"
_store.enable(_root_o)
_idx = _pd.DatetimeIndex([d + _pd.Timedelta(minutes=555 + i) for d in _pd.bdate_range("2026-11-02", periods=10)
                          for i in range(375)], name="Date")
_c = 100 + _np.arange(len(_idx)) * 0.01
_one = _pd.DataFrame({"Open": _c, "High": _c + 1, "Low": _c - 1, "Close": _c + 0.5, "Volume": 10.0}, index=_idx)
_store.merge("RELIANCE", "1m", _one, force=True)
_day = _one.groupby(_one.index.normalize()).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
_store.merge("NIFTY", "1d", _day, force=True)
_o = _one.iloc[:750].copy(); _o["OI"] = 5000.0
for _name, _exp in (("NIFTY261110C25000", "2026-11-10"), ("NIFTY261110P25000", "2026-11-10"), ("NIFTY261117C25100", "2026-11-17")):
    _opt._write(_root_o / "NIFTY" / _exp / f"{_name}.parquet", _o)
_opt._write(_root_o / "NIFTY" / "spot_1m.parquet", _o)
_index = {"NIFTY261110C25000": {"file": "NIFTY/2026-11-10/NIFTY261110C25000.parquet", "first": "a", "last": "b", "rows": 750,
                                "und": "NIFTY", "expiry": "2026-11-10", "strike": 25000.0, "kind": "CE", "lot": 75},
          "NIFTY261110P25000": {"file": "NIFTY/2026-11-10/NIFTY261110P25000.parquet", "first": "a", "last": "b", "rows": 750},
          "NIFTY261117C25100": {"file": "NIFTY/2026-11-17/NIFTY261117C25100.parquet", "first": "a", "last": "b", "rows": 750},
          "NIFTY": {"file": "NIFTY/spot_1m.parquet", "first": "a", "last": "b", "rows": 750, "und": "NIFTY", "spot": True}}
(_root_o / "index.json").write_text(_json.dumps(_index))
_work = _sm.prepare(None)
_env = _sm.environment(_work, None)
assert _env["CHARTLAB_OPTIONS"] == str(_root_o) and _env["CHARTLAB_CANDLES"] == str(_tmp / "ChartLab" / "candles")
(_work / "t.py").write_text('''
import chartlab, pandas as pd
five = chartlab.history("NSE:RELIANCE", "5m")
assert len(five) == 10 * 75 and five.index[0] == pd.Timestamp("2026-11-02 09:15") and abs(five["Volume"].iloc[0] - 50) < 1e-9
assert len(chartlab.history("reliance", "1m", start="2026-11-11")) == 3 * 375
assert len(chartlab.history("RELIANCE", "1h")) == 10 * 7 and len(chartlab.history("RELIANCE", "1d")) == 10
assert len(chartlab.history("NIFTY", "1d", end="2026-11-06")) == 5
t = chartlab.contracts("NIFTY")
assert list(t["symbol"]) == ["NIFTY261110C25000", "NIFTY261110P25000", "NIFTY261117C25100"], list(t["symbol"])
assert list(t["kind"]) == ["CE", "PE", "CE"] and list(t["strike"]) == [25000.0, 25000.0, 25100.0]
assert list(chartlab.contracts("NIFTY", "2026-11-10", "PE")["symbol"]) == ["NIFTY261110P25000"]
assert chartlab.expiries("NIFTY") == ["2026-11-10", "2026-11-17"]
o = chartlab.history("NIFTY261110C25000", "15m")
assert "OI" in o.columns and len(o) == 2 * 25 and o["OI"].iloc[-1] == 5000.0
assert len(chartlab.option_data("NIFTY")) == 750 and len(chartlab.history("NIFTY261110C25000")) == 750
s = chartlab.stored()
assert s["candles"] == {"NIFTY": ["1d"], "RELIANCE": ["1m"]} and s["options"] == 4, s
try:
    chartlab.history("TCS", "5m"); raise SystemExit("no error for a symbol that is not stored")
except KeyError as e:
    assert "watchlist" in str(e)
for typed in ("reliance", "NSE:RELIANCE", "RELIANCE.NS", "BSE:TCS", "TCS.BO", "NIFTY", "^NSEI", "banknifty", "US:AAPL", "BTC-USD", "M&M"):
    print("KEY", typed, chartlab._key(typed))
print("HELPER STORE OK")
''')
_r = _sp.run([sys.executable, str(_work / "_runner.py"), str(_work / "t.py")], env=_env, capture_output=True, text=True, timeout=120)
assert "HELPER STORE OK" in _r.stdout, _r.stdout[-1500:] + _r.stderr[-3000:]
for _line in _r.stdout.splitlines():
    if _line.startswith("KEY "):
        _typed, _k = _line.split(" ")[1], _line.split(" ")[2]
        assert _store.key_for(_typed) == _k, (_typed, _k, _store.key_for(_typed))     # the script finds the app's folders
_store.disable()
_sh.rmtree(_tmp, ignore_errors=True)
print("SCRIPT STORE OK")
