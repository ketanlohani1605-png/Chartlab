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

TOOLS = {"trend": 2, "ray": 2, "hline": 1, "vline": 1, "rect": 2, "fib": 2, "measure": 2, "text": 1, "hray": 1, "crossline": 1,
         "ext": 2, "arrow": 2, "ellipse": 2, "pricerange": 2, "daterange": 2, "long": 2, "short": 2,
         "channel": 3, "brush": 2}
TOOL_LABELS = {"trend": "Trend line", "ray": "Ray", "hline": "Horizontal line",
               "vline": "Vertical line", "rect": "Rectangle", "fib": "Fib retracement",
               "measure": "Measure (price / % / bars)", "text": "Text note",
               "hray": "Horizontal ray", "crossline": "Cross line", "ext": "Extended line",
               "arrow": "Arrow", "ellipse": "Ellipse", "pricerange": "Price range", "daterange": "Date range",
               "long": "Long position", "short": "Short position",
               "channel": "Parallel channel", "brush": "Brush"}
FIB_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
FIB_COLORS = ["#787b86", "#f23645", "#ff9800", "#4caf50", "#089981", "#00bcd4", "#787b86"]
DEFAULT_COLOR = "#2962ff"
DEFAULT_WIDTH = 1.6
STYLES = {"solid": QtCore.Qt.PenStyle.SolidLine, "dash": QtCore.Qt.PenStyle.DashLine, "dot": QtCore.Qt.PenStyle.DotLine}
ANGLE_TOOLS = ("trend", "ray", "ext", "arrow")           # Shift while drawing: horizontal / vertical / 45 degrees
EXTRA_KEYS = ("text", "w", "ls", "lock")                 # optional per-drawing properties that are saved
_CLIP: dict = {}                                         # last copied drawing (Cmd+C), shared by all panes
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
        self._tags: dict = {}          # id -> price-scale label of a horizontal line / ray (TradingView shows it)
        self._tag_y: dict = {}

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
    locked = False
    hidden = False

    def set_hidden(self, on: bool):
        self.hidden = on
        for items in self._items.values():
            for it in items:
                it.setVisible(not on)
        self.place_tags()
        self.chart.selection_changed()

    def begin_drag(self, x: float, y: float) -> bool:
        if self.tool or self.locked or not self.chart.has_data():
            return False
        vb = self.chart.price.vb
        click = vb.mapViewToScene(QtCore.QPointF(x, y))
        if self.selected is not None:                      # grab a handle of the selected drawing
            d = next((d for d in self.drawings if d["id"] == self.selected), None)
            if d and d["type"] not in ("hline", "vline", "text", "brush") and not d.get("lock"):
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
        if d.get("lock"):
            return False                                   # a locked drawing stays put: the drag pans the chart
        mods = QtWidgets.QApplication.keyboardModifiers()
        M = QtCore.Qt.KeyboardModifier
        if mods & (M.AltModifier | M.ControlModifier | M.MetaModifier):
            # TradingView: hold Option / Cmd and drag a drawing to pull a copy off it
            state = self._snapshot()
            c = self._copy_of(d)
            self.drawings.append(c)
            self.selected = c["id"]
            self.rebuild()
            self._start_drag(c, ("move", 0), x, y)
            self._drag["state"], self._drag["moved"] = state, True
            return True
        return self._start_drag(d, ("move", 0), x, y)

    def _copy_of(self, d) -> dict:
        c = copy.deepcopy(d)
        c["id"] = self._next_id
        c.pop("lock", None)
        self._next_id += 1
        return c

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
            if d["type"] in ANGLE_TOOLS and len(d["pts"]) == 2 and self._shift():
                ax, ay = self._xy(d)[1 - idx]
                sx, sy = self._constrain(ax, ay, x, y)
            d["pts"][idx] = [self.chart.x_to_time(sx), float(self.chart.iy(sy))]
        else:
            dx, dy = x - g["x0"], y - g["y0"]
            new = []
            for (t, p) in g["orig"]:
                nx = self.chart.time_to_x(t) + (0 if d["type"] == "hline" else dx)
                new.append([self.chart.x_to_time(nx), p if d["type"] == "vline" else float(self.chart.iy(self.chart.ty(p) + dy))])
            d["pts"] = new
        if d["type"] in ("long", "short") and len(d["pts"]) == 3:
            d["pts"][2][0] = d["pts"][1][0]
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
            c = self._copy_of(d)
            off = (self.chart.y_range()[1] - self.chart.y_range()[0]) * 0.03
            for q in c["pts"]:
                q[1] = float(self.chart.iy(self.chart.ty(q[1]) + off))
            self.drawings.append(c)
            nid = c["id"]
            self.selected = nid
            self.rebuild()
            self.changed.emit()

    def set_color(self, did: int, color: str):
        self.set_props(did, color=color)

    def get(self, did):
        return next((d for d in self.drawings if d["id"] == did), None)

    def set_props(self, did: int, **props):
        """Change colour / width ('w') / line style ('ls') / lock / text / points of one drawing (one undo step)."""
        d = self.get(did)
        if d is None:
            return
        self._push_undo()
        for k, v in props.items():
            if v is None:
                d.pop(k, None)
            else:
                d[k] = v
        self.rebuild()
        self.changed.emit()

    def set_width(self, did: int, w: float):
        self.set_props(did, w=float(w))

    def set_style(self, did: int, ls: str):
        self.set_props(did, ls=ls if ls in STYLES else "solid")

    def set_locked(self, did: int, on: bool):
        self.set_props(did, lock=True if on else None)

    # ------------------------------------------------------------------ copy / paste
    def copy_selected(self) -> bool:
        d = self.get(self.selected)
        if d is None:
            return False
        _CLIP.clear()
        _CLIP.update(copy.deepcopy(d))
        return True

    def paste(self) -> bool:
        """Paste the copied drawing a little below the original (or onto this pane, when copied from another)."""
        if not _CLIP or not self.chart.has_data():
            return False
        self._push_undo()
        c = self._copy_of(_CLIP)
        lo, hi = self.chart.y_range()
        (x0, x1) = self.chart.price.vb.viewRange()[0]
        xs = [self.chart.time_to_x(t) for t, _ in c["pts"]]
        dx = 0.0 if (x0 <= min(xs) and max(xs) <= x1) else (x0 + x1) / 2 - (min(xs) + max(xs)) / 2
        for q in c["pts"]:
            q[0] = self.chart.x_to_time(self.chart.time_to_x(q[0]) + dx)
            q[1] = float(self.chart.iy(self.chart.ty(q[1]) - (hi - lo) * 0.04))
        self.drawings.append(c)
        self.selected = c["id"]
        self.rebuild()
        self.changed.emit()
        return True

    # ------------------------------------------------------------------ Shift = straight lines
    @staticmethod
    def _shift() -> bool:
        return bool(QtWidgets.QApplication.keyboardModifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier)

    def _constrain(self, ax: float, ay: float, x: float, y: float):
        """Second point of a line with Shift held: snap the direction (as seen on screen) to the nearest of
        horizontal, vertical and the two diagonals. All in view coordinates."""
        vb = self.chart.price.vb
        a, b = vb.mapViewToScene(QtCore.QPointF(ax, ay)), vb.mapViewToScene(QtCore.QPointF(x, y))
        dx, dy = b.x() - a.x(), b.y() - a.y()
        if dx == 0 and dy == 0:
            return x, y
        ang = round(math.atan2(dy, dx) / (math.pi / 4)) * (math.pi / 4)
        L = dx * math.cos(ang) + dy * math.sin(ang)
        q = vb.mapSceneToView(QtCore.QPointF(a.x() + L * math.cos(ang), a.y() + L * math.sin(ang)))
        return q.x(), (ay if abs(math.sin(ang)) < 1e-9 else q.y())

    # ------------------------------------------------------------------ public API
    def set_tool(self, tool: str | None):
        self.tool = tool if tool in TOOLS else None
        self.cancel(keep_tool=True)
        self.chart.set_cursor_for_tool(self.tool is not None)
        self.chart.selection_changed()                       # the floating toolbar hides while a tool is in hand

    def add(self, dtype: str, pts: list, color: str = DEFAULT_COLOR) -> int:
        if dtype not in TOOLS or len(pts) < TOOLS[dtype]:
            raise ValueError(f"'{dtype}' needs {TOOLS.get(dtype, '?')} point(s)")
        self._push_undo()
        n = 3 if dtype in ("long", "short") and len(pts) >= 3 else (len(pts) if dtype == "brush" else TOOLS[dtype])
        d = {"id": self._next_id, "type": dtype, "pts": [[float(t), float(p)] for t, p in pts[:n]],
             "color": color}
        if dtype in ("long", "short") and len(d["pts"]) == 2:      # stop starts mirrored; drag its handle to change R/R
            (t0, p0), (t1, p1) = d["pts"]
            d["pts"].append([t1, p0 - (p1 - p0)])
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
                self._sync_tags()
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
        self._sync_tags()
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
                if d["type"] not in TOOLS:
                    continue
                self.drawings.append({"id": self._next_id, "type": d["type"],
                                      "pts": [[float(a), float(b)] for a, b in d["pts"]],
                                      "color": d.get("color", DEFAULT_COLOR),
                                      **{k: d[k] for k in EXTRA_KEYS if k in d}})
                self._next_id += 1
            except Exception:  # noqa: BLE001 - ignore a corrupt stored drawing
                continue
        self.rebuild()

    def clear_silent(self):
        for did in list(self._items):
            self._drop_items(did)
        self.drawings, self.selected, self.pending = [], None, []
        self._clear_preview()
        self._sync_tags()

    # ------------------------------------------------------------------ mouse input (view coordinates)
    def handle_click(self, x: float, y: float):
        if self.tool == "brush":
            return                                           # the brush is drawn by dragging (see brush_stroke)
        if self.tool:
            if self.tool in ANGLE_TOOLS and len(self.pending) == 1 and self._shift():
                ax, ay = self.chart.time_to_x(self.pending[0][0]), float(self.chart.ty(self.pending[0][1]))
                x, y = self._constrain(ax, ay, x, y)
            t = self.chart.x_to_time(x)
            self.pending.append([t, float(self.chart.iy(y))])
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
                if self.chart.stay_in_drawing and self.tool != "measure":
                    self.selected = None                     # keep drawing: the tool stays in the hand
                    self.rebuild()
                    return
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
        if not (self.tool and self.pending) or self.tool == "brush":
            return
        if self.tool in ANGLE_TOOLS and len(self.pending) == 1 and self._shift():
            ax, ay = self.chart.time_to_x(self.pending[0][0]), float(self.chart.ty(self.pending[0][1]))
            x, y = self._constrain(ax, ay, x, y)
        d = {"id": -1, "type": self.tool, "pts": self.pending + [[self.chart.x_to_time(x), float(self.chart.iy(y))]],
             "color": DEFAULT_COLOR}
        self._clear_preview()
        self._preview_items = self._make_items(d, preview=True)
        for it in self._preview_items:
            self.chart.price.addItem(it, ignoreBounds=True)

    def brush_stroke(self, x: float, y: float, start: bool, finish: bool):
        """Freehand: called while the mouse is dragged with the Brush tool."""
        pt = [self.chart.x_to_time(x), float(self.chart.iy(y))]
        if start:
            self.pending = [pt]
        elif self.pending:
            a = self.chart.price.vb.mapViewToScene(QtCore.QPointF(self.chart.time_to_x(self.pending[-1][0]),
                                                                  float(self.chart.ty(self.pending[-1][1]))))
            b = self.chart.price.vb.mapViewToScene(QtCore.QPointF(x, y))
            if math.hypot(a.x() - b.x(), a.y() - b.y()) >= 2.0 or finish:      # skip points on top of each other
                self.pending.append(pt)
        if not finish:
            self._clear_preview()
            if len(self.pending) >= 2:
                self._preview_items = self._make_items({"id": -1, "type": "brush", "pts": self.pending,
                                                        "color": DEFAULT_COLOR})
                for it in self._preview_items:
                    self.chart.price.addItem(it, ignoreBounds=True)
            return
        pts, self.pending = self.pending, []
        self._clear_preview()
        if len(pts) >= 2:
            self.selected = self.add("brush", pts, DEFAULT_COLOR)
            self.rebuild()
        if not self.chart.stay_in_drawing:
            self.tool = None
            self.chart.set_cursor_for_tool(False)
            self.chart.toolChanged.emit(None)

    # ------------------------------------------------------------------ building visuals
    def rebuild(self):
        for did in list(self._items):
            self._drop_items(did)
        for d in self.drawings:
            self._build(d)
        self._sync_tags()
        self.chart.selection_changed()

    def _build(self, d):
        items = self._make_items(d)
        for it in items:
            self.chart.price.addItem(it, ignoreBounds=True)
        self._items[d["id"]] = items
        if d["type"] in ("hline", "hray") and self.chart.has_data():
            self._set_tag(d)

    # -- price labels on the scale for horizontal lines (kept and re-used while a line is dragged)
    def _set_tag(self, d):
        did, color = d["id"], d.get("color", DEFAULT_COLOR)
        tag = self._tags.get(did)
        if tag is None:
            tag = self._tags[did] = self.chart.make_axis_tag(color)
        tag.fill = color
        price = float(d["pts"][0][1])
        tag.setText(f" {self.chart.axis_price(price)} ")
        self._tag_y[did] = float(self.chart.ty(price))
        self.place_tags()

    def _sync_tags(self):
        keep = {d["id"] for d in self.drawings if d["type"] in ("hline", "hray")}
        for did in [k for k in self._tags if k not in keep]:
            tag = self._tags.pop(did)
            self._tag_y.pop(did, None)
            tag.hide()
            tag.deleteLater()
        self.place_tags()

    def place_tags(self):
        if not self._tags:
            return
        ch = self.chart
        if not ch.has_data():
            for tag in self._tags.values():
                tag.hide()
            return
        (_x0, x1), (y0, y1) = ch.price.vb.viewRange()
        for did, tag in self._tags.items():
            y = self._tag_y.get(did)
            ok = y is not None and y0 <= y <= y1 and not self.hidden and did in self._items
            tag.setVisible(ok)
            if ok:
                tag.setPos(x1, y)
        ch.last_tag.raise_()                    # the live price label stays on top, like TradingView

    def _drop_items(self, did):
        for it in self._items.pop(did, []):
            self.chart.price.removeItem(it)

    def _clear_preview(self):
        for it in self._preview_items:
            self.chart.price.removeItem(it)
        self._preview_items = []

    def _xy(self, d):
        ty = self.chart.ty
        return [(self.chart.time_to_x(t), float(ty(p))) for t, p in d["pts"]]

    def _make_items(self, d, preview: bool = False) -> list:
        if not self.chart.has_data():
            return []
        sel = d["id"] == self.selected and not preview
        color = d.get("color", DEFAULT_COLOR)
        w = float(d.get("w", DEFAULT_WIDTH))
        pen = pg.mkPen(color, width=w + 0.9 if sel else w,
                       style=QtCore.Qt.PenStyle.DashLine if preview else STYLES.get(d.get("ls"), QtCore.Qt.PenStyle.SolidLine))
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
            items.append(pg.InfiniteLine(pos=pts[0][1], angle=0, movable=False, pen=pen))
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
        elif t == "brush" and len(pts) >= 2:
            items.append(pg.PlotDataItem([q[0] for q in pts], [q[1] for q in pts], pen=pen))
        elif t == "channel" and len(pts) >= 2:
            (x1, y1), (x2, y2) = pts[0], pts[1]
            items.append(pg.PlotDataItem([x1, x2], [y1, y2], pen=pen))
            if len(pts) >= 3:
                off = self._channel_offset(pts)
                items.append(pg.PlotDataItem([x1, x2], [y1 + off, y2 + off], pen=pen))
                items.append(pg.PlotDataItem([x1, x2], [y1 + off / 2, y2 + off / 2],
                                             pen=pg.mkPen(color, width=1, style=QtCore.Qt.PenStyle.DashLine)))
                poly = QtWidgets.QGraphicsPolygonItem(QtGui.QPolygonF([QtCore.QPointF(x1, y1), QtCore.QPointF(x2, y2),
                                                                       QtCore.QPointF(x2, y2 + off), QtCore.QPointF(x1, y1 + off)]))
                poly.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
                c = QtGui.QColor(color)
                c.setAlpha(30)
                poly.setBrush(QtGui.QBrush(c))
                poly.setZValue(-2)
                items.append(poly)
        elif t == "ext" and len(pts) == 2:
            (x1, y1), (x2, y2) = pts
            if x2 == x1:
                items.append(pg.PlotDataItem([x1, x2], [y1, y2], pen=pen))
            else:
                ya = y1 + (y2 - y1) * (-5 - x1) / (x2 - x1)
                yb = y1 + (y2 - y1) * (xmax - x1) / (x2 - x1)
                items.append(pg.PlotDataItem([-5, xmax], [ya, yb], pen=pen))
        elif t == "arrow" and len(pts) == 2:
            (x1, y1), (x2, y2) = pts
            items.append(pg.PlotDataItem([x1, x2], [y1, y2], pen=pen))
            import math as _m
            vb = self.chart.price.vb
            pa = vb.mapViewToScene(QtCore.QPointF(x1, y1)); pb = vb.mapViewToScene(QtCore.QPointF(x2, y2))
            ang = _m.degrees(_m.atan2(-(pb.y() - pa.y()), pb.x() - pa.x()))
            ar = pg.ArrowItem(pos=(x2, y2), angle=180 - ang, headLen=14, tipAngle=30, brush=pg.mkBrush(color), pen=pg.mkPen(color))
            items.append(ar)
        elif t == "ellipse" and len(pts) == 2:
            (x1, y1), (x2, y2) = pts
            r = QtWidgets.QGraphicsEllipseItem(QtCore.QRectF(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)))
            qpen = QtGui.QPen(pen); qpen.setCosmetic(True)
            r.setPen(qpen)
            c = QtGui.QColor(color); c.setAlpha(40)
            r.setBrush(QtGui.QBrush(c))
            items.append(r)
        elif t in ("pricerange", "daterange") and len(pts) == 2:
            (x1, y1), (x2, y2) = pts
            up = (y2 >= y1) if t == "pricerange" else (x2 >= x1)
            col = theme.UP if up else theme.DOWN
            c = QtGui.QColor(col); c.setAlpha(50)
            r = QtWidgets.QGraphicsRectItem(QtCore.QRectF(min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)))
            qp = QtGui.QPen(pg.mkPen(col, width=1)); qp.setCosmetic(True)
            r.setPen(qp); r.setBrush(QtGui.QBrush(c))
            items.append(r)
            if t == "pricerange":
                ry1, ry2 = float(self.chart.iy(y1)), float(self.chart.iy(y2))
                dp = ry2 - ry1
                txt = f"{dp:+,.2f} ({dp / ry1 * 100 if ry1 else 0:+.2f}%)"
                items.append(pg.PlotDataItem([(x1 + x2) / 2] * 2, [y1, y2], pen=pg.mkPen(col, width=1.2)))
            else:
                txt = f"{int(round(x2 - x1))} bars"
                items.append(pg.PlotDataItem([x1, x2], [(y1 + y2) / 2] * 2, pen=pg.mkPen(col, width=1.2)))
            tx = pg.TextItem(txt, color="#ffffff", anchor=(0.5, 1), fill=pg.mkBrush(col))
            tx.setPos((x1 + x2) / 2, max(y1, y2))
            items.append(tx)
        elif t in ("long", "short") and len(pts) >= 2:
            # TradingView's position tool: entry line, green target zone, red stop zone, labels with % and R/R
            (x1, y1), (x2, y2) = pts[0], pts[1]
            tgt = y2
            stop = pts[2][1] if len(pts) >= 3 else y1 - (tgt - y1)
            xa, xb = min(x1, x2), max(x1, x2, min(x1, x2) + 1)
            for lo, hi, col in ((min(y1, tgt), max(y1, tgt), theme.UP), (min(y1, stop), max(y1, stop), theme.DOWN)):
                c = QtGui.QColor(col)
                c.setAlpha(55)
                r = QtWidgets.QGraphicsRectItem(QtCore.QRectF(xa, lo, xb - xa, hi - lo))
                r.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
                r.setBrush(QtGui.QBrush(c))
                items.append(r)
            items.append(pg.PlotDataItem([xa, xb], [y1, y1], pen=pg.mkPen("#d1d4dc", width=1.2)))
            if not preview or len(pts) >= 2:
                iy = self.chart.iy
                pe, pt_, ps = float(iy(y1)), float(iy(tgt)), float(iy(stop))
                rr = abs(pt_ - pe) / abs(ps - pe) if ps != pe else 0.0
                tp = abs(pt_ / pe - 1) * 100 if pe else 0.0
                sp = abs(ps / pe - 1) * 100 if pe else 0.0
                fp = self.chart.fp
                mid = (xa + xb) / 2
                up_t = tgt >= y1                              # target above entry (a long) -> its label at the top
                lab_t = pg.TextItem(f"Target: {fp(pt_)} ({tp:.2f}%)", color="#ffffff", anchor=(0.5, 0 if up_t else 1),
                                    fill=pg.mkBrush(theme.UP))
                lab_t.setPos(mid, tgt)
                lab_s = pg.TextItem(f"Stop: {fp(ps)} ({sp:.2f}%)", color="#ffffff", anchor=(0.5, 1 if up_t else 0),
                                    fill=pg.mkBrush(theme.DOWN))
                lab_s.setPos(mid, stop)
                lab_m = pg.TextItem(f"{'Long' if t == 'long' else 'Short'}  \u00b7  Risk/Reward {rr:.2f}",
                                    color="#ffffff", anchor=(0.5, 0.5), fill=pg.mkBrush("#2a2e39"))
                lab_m.setPos(mid, y1)
                items += [lab_t, lab_s, lab_m]
        elif t == "fib" and len(pts) == 2:
            (x1, y1), (x2, y2) = pts
            xa, xb = min(x1, x2), max(x1, x2, min(x1, x2) + 1)
            items.append(pg.PlotDataItem([x1, x2], [y1, y2], pen=pg.mkPen("#787b86", width=1,
                                                                         style=QtCore.Qt.PenStyle.DashLine)))
            prev = None
            for lvl, col in zip(FIB_LEVELS, FIB_COLORS):
                price = y2 + (y1 - y2) * lvl
                if prev is not None and not preview:           # soft band between this level and the last one
                    c = QtGui.QColor(col)
                    c.setAlpha(26)
                    band = QtWidgets.QGraphicsRectItem(QtCore.QRectF(xa, min(prev, price), xb - xa, abs(price - prev)))
                    band.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
                    band.setBrush(QtGui.QBrush(c))
                    band.setZValue(-2)
                    items.append(band)
                prev = price
                items.append(pg.PlotDataItem([xa, xb], [price, price], pen=pg.mkPen(col, width=1.4 if sel else 1)))
                tx = pg.TextItem(f"{lvl:g}  ({self.chart.iy(price):,.2f})", color=col, anchor=(0, 1))
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
            ry1, ry2 = (float(self.chart.iy(y1)), float(self.chart.iy(y2)))
            dp = ry2 - ry1
            pct = dp / ry1 * 100 if ry1 else 0.0
            bars = int(round(x2 - x1))
            tx = pg.TextItem(f"{dp:+,.2f} ({pct:+.2f}%)\n{bars} bars", color="#ffffff", anchor=(0.5, 1 if up else 0),
                             fill=pg.mkBrush(col))
            tx.setPos((x1 + x2) / 2, y2)
            items.append(tx)
        elif t == "text" and pts:
            q = QtGui.QColor(color)
            light = 0.2126 * q.redF() + 0.7152 * q.greenF() + 0.0722 * q.blueF() > 0.7
            tx = pg.TextItem(d.get("text", "Text"), color="#000000" if light else "#ffffff", anchor=(0, 1),
                             fill=pg.mkBrush(color))
            tx.setPos(*pts[0])
            items.append(tx)
        if sel:
            hs = [p for p in pts] if t not in ("hline", "vline", "text", "brush") else []
            if d.get("lock"):
                hs = []
            if hs:
                items.append(pg.ScatterPlotItem(x=[p[0] for p in hs], y=[p[1] for p in hs], size=9,
                                                brush=pg.mkBrush(theme.BG), pen=pg.mkPen(color, width=2)))
        return items

    @staticmethod
    def _channel_offset(pts) -> float:
        """How far (in price-axis units) the second line of a channel sits from the first: it runs through point 3."""
        (x1, y1), (x2, y2), (x3, y3) = pts[0], pts[1], pts[2]
        base = y1 if x2 == x1 else y1 + (y2 - y1) * (x3 - x1) / (x2 - x1)
        return y3 - base

    # ------------------------------------------------------------------ hit testing
    def _segments(self, d):
        pts = self._xy(d)
        t = d["type"]
        far = self.chart.x_limit()
        if t == "brush" and len(pts) >= 2:
            step = max(1, len(pts) // 60)
            q = pts[::step] + ([pts[-1]] if (len(pts) - 1) % step else [])
            return list(zip(q[:-1], q[1:]))
        if t == "channel" and len(pts) >= 3:
            (x1, y1), (x2, y2) = pts[0], pts[1]
            off = self._channel_offset(pts)
            return [(pts[0], pts[1]), ((x1, y1 + off), (x2, y2 + off))]
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
        if t in ("long", "short") and len(pts) >= 2:
            (x1, y1), (x2, y2) = pts[0], pts[1]
            ys = y1 - (y2 - y1) if len(pts) < 3 else pts[2][1]
            return [((x1, y), (x2, y)) for y in (y1, y2, ys)] + [((x, y2), (x, ys)) for x in (x1, x2)]
        if t in ("rect", "ellipse", "pricerange", "daterange") and len(pts) == 2:
            (x1, y1), (x2, y2) = pts
            return [((x1, y1), (x2, y1)), ((x2, y1), (x2, y2)), ((x2, y2), (x1, y2)), ((x1, y2), (x1, y1))]
        if t == "arrow" and len(pts) == 2:
            return [(pts[0], pts[1])]
        if t == "ext" and len(pts) == 2:
            (x1, y1), (x2, y2) = pts
            if x2 == x1:
                return [(pts[0], pts[1])]
            f = lambda x: y1 + (y2 - y1) * (x - x1) / (x2 - x1)
            return [((-5, f(-5)), (far, f(far)))]
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
