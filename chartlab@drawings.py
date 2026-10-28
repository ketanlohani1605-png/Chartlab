"""Chart drawing tools: trend line, ray, horizontal / vertical line, rectangle, Fibonacci retracement.

A drawing is stored as  {"id": int, "type": str, "pts": [[epoch_seconds, price], ...], "color": "#rrggbb"}
Time-based points mean drawings survive timeframe changes and reloads.
"""
from __future__ import annotations

import copy
import math

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from . import theme

TOOLS = {"trend": 2, "ray": 2, "hline": 1, "vline": 1, "rect": 2, "fib": 2, "measure": 2, "text": 1, "hray": 1, "crossline": 1}
TOOL_LABELS = {"trend": "Trend line", "ray": "Ray", "hline": "Horizontal line",
               "vline": "Vertical line", "rect": "Rectangle", "fib": "Fib retracement",
               "measure": "Measure (price / % / bars)", "text": "Text note",
               "hray": "Horizontal ray", "crossline": "Cross line"}
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
        self._undo: list = []
        self._redo: list = []
        self._drag = None

    # ------------------------------------------------------------------ undo / redo
    def _snapshot(self):
        return copy.deepcopy(self.drawings)

    def _push_undo(self):
        self._undo.append(self._snapshot())
        del self._undo[:-100]
        self._redo.clear()

    def _restore(self, state):
        for did in list(self._items):
            self._drop_items(did)
        self.drawings = copy.deepcopy(state)
        self._next_id = max([d["id"] for d in self.drawings], default=0) + 1
        self.selected = None
        self.rebuild()
        self.changed.emit()

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._snapshot())
        self._restore(self._undo.pop())
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._snapshot())
        self._restore(self._redo.pop())
        return True

    # ------------------------------------------------------------------ drag to move / resize
    def begin_drag(self, x: float, y: float) -> bool:
        if self.tool or not self.chart.has_data():
            return False
        vb = self.chart.price.vb
        click = vb.mapViewToScene(QtCore.QPointF(x, y))
        if self.selected is not None:                      # grab a handle of the selected drawing
            d = next((d for d in self.drawings if d["id"] == self.selected), None)
            if d and d["type"] not in ("hline", "vline", "text"):
                for i, (hx, hy) in enumerate(self._xy(d)):
                    ps = vb.mapViewToScene(QtCore.QPointF(hx, hy))
                    if math.hypot(ps.x() - click.x(), ps.y() - click.y()) <= HIT_PIXELS + 2:
                        return self._start_drag(d, ("pt", i), x, y)
        hit = self._hit_test(x, y)
        if hit is None:
            return False
        if hit != self.selected:
            self.selected = hit
            self.rebuild()
        d = next(d for d in self.drawings if d["id"] == hit)
        return self._start_drag(d, ("move", 0), x, y)

    def _start_drag(self, d, mode, x, y) -> bool:
        self._drag = {"id": d["id"], "mode": mode, "x0": x, "y0": y, "orig": copy.deepcopy(d["pts"]),
                      "state": self._snapshot(), "moved": False}
        return True

    def drag_to(self, x: float, y: float):
        g = self._drag
        if not g:
            return
        d = next((d for d in self.drawings if d["id"] == g["id"]), None)
        if d is None:
            return
        g["moved"] = True
        kind, idx = g["mode"]
        if kind == "pt":
            sx, sy = self.chart.snap(x, y) if self.chart.magnet else (x, y)
            d["pts"][idx] = [self.chart.x_to_time(sx), sy]
        else:
            dx, dy = x - g["x0"], y - g["y0"]
            new = []
            for (t, p) in g["orig"]:
                nx = self.chart.time_to_x(t) + (0 if d["type"] == "hline" else dx)
                new.append([self.chart.x_to_time(nx), p + (0 if d["type"] == "vline" else dy)])
            d["pts"] = new
        self._drop_items(d["id"])
        self._build(d)

    def end_drag(self):
        g, self._drag = self._drag, None
        if g and g["moved"]:
            self._undo.append(g["state"])
            del self._undo[:-100]
            self._redo.clear()
            self.changed.emit()

    def hover_hit(self, x: float, y: float) -> bool:
        return (not self.tool) and self._hit_test(x, y) is not None

    def duplicate_selected(self):
        d = next((d for d in self.drawings if d["id"] == self.selected), None)
        if d:
            self._push_undo()
            nid = self.add(d["type"], [[t + 86400 * 0, p] for t, p in d["pts"]], d.get("color", DEFAULT_COLOR))
            for e in self.drawings:
                if e["id"] == nid:
                    for q in e["pts"]:
                        q[1] += (self.chart.y_range()[1] - self.chart.y_range()[0]) * 0.03
                    if "text" in d:
                        e["text"] = d["text"]
            self.selected = nid
            self.rebuild()
            self.changed.emit()

    def set_color(self, did: int, color: str):
        for d in self.drawings:
            if d["id"] == did:
                self._push_undo()
                d["color"] = color
                self.rebuild()
                self.changed.emit()

    # ------------------------------------------------------------------ public API
    def set_tool(self, tool: str | None):
        self.tool = tool if tool in TOOLS else None
        self.cancel(keep_tool=True)
        self.chart.set_cursor_for_tool(self.tool is not None)

    def add(self, dtype: str, pts: list, color: str = DEFAULT_COLOR) -> int:
        if dtype not in TOOLS or len(pts) < TOOLS[dtype]:
            raise ValueError(f"'{dtype}' needs {TOOLS.get(dtype, '?')} point(s)")
        self._push_undo()
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
                self._push_undo()
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
        if self.drawings:
            self._push_undo()
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
                                      "color": d.get("color", DEFAULT_COLOR), **({"text": d["text"]} if "text" in d else {})})
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
                note = None
                if self.tool == "text":
                    note, ok = QtWidgets.QInputDialog.getText(self.chart, "Text note", "Text:")
                    if not ok or not note.strip():
                        self.pending = []
                        return
                did = self.add(self.tool, self.pending, DEFAULT_COLOR)
                if note:
                    for e in self.drawings:
                        if e["id"] == did:
                            e["text"] = note.strip()
                            self._drop_items(did)
                            self._build(e)
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
        elif t == "hray" and pts:
            items.append(pg.PlotDataItem([pts[0][0], xmax], [pts[0][1], pts[0][1]], pen=pen))
        elif t == "crossline" and pts:
            items.append(pg.InfiniteLine(pos=pts[0][1], angle=0, movable=False, pen=pen))
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
        elif t == "measure" and len(pts) == 2:
            (x1, y1), (x2, y2) = pts
            up = y2 >= y1
            col = theme.UP if up else theme.DOWN
            c = QtGui.QColor(col)
            c.setAlpha(60)
            r = QtWidgets.QGraphicsRectItem(QtCore.QRectF(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)))
            qp = QtGui.QPen(pg.mkPen(col, width=1))
            qp.setCosmetic(True)
            r.setPen(qp)
            r.setBrush(QtGui.QBrush(c))
            items.append(r)
            items.append(pg.PlotDataItem([x1, x2], [y1, y2], pen=pg.mkPen(col, width=1.2)))
            dp = y2 - y1
            pct = dp / y1 * 100 if y1 else 0.0
            bars = int(round(x2 - x1))
            tx = pg.TextItem(f"{dp:+,.2f} ({pct:+.2f}%)\n{bars} bars", color="#ffffff", anchor=(0.5, 1 if up else 0),
                             fill=pg.mkBrush(col))
            tx.setPos((x1 + x2) / 2, y2)
            items.append(tx)
        elif t == "text" and pts:
            tx = pg.TextItem(d.get("text", "Text"), color="#ffffff", anchor=(0, 1), fill=pg.mkBrush(color))
            tx.setPos(*pts[0])
            items.append(tx)
        if sel:
            hs = [p for p in pts] if t not in ("hline", "vline", "text") else []
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
        if t == "hray":
            return [((pts[0][0], pts[0][1]), (far, pts[0][1]))]
        if t == "crossline":
            lo, hi = self.chart.y_range()
            return [((-5, pts[0][1]), (far, pts[0][1])), ((pts[0][0], lo), (pts[0][0], hi))]
        if t == "vline":
            lo, hi = self.chart.y_range()
            return [((pts[0][0], lo), (pts[0][0], hi))]
        if t == "rect" and len(pts) == 2:
            (x1, y1), (x2, y2) = pts
            return [((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)), ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))]
        if t == "text" and pts:
            x, y = pts[0]
            return [((x, y), (x + 8, y))]
        if t == "measure" and len(pts) == 2:
            (x1, y1), (x2, y2) = pts
            return [(pts[0], pts[1]), ((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)), ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))]
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
