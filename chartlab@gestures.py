"""TradingView-style navigation: a ViewBox that pans/zooms the way Supercharts does."""
from __future__ import annotations

import math
import os
import time

import pyqtgraph as pg
from PySide6 import QtCore

# Smooth motion is on for the real app; headless tests need instant results.
ANIMATE = os.environ.get("QT_QPA_PLATFORM") != "offscreen" or os.environ.get("CHARTLAB_ANIM") == "1"


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

    # --- smooth motion
    def _stop_anim(self):
        if self._anim is not None:
            self._anim.stop()
            self._anim = None

    def _animate_x(self, nx0, nx1, ms=150, curve=QtCore.QEasingCurve.Type.OutCubic):
        if not ANIMATE:
            self.setXRange(nx0, nx1, padding=0)
            return
        x0, x1, _, _ = self._span()
        self._stop_anim()
        a = QtCore.QVariantAnimation(self)
        a.setDuration(ms)
        a.setStartValue(0.0)
        a.setEndValue(1.0)
        a.setEasingCurve(curve)
        a.valueChanged.connect(lambda t: self.setXRange(x0 + (nx0 - x0) * t, x1 + (nx1 - x1) * t, padding=0))
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
        if smooth:
            self._goal = (nx0, nx1)
            self._animate_x(nx0, nx1)
        else:
            self._stop_anim()
            self.setXRange(nx0, nx1, padding=0)

    def pan_x(self, dbars: float):
        x0, x1, _, _ = self._span()
        self.setXRange(x0 + dbars, x1 + dbars, padding=0)

    def zoom_y(self, factor: float, cy: float | None = None):
        _, _, y0, y1 = self._span()
        cy = (y0 + y1) / 2 if cy is None else cy
        self.chart.set_auto_y(False)
        self.setYRange(cy - (cy - y0) * factor, cy + (y1 - cy) * factor, padding=0)

    def pan_y(self, dy: float):
        _, _, y0, y1 = self._span()
        self.chart.set_auto_y(False)
        self.setYRange(y0 + dy, y1 + dy, padding=0)

    # --- events
    def wheelEvent(self, ev, axis=None):
        d = ev.delta()
        self._vel = 0.0
        if d == 0:
            ev.ignore()
            return
        horizontal = ev.orientation() == QtCore.Qt.Orientation.Horizontal
        mods = ev.modifiers()
        shift = bool(mods & QtCore.Qt.KeyboardModifier.ShiftModifier)
        focused = bool(mods & (QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.MetaModifier))
        x0, x1, _, _ = self._span()
        if horizontal or shift:                          # trackpad swipe sideways / Shift+wheel = pan in time
            self.pan_x(-d / 120.0 * (x1 - x0) * 0.06)
        else:
            pos = self.mapSceneToView(ev.scenePos())
            factor = 0.85 ** (d / 120.0)
            if axis == 1 and self.is_price:
                self.zoom_y(factor, pos.y())
            else:
                # TradingView: plain wheel zooms keeping the right edge (latest bars) in place;
                # Cmd/Ctrl+wheel zooms around the bar under the cursor
                anchor = pos.x() if focused else min(x1, self.chart.x_limit() + 2)
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
                self.zoom_y(math.exp(dp.y() / 200.0))
            return
        if axis == 0:                                    # time axis: drag right/left scales time
            self.zoom_x(math.exp(-dp.x() / 200.0), x1)
            return
        dbars = -dp.x() * (x1 - x0) / w
        self.pan_x(dbars)
        now = time.monotonic()
        dt_ = max(now - self._last_t, 1e-3)
        self._vel = 0.6 * self._vel + 0.4 * (dbars / dt_)
        self._last_t = now
        if self.is_price:
            self._moved_y += abs(dp.y())
            if self._moved_y > 6 or not self.chart.auto_y:
                self.pan_y(dp.y() * (y1 - y0) / h)
        if ev.isFinish():
            self._moved_y = 0.0
            self._coast()

    def _coast(self):
        """Kinetic pan: keep gliding after a quick flick, decelerating smoothly."""
        if not ANIMATE or time.monotonic() - self._last_t > 0.08 or abs(self._vel) < 8:
            return
        x0, x1, _, _ = self._span()
        dist = max(-(x1 - x0), min(x1 - x0, self._vel * 0.35))
        self._animate_x(x0 + dist, x1 + dist, ms=650, curve=QtCore.QEasingCurve.Type.OutQuart)
