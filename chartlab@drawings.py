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
# A selected drawing, measured on TradingView's own chart canvas (dark theme, Retina):
SEL_RING = "#1e53e5"     # its handles: a ring in this blue whatever the line colour, filled with the chart background
HANDLE_R = 5.25          # 12 px across on the outside, 9 px inside: a 1.5 px ring around a 10.5 px path
HANDLE_W = 1.5
SEL_LABEL = "#2962ff"    # each anchor's price / time is labelled on the scales in this blue (band between: 25 %)
FLOAT_AT = 0.9           # a horizontal line's handle sits 90 % across the pane, a vertical line's 90 % down


def _seg(*a, **k):
    """A drawing's line. Slanted and curved lines are antialiased, like TradingView's (smooth, no stair-steps);
    exactly horizontal / vertical ones are not, so they stay one crisp pixel wide."""
    xs = k.get("x", a[0] if len(a) > 0 else None)
    ys = k.get("y", a[1] if len(a) > 1 else None)
    straight = False
    try:
        if xs is not None and ys is not None and len(xs) == 2:
            straight = float(xs[0]) == float(xs[1]) or float(ys[0]) == float(ys[1])
    except Exception:  # noqa: BLE001
        straight = False
    k.setdefault("antialias", not straight)
    return pg.PlotDataItem(*a, **k)


class _Handle(QtWidgets.QGraphicsEllipseItem):
    """One handle of the selected drawing: the same size on screen at any zoom, antialiased (the chart itself is
    drawn without antialiasing for speed). Clicks go through to the chart, which does the dragging."""

    def __init__(self, x: float, y: float, bg: str):
        r = HANDLE_R
        super().__init__(-r, -r, 2 * r, 2 * r)
        self.setFlag(QtWidgets.QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        self.setAcceptHoverEvents(False)
        pen = QtGui.QPen(QtGui.QColor(SEL_RING))
        pen.setWidthF(HANDLE_W)
        self.setPen(pen)
        self.setBrush(QtGui.QBrush(QtGui.QColor(bg)))
        self.setPos(x, y)
        self.setZValue(55)                     # above the drawings and the crosshair lines

    def paint(self, p, opt, widget=None):
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        p.setPen(self.pen())
        p.setBrush(self.brush())
        p.drawEllipse(self.rect())


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
        self._float: list = []         # (handle, drawing id): handles that keep their place on screen (h / v line)
        self._sel_y: list = []         # the selected drawing's anchor labels on the price scale ...
        self._sel_x: list = []         # ... and on the time scale
        self._sel_plot = None          # the plot whose time scale carries the time labels
        self._band_axes: list = []     # scales showing the selected drawing's band

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
        self._sync_sel()
        self.chart.selection_changed()

    def handle_at(self, x: float, y: float):
        """The handle of the selected drawing under view point (x, y): its drag mode, or None."""
        d = self.get(self.selected) if self.selected is not None else None
        if d is None or d.get("lock") or not self.chart.has_data():
            return None
        vb = self.chart.price.vb
        click = vb.mapViewToScene(QtCore.QPointF(x, y))
        best, best_d = None, HIT_PIXELS + 2.0
        for hx, hy, mode in self._handles(d):
            ps = vb.mapViewToScene(QtCore.QPointF(hx, hy))
            dist = math.hypot(ps.x() - click.x(), ps.y() - click.y())
            if dist <= best_d:
                best, best_d = mode, dist
        return best

    def begin_drag(self, x: float, y: float) -> bool:
        if self.tool or self.locked or not self.chart.has_data():
            return False
        mode = self.handle_at(x, y)                        # grab a handle of the selected drawing
        if mode is not None:
            return self._start_drag(self.get(self.selected), mode, x, y)
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
        ch = self.chart
        mode = g["mode"]
        kind = mode[0]
        sx, sy = ch.snap(x, y) if ch.magnet else (x, y)
        if kind == "pt":
            idx = mode[1]
            if d["type"] in ANGLE_TOOLS and len(d["pts"]) == 2 and self._shift():
                ax, ay = self._xy(d)[1 - idx]
                sx, sy = self._constrain(ax, ay, x, y)
            d["pts"][idx] = [ch.x_to_time(sx), float(ch.iy(sy))]
        elif kind == "xy":                               # a corner / side of a box: moves one or two of its sides
            _k, ix, iy = mode
            if ix is not None:
                d["pts"][ix][0] = ch.x_to_time(sx)
            if iy is not None:
                d["pts"][iy][1] = float(ch.iy(sy))
        elif kind == "pos":                              # long / short position: entry, target, stop, width
            pts = d["pts"]
            if len(pts) < 3:
                pts.append([pts[1][0], pts[0][1] - (pts[1][1] - pts[0][1])])
            il = 0 if ch.time_to_x(pts[0][0]) <= ch.time_to_x(pts[1][0]) else 1
            which = mode[1]
            if which == "entry":
                pts[0][1] = float(ch.iy(sy))
                pts[il][0] = ch.x_to_time(sx)
            elif which == "target":
                pts[1][1] = float(ch.iy(sy))
            elif which == "stop":
                pts[2][1] = float(ch.iy(sy))
            else:
                pts[1 - il][0] = ch.x_to_time(sx)
        elif kind == "chan":
            self._drag_channel(d, mode[1], g, y, sx, sy)
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
        self._sync_sel()

    def _drag_channel(self, d, which, g, y, sx, sy):
        """Parallel channel, TradingView's six handles: the ends of the first line re-aim it (the channel keeps its
        width), its middle moves it up / down (the second line stays), the second line's handles set the width."""
        ch = self.chart
        o = g["orig"]
        (x1, y1), (x2, y2), (x3, _y3) = [(ch.time_to_x(t), float(ch.ty(p))) for t, p in o[:3]]
        off = self._channel_offset([(x1, y1), (x2, y2), (x3, _y3)])

        def on_line(xx, a, b):
            (ax, ay), (bx, by) = a, b
            return ay if bx == ax else ay + (by - ay) * (xx - ax) / (bx - ax)

        if which in ("p0", "p1"):
            ends = [[x1, y1], [x2, y2]]
            ends[0 if which == "p0" else 1] = [sx, sy]
            base = on_line(x3, ends[0], ends[1])
            d["pts"] = [[ch.x_to_time(ends[0][0]), float(ch.iy(ends[0][1]))],
                        [ch.x_to_time(ends[1][0]), float(ch.iy(ends[1][1]))],
                        [o[2][0], float(ch.iy(base + off))]]
        elif which == "mid1":
            dy = y - g["y0"]
            d["pts"] = [[o[0][0], float(ch.iy(y1 + dy))], [o[1][0], float(ch.iy(y2 + dy))], list(o[2])]
        else:                                            # "w": the second line goes through the mouse
            width = sy - on_line(sx, (x1, y1), (x2, y2))
            d["pts"] = [list(o[0]), list(o[1]), [o[0][0], float(ch.iy(y1 + width))]]

    def _handles(self, d) -> list:
        """TradingView's handles of a selected drawing: [(x, y, drag mode)] in view coordinates. Measured on its chart:
        a box has its 4 corners and 4 side middles, a channel both ends and the middle of each line, a position tool
        entry / target / stop on its left side and its width on the right, a horizontal line one handle 90 % across
        the pane (a vertical one 90 % down), everything else a handle on each of its points."""
        t = d["type"]
        if t in ("text", "brush") or not d["pts"] or not self.chart.has_data():
            return []
        pts = self._xy(d)
        if t == "hline":
            (x0, x1), _ = self.chart.price.vb.viewRange()
            return [(x0 + FLOAT_AT * (x1 - x0), pts[0][1], ("move", 0))]
        if t == "vline":
            _, (y0, y1) = self.chart.price.vb.viewRange()
            return [(pts[0][0], y1 - FLOAT_AT * (y1 - y0), ("move", 0))]
        if t == "rect" and len(pts) == 2:
            (x0, y0), (x1, y1) = pts
            xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
            return [(x0, y0, ("xy", 0, 0)), (x1, y1, ("xy", 1, 1)), (x0, y1, ("xy", 0, 1)), (x1, y0, ("xy", 1, 0)),
                    (xm, y0, ("xy", None, 0)), (xm, y1, ("xy", None, 1)),
                    (x0, ym, ("xy", 0, None)), (x1, ym, ("xy", 1, None))]
        if t == "ellipse" and len(pts) == 2:            # on the ellipse itself: top, bottom, left, right
            (x0, y0), (x1, y1) = pts
            xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
            return [(xm, y0, ("xy", None, 0)), (xm, y1, ("xy", None, 1)),
                    (x0, ym, ("xy", 0, None)), (x1, ym, ("xy", 1, None))]
        if t in ("long", "short") and len(pts) >= 2:
            (x1, y1), (x2, y2) = pts[0], pts[1]
            stop = pts[2][1] if len(pts) >= 3 else y1 - (y2 - y1)
            xa, xb = min(x1, x2), max(x1, x2)
            return [(xa, y1, ("pos", "entry")), (xa, y2, ("pos", "target")), (xa, stop, ("pos", "stop")),
                    (xb, y1, ("pos", "width"))]
        if t == "channel" and len(pts) >= 3:
            (x1, y1), (x2, y2) = pts[0], pts[1]
            off = self._channel_offset(pts)
            xm, ym = (x1 + x2) / 2, (y1 + y2) / 2
            return [(x1, y1, ("chan", "p0")), (x2, y2, ("chan", "p1")), (xm, ym, ("chan", "mid1")),
                    (x1, y1 + off, ("chan", "w")), (x2, y2 + off, ("chan", "w")), (xm, ym + off, ("chan", "w"))]
        return [(x, y, ("pt", i)) for i, (x, y) in enumerate(pts)]

    def end_drag(self):
        g, self._drag = self._drag, None
        if g and g["moved"]:
            self._undo.append(g["state"])
            del self._undo[:-100]
            self._redo.clear()
            self.changed.emit()

    def hover_hit(self, x: float, y: float) -> bool:
        return (not self.tool) and (self.handle_at(x, y) is not None or self._hit_test(x, y) is not None)

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
        self._sync_sel()

    # -- the selected drawing on the scales (TradingView): a blue label at each anchor, a light band between them
    def _sel_values(self, d):
        """(prices, times) of drawing d that TradingView labels on the scales while it is selected."""
        t, pts = d["type"], d["pts"]
        if t in ("text", "brush") or not pts:
            return [], []
        q = pts[:3] if t in ("long", "short", "channel") else pts
        prices = [] if t in ("vline", "hline", "hray") else [float(p) for _t, p in q]   # (h lines: their own label)
        times = [] if t == "hline" else [float(tt) for tt, _p in (pts[:2] if t in ("long", "short", "channel") else pts)]
        return sorted(set(prices)), sorted(set(times))

    def _time_text(self, t: float, xv: float) -> str:
        ch = self.chart
        ts = ch._time_at(int(round(xv)))                 # the bar the anchor sits on, like TradingView
        if ts is None:
            import pandas as pd
            ts = pd.Timestamp(int(round(t)), unit="s")
        return ch._crosshair_time(ts)

    def _sync_sel(self):
        ch = self.chart
        d = self.get(self.selected) if self.selected is not None else None
        live = d is not None and not self.hidden and ch.has_data() and d["id"] in self._items
        prices, times = self._sel_values(d) if live else ([], [])
        if not (prices or times or self._sel_y or self._sel_x or self._band_axes):
            return
        from .chart import AxisTag
        plot = ch._bottom_plot()
        if plot is not self._sel_plot:                   # an indicator pane came or went: the time scale moved
            for tg in self._sel_x:
                tg.hide()
                tg.deleteLater()
            self._sel_x, self._sel_plot = [], plot
        while len(self._sel_y) < len(prices):
            self._sel_y.append(AxisTag(ch, ch.price, "y", SEL_LABEL))
        while len(self._sel_x) < len(times):
            self._sel_x.append(AxisTag(ch, plot, "x", SEL_LABEL))
        for i, tg in enumerate(self._sel_y):
            tg._v = float(ch.ty(prices[i])) if i < len(prices) else None
            if tg._v is not None:
                tg.setText(ch.axis_label(tg._v))
        for i, tg in enumerate(self._sel_x):
            tg._v = float(ch.time_to_x(times[i])) if i < len(times) else None
            if tg._v is not None:
                tg.setText(self._time_text(times[i], tg._v))
        want = []
        if len(prices) >= 2:
            want.append((ch.price.getAxis("right"), (float(ch.ty(prices[0])), float(ch.ty(prices[-1])))))
        if len(times) >= 2:
            want.append((plot.getAxis("bottom"), (float(ch.time_to_x(times[0])), float(ch.time_to_x(times[-1])))))
        for ax in self._band_axes:
            if all(ax is not a for a, _b in want) and ax.sel_band is not None:
                ax.sel_band = None
                ax.picture = None
                ax.update()
        for ax, band in want:
            if ax.sel_band != band:
                ax.sel_band = band
                ax.picture = None
                ax.update()
        self._band_axes = [a for a, _b in want]
        self._place_sel()
        self._blocks()

    def _place_sel(self):
        tags = self._sel_y + self._sel_x
        if not tags:
            return
        ch = self.chart
        if not ch.has_data():
            for tg in tags:
                tg.hide()
            return
        (x0, x1), (y0, y1) = ch.price.vb.viewRange()
        for tg in self._sel_y:
            v = getattr(tg, "_v", None)
            ok = v is not None and y0 <= v <= y1
            tg.setVisible(ok)
            if ok:
                tg.setPos(x1, v)
        for tg in self._sel_x:
            v = getattr(tg, "_v", None)
            ok = v is not None and x0 <= v <= x1
            tg.setVisible(ok)
            if ok:
                tg.setPos(v, 0.0)

    def _place_float(self):
        for h, did in self._float:
            d = self.get(did)
            hs = self._handles(d) if d is not None else []
            if hs:
                h.setPos(hs[0][0], hs[0][1])

    def place_tags(self):
        self._place_float()
        self._place_sel()
        ch = self.chart
        if self._tags:
            if not ch.has_data():
                for tag in self._tags.values():
                    tag.hide()
            else:
                (_x0, x1), (y0, y1) = ch.price.vb.viewRange()
                for did, tag in self._tags.items():
                    y = self._tag_y.get(did)
                    ok = y is not None and y0 <= y <= y1 and not self.hidden and did in self._items
                    tag.setVisible(ok)
                    if ok:
                        tag.setPos(x1, y)
                ch.last_tag.raise_()            # the live price label stays on top, like TradingView
        self._blocks()

    def _blocks(self):
        """The scale's own numbers are not drawn under these labels (TradingView), as under the last price."""
        f = getattr(self.chart, "_sync_scale_blocks", None)
        if f is not None and hasattr(self.chart, "last_tag"):
            f()

    def _drop_items(self, did):
        self._float = [(h, i) for h, i in self._float if i != did]
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
        # (TradingView keeps the line as it is when it is selected: the handles show the selection)
        pen = pg.mkPen(color, width=w,
                       style=QtCore.Qt.PenStyle.DashLine if preview else STYLES.get(d.get("ls"), QtCore.Qt.PenStyle.SolidLine))
        pts = self._xy(d)
        items: list = []
        t = d["type"]
        xmax = self.chart.x_limit()

        if t == "trend" and len(pts) == 2:
            items.append(_seg([pts[0][0], pts[1][0]], [pts[0][1], pts[1][1]], pen=pen))
        elif t == "ray" and len(pts) == 2:
            (x1, y1), (x2, y2) = pts
            if x2 == x1:
                ex, ey = x2, y2
            else:
                ex = xmax if x2 > x1 else -5
                ey = y1 + (y2 - y1) * (ex - x1) / (x2 - x1)
            items.append(_seg([x1, ex], [y1, ey], pen=pen))
        elif t == "hline" and pts:
            items.append(pg.InfiniteLine(pos=pts[0][1], angle=0, movable=False, pen=pen))
        elif t == "vline" and pts:
            items.append(pg.InfiniteLine(pos=pts[0][0], angle=90, movable=False, pen=pen))
        elif t == "hray" and pts:
            items.append(_seg([pts[0][0], xmax], [pts[0][1], pts[0][1]], pen=pen))
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
            items.append(_seg([q[0] for q in pts], [q[1] for q in pts], pen=pen))
        elif t == "channel" and len(pts) >= 2:
            (x1, y1), (x2, y2) = pts[0], pts[1]
            items.append(_seg([x1, x2], [y1, y2], pen=pen))
            if len(pts) >= 3:
                off = self._channel_offset(pts)
                items.append(_seg([x1, x2], [y1 + off, y2 + off], pen=pen))
                items.append(_seg([x1, x2], [y1 + off / 2, y2 + off / 2],
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
                items.append(_seg([x1, x2], [y1, y2], pen=pen))
            else:
                ya = y1 + (y2 - y1) * (-5 - x1) / (x2 - x1)
                yb = y1 + (y2 - y1) * (xmax - x1) / (x2 - x1)
                items.append(_seg([-5, xmax], [ya, yb], pen=pen))
        elif t == "arrow" and len(pts) == 2:
            (x1, y1), (x2, y2) = pts
            items.append(_seg([x1, x2], [y1, y2], pen=pen))
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
                items.append(_seg([(x1 + x2) / 2] * 2, [y1, y2], pen=pg.mkPen(col, width=1.2)))
            else:
                txt = f"{int(round(x2 - x1))} bars"
                items.append(_seg([x1, x2], [(y1 + y2) / 2] * 2, pen=pg.mkPen(col, width=1.2)))
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
            items.append(_seg([xa, xb], [y1, y1], pen=pg.mkPen("#d1d4dc", width=1.2)))
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
            items.append(_seg([x1, x2], [y1, y2], pen=pg.mkPen("#787b86", width=1,
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
                items.append(_seg([xa, xb], [price, price], pen=pg.mkPen(col, width=1)))
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
            items.append(_seg([x1, x2], [y1, y2], pen=pg.mkPen(col, width=1.2)))
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
        if sel and not d.get("lock"):
            bg = self.chart.glw.backgroundBrush().color()
            bg = bg.name() if bg.alpha() else theme.BG
            for hx, hy, _mode in self._handles(d):
                h = _Handle(hx, hy, bg)
                items.append(h)
                if t in ("hline", "vline"):
                    self._float.append((h, d["id"]))           # keeps its place on screen while the chart moves
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
