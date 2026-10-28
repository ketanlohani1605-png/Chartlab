"""Chart drawing tools: trend line, ray, horizontal / vertical line, rectangle, Fibonacci retracement.

A drawing is stored as  {"id": int, "type": str, "pts": [[epoch_seconds, price], ...], "color": "#rrggbb"}
Time-based points mean drawings survive timeframe changes and reloads.
"""
from __future__ import annotations

import math

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from . import theme

TOOLS = {"trend": 2, "ray": 2, "hline": 1, "vline": 1, "rect": 2, "fib": 2}
TOOL_LABELS = {"trend": "Trend line", "ray": "Ray", "hline": "Horizontal line",
               "vline": "Vertical line", "rect": "Rectangle", "fib": "Fib retracement"}
FIB_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
FIB_COLORS = ["#787b86", "#f23645", "#ff9800", "#4caf50", "#089981", "#00bcd4", "#787b86"]
DEFAULT_COLOR = "#2962ff"
HIT_PIXELS = 9


def _seg_dist(p, a, b) -> float:
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class DrawingLayer(QtCore.QObject):
    changed = QtCore.Signal()

    def __init__(self, chart):
        super().__init__()
        self.chart = chart
        self.drawings: list[dict] = []
        self.tool: str | None = None
        self.pending: list[list[float]] = []   # [[time, price], ...] while a 2-point drawing is in progress
        self.selected: int | None = None
        self._next_id = 1
        self._items: dict[int, list] = {}
        self._preview_items: list = []

    # ------------------------------------------------------------------ public API
    def set_tool(self, tool: str | None):
        self.tool = tool if tool in TOOLS else None
        self.cancel(keep_tool=True)
        self.chart.set_cursor_for_tool(self.tool is not None)

    def add(self, dtype: str, pts: list, color: str = DEFAULT_COLOR) -> int:
        if dtype not in TOOLS or len(pts) < TOOLS[dtype]:
            raise ValueError(f"'{dtype}' needs {TOOLS.get(dtype, '?')} point(s)")
        d = {"id": self._next_id, "type": dtype, "pts": [[float(t), float(p)] for t, p in pts[:TOOLS[dtype]]],
             "color": color}
        self._next_id += 1
        self.drawings.append(d)
        self._build(d)
        self.changed.emit()
        return d["id"]

    def remove(self, did: int) -> bool:
        for d in self.drawings:
            if d["id"] == did:
                self.drawings.remove(d)
                self._drop_items(did)
                if self.selected == did:
                    self.selected = None
                self.changed.emit()
                return True
        return False

    def delete_selected(self):
        if self.selected is not None:
            self.remove(self.selected)

    def clear(self):
        for did in list(self._items):
            self._drop_items(did)
        self.drawings = []
        self.selected = None
        self.cancel()
        self.changed.emit()

    def cancel(self, keep_tool: bool = False):
        self.pending = []
        self._clear_preview()
        if self.selected is not None and not keep_tool:
            self.selected = None
            self.rebuild()
        if not keep_tool and self.tool is not None:
            self.tool = None
            self.chart.set_cursor_for_tool(False)
            self.chart.toolChanged.emit(None)

    def to_json(self) -> list:
        return [dict(d) for d in self.drawings]

    def load_json(self, data: list):
        self.clear_silent()
        for d in data or []:
            try:
                self.drawings.append({"id": self._next_id, "type": d["type"],
                                      "pts": [[float(a), float(b)] for a, b in d["pts"]],
                                      "color": d.get("color", DEFAULT_COLOR)})
                self._next_id += 1
            except Exception:  # noqa: BLE001 - ignore a corrupt stored drawing
                continue
        self.rebuild()

    def clear_silent(self):
        for did in list(self._items):
            self._drop_items(did)
        self.drawings, self.selected, self.pending = [], None, []
        self._clear_preview()

    # ------------------------------------------------------------------ mouse input (view coordinates)
    def handle_click(self, x: float, y: float):
        if self.tool:
            t = self.chart.x_to_time(x)
            self.pending.append([t, y])
            if len(self.pending) >= TOOLS[self.tool]:
                did = self.add(self.tool, self.pending, DEFAULT_COLOR)
                self.pending = []
                self._clear_preview()
                self.selected = did
                self.rebuild()
                self.tool = None
                self.chart.set_cursor_for_tool(False)
                self.chart.toolChanged.emit(None)
            return
        hit = self._hit_test(x, y)
        if hit != self.selected:
            self.selected = hit
            self.rebuild()

    def handle_move(self, x: float, y: float):
        if not (self.tool and self.pending):
            return
        d = {"id": -1, "type": self.tool, "pts": self.pending + [[self.chart.x_to_time(x), y]],
             "color": DEFAULT_COLOR}
        self._clear_preview()
        self._preview_items = self._make_items(d, preview=True)
        for it in self._preview_items:
            self.chart.price.addItem(it, ignoreBounds=True)

    # ------------------------------------------------------------------ building visuals
    def rebuild(self):
        for did in list(self._items):
            self._drop_items(did)
        for d in self.drawings:
            self._build(d)

    def _build(self, d):
        items = self._make_items(d)
        for it in items:
            self.chart.price.addItem(it, ignoreBounds=True)
        self._items[d["id"]] = items

    def _drop_items(self, did):
        for it in self._items.pop(did, []):
            self.chart.price.removeItem(it)

    def _clear_preview(self):
        for it in self._preview_items:
            self.chart.price.removeItem(it)
        self._preview_items = []

    def _xy(self, d):
        return [(self.chart.time_to_x(t), p) for t, p in d["pts"]]

    def _make_items(self, d, preview: bool = False) -> list:
        if not self.chart.has_data():
            return []
        sel = d["id"] == self.selected and not preview
        color = d.get("color", DEFAULT_COLOR)
        pen = pg.mkPen(color, width=2.5 if sel else 1.6,
                       style=QtCore.Qt.PenStyle.DashLine if preview else QtCore.Qt.PenStyle.SolidLine)
        pts = self._xy(d)
        items: list = []
        t = d["type"]
        xmax = self.chart.x_limit()

        if t == "trend" and len(pts) == 2:
            items.append(pg.PlotDataItem([pts[0][0], pts[1][0]], [pts[0][1], pts[1][1]], pen=pen))
        elif t == "ray" and len(pts) == 2:
            (x1, y1), (x2, y2) = pts
            if x2 == x1:
                ex, ey = x2, y2
            else:
                ex = xmax if x2 > x1 else -5
                ey = y1 + (y2 - y1) * (ex - x1) / (x2 - x1)
            items.append(pg.PlotDataItem([x1, ex], [y1, ey], pen=pen))
        elif t == "hline" and pts:
            items.append(pg.InfiniteLine(pos=pts[0][1], angle=0, movable=False, pen=pen,
                                         label="{value:0.2f}",
                                         labelOpts={"position": 0.04, "color": color,
                                                    "fill": (19, 23, 34, 200)}))
        elif t == "vline" and pts:
            items.append(pg.InfiniteLine(pos=pts[0][0], angle=90, movable=False, pen=pen))
        elif t == "rect" and len(pts) == 2:
            (x1, y1), (x2, y2) = pts
            r = QtWidgets.QGraphicsRectItem(QtCore.QRectF(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)))
            qpen = QtGui.QPen(pen)
            qpen.setCosmetic(True)
            r.setPen(qpen)
            c = QtGui.QColor(color)
            c.setAlpha(45)
            r.setBrush(QtGui.QBrush(c))
            items.append(r)
        elif t == "fib" and len(pts) == 2:
            (x1, y1), (x2, y2) = pts
            xa, xb = min(x1, x2), max(x1, x2, min(x1, x2) + 1)
            items.append(pg.PlotDataItem([x1, x2], [y1, y2], pen=pg.mkPen("#787b86", width=1,
                                                                         style=QtCore.Qt.PenStyle.DashLine)))
            for lvl, col in zip(FIB_LEVELS, FIB_COLORS):
                price = y2 + (y1 - y2) * lvl
                items.append(pg.PlotDataItem([xa, xb], [price, price], pen=pg.mkPen(col, width=1.4 if sel else 1)))
                tx = pg.TextItem(f"{lvl:g}  ({price:,.2f})", color=col, anchor=(0, 1))
                tx.setPos(xa, price)
                items.append(tx)
        if sel:
            hs = [p for p in pts] if t not in ("hline", "vline") else []
            if hs:
                items.append(pg.ScatterPlotItem(x=[p[0] for p in hs], y=[p[1] for p in hs], size=9,
                                                brush=pg.mkBrush(theme.BG), pen=pg.mkPen(color, width=2)))
        return items

    # ------------------------------------------------------------------ hit testing
    def _segments(self, d):
        pts = self._xy(d)
        t = d["type"]
        far = self.chart.x_limit()
        if t == "trend" and len(pts) == 2:
            return [(pts[0], pts[1])]
        if t == "ray" and len(pts) == 2:
            (x1, y1), (x2, y2) = pts
            if x2 == x1:
                return [((x1, y1), (x2, y2))]
            ex = far if x2 > x1 else -5
            return [((x1, y1), (ex, y1 + (y2 - y1) * (ex - x1) / (x2 - x1)))]
        if t == "hline":
            return [((-5, pts[0][1]), (far, pts[0][1]))]
        if t == "vline":
            lo, hi = self.chart.y_range()
            return [((pts[0][0], lo), (pts[0][0], hi))]
        if t == "rect" and len(pts) == 2:
            (x1, y1), (x2, y2) = pts
            return [((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)), ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))]
        if t == "fib" and len(pts) == 2:
            (x1, y1), (x2, y2) = pts
            xa, xb = min(x1, x2), max(x1, x2)
            return [((xa, y2 + (y1 - y2) * l), (xb, y2 + (y1 - y2) * l)) for l in FIB_LEVELS] + [(pts[0], pts[1])]
        return []

    def _hit_test(self, x: float, y: float):
        vb = self.chart.price.vb
        click = vb.mapViewToScene(QtCore.QPointF(x, y))
        best, best_d = None, HIT_PIXELS
        for d in reversed(self.drawings):
            for a, b in self._segments(d):
                pa = vb.mapViewToScene(QtCore.QPointF(*a))
                pb = vb.mapViewToScene(QtCore.QPointF(*b))
                dist = _seg_dist((click.x(), click.y()), (pa.x(), pa.y()), (pb.x(), pb.y()))
                if dist < best_d:
                    best, best_d = d["id"], dist
        return best
