import os, sys, json, datetime as dt
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, ".")
from PySide6 import QtWidgets, QtCore, QtTest
app = QtWidgets.QApplication([])
from chartlab import accounts as A

def mk(valid_days, fund=1234.5, fail=None):
    def fake(url, body, headers, timeout=30):
        if fail:
            raise RuntimeError(fail)
        if url.endswith("/profile"):
            v = (dt.datetime.now() + dt.timedelta(days=valid_days)).strftime("%d/%m/%Y %H:%M")
            return json.dumps({"dhanClientId": "111", "tokenValidity": v, "activeSegment": "Equity, Derivative", "dataPlan": "Active"}).encode()
        return json.dumps({"availabelBalance": fund}).encode()
    return fake
acct = {"name": "A1", "broker": "dhan", "client_id": "111", "access_token": "x" * 50}
for days, state in ((5, "ok"), (0.5, "ok"), (0.1, "expiring"), (-1, "expired")):      # Dhan tokens live 24 h: warn in the last 4
    A._http = mk(days)
    st = A.get_status(acct); assert st["state"] == state, (days, st)
assert A.get_status(acct)["funds"] == 1234.5
A._http = mk(5, fail="Dhan API error 401: DH-901 invalid token"); assert A.get_status(acct)["state"] == "auth"
A._http = mk(5, fail="Could not reach Dhan (x). Check your internet."); assert A.get_status(acct)["state"] == "offline"
A._http = mk(5)
pl = A.StatusPoller(); got = []; pl.updated.connect(got.append); pl.refresh([acct])
for _ in range(50):
    QtTest.QTest.qWait(50)
    if got: break
assert got == ["A1"] and A.STATUS["A1"]["state"] == "ok"
assert A.overall_state([acct])[0] == "ok"
d = A.AccountsDialog(path=None); d.show(); app.processEvents()
d.name.setText("A1"); d.client.setText("111"); d.token.setText("x" * 50); d.test()
for _ in range(50):
    QtTest.QTest.qWait(50)
    if "Connected" in d.card.text(): break
assert "Connected" in d.card.text(), d.card.text()
d.grab().save("/tmp/acct.png")
print("API STATUS OK")
