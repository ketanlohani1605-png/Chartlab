import os, sys
os.environ["QT_QPA_PLATFORM"]="offscreen"; os.environ["CHARTLAB_HOME"]="/tmp/chartlab_test_upd"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from PySide6 import QtCore, QtTest, QtWidgets
app=QtWidgets.QApplication([]); QtCore.QCoreApplication.setOrganizationName("clt"); QtCore.QCoreApplication.setApplicationName("upd_test")
from chartlab import theme, updater, settings as cs
app.setStyle(theme.make_style()); app.setStyleSheet(theme.STYLESHEET)
from chartlab.mainwindow import MainWindow
win=MainWindow(QtCore.QSettings(os.path.join(__import__("tempfile").mkdtemp(), "t.ini"), QtCore.QSettings.Format.IniFormat)); win.show(); app.processEvents()
updater.check=lambda cur, timeout=12.0: None
d=cs.SettingsDialog(win, cs.SettingsDialog.UPDATES_TAB); d.show(); app.processEvents()
assert d.stack.currentIndex()==cs.SettingsDialog.UPDATES_TAB==5   # (Navigation is page 4)
d.upd_check.click()
for _ in range(60): app.processEvents(); QtTest.QTest.qWait(20)
assert "newest version" in d.upd_state.text(), d.upd_state.text()
win._upd_ready=("9.9.9","/x"); d._upd_tick(); assert not d.upd_go.isHidden() and "9.9.9" in d.upd_state.text()
win._upd_ready=None
d.upd_auto.setChecked(False); assert win.settings.value("auto_update")=="false"
called=[]; updater.check=lambda cur, timeout=12.0: called.append(1)
win.check_updates(False); QtTest.QTest.qWait(200); app.processEvents(); assert not called
d.upd_auto.setChecked(True)
print("UPD OK")
