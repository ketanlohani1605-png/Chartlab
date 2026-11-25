"""TradingView-style navigation: a ViewBox that pans/zooms the way Supercharts does."""
from __future__ import annotations

import math
import os
import time

import pyqtgraph as pg
from PySide6 import QtCore, QtGui

# Smooth motion is on for the real app; headless tests need instant results.
ANIMATE = os.environ.get("QT_QPA_PLATFORM") != "offscreen" or os.environ.get("CHARTLAB_ANIM") == "1"

ZOOM_SPEED = {"slow": 0.55, "normal": 1.0, "fast": 1.8}


def nav() -> dict:
    """The Navigation settings (Settings -> Navigation)."""
    from . import settings
    return settings.get()


def zoom_power(S=None) -> float:
    """How far one notch / one pixel of swipe zooms, and which way (Settings -> Navigation)."""
    S = S or nav()
    k = ZOOM_SPEED.get(str(S.get("zoom_speed", "normal")), 1.0)
    return -k if S.get("zoom_invert") else k


class TVViewBox(pg.ViewBox):
    """wheel / two-finger scroll: vertical = zoom about cursor, horizontal = pan.
    Left-drag in the plot = pan (y too once you drag vertically). Drag on price axis = scale price,
    drag on time axis = scale time."""

    def __init__(self, chart, price=False):
        super().__init__(enableMenu=False)
        self.chart, self.is_price = chart, price
        self.setMouseEnabled(x=False, y=False)
        self._moved_y = 0.0
        self._anim = None
        self._vel = 0.0            # bars / second, for kinetic pan
        self._last_t = 0.0
        self._q = None             # navigation gathered since the last frame (see queue)

    # --- one frame per change: after the outermost range change every linked view's transform is set at once.
    # Left to the paint, the panes' and volume's new transforms arrived DURING the first paint and every move of
    # the chart was painted twice (measured: 2.00 paints per frame with RSI + MACD).
    _depth = 0

    def setRange(self, *a, **k):
        TVViewBox._depth += 1
        try:
            return super().setRange(*a, **k)
        finally:
            TVViewBox._depth -= 1
            if TVViewBox._depth == 0:
                settle = getattr(self.chart, "_settle_views", None)
                if settle is not None:
                    settle()

    # --- grid (drawn here, behind the series, instead of by the axes)
    plot_item = None          # the PlotItem this ViewBox belongs to (set by the chart)

    def _grid_pen(self):
        """TradingView's grid (measured on its dark theme): #303030 dots, 1 point wide, 1 point on / 3 off."""
        from . import settings
        S = settings.get()
        try:
            dpr = max(1.0, float(self.chart.glw.devicePixelRatioF()))
        except Exception:  # noqa: BLE001
            dpr = 1.0
        key = (S.get("grid_color", "#303030"), dpr)
        if getattr(self, "_gp_key", None) != key:
            pen = QtGui.QPen(QtGui.QColor(key[0]))
            pen.setCosmetic(True)
            pen.setCapStyle(QtCore.Qt.PenCapStyle.FlatCap)
            n = int(round(dpr))
            if abs(dpr - n) < 0.01 and n >= 2:
                # a 1-point line on an n-x screen = n one-pixel lines side by side, each with Qt's quick
                # one-pixel stroker (a 2-pixel dashed pen takes the slow general path): the same pixels, half the time
                pen.setWidthF(1.0)
                pen.setDashPattern([float(n), 3.0 * n])
                offs = tuple((i + 0.5) / n - 0.5 for i in range(n))
            else:
                pen.setWidthF(dpr)                   # cosmetic widths are device pixels: 2 on Retina = 1 point
                pen.setDashPattern([1.0, 3.0])
                offs = (0.0,)
            self._gp, self._gp_offs, self._gp_key = pen, offs, key
        return self._gp, S

    @staticmethod
    def _grid_values(axis, lo, hi, size_px):
        """Tick positions to draw grid lines at: the finest tick level that is still >= ~45 px apart."""
        # the same range is asked for at every repaint (hover, a tick, the other panes): worked out once
        key = (lo, hi, size_px, id(getattr(axis, "_index", None)), id(getattr(axis, "_ti", None)),
               getattr(getattr(axis, "chart", None), "scale_mode", None))
        hit = getattr(axis, "_gv_cache", None)
        if hit is not None and hit[0] == key:
            return hit[1]
        try:
            levels = axis.tickValues(lo, hi, size_px)
        except Exception:  # noqa: BLE001
            return []
        span = (hi - lo) or 1.0
        out = []
        for spacing, vals in levels:
            if spacing / span * size_px < 45 and out:
                break
            out = list(vals)
        try:
            axis._gv_cache = (key, out)
        except Exception:  # noqa: BLE001
            pass
        return out

    def paint(self, p, opt, widget):
        super().paint(p, opt, widget)
        pl = self.plot_item
        if pl is None or getattr(self.chart, "df", None) is None:
            return
        pen, S = self._grid_pen()
        gv, gh = bool(S.get("grid_v", True)), bool(S.get("grid_h", True))
        if not (gv or gh):
            return
        (x0, x1), (y0, y1) = self.viewRange()
        w, h = self.width(), self.height()
        if w <= 2 or h <= 2 or x1 <= x0 or y1 <= y0:
            return
        inv = bool(self.state.get("yInverted"))
        offs = self._gp_offs
        L = QtCore.QLineF
        lines = []
        if gh:
            for y in self._grid_values(pl.getAxis("right"), y0, y1, h):
                fy = (y - y0) / (y1 - y0)
                ly = round((fy if inv else 1 - fy) * h) + 0.5
                if 0 <= ly <= h:
                    lines += [L(0, ly + o, w, ly + o) for o in offs]
        if gv:
            for x in self._grid_values(pl.getAxis("bottom"), x0, x1, w):
                lx = round((x - x0) / (x1 - x0) * w) + 0.5
                if 0 <= lx <= w:
                    lines += [L(lx + o, 0, lx + o, h) for o in offs]
        if lines:
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
            p.setPen(pen)
            p.drawLines(lines)

    # --- smooth motion
    def _moved(self):
        """The user is moving the chart: it draws its quick way until the motion stops (chart._motion)."""
        m = getattr(self.chart, "_motion", None)
        if m is not None:
            m()

    def _stop_anim(self):
        if self._anim is not None:
            self._anim.stop()
            self._anim = None

    def _animate_x(self, nx0, nx1, ms=150, curve=QtCore.QEasingCurve.Type.OutCubic):
        if not ANIMATE or not nav().get("smooth", True):
            self.setXRange(nx0, nx1, padding=0)
            return
        x0, x1, _, _ = self._span()
        self._stop_anim()
        a = QtCore.QVariantAnimation(self)
        a.setDuration(ms)
        a.setStartValue(0.0)
        a.setEndValue(1.0)
        a.setEasingCurve(curve)

        def step(t):
            self._moved()
            self.setXRange(x0 + (nx0 - x0) * t, x1 + (nx1 - x1) * t, padding=0)
        a.valueChanged.connect(step)
        a.start()
        self._anim = a

    def _target_x(self):
        a = self._anim
        if a is not None and a.state() == QtCore.QAbstractAnimation.State.Running:
            return self._goal
        return self._span()[:2]

    # --- helpers
    def _span(self):
        (x0, x1), (y0, y1) = self.viewRange()
        return x0, x1, y0, y1

    def zoom_x(self, factor: float, cx: float | None = None, smooth: bool = False):
        x0, x1 = self._target_x() if smooth else self._span()[:2]
        cx = (x0 + x1) / 2 if cx is None else cx
        nx0, nx1 = cx - (cx - x0) * factor, cx + (x1 - cx) * factor
        self._moved()
        if smooth:
            self._goal = (nx0, nx1)
            self._animate_x(nx0, nx1)
        else:
            self._stop_anim()
            self.setXRange(nx0, nx1, padding=0)

    def pan_x(self, dbars: float):
        x0, x1, _, _ = self._span()
        self._moved()
        self.setXRange(x0 + dbars, x1 + dbars, padding=0)

    def zoom_y(self, factor: float, cy: float | None = None):
        _, _, y0, y1 = self._span()
        cy = (y0 + y1) / 2 if cy is None else cy
        self._moved()
        self.chart.set_auto_y(False)
        self.setYRange(cy - (cy - y0) * factor, cy + (y1 - cy) * factor, padding=0)

    def pan_y(self, dy: float):
        _, _, y0, y1 = self._span()
        self._moved()
        self.chart.set_auto_y(False)
        self.setYRange(y0 + dy, y1 + dy, padding=0)

    # --- fast input, applied once per frame
    def queue(self, dx: float = 0.0, zoom: float = 1.0, anchor=None, dy: float = 0.0, zoom_y: float = 1.0):
        """A trackpad sends 60 - 120 events a second, a drag one per mouse move: each range change re-fits the price
        scale, the panes and the labels (~6 ms), so they are added up here and applied once, when the event loop
        is free - the chart follows the fingers instead of falling behind them.
        dx: bars to move; zoom: time factor (< 1 = in) about `anchor` (a bar, or f(x0, x1) -> bar);
        dy: price to move; zoom_y: price factor about the middle."""
        q = self._q
        if q is None:
            q = self._q = {"dx": 0.0, "zoom": 1.0, "anchor": None, "dy": 0.0, "zy": 1.0}
            QtCore.QTimer.singleShot(0, self.flush)
        q["dx"] += dx
        q["dy"] += dy
        q["zy"] *= zoom_y
        if zoom != 1.0:
            q["zoom"] *= zoom
            q["anchor"] = anchor

    def flush(self):
        q, self._q = self._q, None
        if not q:
            return
        x0, x1, _, _ = self._span()
        moved = False
        if q["dx"]:
            x0, x1, moved = x0 + q["dx"], x1 + q["dx"], True
        if q["zoom"] != 1.0:
            a = q["anchor"]
            cx = a(x0, x1) if callable(a) else ((x0 + x1) / 2 if a is None else float(a))
            f = q["zoom"]
            x0, x1, moved = cx - (cx - x0) * f, cx + (x1 - cx) * f, True
        if moved:
            self._stop_anim()
            self._moved()
            self.setXRange(x0, x1, padding=0)
        if q["zy"] != 1.0:
            self.zoom_y(q["zy"])
        if q["dy"]:
            self.pan_y(q["dy"])

    def latest_anchor(self, x0: float, x1: float) -> float:
        """TradingView's zoom point: the right edge with the latest bars (never past the data's end)."""
        return min(x1, self.chart.x_limit() + 2)

    # --- events
    def wheelEvent(self, ev, axis=None):
        d = ev.delta()
        self._vel = 0.0
        if d == 0:
            ev.ignore()
            return
        S = nav()
        horizontal = ev.orientation() == QtCore.Qt.Orientation.Horizontal
        mods = ev.modifiers()
        shift = bool(mods & QtCore.Qt.KeyboardModifier.ShiftModifier)
        focused = bool(mods & (QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.MetaModifier))
        x0, x1, _, _ = self._span()
        pan = horizontal or shift or (S.get("wheel") == "pan" and axis is None and not focused)
        if pan:                                          # trackpad swipe sideways / Shift+wheel = pan in time
            self.pan_x(-d / 120.0 * (x1 - x0) * 0.06)
        else:
            pos = self.mapSceneToView(ev.scenePos())
            factor = 0.85 ** (d / 120.0 * zoom_power(S))
            if axis == 1 and self.is_price:
                self.zoom_y(factor, pos.y())
            else:
                # TradingView: plain wheel zooms keeping the right edge (latest bars) in place;
                # Cmd/Ctrl+wheel zooms around the bar under the cursor (Settings can swap the two)
                at_cursor = focused != (S.get("zoom_anchor") == "cursor")
                anchor = pos.x() if at_cursor else self.latest_anchor(x0, x1)
                self.zoom_x(factor, anchor, smooth=True)
        ev.accept()

    def mouseDragEvent(self, ev, axis=None):
        if ev.button() != QtCore.Qt.MouseButton.LeftButton:
            ev.ignore()
            return
        ev.accept()
        if ev.isStart():
            self._stop_anim()
            self._vel, self._last_t = 0.0, time.monotonic()
        if self.is_price and axis is None and self.chart.layer.tool == "brush":
            pos = self.mapSceneToView(ev.buttonDownScenePos() if ev.isStart() else ev.scenePos())
            self.chart.layer.brush_stroke(pos.x(), pos.y(), ev.isStart(), ev.isFinish())
            return
        if self.is_price and axis is None:
            layer = self.chart.layer
            if ev.isStart():
                st = self.mapSceneToView(ev.buttonDownScenePos())
                self._dragging_drawing = layer.begin_drag(st.x(), st.y())
            if getattr(self, "_dragging_drawing", False):
                pos = self.mapSceneToView(ev.scenePos())
                layer.drag_to(pos.x(), pos.y())
                if ev.isFinish():
                    layer.end_drag()
                    self._dragging_drawing = False
                return
        dp = ev.pos() - ev.lastPos()
        x0, x1, y0, y1 = self._span()
        w, h = max(self.width(), 1.0), max(self.height(), 1.0)
        if axis == 1:                                    # price axis: drag up/down scales price
            if self.is_price:
                self.queue(zoom_y=math.exp(dp.y() / 200.0))
            return
        if axis == 0:                                    # time axis: drag right/left scales time
            self.queue(zoom=math.exp(-dp.x() / 200.0), anchor=lambda a, b: b)
            return
        dbars = -dp.x() * (x1 - x0) / w
        self.queue(dx=dbars)
        now = time.monotonic()
        dt_ = max(now - self._last_t, 1e-3)
        self._vel = 0.6 * self._vel + 0.4 * (dbars / dt_)
        self._last_t = now
        if self.is_price:
            self._moved_y += abs(dp.y())
            if self._moved_y > 6 or not self.chart.auto_y:
                self.queue(dy=dp.y() * (y1 - y0) / h)
        if ev.isFinish():
            self._moved_y = 0.0
            self.flush()                                 # (the glide starts from where the fingers left it)
            self._coast()

    def _coast(self):
        """Kinetic pan: keep gliding after a quick flick, decelerating smoothly."""
        if (not ANIMATE or not nav().get("kinetic", True) or time.monotonic() - self._last_t > 0.08
                or abs(self._vel) < 8):
            return
        x0, x1, _, _ = self._span()
        dist = max(-(x1 - x0), min(x1 - x0, self._vel * 0.35))
        self._animate_x(x0 + dist, x1 + dist, ms=650, curve=QtCore.QEasingCurve.Type.OutQuart)
