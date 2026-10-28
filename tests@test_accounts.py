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
# order(): paper prints only, live sends exactly one POST
n = len(calls)
r = D.place_order("111", "tokA", "RELIANCE", "buy", 5, live=False)
assert r["paper"] and len(calls) == n and r["request"]["quantity"] == 5
r = D.place_order("111", "tokA", "RELIANCE", "SELL", 2, "LIMIT", 100.5, live=True)
assert len(calls) == n + 1 and calls[-1][0].endswith("/orders") and calls[-1][1]["transactionType"] == "SELL" and calls[-1][1]["price"] == 100.5
A.save_accounts([{"name": "P", "broker": "dhan", "client_id": "9", "access_token": "t", "live": False},
                 {"name": "L", "broker": "dhan", "client_id": "8", "access_token": "u", "live": True}], tmp)
w.editor.setPlainText("import chartlab\nchartlab.order('NIFTY','BUY',1)\n")
import chartlab.dhan_core as D2
w.run_multi(["P"])
t0 = time.time()
while any(p.state() != QtCore.QProcess.ProcessState.NotRunning for p in w._multi):
    app.processEvents(); time.sleep(0.02)
    assert time.time() - t0 < 60
lg = w.log_view.toPlainText()
assert "[P] [PAPER] BUY 1 NIFTY" in lg and "Traceback" not in lg
print(lg.split("running on")[-1])
print("ACCOUNTS OK")
