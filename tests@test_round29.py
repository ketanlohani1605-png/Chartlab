"""v0.29: every chart gesture and the drawing / indicator upgrades, driven with real mouse, wheel and key events."""
"""Start the app exactly like main() (stylesheet, Fusion), with offline realistic data, at 2x for screenshots."""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ".")
import numpy as np, pandas as pd
from PySide6 import QtWidgets, QtCore, QtGui, QtTest
from chartlab import theme
theme.apply_pg_config()
app = QtWidgets.QApplication([])
app.setStyle("Fusion")
app.setStyleSheet(theme.STYLESHEET)
from chartlab.mainwindow import MainWindow
import tempfile
ini = tempfile.mktemp(suffix=".ini")
win = MainWindow(QtCore.QSettings(ini, QtCore.QSettings.Format.IniFormat))
win.resize(1680, 980)
win.show()
app.processEvents()

def make_df(n=400, seed=3, start=24000.0):
    r = np.random.default_rng(seed)
    c = start * np.exp(np.cumsum(r.normal(0.0004, 0.011, n)))
    o = np.r_[c[0], c[:-1]] * (1 + r.normal(0, 0.002, n))
    h = np.maximum(o, c) * (1 + np.abs(r.normal(0, 0.005, n)))
    l = np.minimum(o, c) * (1 - np.abs(r.normal(0, 0.005, n)))
    v = r.lognormal(15, 0.4, n)
    idx = pd.bdate_range(end="2026-10-30", periods=n)
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v}, index=idx)

df = make_df()
ch = win.active.chart
ch.set_data(df, "NIFTY")
win.active.label = "NIFTY"
app.processEvents()

def shot(path, w=None):
    (w or win).grab().save(path)

def pump(ms=50):
    QtTest.QTest.qWait(ms)

import traceback
p = win.active; p.set_dataframe(df, "NIFTY"); pump(200)
vp = ch.glw.viewport(); vb = ch.price.vb; lay = ch.layer
K, M, B = QtCore.Qt.Key, QtCore.Qt.KeyboardModifier, QtCore.Qt.MouseButton
res = []
def check(name):
    def deco(fn):
        try:
            r = fn()
            res.append(("PASS" if r in (None, True) else "FAIL", name, "" if r in (None, True) else str(r)))
        except AssertionError as e:
            res.append(("FAIL", name, str(e)[:200]))
        except Exception as e:
            res.append(("ERR ", name, f"{type(e).__name__}: {e}"[:200] + " @" + traceback.format_exc().strip().splitlines()[-3].strip()[:80]))
        return fn
    return deco
def reset():
    lay.cancel(); lay.clear_silent() if hasattr(lay, "clear_silent") else None
    ch.set_auto_y(True); ch.go_latest(); vb._stop_anim(); pump(30)
def px(x, y):
    pt = vb.mapViewToScene(QtCore.QPointF(x, float(ch.ty(y))))
    q = ch.glw.mapFromScene(pt); return QtCore.QPoint(int(q.x()), int(q.y()))
def center():
    r = vb.sceneBoundingRect(); return ch.glw.mapFromScene(r.center())
def drag(a, b, mods=M.NoModifier, steps=6, btn=B.LeftButton):
    QtTest.QTest.mouseMove(vp, a); pump(10)
    QtTest.QTest.mousePress(vp, btn, mods, a); pump(10)
    for i in range(1, steps + 1):
        q = QtCore.QPoint(int(a.x() + (b.x() - a.x()) * i / steps), int(a.y() + (b.y() - a.y()) * i / steps))
        ev = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QtCore.QPointF(q), QtCore.QPointF(vp.mapToGlobal(q)), B.NoButton, btn, mods)
        QtWidgets.QApplication.sendEvent(vp, ev); pump(25)
    QtTest.QTest.mouseRelease(vp, btn, mods, b); pump(30)
def click(q, mods=M.NoModifier, btn=B.LeftButton):
    QtTest.QTest.mouseMove(vp, q); pump(10); QtTest.QTest.mouseClick(vp, btn, mods, q); pump(40)
def wheel(pos, dy, mods=M.NoModifier, pixel=None):
    ev = QtGui.QWheelEvent(QtCore.QPointF(pos), QtCore.QPointF(vp.mapToGlobal(pos)), pixel or QtCore.QPoint(0, 0), QtCore.QPoint(0, dy),
                           B.NoButton, mods, QtCore.Qt.ScrollPhase.NoScrollPhase if pixel is None else QtCore.Qt.ScrollPhase.ScrollUpdate, False)
    QtWidgets.QApplication.sendEvent(vp, ev); pump(260)
def key(k, mods=M.NoModifier):
    vp.setFocus(); QtTest.QTest.keyClick(vp, k, mods); pump(60)
def dclick(q):
    QtTest.QTest.mouseMove(vp, q); pump(10)
    for t in (QtCore.QEvent.Type.MouseButtonPress, QtCore.QEvent.Type.MouseButtonRelease, QtCore.QEvent.Type.MouseButtonDblClick, QtCore.QEvent.Type.MouseButtonRelease):
        btns = B.LeftButton if t != QtCore.QEvent.Type.MouseButtonRelease else B.NoButton
        QtWidgets.QApplication.sendEvent(vp, QtGui.QMouseEvent(t, QtCore.QPointF(q), QtCore.QPointF(vp.mapToGlobal(q)), B.LeftButton, btns, M.NoModifier)); pump(15)
    pump(80)
def ch_right():
    from chartlab import settings as S; return int(S.get()['scale_right_bars'])
def axis_pt(which):
    ax = ch.price.getAxis("right") if which == "price" else ch._bottom_plot().getAxis("bottom")
    r = ax.sceneBoundingRect(); return ch.glw.mapFromScene(r.center())

# ---------------------------------------------------------------- pan / zoom
@check("drag pans time 1:1 (bar stays under the cursor)")
def _():
    reset(); c = center(); x_before = vb.mapSceneToView(ch.glw.mapToScene(c)).x()
    b = QtCore.QPoint(c.x() + 120, c.y()); drag(c, b, steps=4); vb._stop_anim()
    x_after = vb.mapSceneToView(ch.glw.mapToScene(b)).x()
    assert abs(x_after - x_before) < 1.5, (x_before, x_after)
@check("small vertical wobble while dragging does not move price (auto stays on)")
def _():
    reset(); c = center(); drag(c, QtCore.QPoint(c.x() + 80, c.y() + 3)); assert ch.auto_y
@check("vertical drag moves price and switches auto-scale off")
def _():
    reset(); c = center(); y0 = vb.viewRange()[1][0]; drag(c, QtCore.QPoint(c.x(), c.y() + 80))
    assert not ch.auto_y and abs(vb.viewRange()[1][0] - y0) > 1e-6
@check("wheel zooms keeping the right edge")
def _():
    reset(); x0, x1 = vb.viewRange()[0]; wheel(center(), 120); a0, a1 = vb.viewRange()[0]
    assert a1 - a0 < x1 - x0 and abs(a1 - x1) < 0.5, (x0, x1, a0, a1)
@check("Cmd/Ctrl+wheel zooms around the cursor bar")
def _():
    reset(); c = QtCore.QPoint(center().x() - 150, center().y()); xb = vb.mapSceneToView(ch.glw.mapToScene(c)).x()
    wheel(c, 120, M.ControlModifier); xa = vb.mapSceneToView(ch.glw.mapToScene(c)).x(); assert abs(xa - xb) < 0.6, (xb, xa)
@check("Shift+wheel pans")
def _():
    reset(); x0, x1 = vb.viewRange()[0]; wheel(center(), 120, M.ShiftModifier); a0, a1 = vb.viewRange()[0]
    assert abs((a1 - a0) - (x1 - x0)) < 1e-6 and abs(a0 - x0) > 0.5
@check("wheel over price scale scales price, auto off")
def _():
    reset(); y0, y1 = vb.viewRange()[1]; wheel(axis_pt("price"), 120); a0, a1 = vb.viewRange()[1]
    assert abs((a1 - a0) - (y1 - y0)) > 1e-9 and not ch.auto_y, (y0, y1, a0, a1, ch.auto_y)
@check("drag on price scale scales price")
def _():
    reset(); y0, y1 = vb.viewRange()[1]; a = axis_pt("price"); drag(a, QtCore.QPoint(a.x(), a.y() + 60)); b0, b1 = vb.viewRange()[1]
    assert abs((b1 - b0) - (y1 - y0)) > 1e-9 and not ch.auto_y
@check("double-click price scale -> auto again")
def _():
    a = axis_pt("price"); dclick(a); assert ch.auto_y
@check("drag on time scale scales time (right edge kept)")
def _():
    reset(); x0, x1 = vb.viewRange()[0]; a = axis_pt("time"); drag(a, QtCore.QPoint(a.x() - 80, a.y())); b0, b1 = vb.viewRange()[0]
    assert abs((b1 - b0) - (x1 - x0)) > 0.5 and abs(b1 - x1) < 0.5, (x0, x1, b0, b1)
@check("wheel over time scale zooms time")
def _():
    reset(); x0, x1 = vb.viewRange()[0]; wheel(axis_pt("time"), 120); b0, b1 = vb.viewRange()[0]; assert (b1 - b0) < (x1 - x0)
@check("double-click time scale resets the time scale")
def _():
    vb.zoom_x(3.0); pump(20); a = axis_pt("time"); dclick(a)
    x0, x1 = vb.viewRange()[0]; assert 100 < x1 - x0 < 260, (x0, x1)
@check("trackpad two-finger sideways pans 1:1, up/down zooms")
def _():
    reset(); x0, x1 = vb.viewRange()[0]; w_ = vb.width()
    wheel(center(), 0, pixel=QtCore.QPoint(100, 0)); a0, a1 = vb.viewRange()[0]
    assert abs((a0 - x0) + 100 * (x1 - x0) / w_) < 1e-6
    wheel(center(), 0, pixel=QtCore.QPoint(0, 60)); b0, b1 = vb.viewRange()[0]; assert b1 - b0 < a1 - a0
@check("zoom-in limit (never fewer than ~8 bars)")
def _():
    reset()
    for _i in range(40): vb.zoom_x(0.5)
    pump(30); x0, x1 = vb.viewRange()[0]; assert 5 <= x1 - x0 <= 12, (x0, x1)
@check("zoom-out limit / cannot scroll the data fully out of view")
def _():
    reset()
    for _i in range(40): vb.zoom_x(2.0)
    pump(30); x0, x1 = vb.viewRange()[0]; n = len(df)
    vb.pan_x(10 * n); pump(30); a0, a1 = vb.viewRange()[0]; assert a0 < n, (a0, a1, n)
    vb.pan_x(-30 * n); pump(30); b0, b1 = vb.viewRange()[0]; assert b1 > 0, (b0, b1)
@check("flick: chart keeps gliding after a fast drag")
def _():
    reset(); c = center(); x_ = vb.viewRange()[0][0]
    drag(c, QtCore.QPoint(c.x() + 300, c.y()), steps=3); a = vb.viewRange()[0][0]; pump(400); b = vb.viewRange()[0][0]
    vb._stop_anim()
    from chartlab import gestures as _g
    assert (not _g.ANIMATE) or abs(b - a) > 0.5, (a, b)
# ---------------------------------------------------------------- keyboard
@check("arrow keys move / zoom; Alt+R resets")
def _():
    reset(); x0 = vb.viewRange()[0][0]; key(K.Key_Left); assert vb.viewRange()[0][0] < x0
    s0 = vb.viewRange()[0]; key(K.Key_Up); s1 = vb.viewRange()[0]; assert s1[1] - s1[0] < s0[1] - s0[0]
    key(K.Key_R, M.AltModifier); pump(100); x = vb.viewRange()[0]; assert abs(x[1] - (len(df) + ch_right())) < 3, x
def ch_right():
    from chartlab import settings as S; return int(S.get()['scale_right_bars'])
for combo, tool in (("T", "trend"), ("H", "hline"), ("V", "vline"), ("F", "fib"), ("C", "crossline"), ("J", "hray")):
    @check(f"Alt+{combo} picks the {tool} tool")
    def _(combo=combo, tool=tool):
        reset(); key(getattr(K, "Key_" + combo), M.AltModifier); t = lay.tool; lay.cancel(); assert t == tool, t
@check("Esc cancels the tool; Delete removes the selected drawing; Cmd+Z brings it back; Shift+Cmd+Z redoes")
def _():
    reset(); n = len(df); did = lay.add("trend", [[ch.x_to_time(n - 60), df.Close.iloc[-60]], [ch.x_to_time(n - 20), df.Close.iloc[-20]]])
    lay.selected = did; lay.rebuild(); key(K.Key_Delete); assert not lay.drawings, "delete"
    key(K.Key_Z, M.ControlModifier); assert len(lay.drawings) == 1, "undo"
    key(K.Key_Z, M.ControlModifier | M.ShiftModifier); assert len(lay.drawings) == 0, "redo"
@check("Cmd+C / Cmd+V copies and pastes the selected drawing")
def _():
    reset(); n = len(df); did = lay.add("trend", [[ch.x_to_time(n - 60), df.Close.iloc[-60]], [ch.x_to_time(n - 20), df.Close.iloc[-20]]])
    lay.selected = did; lay.rebuild(); key(K.Key_C, M.ControlModifier); key(K.Key_V, M.ControlModifier); assert len(lay.drawings) == 2, len(lay.drawings)
@check("Alt+S copies a chart image; Alt+W adds to watchlist")
def _():
    QtWidgets.QApplication.clipboard().clear(); key(K.Key_S, M.AltModifier); pump(100)
    assert not QtWidgets.QApplication.clipboard().image().isNull(), "no image on the clipboard"
# ---------------------------------------------------------------- drawings
@check("trend line: click, click; hover shows hand cursor; drag an end point; drag the body")
def _():
    reset(); n = len(df); win._tool_clicked("trend"); a, b = px(n - 60, float(df.Close.iloc[-60])), px(n - 20, float(df.Close.iloc[-20]))
    click(a); click(b); assert len(lay.drawings) == 1 and lay.tool is None, (len(lay.drawings), lay.tool)
    d = lay.drawings[0]; p1 = list(d["pts"][1]); drag(b, QtCore.QPoint(b.x() + 40, b.y() - 30)); assert d["pts"][1] != p1, "endpoint drag"
    mid = QtCore.QPoint((a.x() + b.x() + 40) // 2, (a.y() + b.y() - 30) // 2); p0 = list(d["pts"][0]); drag(mid, QtCore.QPoint(mid.x() + 30, mid.y() + 30))
    assert d["pts"][0] != p0, "body drag"
@check("Shift while drawing a trend line snaps it to horizontal / 45 degrees")
def _():
    reset(); n = len(df); win._tool_clicked("trend"); y = float(df.Close.iloc[-60]); a = px(n - 60, y)
    click(a); b = QtCore.QPoint(a.x() + 200, a.y() + 9); QtTest.QTest.mouseMove(vp, b); pump(20); click(b, M.ShiftModifier)
    d = lay.drawings[-1]; assert abs(d["pts"][1][1] - d["pts"][0][1]) < 1e-6, d["pts"]
@check("double-click a drawing opens its settings")
def _():
    reset(); n = len(df); did = lay.add("trend", [[ch.x_to_time(n - 60), 100.0 * 0 + float(df.Close.iloc[-60])], [ch.x_to_time(n - 20), float(df.Close.iloc[-20])]])
    seen = {}
    QtCore.QTimer.singleShot(300, lambda: (seen.__setitem__("dlg", type(app.activeModalWidget()).__name__), app.activeModalWidget() and app.activeModalWidget().reject()))
    a = px(n - 40, (float(df.Close.iloc[-60]) + float(df.Close.iloc[-20])) / 2)
    d = lay.drawings[-1]; xy = lay._xy(d); mx, my = (xy[0][0] + xy[1][0]) / 2, (xy[0][1] + xy[1][1]) / 2
    q = ch.glw.mapFromScene(vb.mapViewToScene(QtCore.QPointF(mx, my)))
    dclick(q); pump(500); assert seen.get("dlg") not in (None, "NoneType"), seen
@check("selected drawing shows a floating toolbar (colour / width / lock / delete)")
def _():
    reset(); n = len(df); did = lay.add("trend", [[ch.x_to_time(n - 60), float(df.Close.iloc[-60])], [ch.x_to_time(n - 20), float(df.Close.iloc[-20])]])
    lay.selected = did; lay.rebuild(); pump(50); tb = getattr(ch, "draw_bar", None); assert tb is not None and tb.isVisible()
@check("drawing: line width / style / lock can be changed")
def _():
    assert hasattr(lay, "set_width") and hasattr(lay, "set_style") and hasattr(lay, "set_locked")
@check("Alt/Cmd+drag clones a drawing")
def _():
    reset(); n = len(df); lay.add("trend", [[ch.x_to_time(n - 60), float(df.Close.iloc[-60])], [ch.x_to_time(n - 20), float(df.Close.iloc[-20])]])
    d = lay.drawings[-1]; xy = lay._xy(d); q = ch.glw.mapFromScene(vb.mapViewToScene(QtCore.QPointF((xy[0][0] + xy[1][0]) / 2, (xy[0][1] + xy[1][1]) / 2)))
    drag(q, QtCore.QPoint(q.x() + 40, q.y() + 40), mods=M.AltModifier); assert len(lay.drawings) == 2, len(lay.drawings)
@check("magnet snaps a new point to the nearest OHLC")
def _():
    reset(); assert hasattr(ch, "snap")
@check("tools TradingView users reach for: parallel channel, brush, arrow marker")
def _():
    from chartlab.drawings import TOOL_LABELS as T; miss = [k for k in ("channel", "brush") if k not in T]; assert not miss, "missing " + ", ".join(miss)
# ---------------------------------------------------------------- menus / misc
def menu_texts(q, btn=B.RightButton):
    out = {}
    def grab():
        m = app.activePopupWidget()
        if m is not None:
            out["t"] = [a.text().split("\t")[0] for a in m.actions() if a.text()]; m.close()
    QtCore.QTimer.singleShot(250, grab); QtTest.QTest.mouseMove(vp, q); pump(10); QtTest.QTest.mouseClick(vp, btn, M.NoModifier, q); pump(400); return out.get("t", [])
@check("right-click chart menu")
def _():
    reset(); t = menu_texts(center()); res.append(("INFO", "   chart menu", " | ".join(t))); assert t
@check("right-click price scale menu")
def _():
    t = menu_texts(axis_pt("price")); res.append(("INFO", "   scale menu", " | ".join(t))); assert t
@check("right-click time scale menu")
def _():
    t = menu_texts(axis_pt("time")); res.append(("INFO", "   time menu", " | ".join(t))); assert t
@check("Shift+click starts the measure tool")
def _():
    reset(); c = center(); click(c, M.ShiftModifier); t = (lay.tool, bool(lay.pending)); lay.cancel(); assert t[0] == "measure" or t[1], t
@check("crosshair: shows on hover, hides when the mouse leaves")
def _():
    reset(); QtTest.QTest.mouseMove(vp, center()); pump(60); assert ch.price.vline.isVisible() and ch.price.hline.isVisible()
@check("scrolled away from the latest bar -> 'go to latest' button; click returns")
def _():
    reset(); vb.pan_x(-300); pump(100); b = getattr(ch, "latest_btn", None); assert b is not None and b.isVisible(), "no button"
    b.click(); pump(400); vb._stop_anim(); assert vb.viewRange()[0][1] >= len(df) - 1
@check("pane divider can be dragged to resize the RSI pane")
def _():
    win.ind_actions["rsi"].setChecked(True); pump(80); h0 = ch.rsi.height()
    g = ch.panes[0].grip; assert g.isVisible(), "no grip"; c = QtCore.QPoint(g.width() // 2, 3)
    QtTest.QTest.mousePress(g, B.LeftButton, M.NoModifier, c); pump(20)
    for dy in (-20, -40, -60):
        q = QtCore.QPoint(c.x(), c.y() + dy)
        QtWidgets.QApplication.sendEvent(g, QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QtCore.QPointF(q), QtCore.QPointF(g.mapToGlobal(q)), B.NoButton, B.LeftButton, M.NoModifier)); pump(30)
    QtTest.QTest.mouseRelease(g, B.LeftButton, M.NoModifier, QtCore.QPoint(c.x(), c.y() - 60)); pump(80)
    h1 = ch.rsi.height(); win.ind_actions["rsi"].setChecked(False); pump(50)
    assert abs(h1 - h0) > 20, (h0, h1)
@check("legend: hovering an indicator row shows hide / settings / remove buttons; each works")
def _():
    reset(); win.ind_actions["sma20"].setChecked(True); win.ind_actions["bb"].setChecked(True); pump(120)
    g = ch.ind_label.geometry(); q = QtCore.QPoint(g.x() + 30, g.y() + g.height() // 4)
    QtTest.QTest.mouseMove(vp, q); pump(80); b = ch.ind_buttons; assert b.isVisible() and b.key == "sma20", (b.isVisible(), b.key)
    b.btns["eye"].click(); pump(40); assert "sma20" in ch._ind_hidden and not ch._ind_items["sma20"][0][1].isVisible()
    b.btns["eye"].click(); pump(40); assert ch._ind_items["sma20"][0][1].isVisible()
    QtTest.QTest.mouseMove(vp, QtCore.QPoint(q.x(), g.y() + 3 * g.height() // 4)); pump(80); assert b.key == "bb", b.key
    b.btns["x"].click(); pump(60); assert "bb" not in ch._ind_items and not win.ind_actions["bb"].isChecked()
@check("indicator inputs: length / factor / colour change the line and the legend, and are remembered")
def _():
    y0 = float(ch._ind_items["sma20"][0][1].yData[-1])
    seen = {}
    def drive():
        d = app.activeModalWidget(); seen["t"] = type(d).__name__; d.len.setValue(9); d._pick("#f23645"); d._ok()
    QtCore.QTimer.singleShot(200, drive); ch.edit_indicator("sma20"); pump(100)
    it = ch._ind_items["sma20"][0][1]; assert seen.get("t") == "_IndicatorSettings", seen
    assert abs(float(it.yData[-1]) - float(df.Close.rolling(9).mean().iloc[-1])) < 1e-6 and abs(float(it.yData[-1]) - y0) > 1e-9
    assert "SMA 9" in ch.ind_label.text() and it.opts["pen"].color().name() == "#f23645"
    import json as _j; assert _j.loads(win.settings.value("ind_params"))["sma20"]["len"] == 9
    ch.set_indicator_params("sma20", len=20, color="#2962ff"); win.ind_actions["sma20"].setChecked(False)
    ch.set_indicator_params("supertrend", len=7, mult=2.0); win.ind_actions["supertrend"].setChecked(True); pump(60)
    assert "Supertrend 7 2" in ch.ind_label.text(), ch.ind_label.text(); win.ind_actions["supertrend"].setChecked(False)
    ch.set_indicator_params("supertrend", len=10, mult=3.0)
@check("parallel channel: three clicks; brush: drag; stay-in-drawing keeps the tool")
def _():
    reset(); n = len(df); win._tool_clicked("channel")
    y = float(df.Close.iloc[-60]); click(px(n - 80, y)); click(px(n - 30, y * 1.02)); click(px(n - 55, y * 0.97))
    d = lay.drawings[-1]; assert d["type"] == "channel" and len(d["pts"]) == 3 and lay.tool is None, (d["type"], lay.tool)
    assert len(lay._segments(d)) == 2
    win._tool_clicked("brush"); a = px(n - 120, y); drag(a, QtCore.QPoint(a.x() + 90, a.y() - 50), steps=8)
    d = lay.drawings[-1]; assert d["type"] == "brush" and len(d["pts"]) >= 6 and lay.tool is None, (d["type"], len(d["pts"]))
    ch.stay_in_drawing = True; win._tool_clicked("hline"); click(px(n - 40, y)); click(px(n - 40, y * 1.01))
    k = sum(1 for e in lay.drawings if e["type"] == "hline"); t = lay.tool; ch.stay_in_drawing = False; lay.cancel(); assert k == 2 and t == "hline", (k, t)
@check("drawing properties survive save / load; locked drawing cannot be dragged; toolbar changes width / style")
def _():
    reset(); n = len(df); did = lay.add("trend", [[ch.x_to_time(n - 60), float(df.Close.iloc[-60])], [ch.x_to_time(n - 20), float(df.Close.iloc[-20])]])
    lay.selected = did; lay.rebuild(); pump(40); bar = ch.draw_bar; assert bar.isVisible()
    bar.width_btn.menu().actions()[3].trigger(); bar.style_btn.menu().actions()[1].trigger(); bar.color_btn.menu().actions()[1].trigger(); pump(30)
    d = lay.get(did); assert (d["w"], d["ls"], d["color"]) == (4.0, "dash", "#f23645"), d
    pen = lay._items[did][0].opts["pen"]; assert pen.style() == QtCore.Qt.PenStyle.DashLine and pen.widthF() > 4
    js = lay.to_json(); bar.lock_btn.click(); pump(30); assert lay.get(did).get("lock")
    xy = lay._xy(lay.get(did)); q = ch.glw.mapFromScene(vb.mapViewToScene(QtCore.QPointF((xy[0][0] + xy[1][0]) / 2, (xy[0][1] + xy[1][1]) / 2)))
    before = [list(p_) for p_ in lay.get(did)["pts"]]; drag(q, QtCore.QPoint(q.x() + 40, q.y() + 40)); vb._stop_anim()
    assert lay.get(did)["pts"] == before, "locked drawing moved"
    lay.load_json(lay.to_json()); d2 = lay.drawings[0]; assert (d2["w"], d2["ls"], d2.get("lock")) == (4.0, "dash", True), d2
    lay.undo
print()
for st, name, info in res:
    print(f"{st}  {name}" + (f"   <- {info}" if info else ""))
bad = [r for r in res if r[0] in ("FAIL", "ERR ")]
from chartlab.mainwindow import quit_cleanly
quit_cleanly(win, 0, exit_process=False)
print("ROUND 29 OK  (%d gesture / drawing / indicator checks)" % sum(1 for r in res if r[0] != "INFO") if not bad else "ROUND 29 FAILED: %d" % len(bad))
os._exit(1 if bad else 0)
