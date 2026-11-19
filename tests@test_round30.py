"""v0.30: indicator library with panes (MACD, Stochastic, ADX ...), TradingView crosshair (Retina-thick dashed lines,
labels, '+' that never blocks, future time), frozen-bar cleanup, renamed symbols, watchlist follows the chart."""
import os, sys, json, math, tempfile, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, ".")
import numpy as np, pandas as pd
from PySide6 import QtWidgets, QtCore, QtGui, QtTest
from chartlab import theme
theme.apply_pg_config()
app = QtWidgets.QApplication([])
app.setStyleSheet(theme.STYLESHEET)
from chartlab import studies as STD, data as D, indicators as ta
from chartlab.mainwindow import MainWindow, quit_cleanly
from chartlab.render import HistItem, DotsItem, BandFill
from chartlab.chart import axis_rect

B, M = QtCore.Qt.MouseButton, QtCore.Qt.KeyboardModifier


def pump(ms=40):
    QtTest.QTest.qWait(ms)


def daily(n=400, seed=3, vol=True):
    r = np.random.default_rng(seed)
    c = 24000 * np.exp(np.cumsum(r.normal(0.0004, 0.011, n)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + np.abs(r.normal(0, 0.005, n)))
    lo = np.minimum(o, c) * (1 - np.abs(r.normal(0, 0.005, n)))
    v = r.lognormal(15, 0.4, n) if vol else np.zeros(n)
    return pd.DataFrame({"Open": o, "High": h, "Low": lo, "Close": c, "Volume": v},
                        index=pd.bdate_range(end="2026-10-30", periods=n))


def intraday(days=5, seed=7):
    idx = []
    for d in pd.bdate_range(end="2026-11-17", periods=days):
        idx += list(pd.date_range(d + pd.Timedelta(hours=9, minutes=15), periods=375, freq="1min"))
    idx = pd.DatetimeIndex(idx)
    r = np.random.default_rng(seed)
    c = 25400 * np.exp(np.cumsum(r.normal(0, 0.0004, len(idx))))
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c) * 1.0002, "Low": np.minimum(o, c) * 0.9998, "Close": c,
                         "Volume": 0.0}, index=idx)


# ---------------------------------------------------------------- 1) every study computes on daily and intraday data
dfd, dfi = daily(), intraday()
for key in STD.KEYS:
    for df in (dfd, dfi):
        outs, fills = STD.compute(key, df, STD.defaults(key))
        assert outs, key
        for o in outs:
            vals = o.vals if o.vals is not None else o.y
            assert len(vals) == len(df), (key, len(vals))
            if not STD.STUDIES[key].volume or df["Volume"].any():
                assert np.isfinite(np.asarray(vals, float)).any(), key
# TradingView formulas, spot checks
st = ta.stoch(dfd)
ll, hh = dfd["Low"].rolling(14).min(), dfd["High"].rolling(14).max()
assert np.allclose((100 * (dfd["Close"] - ll) / (hh - ll)).dropna().iloc[-5:], st["k"].dropna().iloc[-5:])
dmi = ta.dmi(dfd)
assert ((dmi["adx"].dropna() >= 0) & (dmi["adx"].dropna() <= 100)).all()
pv = ta.pivots(dfi)
last_day = dfi.index[-1].normalize()
prev = dfi[dfi.index.normalize() < last_day]
prev = prev[prev.index.normalize() == prev.index[-1].normalize()]
p_ref = (prev["High"].max() + prev["Low"].min() + prev["Close"].iloc[-1]) / 3
assert abs(pv["P"].iloc[-1] - p_ref) < 1e-6

# ---------------------------------------------------------------- 2) panes in the window
ini = os.path.join(tempfile.mkdtemp(), "s.ini")
w = MainWindow(QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat))
w.resize(1500, 950)
w.show()
pump(100)
p = w.active
ch = p.chart
p.set_dataframe(dfd, "TEST")
pump(100)
h0 = ch.price.height()
for key in ("macd", "rsi", "stoch", "ichimoku", "bb", "psar", "pivots"):
    w.ind_actions[key].setChecked(True)
pump(150)
assert [pn.keys for pn in ch.panes] == [["macd"], ["rsi"], ["stoch"]], [pn.keys for pn in ch.panes]
assert [ch.glw.ci.items[pn.plot][0][0] for pn in ch.panes] == [2, 3, 4]
assert ch.price.height() < h0 and all(pn.plot.height() > 40 for pn in ch.panes)
assert ch._bottom_plot() is ch.panes[-1].plot                       # only the lowest pane carries the time scale
assert ch.panes[-1].plot.getAxis("bottom").isVisible() and not ch.price.getAxis("bottom").isVisible()
assert any(isinstance(it, HistItem) for _pl, it in ch._ind_items["macd"])
assert any(isinstance(it, DotsItem) for _pl, it in ch._ind_items["psar"])
assert any(isinstance(it, BandFill) and it.brush_dn is not None for _pl, it in ch._ind_items["ichimoku"])
lab = ch.panes[1].label.text()
assert "RSI 14" in lab and lab.count("<span") >= 3, lab                # name + RSI value + its MA value
assert "MACD 12 26 close 9" in ch.panes[0].label.text()
assert "BB 20 2" in ch.ind_label.text() and "Ichimoku" in ch.ind_label.text()
# pane scales fit what is on screen, levels included
y0, y1 = ch.panes[1].plot.vb.viewRange()[1]
assert y0 < 30 and y1 > 70
# a pane removed from the middle: the rest closes up, order kept
w.ind_actions["rsi"].setChecked(False)
pump(80)
assert [pn.keys for pn in ch.panes] == [["macd"], ["stoch"]]
assert [ch.glw.ci.items[pn.plot][0][0] for pn in ch.panes] == [2, 3]
assert ch.rsi is None
# new data keeps the panes and their order
p.set_dataframe(daily(300, 9), "TEST2")
pump(100)
assert [pn.keys for pn in ch.panes] == [["macd"], ["stoch"]] and "macd" in ch._ind_items
# legend buttons on a pane row: settings change the inputs, remove drops the pane
pn = ch.panes[1]
g = pn.label.geometry()
vp = ch.glw.viewport()


def move_to(q, target=None):
    t = target or vp
    QtWidgets.QApplication.sendEvent(t, QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QtCore.QPointF(q),
                                                          QtCore.QPointF(t.mapToGlobal(q)), B.NoButton, B.NoButton,
                                                          M.NoModifier))
    pump(60)


move_to(QtCore.QPoint(g.x() + 20, g.y() + g.height() // 2))
assert ch.ind_buttons.isVisible() and ch.ind_buttons.key == "stoch"
seen = {}


def drive():
    d = app.activeModalWidget()
    seen["t"] = type(d).__name__
    d.inputs["k"].setValue(9)
    d._pick("color_d", "#e91e63")
    d._ok()


QtCore.QTimer.singleShot(200, drive)
ch.ind_buttons.btns["gear"].click()
pump(100)
assert seen.get("t") == "_IndicatorSettings", seen
assert "Stoch 9 1 3" in ch.panes[1].label.text()
assert any(getattr(it, "_legend_color", "") == "#e91e63" for _pl, it in ch._ind_items["stoch"])
assert json.loads(w.settings.value("ind_params"))["stoch"]["k"] == 9
ch.ind_buttons.key = "stoch"
ch.ind_buttons.btns["x"].click()
pump(80)
assert [pn.keys for pn in ch.panes] == [["macd"]] and not w.ind_actions["stoch"].isChecked()
# pane height can be dragged
pn = ch.panes[0]
before = pn.plot.height()
ch.set_pane_height(pn, before + 60)
pump(80)
assert abs(pn.plot.height() - (before + 60)) < 6, (before, pn.plot.height())
# replay with panes: no crash, the histogram follows the replay position
ch.replay_begin()
ch.replay_start(150)
pump(50)
hist = next(it for _pl, it in ch._ind_items["macd"] if isinstance(it, HistItem))
assert hist.limit == ch._limit
ch.replay_exit()
pump(50)
# log scale: price overlays are rebuilt in log space, panes are not touched
ch.set_scale_mode("log")
pump(50)
bb_mid = next(it for _pl, it in ch._ind_items["bb"] if hasattr(it, "_full"))
assert abs(bb_mid._full[1][-1] - np.log10(bb_mid._raw[-1])) < 1e-9
ch.set_scale_mode("regular")
pump(50)

# TradingView details measured on its canvas: bold price labels at 5x a 1 / 2 / 4 step and 2x a 2.5 / 5 step
# (step 20: 1,200 and 1,300 bold), the newest bar's volume as a label on the price scale
from chartlab.chart import tv_bold_step, tv_price_step
ax = ch.price.getAxis("right")
(y0, y1) = ch.price.vb.viewRange()[1]
spacing, ticks = ax.tickValues(y0, y1, ch.price.vb.height())[0]
strs = ax.tickStrings(ticks, 1, spacing)
heavy = [t for t, s_ in zip(ticks, strs) if s_ in ax._bold]
bstep = tv_bold_step(spacing)
assert heavy and len(heavy) < len(ticks) and all(abs(t / bstep - round(t / bstep)) < 1e-6 for t in heavy), (ticks, ax._bold)
assert all(abs(t / bstep - round(t / bstep)) > 1e-6 for t, s_ in zip(ticks, strs) if s_ not in ax._bold)
assert [tv_bold_step(s) for s in (0.1, 0.2, 0.5, 1, 2, 2.5, 10, 20, 40, 100)] == [0.5, 1, 1, 5, 10, 5, 50, 100, 200, 500]
assert [tv_price_step(r) for r in (0.07, 0.15, 0.3, 0.9, 1.7, 2.3, 3.1, 4.5, 11.7, 27.3, 58.5, 124.8)] == \
    [0.1, 0.2, 0.5, 1, 2, 2.5, 4, 5, 20, 40, 100, 200]          # TradingView's steps (measured), 30 px apart
ch._place_last_tag()
vt = ch.vol_tag
assert vt.isVisible() and vt.text() == theme.fmt_vol(float(ch._vols[ch._n() - 1])), vt.text()
assert vt.styleSheet().count(ch.volbars._c(True).name()) == 0          # full-strength colour, not the faded bar colour
# ---------------------------------------------------------------- 3) crosshair
vb = ch.price.vb
r = vb.sceneBoundingRect()


def at(fx, fy):
    return ch.glw.mapFromScene(QtCore.QPointF(r.left() + r.width() * fx, r.top() + r.height() * fy))


move_to(at(0.5, 0.5))
pen = ch.price.vline.pen
dpr = ch.glw.devicePixelRatioF()
# TradingView, measured on its own canvas: #9c9c9c, 1 point, dashed 5 on / 6 off
assert pen.isCosmetic() and abs(pen.widthF() - dpr) < 1e-6 and pen.dashPattern() == [5.0, 6.0]
assert pen.color().name() == "#9c9c9c"
bp = ch._bottom_plot()
assert ch.price.vline.isVisible() and ch.price.hline.isVisible() and bp.dtag.isVisible()
i = ch._hover_i
assert bp.dtag.text() == ch.df.index[i].strftime("%a %d %b '%y")
assert bp.dtag.alignment() & QtCore.Qt.AlignmentFlag.AlignHCenter
assert ch.price.ytag.alignment() & QtCore.Qt.AlignmentFlag.AlignLeft    # price labels line up with the scale's numbers
# geometry, TradingView's: the price label is 21 px tall and its middle row is the line's row; the '+' box (21 x 21)
# ends where the scale starts and the label begins 1 px inside it; the time label hangs from the top of the scale
yt, pb = ch.price.ytag, ch.plus_btn
line_row = math.floor(ch.glw.viewportTransform().map(vb.mapViewToScene(QtCore.QPointF(0, ch.price.hline.value()))).y())
assert yt.height() == 21 and yt.y() + 10 == line_row, (yt.y(), line_row)
assert pb.size() == QtCore.QSize(21, 21) and pb.y() == yt.y()
axl = round(ch.glw.viewportTransform().map(QtCore.QPointF(axis_rect(ch.price.getAxis("right")).left(), 0)).x())
assert pb.x() + pb.width() == axl and yt.x() == axl + 1
assert bp.dtag.height() == 24 and bp.dtag.y() == round(ch.glw.mapFromScene(axis_rect(bp.getAxis("bottom")).topLeft()).y())
assert "#303030" in yt.styleSheet() and "font-weight: 400" in yt.styleSheet()
assert "border-top-right-radius: 2px" in yt.styleSheet() and "border-bottom-left-radius: 2px" in bp.dtag.styleSheet()
assert yt.width() < ch.price.getAxis("right").width()                  # as wide as its text, not the whole scale
# the line really is one crisp row: render the chart and look at the pixels left of the '+'
img = ch.glw.grab().toImage()
d = img.devicePixelRatio()
sx = int((pb.x() - 60) * d)
cols = range(sx, sx + int(24 * d))                     # wider than one dash period: some column is always "on"
rows = sorted({y for y in range(int((yt.y() - 4) * d), int((yt.y() + yt.height() + 4) * d))
               for x in cols if QtGui.QColor(img.pixel(x, y)).name() == "#9c9c9c"})
assert rows and min(rows) >= line_row * d - 1 and max(rows) <= (line_row + 1) * d, (rows[:6], line_row, d)
# the crosshair runs through every pane, the value label shows in the hovered one
for pn in ch.panes:
    assert pn.plot.vline.isVisible() and pn.plot.vline.value() == ch.price.vline.value()
# right of the newest bar: the time keeps going (next business days)
move_to(at(0.985, 0.4))
assert ch._hover_i >= ch._n()
assert bp.dtag.isVisible()
assert bp.dtag.text() != ch.df.index[-1].strftime("%a %d %b '%y")
fut = ch.axes[0]._future
assert fut is not None and fut[0] > ch.df.index[-1] and all(t.dayofweek < 5 for t in fut[:20])
# the time label never runs off the pane
move_to(at(0.001, 0.4))
g = bp.dtag.geometry()
left = ch.glw.mapFromScene(bp.vb.sceneBoundingRect().topLeft()).x()
assert g.left() >= left
# the '+' never takes the mouse away from the crosshair
move_to(at(0.5, 0.45))
assert ch.plus_btn.isVisible()
assert ch.plus_btn.testAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
plus_c = ch.plus_btn.geometry().center()
move_to(plus_c)
assert ch.plus_btn_hot and ch.price.hline.isVisible() and ch.price.ytag.isVisible()
menu = {}


def grab_menu():
    m = app.activePopupWidget()
    menu["items"] = [a.text() for a in m.actions() if a.text()] if m else []
    if m:
        m.close()


QtCore.QTimer.singleShot(250, grab_menu)
QtTest.QTest.mouseClick(vp, B.LeftButton, M.NoModifier, plus_c)
pump(400)
assert any(t.startswith("Add alert on") for t in menu.get("items", [])), menu
assert any(t.startswith("Draw horizontal line at") for t in menu["items"])
# leaving the chart hides everything (nothing stays stuck on screen)
QtWidgets.QApplication.sendEvent(vp, QtCore.QEvent(QtCore.QEvent.Type.Leave))
pump(80)
assert not ch.price.vline.isVisible() and bp.dtag.isHidden() and ch.plus_btn.isHidden() and not ch.plus_btn_hot
# intraday: TradingView's time label and the market-status dot after the title
p.params["interval"] = "1m"
p.set_dataframe(dfi, "^NSEI · 1m")
pump(100)
r = vb.sceneBoundingRect()
move_to(at(0.5, 0.5))
bp = ch._bottom_plot()
da = bp.getAxis("bottom")
n_ = len(dfi)
sp_, tv_ = da.tickValues(n_ - 500, n_ - 1, 1200)[0]
ts_ = da.tickStrings(tv_, 1, sp_)
days = [x for x in ts_ if x.isdigit()]
assert days and all(x in da._bold for x in days) and not any(x in da._bold for x in ts_ if ":" in x), (ts_, da._bold)
assert ch.vol_tag.isHidden()                                            # an index has no volume to label
lab = bp.dtag.text()
assert lab.endswith(ch.df.index[ch._hover_i].strftime("%H:%M")) and "'26   " in lab, lab          # three spaces: the gap TradingView leaves (measured)
# the legend: logo circle before the title, TradingView's status pill after it (none while the market is open)
st = ch.market_status()
assert st in ("open", "pre", "closed")
assert ch.header.text().count("<img") == 2, ch.header.text()[:300]       # TradingView shows "Open" too
from chartlab.chart import status_pill_html
assert all("<img" in status_pill_html(k) for k in ("open", "closed", "pre")) and status_pill_html("") == ""
assert theme.fmt_vol(342_550_000) == "342.55M"                 # the unit follows the number, as TradingView looks
move_to(at(0.985, 0.4))
fut = ch.axes[0]._future
assert fut[0].strftime("%H:%M") == "09:15" and fut[0].normalize() > dfi.index[-1].normalize()   # next session
# no 'Vol 0' for an index; Buy / Sell shown like TradingView: the last price on both, spread 0.00 (no bid / ask)
assert not ch._show_vol_legend() and ch.vol_label.isHidden()
ch._place_overlays()
assert ch.sell_btn.isVisible() and ch.buy_btn.isVisible()
lp = ch.fp(float(dfi["Close"].iloc[-1]))
assert ch.sell_btn.price_lbl.text() == lp == ch.buy_btn.price_lbl.text() and "0.00" in ch.spread_lbl.text()
assert ch.sell_btn.size() == QtCore.QSize(85, 34) and ch.buy_btn.x() - (ch.sell_btn.x() + 85) == 39

# ---------------------------------------------------------------- 4) frozen bars, renamed symbols, watchlist
idx = pd.date_range("2026-11-17 15:10", periods=20, freq="1min")
rows = ([[25411.0, 25419.3, 25406.7, 25418.75], [25419.8, 25425.1, 25415.5, 25416.9], [25417.1, 25418.3, 25400.6, 25401.1],
         [25401.4, 25407.0, 25396.9, 25403.25], [25403.9, 25420.6, 25402.0, 25417.8]] + [[25413.0] * 4] * 13
        + [[25413.0, 25413.0, 25387.4, 25387.4], [25387.4] * 4])
fz = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)
fz["Volume"] = 0.0
out = D.drop_frozen(fz)
assert list(out.index.strftime("%H:%M")) == ["15:10", "15:11", "15:12", "15:13", "15:14", "15:28"]
real = daily(50)
assert len(D.drop_frozen(real)) == 50                                  # real bars are never touched
assert D.normalize_symbol("TATAMOTORS") == "TMPV.NS"
from chartlab.watchlist import WatchlistWidget, market_state
assert WatchlistWidget.loads('["NIFTY","TATAMOTORS","TCS"]') == ["NIFTY", "TMPV", "TCS"]
import datetime as dt
assert market_state("RELIANCE", dt.datetime(2026, 11, 17, 6, 0, tzinfo=dt.timezone.utc))[0] == "open"     # 11:30 IST
assert market_state("RELIANCE", dt.datetime(2026, 11, 17, 3, 35, tzinfo=dt.timezone.utc))[0] == "pre"     # 09:05 IST
assert market_state("RELIANCE", dt.datetime(2026, 11, 15, 6, 0, tzinfo=dt.timezone.utc))[0] == "closed"   # Sunday
assert market_state("BTC-USD")[0] == "open"
# the watchlist row of the charted symbol shows the chart's price
w.watchlist.set_symbols(["NIFTY", "RELIANCE"])
p.params["symbol"] = "NIFTY"
p.params["source"] = "auto"
w._sync_watch_price(p)
q = w.watchlist.quotes["NIFTY"]
prev_close = float(dfi[dfi.index.normalize() < dfi.index[-1].normalize()]["Close"].iloc[-1])
assert abs(q[0] - dfi["Close"].iloc[-1]) < 1e-9 and abs(q[1] - (dfi["Close"].iloc[-1] / prev_close - 1)) < 1e-12

# ---------------------------------------------------------------- 4b) TradingView's toolbars
tb = w.topbar
assert w.sym_btn.icon().isNull() and w.sym_btn.text() and w.acct_btn.objectName() == "tradepill"
assert tb.iconSize() == QtCore.QSize(28, 28)
assert w.statusBar().isHidden()                                         # TradingView has no status bar
w.statusBar().showMessage("hello")
pump(30)
assert w.toast.isVisible() and w.toast.text() == "hello"
w.statusBar().clearMessage()
pump(30)
assert w.toast.isHidden()
vis = [v for v, a in w.interval_btn_actions.items() if a.isVisible()]
cur = next(v for v, b in w.interval_btns.items() if b.isChecked())
# TradingView's default: no favourites - one button with the current interval opens the list
assert w.fav_intervals == [] and vis == [] and w.interval_btn.text() == w.interval_btns[cur].text(), vis
w.toggle_fav_interval("3m", True)
vis = [v for v, a in w.interval_btn_actions.items() if a.isVisible()]
assert set(vis) == {"3m", cur} and "3m" in json.loads(w.settings.value("fav_intervals")) and w.interval_btn.text() == "⌄"
w.toggle_fav_interval("3m", False)
assert not any(a.isVisible() for a in w.interval_btn_actions.values()) and w.interval_btn.text() != "⌄"
groups = list(w.tool_groups.items())
assert [ks[0] for _a, ks in groups][:5] == ["trend", "fib", "long", "brush", "text"], [ks for _a, ks in groups]
shapes = next(a for a, ks in groups if "rect" in ks)
assert w.leftbar_mode(shapes) == QtWidgets.QToolButton.ToolButtonPopupMode.MenuButtonPopup
w.tool_actions["rect"].trigger()
assert ch.layer.tool == "rect" and shapes.data() == "rect"              # the group now shows the rectangle
w.draw_actions[None].trigger()
ch._place_overlays()
assert ch.axis_gear.isVisible()
seen_settings = []
ch.settingsRequested.disconnect()                                       # (the window would open the modal dialog)
ch.settingsRequested.connect(lambda: seen_settings.append(1))
ch.axis_gear.click()
assert seen_settings

# ---------------------------------------------------------------- 5) indicators and chart type come back after a restart
w.ind_actions["macd"].setChecked(True)
w.ind_actions["adx"].setChecked(True)
w._save_panel_state()
saved = json.loads(w.settings.value("panel_state"))
assert saved[0]["on"][-1] == "adx" and "macd" in saved[0]["on"]
w.close()
w2 = MainWindow(QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat))
w2._restore_panel_state()
w2.active.set_dataframe(dfd, "TEST")
pump(100)
assert "adx" in w2.active.chart._enabled and "macd" in w2.active.chart._enabled
assert [pn.keys[0] for pn in w2.active.chart.panes][-1] == "adx"
assert w2.ind_actions["adx"].isChecked()
w2.close()
# a v0.28 user's untouched old defaults move to TradingView's measured ones once; a later choice is kept
from chartlab import settings as SET
qs = QtCore.QSettings(os.path.join(tempfile.mkdtemp(), "m.ini"), QtCore.QSettings.Format.IniFormat)
qs.setValue(SET.KEY, json.dumps({"crosshair": "#9598a1", "grid_color": "#262626", "text_color": "#d1d4dc",
                                 "sym_label": True}))
SET.load(qs)
assert (SET.get()["crosshair"], SET.get()["grid_color"], SET.get()["text_color"], SET.get()["sym_label"]) == \
    ("#9c9c9c", "#303030", "#b8b8b8", False)
SET.get()["sym_label"] = True
SET.save(qs)
SET.load(qs)
assert SET.get()["sym_label"] is True
SET.get()["sym_label"] = False
quit_cleanly(w, 0, exit_process=False)
print("ROUND 30 OK")
os._exit(0)
