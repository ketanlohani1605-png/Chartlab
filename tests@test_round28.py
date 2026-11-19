"""v0.28: many accounts - paste a list, check all at once, one table with balance / positions / P&L, token renewal
(by hand, for all, and automatic when switched on). Runs against a fake Dhan."""
import os, sys, json, tempfile, time, datetime as dt
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, ".")
from pathlib import Path
from PySide6 import QtWidgets, QtCore, QtTest
from chartlab import theme
theme.apply_pg_config()
app = QtWidgets.QApplication([])
app.setStyleSheet(theme.STYLESHEET)
import chartlab.dhan_core as D
from chartlab import accounts as A

tmp = Path(tempfile.mkdtemp())
A.PATH = tmp / "accounts.json"


def tok(tag):
    return f"eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzUxMiJ9.eyJpc3MiOiJkaGFuIiwiY2xpZW50IjoiMTEwMDAwIn0{tag}.sig0123456789abcdefghijklmnopqrstuvwxyzABCD"


HOURS = {"1100000001": 20.0, "1100000002": 2.0, "1100000003": -1.0}      # token time left per client
renewed, calls = [], []


def fake_http(url, body, headers, timeout=30):
    calls.append(url)
    cid = headers.get("client-id") or headers.get("dhanClientId") or next(
        (c for c in HOURS if c[-1] in headers.get("access-token", "")[-60:-40]), "")
    t = headers.get("access-token", "")
    cid = next((c for c in HOURS if f"c{c[-1]}" in t), cid)
    if "bad" in t:
        raise RuntimeError("Dhan API error 401: DH-901 Invalid token")
    if url.endswith("/profile"):
        left = HOURS[cid] if "new" not in t else 24.0
        when = dt.datetime.now() + dt.timedelta(hours=left)
        return json.dumps({"dhanClientId": cid, "tokenValidity": when.strftime("%d/%m/%Y %H:%M"),
                           "activeSegment": "Equity, Derivative", "dataPlan": "Active" if cid.endswith("1") else "Deactive"}).encode()
    if url.endswith("/fundlimit"):
        return json.dumps({"availabelBalance": 100000.0 * int(cid[-1])}).encode()
    if url.endswith("/positions"):
        return json.dumps([{"positionType": "LONG", "netQty": 75, "realizedProfit": 0.0, "unrealizedProfit": 1500.5},
                           {"positionType": "CLOSED", "netQty": 0, "realizedProfit": -200.0, "unrealizedProfit": 0.0}]).encode()
    if url.endswith("/RenewToken"):
        if HOURS[cid] <= 0 and "new" not in t:
            raise RuntimeError("Dhan API error 400: token expired")
        renewed.append(cid)
        return json.dumps({"status": "success", "data": {"accessToken": tok(f"c{cid[-1]}new{len(renewed)}"),
                                                         "expiryTime": "2026-11-17T10:00:00"}}).encode()
    raise RuntimeError("unexpected " + url)


A._http = D._http = fake_http


def pump(ms=40):
    QtTest.QTest.qWait(ms)


def wait(cond, ms=5000):
    end = time.time() + ms / 1000
    while time.time() < end:
        if cond():
            return True
        pump(20)
    return bool(cond())


# ---------------------------------------------------------------- 1) status: 24-hour tokens, positions, P&L
a1 = {"name": "Main", "broker": "dhan", "client_id": "1100000001", "access_token": tok("c1"), "live": False}
st = A.get_status(a1)
assert st["state"] == "ok" and "20 h" in st["msg"], st                    # 20 hours left is healthy, not a warning
assert st["funds"] == 100000.0 and st["open"] == 1 and abs(st["pnl"] - 1300.5) < 1e-6 and st["data"] is True
st2 = A.get_status({**a1, "client_id": "1100000002", "access_token": tok("c2")})
assert st2["state"] == "expiring" and "2.0 h" in st2["msg"] and st2["data"] is False, st2
assert A.get_status({**a1, "client_id": "1100000003", "access_token": tok("c3")})["state"] == "expired"
assert A.get_status({**a1, "access_token": tok("bad")})["state"] == "auth"

# ---------------------------------------------------------------- 2) pasted list -> accounts
text = (f"name, client id, token\nPapa, 1100000001, {tok('c1')}\nFund 2\t1100000002\t{tok('c2')}\n"
        f"1100000003 {tok('c3')} live\nthis line is not an account\n")
accts, bad = A.parse_bulk(text)
assert [(x["name"], x["client_id"]) for x in accts] == [("Papa", "1100000001"), ("Fund 2", "1100000002"),
                                                        ("1100000003", "1100000003")], accts
assert bad == ["this line is not an account"] and not any(x["live"] for x in accts)     # never LIVE by paste

# ---------------------------------------------------------------- 3) Add many: checked together, only good ones added
dlg = A.AccountsDialog(None, A.PATH)
picked = {}


def drive_bulk():
    d = app.activeModalWidget()
    picked["type"] = type(d).__name__
    d.text.setPlainText(text)
    d.check()
    assert wait(lambda: d.add_btn.isEnabled()), d.note.text()
    picked["rows"] = [d.tbl.item(r, 2).text() for r in range(d.tbl.rowCount())]
    picked["btn"] = d.add_btn.text()
    d._add()


QtCore.QTimer.singleShot(150, drive_bulk)
dlg.add_many()
assert picked["type"] == "BulkAddDialog" and picked["btn"] == "Add 2 accounts", picked
assert "token good for 20 h" in picked["rows"][0] and "expires in 2.0 h" in picked["rows"][1] and "expired" in picked["rows"][2]
saved = A.load_accounts()
assert [x["name"] for x in saved] == ["Papa", "Fund 2"] and not any(x["live"] for x in saved)
assert oct(A.PATH.stat().st_mode & 0o777) == "0o600"                      # the file stays private

# ---------------------------------------------------------------- 4) renew one token by hand
dlg.list.setCurrentRow(1)
old = A.load_accounts()[1]["access_token"]
dlg.renew()
assert wait(lambda: A.load_accounts()[1]["access_token"] != old), dlg.msg.text()
assert "new" in A.load_accounts()[1]["access_token"] and renewed == ["1100000002"]
assert wait(lambda: "renewed" in dlg.msg.text()) and dlg.token.text() == A.load_accounts()[1]["access_token"]
assert A.load_accounts()[0]["access_token"] == tok("c1")                  # the other account is untouched
dlg.reject()

# an expired token cannot be renewed: clear error, nothing overwritten
A.save_accounts(A.load_accounts() + [{"name": "Old", "broker": "dhan", "client_id": "1100000003",
                                      "access_token": tok("c3"), "live": False}])
try:
    A.renew_saved("Old")
    raise SystemExit("an expired token must not renew")
except RuntimeError as e:
    assert "expired" in str(e)
assert A.load_accounts()[2]["access_token"] == tok("c3")

# ---------------------------------------------------------------- 5) All accounts table
ov = A.OverviewDialog(None, A.PATH)
assert wait(lambda: ov.tbl.item(2, 4) is not None and ov.tbl.item(2, 4).text() == "expired" and ov.check_btn.isEnabled()), "overview"
cell = lambda r, c: ov.tbl.item(r, c).text()  # noqa: E731
assert [cell(r, 0) for r in range(3)] == ["Papa", "Fund 2", "Old"] and cell(0, 2) == "PAPER"
assert cell(0, 4) == "20.0 h" and cell(1, 4) == "24.0 h" and cell(2, 4) == "expired", [cell(r, 4) for r in range(3)]
assert cell(0, 5) == "active" and cell(1, 5) == "not active" and cell(0, 7) == "1" and cell(0, 8) == "₹+1,300.50"
assert cell(3, 0) == "2 of 3 connected" and cell(3, 6) == "₹600,000.00" and cell(3, 8) == "₹+3,901.50"
ov.reject()

# ---------------------------------------------------------------- 6) automatic renewal (only when switched on)
from chartlab.mainwindow import MainWindow, quit_cleanly
HOURS["1100000001"] = 3.0                                                # Papa's token is now close to its end
_qs = QtCore.QSettings(str(tmp / "s.ini"), QtCore.QSettings.Format.IniFormat)
_qs.setValue("auto_renew", "false")                                      # (on by default since 0.34)
w = MainWindow(_qs)
w.show()
n0 = len(renewed)
w.acct_poller.refresh()
assert wait(lambda: A.STATUS.get("Papa", {}).get("state") == "expiring")
pump(300)
assert len(renewed) == n0 and A.load_accounts()[0]["access_token"] == tok("c1")       # switched off: nothing happens
w.settings.setValue("auto_renew", "true")
w.acct_poller.refresh()
assert wait(lambda: "new" in A.load_accounts()[0]["access_token"]), renewed
assert renewed[-1] == "1100000001" and renewed.count("1100000001") == 1
assert wait(lambda: A.STATUS.get("Papa", {}).get("state") == "ok")
w.acct_poller.refresh()
pump(400)
assert renewed.count("1100000001") == 1                                               # not renewed again
w.close()
quit_cleanly(w, 0, exit_process=False)
print("ROUND 28 OK")
os._exit(0)
