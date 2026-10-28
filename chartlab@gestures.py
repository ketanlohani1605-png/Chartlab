"""TradingView-style navigation: a ViewBox that pans/zooms the way Supercharts does."""
from __future__ import annotations

import math

import pyqtgraph as pg
from PySide6 import QtCore


class TVViewBox(pg.ViewBox):
    """wheel / two-finger scroll: vertical = zoom about cursor, horizontal = pan.
    Left-drag in the plot = pan (y too once you drag vertically). Drag on price axis = scale price,
    drag on time axis = scale time."""

    def __init__(self, chart, price=False):
        super().__init__(enableMenu=False)
        self.chart, self.is_price = chart, price
        self.setMouseEnabled(x=False, y=False)
        self._moved_y = 0.0

    # --- helpers
    def _span(self):
        (x0, x1), (y0, y1) = self.viewRange()
        return x0, x1, y0, y1

    def zoom_x(self, factor: float, cx: float | None = None):
        x0, x1, _, _ = self._span()
        cx = (x0 + x1) / 2 if cx is None else cx
        nx0, nx1 = cx - (cx - x0) * factor, cx + (x1 - cx) * factor
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
        if d == 0:
            ev.ignore()
            return
        horizontal = ev.orientation() == QtCore.Qt.Orientation.Horizontal
        x0, x1, _, _ = self._span()
        if horizontal:                                   # trackpad swipe sideways
            self.pan_x(-d / 120.0 * (x1 - x0) * 0.06)
        else:
            pos = self.mapSceneToView(ev.scenePos())
            factor = 0.85 ** (d / 120.0)
            if axis == 1 and self.is_price:
                self.zoom_y(factor, pos.y())
            else:
                self.zoom_x(factor, pos.x())
        ev.accept()

    def mouseDragEvent(self, ev, axis=None):
        if ev.button() != QtCore.Qt.MouseButton.LeftButton:
            ev.ignore()
            return
        ev.accept()
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
        self.pan_x(-dp.x() * (x1 - x0) / w)
        if self.is_price:
            self._moved_y += abs(dp.y())
            if self._moved_y > 6 or not self.chart.auto_y:
                self.pan_y(dp.y() * (y1 - y0) / h)
        if ev.isFinish():
            self._moved_y = 0.0
