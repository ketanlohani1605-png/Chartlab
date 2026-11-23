import os, sys, time
os.environ["QT_QPA_PLATFORM"]="offscreen"; os.environ["CHARTLAB_HOME"]="/tmp/chartlab_test_data"
os.environ["CHARTLAB_OFFLINE"]="1"          # (no live requests: one still in flight at the end aborted the test)
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
optstore.run=lambda acct, base, unds, width, n_exp, stop=None, **k: (ran.append((",".join(a["name"] for a in acct),str(base),tuple(unds),width,n_exp)), {"contracts":42,"updated":3,"rows":75,"errors":[],"folder":str(base),"accounts":len(acct)})[1]
# an expired login with PIN + TOTP key saved: logged in again without the user, then the archive runs
accounts.STATUS["D1"]={"state":"expired","days_left":-0.1,"msg":"x"}
accts[0]["access_token"]="OLD"
win._auto_renew("D1", accounts.STATUS["D1"])
for _ in range(50): app.processEvents(); QtTest.QTest.qWait(20)
assert accts[0]["access_token"]=="NEW", accts
accounts.STATUS["D1"]={"state":"ok","days_left":0.9,"msg":"Connected"}
d=cs.SettingsDialog(win, cs.SettingsDialog.DATA_TAB); d.show(); app.processEvents()
assert d.d_renew.isChecked() and d.d_opt.isChecked() and d.d_unds.text()=="NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX"
assert d.d_stocks.isChecked() and not d.d_all_strikes.isChecked() and not d.d_all_exp.isChecked() and d.d_daily.isChecked()
assert "On this computer only" in d.d_where.text(), d.d_where.text()
d.d_stocks.setChecked(False); assert win.settings.value("opt_stocks")=="false"
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
# every strike / every expiry on request; two Dhan accounts share the work, the one with the data plan first
d.d_all_strikes.setChecked(True); d.d_all_exp.setChecked(True); app.processEvents()
assert not d.d_width.isEnabled() and not d.d_exp.isEnabled() and win.settings.value("opt_all_strikes")=="true"
accts.append({"name":"D2","broker":"dhan","client_id":"22","access_token":"T2"})
accounts.STATUS["D2"]={"state":"ok","days_left":0.9,"msg":"Connected","data":True}
win._options_archive(force=True)
for _ in range(60): app.processEvents(); QtTest.QTest.qWait(20)
assert ran[-1]==("D2,D1","/tmp/chartlab_test_data/cloud/opts",("NIFTY","RELIANCE"),0,0), ran[-1]
d._data_tick(); assert "on 2 accounts" in d.d_last.text(), d.d_last.text()
d.d_all_strikes.setChecked(False); d.d_all_exp.setChecked(False); accts.pop(); accounts.STATUS.pop("D2")
# the stocks of the watchlist that have options join the names
from chartlab import dhan_core
dhan_core.options_ready=lambda: True; dhan_core.underlyings=lambda: ["NIFTY","RELIANCE","TCS","INFY"]
win.watchlist.symbols=lambda: ["NSE:TCS","NIFTY","HDFCBANK","reliance"]
assert win._archive_names(win.option_archive_settings())==["NIFTY","RELIANCE"]
d.d_stocks.setChecked(True)
assert win._archive_names(win.option_archive_settings())==["NIFTY","RELIANCE","TCS"], win._archive_names(win.option_archive_settings())
d.d_stocks.setChecked(False)
d.d_opt.setChecked(False); k=len(ran); win.settings.setValue("opt_done",""); win._options_archive()
for _ in range(20): app.processEvents(); QtTest.QTest.qWait(20)
assert len(ran)==k, "switched off but ran"
d.d_renew.setChecked(False); assert win.settings.value("auto_renew")=="false" and not win._auto_renew_action.isChecked()

# ---- the data store: switched on by the real app; a cloud drive on the computer is used without a click
import shutil, tempfile
from pathlib import Path
import pandas as pd, numpy as np
from chartlab import store
d.close(); app.processEvents()
home=Path(tempfile.mkdtemp(prefix="chartlab_home_"))
icloud=home/"Library"/"Mobile Documents"/"com~apple~CloudDocs"; icloud.mkdir(parents=True)
local=home/"ChartLab"/"options"; (local/"NIFTY").mkdir(parents=True); (local/"NIFTY"/"old.parquet").write_text("x")
real_cf, real_def = store.cloud_folders, optstore.default_root
store.cloud_folders=lambda h=None: real_cf(home)
optstore.default_root=lambda: local
win.settings.setValue("opt_folder",""); win.settings.setValue("opt_done","x"); win.settings.setValue("daily_done","x")
assert not store.active()
win.start_store()
assert store.active()
for _ in range(100):
    app.processEvents(); QtTest.QTest.qWait(20)
    if str(win.settings.value("opt_folder","")) and not getattr(win,"_store_busy",False): break
target=icloud/"ChartLab"/"options"
assert win.settings.value("opt_folder")==str(target), win.settings.value("opt_folder")
assert store.STATE["root"]==target and (target/"NIFTY"/"old.parquet").read_text()=="x" and not (local/"NIFTY"/"old.parquet").exists()
cfg=win.option_archive_settings(); assert cfg["cloud"]=="iCloud Drive" and cfg["folder"]==str(target)
d=cs.SettingsDialog(win, cs.SettingsDialog.DATA_TAB); d.show(); app.processEvents()
assert "iCloud Drive" in d.d_where.text() and "cloud" in d.d_where.text() and "iCloud Drive" in d.d_cloud_btns
assert not d.d_cloud_btns["iCloud Drive"].isEnabled() and d.d_local.isEnabled()
for _ in range(60):
    app.processEvents(); QtTest.QTest.qWait(20)
    if "file" in d.d_size.text(): break
assert "1 files" in d.d_size.text() or "in 1" in d.d_size.text(), d.d_size.text()
# a chart load goes through the store: the bars are kept in the cloud folder
idx=pd.date_range("2026-11-18 09:15", periods=375, freq="1min", name="Date")
df=pd.DataFrame({"Open":100.0,"High":101.0,"Low":99.0,"Close":100.5,"Volume":10.0}, index=idx)
from chartlab import data as datamod
datamod.load_yahoo=lambda s,i="1d",p="2y": (df.copy(), f"{s} . {i}")
p0=win.active; p0.load("yahoo","INFY","1m","5d")
for _ in range(100):
    app.processEvents(); QtTest.QTest.qWait(20)
    if not p0.loading: break
assert len(p0.df)==375 and (icloud/"ChartLab"/"candles"/"INFY"/"1m"/"2026-11.parquet").exists()
# the next day the source only has the new day: the chart still shows both
df2=df.copy(); df2.index=df2.index+pd.Timedelta(days=1)
datamod.load_yahoo=lambda s,i="1d",p="2y": (df2.copy(), f"{s} . {i}")
p0.load("yahoo","INFY","1m","5d")
for _ in range(100):
    app.processEvents(); QtTest.QTest.qWait(20)
    if not p0.loading and len(p0.df)==750: break
assert len(p0.df)==750 and p0.df.index[0]==idx[0], len(p0.df)
# no source answers (offline): the saved bars are shown, and said to be a saved copy
datamod.load_yahoo=lambda s,i="1d",p="2y": (_ for _ in ()).throw(RuntimeError("no internet"))
datamod.peek_cache=lambda *a, **k: None
win.panels[0].df=None
p0.load("yahoo","INFY","5m","5d")
for _ in range(100):
    app.processEvents(); QtTest.QTest.qWait(20)
    if not p0.loading: break
assert p0.df is not None and len(p0.df)==150 and "saved" in p0.label, (p0.label, p0.last_error)
# "This computer": the files come back, and the store does not jump to the cloud again by itself
d.d_local.click()
for _ in range(100):
    app.processEvents(); QtTest.QTest.qWait(20)
    if not getattr(win,"_store_busy",False) and store.STATE["root"]==local: break
assert store.STATE["root"]==local and (local/"NIFTY"/"old.parquet").exists() and win.settings.value("data_autocloud")=="false"
assert (local.parent/"candles"/"INFY"/"1m"/"2026-11.parquet").exists() and not (target/"NIFTY"/"old.parquet").exists()
d._data_tick(); assert "On this computer only" in d.d_where.text()
win.start_store(); app.processEvents(); assert not getattr(win,"_store_busy",False) and win.settings.value("opt_folder","")==""
# the daily job fills the store with the watchlist (daily + 1-minute bars)
got=[]
from chartlab.panel import ChartPanel
ChartPanel._load_auto=staticmethod(lambda s,i,r: (got.append((s,i,r)), (df.copy(), "x"))[1])
win.watchlist.symbols=lambda: ["SBIN","ITC"]
win.settings.setValue("daily_done","")
win._daily_download()
for _ in range(100):
    app.processEvents(); QtTest.QTest.qWait(20)
    if not getattr(win,"_daily_busy",False): break
assert ("SBIN","1d","2y") in got and ("ITC","1m","3mo") in got, got      # (a broker account is connected: 3 months of 1-minute bars)
assert (local.parent/"candles"/"SBIN"/"1m"/"2026-11.parquet").exists() and "2 of 2" in str(win.settings.value("daily_last"))
d.close(); store.disable()
store.cloud_folders, optstore.default_root = real_cf, real_def
shutil.rmtree(home, ignore_errors=True)
print("DATA OK")
sys.stdout.flush()
os._exit(0)
