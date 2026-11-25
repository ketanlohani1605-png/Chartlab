"""Panels that have nothing in them yet say what will be there, on the panel's own surface:

- Strategy Tester before a run: 'No backtest yet' with Run / Open Strategy editor (not an empty plot scaled
  0.2 - 0.8); after a run the equity curve, its scale with thousands separators; empty tables carry one line of help
- Option chain without a Dhan account: one card with the 'Connect' button instead of an empty table and expiry box
- Trading Panel and the Strategy Tester's tab strip on black like every panel, not the window's #222 gutter
- Alerts: 'Clear done' only when there is something to clear; the log says what it is for
- an index's watchlist card shows no volume (Yahoo's index volume is not real); a stock's does
- a removed account field is gone at once (it was painted as a stray frame until the event loop deleted it)
- a toast sits over the chart, not over an open side panel
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["CHARTLAB_OFFLINE"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["HOME"] = tempfile.mkdtemp()

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from chartlab import theme  # noqa: E402

theme.apply_pg_config()
app.setStyle(theme.make_style())
app.setStyleSheet(theme.STYLESHEET)
from chartlab import fx  # noqa: E402
from chartlab.mainwindow import MainWindow  # noqa: E402


def pump(ms=60):
    end = time.time() + ms / 1000
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


win = MainWindow(QtCore.QSettings(tempfile.mktemp(suffix=".ini"), QtCore.QSettings.Format.IniFormat))
win.resize(1411, 786)
win.show()
pump(200)
r = np.random.default_rng(5)
n = 300
c = 1300 * np.exp(np.cumsum(r.normal(0.0003, 0.012, n)))
o = np.r_[c[0], c[:-1]]
df = pd.DataFrame({"Open": o, "High": np.maximum(o, c) * 1.006, "Low": np.minimum(o, c) * 0.994, "Close": c,
                   "Volume": r.integers(5e5, 3e6, n).astype(float)}, index=pd.bdate_range(end="2026-11-20", periods=n))
win.active.params.update(symbol="RELIANCE", source="yahoo", interval="1d")
win.active.set_dataframe(df, "RELIANCE.NS")
pump(200)


def bg_at(widget, x, y):
    return widget.grab().toImage().pixelColor(x, y)


# ---------------------------------------------------------------- the helpers
t = QtWidgets.QTableWidget(0, 2)
t.resize(400, 200)
t.show()
h = fx.EmptyHint(t, "No rows yet.")
pump(30)
assert h.isVisible() and h.text() == "No rows yet."
assert abs(h.geometry().center().x() - t.viewport().width() // 2) <= 2, "in the middle"
t.setRowCount(3)
pump(10)
assert not h.isVisible(), "a row came in: the hint goes"
t.setRowCount(0)
pump(10)
assert h.isVisible(), "empty again: back"
t.close()
ph = fx.Placeholder("code", "Title", "Some help", (("Go", lambda: None), ("Other", lambda: None)))
assert [b.text() for b in ph.buttons] == ["Go", "Other"] and "#f2f2f2" in ph.buttons[0].styleSheet()
assert "transparent" in ph.buttons[1].styleSheet(), "the second way is outlined"
small = fx.Placeholder("code", "T", "x", compact=True)
assert small.icon.isHidden(), "a short panel: the words only"

# ---------------------------------------------------------------- Strategy Tester
win.show_right_panel(None)
win.results_dock.show()
win.results_dock.raise_()
pump(300)
assert win.ov_stack.currentWidget() is win.ov_empty and win.ov_empty.isVisible()
assert win.ov_empty.title.text() == "No backtest yet"
assert [b.text() for b in win.ov_empty.buttons] == ["Run backtest", "Open Strategy editor"]
win.tabs.setCurrentIndex(2)
pump(50)
assert win._trades_hint.isVisible() and "No trades yet" in win._trades_hint.text()
win.tabs.setCurrentIndex(0)
pump(30)
# the tab strip on black, not the window's #222 gutter
strip = win.tabs.tabBar()
right_of_tabs = QtCore.QPoint(strip.width() + 40, strip.height() // 2)
img = win.tabs.grab().toImage()
px = img.pixelColor(int(right_of_tabs.x() * img.devicePixelRatio()), int(right_of_tabs.y() * img.devicePixelRatio()))
assert px.red() < 12 and px.green() < 12 and px.blue() < 12, px.name()
QtCore.QMetaObject.invokeMethod(win.ov_empty.buttons[1], "click")
pump(100)
assert win.strategy_dock.isVisible(), "the second button opens the editor"
win.ov_empty.buttons[0].click()                                   # Run backtest
for _ in range(100):
    pump(50)
    if win.active.result is not None:
        break
assert win.active.result is not None, "the button ran the backtest"
pump(100)
assert win.ov_stack.currentWidget() is win.equity_plot, "after a run: the equity curve"
ax = win.equity_plot.getAxis("right")
assert ax.tickStrings([100000.0, 95000.0], 1.0, 5000.0) == ["100,000", "95,000"]
assert ax.tickStrings([-2500.0], 1.0, 500.0) == ["−2,500"]
win.tabs.setCurrentIndex(2)
pump(50)
assert win.trades_tbl.rowCount() > 0 and not win._trades_hint.isVisible()
win.tabs.setCurrentIndex(0)
win.active.result = None
win._show_result(win.active)
assert win.ov_stack.currentWidget() is win.ov_empty, "no result: the message again"
win.results_dock.hide()

# ---------------------------------------------------------------- Option chain without an account
win.show_right_panel(win.chain_dock)
pump(300)
oc = win.chain
assert oc.account_fn() is None
assert oc.no_acct.isVisible() and not oc.tbl.isVisible() and not oc.exp_box.isVisible(), "one card, no empty table"
assert oc.connect_btn is oc.no_acct.buttons[0] and oc.connect_btn.text().startswith("Connect a Dhan account")
asked = []
oc.needAccount.connect(lambda: asked.append(1))
real_manage = win.manage_accounts
win.manage_accounts = lambda *a, **k: None
try:
    oc.connect_btn.click()
finally:
    win.manage_accounts = real_manage
assert asked == [1]
oc._show_no_account(False)
assert not oc.no_acct.isVisible() and oc.tbl.isVisible() and oc.exp_box.isVisible()
assert oc.exp_box.placeholderText() == "Expiry"
oc._show_no_account(True)
assert oc.testAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground), "its own black, not the #222 gutter"
img = oc.grab().toImage()
px = img.pixelColor(img.width() // 2, int(img.height() * 0.15))
assert px.red() < 12 and px.green() < 12 and px.blue() < 12, px.name()

# ---------------------------------------------------------------- Trading Panel
win.show_right_panel(None)
win.show_trading_panel()
pump(400)
tp = win.trade_panel
assert tp.testAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground)
pos_hint = tp._hints[0]
assert tp.pos.rowCount() == 0 and pos_hint.isVisible() and "No open positions" in pos_hint.text()
img = tp.grab().toImage()
dpr = img.devicePixelRatio()
px = img.pixelColor(int(tp.width() * 0.6 * dpr), int(18 * dpr))     # the top row, between the totals and the buttons
assert px.red() < 12 and px.green() < 12 and px.blue() < 12, px.name()
win.trade_dock.hide()

# ---------------------------------------------------------------- Alerts
win.show_right_panel(win.alerts_dock)
pump(200)
ap = win.alerts_panel
assert not ap.clear_btn.isEnabled(), "nothing to clear"
assert ap.log.placeholderText() and ap.log_head.text() == "LOG"
mgr = win.alerts
from chartlab import alerts as AL0  # noqa: E402

a = mgr.add("RELIANCE", "RELIANCE", next(iter(AL0.COND_TEXT)), 1250.0, 1230.0)
pump(30)
assert not ap.clear_btn.isEnabled(), "an active alert is not 'done'"
mgr.history.append({"time": "2026-11-20T14:05:00", "text": "RELIANCE crossing 1,250.00", "price": 1251.3})
mgr.set_status(a["id"], "triggered")
pump(30)
assert ap.clear_btn.isEnabled() and ap.log_head.text() == "LOG  ·  1"
ap.clear_btn.click()
pump(30)
assert not ap.clear_btn.isEnabled() and not mgr.alerts

# ---------------------------------------------------------------- the watchlist card: no volume for an index
win.show_right_panel(win.watch_dock)
pump(200)
wl = win.watchlist
wl.card._fetch_details = lambda sym: None                         # (no Performance download in a test)
wl.set_symbols(["NIFTY", "RELIANCE"])
ex = {"prev": 25514.3, "high": 25808.8, "low": 25345.2, "open": 25400.0, "volume": 428300.0, "time": "15:30"}
wl.card.show_symbol("NIFTY", (25800.6, 0.0112), ex)
assert not wl.card.vol.isVisible() and wl.card.vol.text() == "", "Yahoo's index volume is not shown"
wl.card.show()
wl.card.show_symbol("RELIANCE", (1377.5, 0.0014), dict(ex, volume=13167730.0))
pump(10)
assert wl.card.vol.isVisible() and "13.17M" in wl.card.vol.text(), wl.card.vol.text()
wl.card.show_symbol("RELIANCE", None, None)
assert not wl.card.range.isVisible(), "no quote yet: no empty day's-range gap"
from chartlab import watchlist as W  # noqa: E402

assert W._is_index_ticker("^NSEI") and not W._is_index_ticker("RELIANCE.NS")

# ---------------------------------------------------------------- accounts: a removed field is gone at once
from chartlab import accounts as A  # noqa: E402

d = A.AccountsDialog(win, __import__("pathlib").Path(tempfile.mkdtemp()) / "acc.json")
d.show()
pump(100)
# the broker is picked with chips (the combo box holds the value); each has its steps; the buttons stay in sight
assert d.broker.isHidden() and set(d.chips) == set(A.BROKERS) and d.chips["dhan"].isChecked()
assert "web.dhan.co" in d.card.text() and not d.login_btn.isVisible() and not d.redirect_row.isVisible(), \
    "Dhan: client id + token; the browser login is under 'More ways to log in'"
d.more.setChecked(True)
assert d.login_btn.isVisible() and d.redirect_row.isVisible()
d.more.setChecked(False)
d.chips["zerodha"].click()
assert d.broker.currentData() == "zerodha" and d.chips["zerodha"].isChecked() and not d.chips["dhan"].isChecked()
assert "developers.kite.trade" in d.card.text() and d.login_btn.isVisible() and d.show_tok.isVisible()
assert d.login_btn.text() == "Log in with Zerodha" and d.redirect_row.isVisible()
d.broker.setCurrentIndex(d.broker.findData("paper"))            # (set from code: the chip follows)
assert d.chips["paper"].isChecked() and not d.show_tok.isVisible() and not d.live_note.isVisible()
assert not d.live.isVisible() and not d.tbtn.isVisible()
foot = d.findChild(QtWidgets.QFrame, "foot")
save_btn = next(b for b in foot.findChildren(QtWidgets.QPushButton) if b.text() == "Save")
assert save_btn.isVisible() and not d.scroll.widget().isAncestorOf(save_btn), "Save is under the form, not in it"
assert d.scroll.horizontalScrollBar().maximum() == 0, "the form fits the dialog's width"
assert all(k in A.STEPS and len(A.STEPS[k]) == 3 and k in A.BROKER_COLOR for k in A.BROKERS)
d.broker.setCurrentIndex(d.broker.findData("dhan"))
pump(30)
old = [d.fields_lay.itemAt(i).widget() for i in range(d.fields_lay.count())]
assert old
d.broker.setCurrentIndex(d.broker.findData("paper"))
assert all(w.isHidden() for w in old if w is not None and not isinstance(w, type(None))), \
    "hidden at once, before the loop deletes them"
d.reject()

# ---------------------------------------------------------------- a toast over the chart, not over the watchlist
from chartlab import alerts as AL  # noqa: E402

toast = AL.Toast(win, "Alert", "RELIANCE crossed 1,250.00")
pump(500)
cw = win.centralWidget()
right_edge = cw.mapTo(win, QtCore.QPoint(cw.width(), 0)).x()
assert toast.geometry().right() <= right_edge, (toast.geometry(), right_edge)
assert not toast.geometry().intersects(QtCore.QRect(win.watch_dock.mapTo(win, QtCore.QPoint(0, 0)),
                                                    win.watch_dock.size()))

print("PANELS OK")
sys.stdout.flush()
os._exit(0)
