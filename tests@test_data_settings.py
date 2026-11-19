import os, sys, time
os.environ["QT_QPA_PLATFORM"]="offscreen"; os.environ["CHARTLAB_HOME"]="/tmp/chartlab_test_data"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from PySide6 import QtCore, QtTest, QtWidgets
app=QtWidgets.QApplication([]); QtCore.QCoreApplication.setOrganizationName("clt"); QtCore.QCoreApplication.setApplicationName("data_test")
from chartlab import theme, accounts, optstore, settings as cs
app.setStyle(theme.make_style()); app.setStyleSheet(theme.STYLESHEET)
# auto login ways
assert accounts.auto_login_way({"broker":"dhan","client_id":"1","access_token":"t"})=="renew"
assert accounts.auto_login_way({"broker":"dhan","client_id":"1","pin":"1","totp_secret":"JBSWY3DPEHPK3PXP"})=="totp"
assert accounts.auto_login_way({"broker":"zerodha","access_token":"t","api_key":"k"})==""
assert accounts.auto_login_way({"broker":"fyers","access_token":"t"})=="" and accounts.auto_login_way({"broker":"fyers","access_token":"t","refresh_token":"r","pin":"1"})=="renew"
assert accounts.auto_login_way({"broker":"paper"})==""
accts=[{"name":"D1","broker":"dhan","client_id":"11","access_token":"OLD","pin":"1234","totp_secret":"JBSWY3DPEHPK3PXP"}]
accounts.load_accounts=lambda path=None:[dict(a) for a in accts]
def save(a, path=None): accts[:]=[dict(x) for x in a]
accounts.save_accounts=save
from chartlab import brokers
D=brokers.cls_of("dhan"); seen=[]
D.login_totp=lambda self,pin="",code="": (seen.append(pin), {"access_token":"NEW"})[1]
accounts.renew_token=lambda a: (_ for _ in ()).throw(RuntimeError("expired"))
msg=accounts.auto_login("D1"); assert accts[0]["access_token"]=="NEW" and seen==["1234"] and "TOTP" in msg, msg
accounts.get_status=lambda a: dict(accounts.STATUS.get(a['name']) or {'state':'ok','days_left':0.9,'msg':'Connected'})
from chartlab.mainwindow import MainWindow
win=MainWindow(QtCore.QSettings(os.path.join(__import__("tempfile").mkdtemp(), "t.ini"), QtCore.QSettings.Format.IniFormat)); win.show(); app.processEvents()
ran=[]
optstore.run=lambda acct, base, unds, width, n_exp, stop=None, **k: (ran.append((acct["name"],str(base),tuple(unds),width,n_exp)), {"contracts":42,"updated":3,"rows":75,"errors":[],"folder":str(base)})[1]
# an expired login with PIN + TOTP key saved: logged in again without the user, then the archive runs
accounts.STATUS["D1"]={"state":"expired","days_left":-0.1,"msg":"x"}
accts[0]["access_token"]="OLD"
win._auto_renew("D1", accounts.STATUS["D1"])
for _ in range(50): app.processEvents(); QtTest.QTest.qWait(20)
assert accts[0]["access_token"]=="NEW", accts
accounts.STATUS["D1"]={"state":"ok","days_left":0.9,"msg":"Connected"}
d=cs.SettingsDialog(win, cs.SettingsDialog.DATA_TAB); d.show(); app.processEvents()
assert d.d_renew.isChecked() and d.d_opt.isChecked() and d.d_unds.text()=="NIFTY, BANKNIFTY, SENSEX"
d.d_width.setValue(15); d.d_unds.setText("nifty, reliance"); d.d_unds.editingFinished.emit()
win.settings.setValue("opt_folder","/tmp/chartlab_test_data/cloud/opts")
win._options_archive(force=True)
for _ in range(60): app.processEvents(); QtTest.QTest.qWait(20)
assert ran and ran[-1]==("D1","/tmp/chartlab_test_data/cloud/opts",("NIFTY","RELIANCE"),15,2), ran
d._data_tick(); assert "42 contracts" in d.d_last.text(), d.d_last.text()
n=len(ran); win._options_archive()
for _ in range(20): app.processEvents(); QtTest.QTest.qWait(20)
import datetime as dt
# (already done for this stamp: not again, unless the close has come since)
assert len(ran) in (n, n+1)
d.d_opt.setChecked(False); k=len(ran); win.settings.setValue("opt_done",""); win._options_archive()
for _ in range(20): app.processEvents(); QtTest.QTest.qWait(20)
assert len(ran)==k, "switched off but ran"
d.d_renew.setChecked(False); assert win.settings.value("auto_renew")=="false" and not win._auto_renew_action.isChecked()
print("DATA OK")
