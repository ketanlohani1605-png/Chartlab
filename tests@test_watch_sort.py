"""Sorting the watchlist by a column, like TradingView:

- a click on Chg% sorts biggest first, again smallest first, a third time back to your own order; Symbol sorts A-Z
  first; each section (Indices, Stocks, ...) sorts on its own
- the saved list keeps your order - sorting only changes what is on screen
- a stock with no price yet goes to the end
- prices move: the rows move with them; the chosen row stays chosen, a glowing price keeps its glow, a folded
  section stays folded
- the sorted heading is brighter and carries the arrow; the app remembers the sort
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["CHARTLAB_OFFLINE"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["HOME"] = tempfile.mkdtemp()

from PySide6 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from chartlab import theme, watchlist as W  # noqa: E402

theme.apply_pg_config()
app.setStyle(theme.make_style())
app.setStyleSheet(theme.STYLESHEET)
from chartlab.mainwindow import MainWindow  # noqa: E402


def pump(ms=40):
    QtTest.QTest.qWait(ms)


ini = tempfile.mktemp(suffix=".ini")
win = MainWindow(QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat))
win.resize(1411, 786)
win.show()
win.show_right_panel(win.watch_dock)
pump(300)
wl = win.watchlist
wl.quotes.clear()
wl.extra.clear()
mine = ["NIFTY", "BANKNIFTY", "RELIANCE", "TCS", "HDFCBANK", "INFY", "SBIN"]
wl.set_symbols(mine)
wl._quotes_in({"NIFTY": (25700.0, 0.0073), "BANKNIFTY": (64500.0, -0.004), "RELIANCE": (1366.5, -0.0066),
               "TCS": (2377.2, 0.0147), "HDFCBANK": (848.3, 0.0317), "INFY": (1500.0, 0.002)},
              {s: {"prev": 100.0} for s in mine})                        # SBIN: no price yet
pump(50)
assert wl.symbols() == mine and wl.shown() == mine


def click(col):
    hdr = wl.header_row
    cell = hdr._cells()[col]
    QtTest.QTest.mouseClick(hdr, QtCore.Qt.MouseButton.LeftButton, pos=cell.center())
    pump(30)


click("pct")
assert wl.sort == ("pct", True)
assert wl.shown() == ["NIFTY", "BANKNIFTY", "HDFCBANK", "TCS", "INFY", "RELIANCE", "SBIN"], wl.shown()
assert wl.symbols() == mine, "your own order is what is saved"
assert win.settings.value("watch_sort") == "pct:desc"
click("pct")
assert wl.sort == ("pct", False)
assert wl.shown() == ["BANKNIFTY", "NIFTY", "RELIANCE", "INFY", "TCS", "HDFCBANK", "SBIN"], wl.shown()
click("pct")
assert wl.sort is None and wl.shown() == mine and win.settings.value("watch_sort") == ""
click("sym")
assert wl.sort == ("sym", False)
assert wl.shown() == ["BANKNIFTY", "NIFTY", "HDFCBANK", "INFY", "RELIANCE", "SBIN", "TCS"], wl.shown()
click("last")
assert wl.sort == ("last", True) and wl.shown()[:2] == ["BANKNIFTY", "NIFTY"]
assert wl.shown()[2:] == ["TCS", "INFY", "RELIANCE", "HDFCBANK", "SBIN"], wl.shown()

# the arrow and the brighter heading
img = wl.header_row.grab().toImage()
dpr = img.devicePixelRatio()
cell = wl.header_row._cells()["last"]
fm = QtGui.QFontMetrics(QtGui.QFont(wl.header_row.font()))
blue = 0
for x in range(cell.left() - 30, cell.right()):
    for y in range(cell.top(), cell.bottom()):
        c = img.pixelColor(int(x * dpr), int(y * dpr))
        if c.blue() > 150 and c.red() < 120:
            blue += 1
assert blue >= 3, "the arrow beside 'Last'"

# prices move -> rows move; the chosen row, a glow and a folded section survive
click("last")
click("last")
click("pct")
assert wl.sort == ("pct", True)
for i in range(wl.list.count()):
    it = wl.list.item(i)
    if it.data(W.ROLE_SYM) == "RELIANCE":
        wl.list.setCurrentItem(it)
assert wl.list.currentItem().data(W.ROLE_SYM) == "RELIANCE"
wl._spark_in({"RELIANCE": {"price": 1420.0, "time": time.time(), "prev": 1375.0},       # +3.27 %: now first
              "SBIN": {"price": 800.0, "time": time.time(), "prev": 790.0}})            # SBIN gets a price
pump(30)
stocks = [s for s in wl.shown() if s not in ("NIFTY", "BANKNIFTY")]
assert stocks[:2] == ["RELIANCE", "HDFCBANK"], stocks
assert stocks.index("SBIN") < len(stocks) - 1 or len(stocks) == 1, "with a price it sorts in"
cur = wl.list.currentItem()
assert cur is not None and cur.data(W.ROLE_SYM) == "RELIANCE", "still the chosen row"
assert cur.data(W.ROLE_FLASH) and cur.data(W.ROLE_FLASH)[0] == 1, "its glow moved with it"
assert wl.symbols() == mine
# fold Stocks, move prices again: still folded
sec = next(wl.list.item(i) for i in range(wl.list.count()) if wl.list.item(i).data(W.ROLE_SECTION) == "STOCKS")
wl._clicked(sec)
pump(30)
wl._spark_in({"TCS": {"price": 2600.0, "time": time.time(), "prev": 2342.7}})
pump(30)
hidden = [wl.list.item(i).isHidden() for i in range(wl.list.count())
          if not wl.list.item(i).data(W.ROLE_SECTION) and wl.list.item(i).data(W.ROLE_SYM) not in ("NIFTY", "BANKNIFTY")]
assert hidden and all(hidden), "the folded section stays folded"
sec = next(wl.list.item(i) for i in range(wl.list.count()) if wl.list.item(i).data(W.ROLE_SECTION) == "STOCKS")
wl._clicked(sec)
pump(30)
assert [s for s in wl.shown() if s not in ("NIFTY", "BANKNIFTY")][0] == "TCS"

# adding a symbol while sorted: saved at the end of your list, shown in its sorted place
assert wl.add("ITC") and wl.symbols()[-1] == "ITC"

# the context menu can stop sorting
assert wl.sort is not None
wl.set_sort("")
assert wl.sort is None and wl.shown() == wl.symbols()

# the app remembers the sort
wl.set_sort("chg:desc")
win.settings.sync()
win2 = MainWindow(QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat))
pump(100)
assert win2.watchlist.sort == ("chg", True), win2.watchlist.sort
assert win2.watchlist.header_row.sort == ("chg", True)

print("WATCH SORT OK")
os._exit(0)
