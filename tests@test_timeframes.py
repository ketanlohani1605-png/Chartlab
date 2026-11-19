import os, sys
os.environ["QT_QPA_PLATFORM"]="offscreen"; os.environ["CHARTLAB_HOME"]="/tmp/chartlab_test_tf"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numpy as np, pandas as pd
from PySide6 import QtCore, QtTest, QtWidgets
app=QtWidgets.QApplication([]); QtCore.QCoreApplication.setOrganizationName("clt"); QtCore.QCoreApplication.setApplicationName("tf_test")
from chartlab import data as datamod, timeframes as TF
def fake(symbol, interval="1d", period="2y"):
    assert interval in TF.YAHOO, interval
    step={"1m":1,"2m":2,"5m":5,"15m":15,"30m":30,"1h":60}.get(interval)
    days=pd.bdate_range("2026-09-01","2026-11-18")
    if step: idx=pd.DatetimeIndex([d+pd.Timedelta(minutes=555+step*i) for d in days[-6:] for i in range(375//step)])
    else: idx=days if interval=="1d" else days[::5] if interval=="1wk" else days[::21]
    c=100+np.arange(len(idx))*0.01
    return pd.DataFrame({"Open":c,"High":c+1,"Low":c-1,"Close":c+.2,"Volume":100.0},index=idx), f"{symbol}.NS · {interval}"
real=datamod.load_yahoo
def patched(symbol, interval="1d", period="2y"):
    if interval in TF.YAHOO: return fake(symbol, interval, period)
    import sys as _s; _s.modules.setdefault("yfinance", type(_s)("yfinance")); return real(symbol, interval, period)
datamod.load_yahoo=patched
real.__globals__["load_yahoo"]=patched
from chartlab.mainwindow import MainWindow
win=MainWindow(QtCore.QSettings(os.path.join(__import__("tempfile").mkdtemp(), "t.ini"), QtCore.QSettings.Format.IniFormat)); win.show(); app.processEvents(); QtTest.QTest.qWait(300)
if hasattr(win,"_auto_timer"): win.set_auto_refresh(0)
def wait():
    p=win.active
    for _ in range(200):
        app.processEvents(); QtTest.QTest.qWait(20)
        if not p.loading: break
p=win.active; p.load("yahoo","RELIANCE","45m","1mo"); wait()
assert p.df is not None and [t.strftime("%H:%M") for t in p.df.index[:2]]==["09:15","10:00"], (p.last_error, p.df.index[:3] if p.df is not None else None)
assert p.chart.interval_tag()=="45", p.chart.interval_tag()
win._sync_toolbar(); assert "45m" in win.interval_btns and win.interval_btns["45m"].isChecked() and "45m" in win.custom_intervals
# a tick inside the bar, then in the next 45m slot, then next morning 09:15
last=p.df.index[-1]; n=len(p.df)
t0=last.value/1e9
assert p.on_tick(123.0, t0+60)=="same" and len(p.df)==n
r=p.on_tick(124.0, t0+45*60+5); assert r=="new" and p.df.index[-1]==last+pd.Timedelta(minutes=45), (r,p.df.index[-1])
nxt=(last.normalize()+pd.Timedelta(days=1)+pd.Timedelta(minutes=555+50)).value/1e9
assert p.on_tick(125.0, nxt)=="new" and p.df.index[-1].strftime("%H:%M")=="10:00", p.df.index[-1]
# 7 minutes (not a divisor of a day): still session anchored
p.load("yahoo","RELIANCE","7m","5d"); wait(); assert p.df.index[1].strftime("%H:%M")=="09:22", p.df.index[:3]
nxt=(p.df.index[-1].normalize()+pd.Timedelta(days=1)+pd.Timedelta(minutes=555+8)).value/1e9
r=p.on_tick(99.0,nxt)
assert r=="new" and p.df.index[-1].strftime("%H:%M")=="09:22"
# 3 days
p.load("yahoo","RELIANCE","3d","2y"); wait(); assert p.chart.interval_tag()=="3D", p.chart.interval_tag()
n=len(p.df); days=p._tf_days; last=p.df.index[-1]
d1=(pd.Timestamp("2026-11-19 10:00")).value/1e9
r=p.on_tick(50.0,d1); exp = "same" if days<3 else "new"; assert r==exp,(r,days)
for k in range(1,4):
    r=p.on_tick(50.0+k,(pd.Timestamp("2026-11-19 10:00")+pd.Timedelta(days=k)).value/1e9)
assert len(p.df)-n in (1,2), (len(p.df),n)
# quarters and typing
p.load("yahoo","RELIANCE","3mo","2y"); wait(); assert p.chart.interval_tag()=="3M" and len(p.df)>=1
from chartlab.tvui import parse_interval, CustomInterval
assert parse_interval("4h")[0]=="4h" and parse_interval("3M")==("3mo","3 months") and parse_interval("zz")[0] is None
d=CustomInterval(win); d.kind.setCurrentIndex(1); d.num.setValue(4); assert d.code=="4h"; d.kind.setCurrentIndex(0); d.num.setValue(120); assert d.code=="2h"
assert win.ensure_interval("4h") and "4h" in win.interval_menu_actions
win.toggle_fav_interval("4h", True); assert "4h" in win.fav_intervals
win.remove_custom_interval("4h"); assert "4h" not in win.interval_btns and "4h" not in win.fav_intervals
print("CUSTOM TF OK")
