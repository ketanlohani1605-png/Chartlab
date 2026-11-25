"""Partial repaints stroke only what changed.

Moving the crosshair (or a label on the price scale) repaints a few thin strips of the chart. Qt then asks every
line that crosses a strip to paint, clipped to the strips - but stroking a line costs the same whether two pixels
or the whole line reach the screen: with ten indicator lines that was ~1.5 ms each on a Retina screen, for every
mouse move (the crosshair lagged behind the mouse with indicators on).

Here a line that only has to fill in small strips strokes just the segments that reach into them, plus one more
on each side so every corner inside a strip is joined exactly as in the whole line: the pixels are the same, the
work is a few segments. Anything else - a full repaint, a fill, steps, dashes, an export - goes to pyqtgraph's own
paint unchanged.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui

_ORIG = pg.PlotCurveItem.paint
_ORIG_VIEW_PAINT = pg.GraphicsView.paintEvent
_SOLID = QtCore.Qt.PenStyle.SolidLine
_REGION = None          # [viewport, QRegion, its pieces] of the paint in progress: what is being repainted
MAX_SHARE = 0.6         # the repaint covers more than this share of the line's box: paint the usual way
stats = {"fast": 0, "full": 0}


def _eligible(item) -> bool:
    o = item.opts
    if o.get("fillLevel") is not None or o.get("stepMode") or o.get("shadowPen") is not None:
        return False
    if o.get("connect") not in ("all", "finite") or item._exportOpts is not False:
        return False
    x, y = item.xData, item.yData
    if x is None or y is None or len(x) < 8 or len(x) != len(y):
        return False
    pen = o.get("pen")
    return isinstance(pen, QtGui.QPen) and pen.style() == _SOLID


def _rects(reg) -> np.ndarray:
    """The repaint's pieces as an (n, 4) array of left, top, right, bottom - worked out once per paint."""
    cur = _REGION
    if cur is not None and cur[1] is reg:
        if cur[2] is None:
            cur[2] = np.array([(r.left(), r.top(), r.right() + 1, r.bottom() + 1) for r in reg], dtype=float)
        return cur[2]
    return np.array([(r.left(), r.top(), r.right() + 1, r.bottom() + 1) for r in reg], dtype=float)


def _strips(item, p, reg) -> bool:
    """Stroke the segments of `item` that reach into `reg` (viewport coordinates); False = paint it the usual way."""
    T = p.worldTransform()                                  # item -> viewport
    if T.m12() != 0.0 or T.m21() != 0.0 or T.m13() != 0.0 or T.m23() != 0.0:
        return False
    box = T.mapRect(item.boundingRect())
    bw, bh = box.width(), box.height()
    if bw <= 0 or bh <= 0:
        return False
    # the pieces of the repaint that touch this line's box
    A = _rects(reg)
    if not len(A):
        return False
    cx0 = np.maximum(A[:, 0], box.left())
    cy0 = np.maximum(A[:, 1], box.top())
    cx1 = np.minimum(A[:, 2], box.right())
    cy1 = np.minimum(A[:, 3], box.bottom())
    hit = (cx1 > cx0) & (cy1 > cy0)
    stats["fast"] += 1
    if not hit.any():
        return True                                         # the repaint does not reach this line at all
    if float(((cx1 - cx0) * (cy1 - cy0))[hit].sum()) > MAX_SHARE * bw * bh:
        stats["fast"] -= 1
        return False                                        # most of it: the usual way is as quick
    A = A[hit]
    L, Tp, R, B = A[:, 0], A[:, 1], A[:, 2], A[:, 3]
    x = np.asarray(item.xData, dtype=float)
    y = np.asarray(item.yData, dtype=float)
    X = T.m11() * x + T.m31()
    Y = T.m22() * y + T.m32()
    ok = np.isfinite(X) & np.isfinite(Y)
    seg_ok = ok[:-1] & ok[1:]
    pen = item.opts["pen"]
    pad = max(pen.widthF(), 1.0) * 2.0 + 2.0
    with np.errstate(invalid="ignore"):
        sx0 = np.minimum(X[:-1], X[1:])[:, None]
        sx1 = np.maximum(X[:-1], X[1:])[:, None]
        sy0 = np.minimum(Y[:-1], Y[1:])[:, None]
        sy1 = np.maximum(Y[:-1], Y[1:])[:, None]
        sel = ((sx0 <= R[None, :] + pad) & (sx1 >= L[None, :] - pad)
               & (sy0 <= B[None, :] + pad) & (sy1 >= Tp[None, :] - pad)).any(axis=1)
    sel &= seg_ok
    if not sel.any():
        return True                                         # nothing of this line in the pieces
    if sel.mean() > 0.8:
        stats["fast"] -= 1
        return False
    grown = sel.copy()                                      # one more segment each side: corners joined as before
    grown[1:] |= sel[:-1]
    grown[:-1] |= sel[1:]
    grown &= seg_ok
    pts = np.zeros(len(X), dtype=bool)                      # the points of those segments, joined run by run
    pts[:-1] |= grown
    pts[1:] |= grown
    idx = np.flatnonzero(pts)
    conn = np.zeros(len(idx), dtype=bool)
    inner = idx < len(grown)
    conn[inner] = grown[idx[inner]]
    path = pg.arrayToQPath(X[idx], Y[idx], connect=conn)
    p.setWorldTransform(QtGui.QTransform())                 # the path is in viewport coordinates
    try:
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, bool(item.opts["antialias"]))
        cmode = item.opts.get("compositionMode")
        if cmode is not None:
            p.setCompositionMode(cmode)
        p.setPen(pen)
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        p.drawPath(path)
    finally:
        p.setWorldTransform(T)
    return True


def paint(self, p, opt, widget):
    reg = _REGION
    try:
        if reg is not None and widget is reg[0] and _eligible(self) and _strips(self, p, reg[1]):
            return
    except Exception:  # noqa: BLE001 - never lose a line over this: the usual paint
        pass
    stats["full"] += 1
    return _ORIG(self, p, opt, widget)


def view_paint(self, ev):
    """A chart view's paint: remember which region is being repainted (a painter does not tell its items)."""
    global _REGION
    _REGION = [self.viewport(), ev.region(), None]
    try:
        return _ORIG_VIEW_PAINT(self, ev)
    finally:
        _REGION = None


def install():
    """Before any chart exists (Qt binds an item's paint when the item is made)."""
    if pg.PlotCurveItem.paint is not paint:
        pg.PlotCurveItem.paint = paint
    if pg.GraphicsView.paintEvent is not view_paint:
        pg.GraphicsView.paintEvent = view_paint
