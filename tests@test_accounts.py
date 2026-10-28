import os, sys, json, time, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, ".")
from pathlib import Path
from PySide6 import QtWidgets, QtCore
app = QtWidgets.QApplication([])
from chartlab import accounts as A
import pandas as pd

tmp = Path(tempfile.mkdtemp()) / "accounts.json"
A.save_accounts([{"name": "A1", "broker": "dhan", "client_id": "111", "access_token": "tokA"},
                 {"name": "A2", "broker": "dhan", "client_id": "222", "access_token": "tokB"}], tmp)
assert oct(tmp.stat().st_mode)[-3:] == "600"
assert A.get_account("A2", tmp)["client_id"] == "222"

# fake Dhan API
calls = []
def fake_http(url, body, headers, timeout=30):
    calls.append((url, body, headers))
    n = 5
    base = 1_700_000_000
    return json.dumps({"open": [100 + i for i in range(n)], "high": [102 + i for i in range(n)],
                       "low": [99 + i for i in range(n)], "close": [101 + i for i in range(n)],
                       "volume": [1000] * n, "timestamp": [base + 86400 * i for i in range(n)]}).encode()
import chartlab.dhan_core as D
A._http = D._http = fake_http
A.resolve_symbol = D.resolve_symbol = lambda s: ("1333", "NSE_EQ", "EQUITY", s)
df = A.fetch_dhan({"client_id": "111", "access_token": "tokA"}, "HDFCBANK", "1d", "1mo")
assert len(df) == 5 and calls[0][0].endswith("/charts/historical") and calls[0][2]["client-id"] == "111"
df = A.fetch_dhan({"client_id": "111", "access_token": "tokA"}, "HDFCBANK", "5m", "1mo")
assert any(c[0].endswith("/charts/intraday") for c in calls)
assert A.resolve_symbol("x")[0] == "1333"

# 1980-epoch timestamps are handled
fr = A._frame({"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1], "start_Time": [1_400_000_000 - A._EPOCH_1980]})
assert fr["Date"].iloc[0].year == 2014

# multi-account run: two real subprocesses at once, each sees only its own credentials
A.PATH = tmp
from chartlab.mainwindow import MainWindow
w = MainWindow(QtCore.QSettings("ChartLabTest", "acct"))
w.editor.setPlainText("import chartlab, time\na = chartlab.account()\ntime.sleep(1)\nprint('I am', a['account'], a['client_id'], a['access_token'])\n")
import numpy as np
idx = pd.date_range("2025-01-01", periods=50, freq="D"); c = np.linspace(1, 2, 50)
w.active.set_dataframe(pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1.0}, index=idx), "T")
w._fill_api_sources()
assert w.source.findData("api:A1") >= 0
t0 = time.time()
r = w.run_multi(["A1", "A2"])
assert r["ok"]
while any(p.state() != QtCore.QProcess.ProcessState.NotRunning for p in w._multi):
    app.processEvents(); time.sleep(0.02)
    assert time.time() - t0 < 60
log = w.log_view.toPlainText()
print(log)
assert "[A1] I am A1 111 tokA" in log and "[A2] I am A2 222 tokB" in log
assert "tokA" not in log.split("[A2]")[1].split("\n")[0]
# ---- fast routing: fake Session so no network; 5 live accounts must fire in parallel
import threading
sent = []
def fake_req(self, method, path, body=None):
    time.sleep(0.25)
    sent.append((self.hdr["client-id"], path, body, time.perf_counter()))
    return {"orderId": "1", "orderStatus": "TRANSIT"}
D.Session.request = fake_req
D.instrument = lambda sym, ex="NSE": {"security_id": "1333", "segment": "NSE_EQ", "instrument": "EQUITY", "lot": 1}
accts = [{"name": f"L{i}", "client_id": str(i), "access_token": "t", "live": True, "qty_mult": 1 + i} for i in range(5)]
accts.append({"name": "PAPER", "client_id": "99", "access_token": "t", "live": False})
t0 = time.perf_counter()
res = D.place_many(accts, "RELIANCE", "buy", 10)
el = time.perf_counter() - t0
assert el < 0.6, el                                   # serial would be 1.25 s
assert [r["request"]["quantity"] for r in res[:5]] == [10, 20, 30, 40, 50]
assert res[5].get("paper") and len(sent) == 5
spread = max(x[3] for x in sent) - min(x[3] for x in sent)
assert spread < 0.15, spread                          # all released together
# one failing account does not block the rest
def flaky(self, method, path, body=None):
    if self.hdr["client-id"] == "2":
        raise RuntimeError("boom")
    return {"ok": 1}
D.Session.request = flaky
res = D.place_many(accts, "RELIANCE", "SELL", 1, "LIMIT", 5.0)
assert [r["ok"] for r in res] == [True, True, False, True, True, True]
# options: lots x lot-size, per-account multiplier, nearest expiry
import datetime as _d
tbl = pd.DataFrame({"inst": ["OPTIDX"] * 3, "und": ["NIFTY"] * 3, "sid": [111, 222, 333], "lot": [75] * 3,
                    "expiry": [_d.date.today() + _d.timedelta(days=d) for d in (7, 1, 14)],
                    "strike": [24500.0] * 3, "type": ["CE"] * 3})
D._options_table = lambda: tbl
o = D.option("NIFTY", 24500, "CE")
assert o["security_id"] == "222" and o["lot"] == 75
D.instrument = lambda sym, ex="NSE": sym if isinstance(sym, dict) else None
D.Session.request = fake_req
sent.clear()
D.place_many(accts[:2], o, "BUY", lots=2)
assert [x[2]["quantity"] for x in sent] == [150, 300] or sorted(x[2]["quantity"] for x in sent) == [150, 300]

# ---- one-script broadcast mode, paper accounts, real subprocess
A.save_accounts([{"name": "P1", "broker": "dhan", "client_id": "1", "access_token": "a", "live": False},
                 {"name": "P2", "broker": "dhan", "client_id": "2", "access_token": "b", "live": False, "qty_mult": 2}], tmp)
w.editor.setPlainText("import chartlab\nprint('n accounts', len(chartlab.accounts()))\nchartlab.order('NIFTY','BUY',lots=0,qty=3)\n")
w.run_multi(["P1", "P2"], broadcast=True)
t0 = time.time()
while any(p.state() != QtCore.QProcess.ProcessState.NotRunning for p in w._multi):
    app.processEvents(); time.sleep(0.02)
    assert time.time() - t0 < 60
lg = w.log_view.toPlainText()
assert "n accounts 2" in lg and "[PAPER] P1: BUY 3 NIFTY" in lg and "[PAPER] P2: BUY 6 NIFTY" in lg, lg
assert "Traceback" not in lg
print("ACCOUNTS OK")
