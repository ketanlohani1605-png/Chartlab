"""Viewport-aware renderers: only the visible bars are painted, and when bars are narrower than ~3 px they are
merged (OHLC aggregation), so a chart with 1,000,000 bars pans and zooms as smoothly as one with 100."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui

from . import theme

MIN_BAR_PX = 3.0


def _visible(item, n: int):
    n = n if getattr(item, 'limit', None) is None else min(n, item.limit)
    """(i0, i1, group) -> visible slice [i0, i1) and how many bars to merge into one drawn bar."""
    vb = item.getViewBox()
    if vb is None or n == 0:
        return 0, 0, 1
    x0, x1 = vb.viewRange()[0]
    i0, i1 = max(0, int(np.floor(x0)) - 1), min(n, int(np.ceil(x1)) + 2)
    if i1 <= i0:
        return 0, 0, 1
    px = max(vb.width(), 50.0)
    group = max(1, int(np.ceil((i1 - i0) * MIN_BAR_PX / px)))
    i0 -= i0 % group          # keep groups stable while panning (no flicker)
    return i0, i1, group


class CandleItem(pg.GraphicsObject):
    def __init__(self, df=None, style="candles"):
        super().__init__()
        self.style = style
        self.limit = None
        self.o = self.h = self.l = self.c = np.zeros(0)
        if df is not None:
            self.set_data(df, style)

    def set_data(self, df, style="candles"):
        self.style = style
        self.o, self.h, self.l, self.c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
        self.prepareGeometryChange()
        self.informViewBoundsChanged()
        self.update()

    def boundingRect(self):
        n = len(self.o)
        if n == 0:
            return QtCore.QRectF()
        lo, hi = float(np.nanmin(self.l)), float(np.nanmax(self.h))
        return QtCore.QRectF(-1, lo, n + 2, max(hi - lo, 1e-9))

    def paint(self, p, *args):
        n = len(self.o)
        i0, i1, g = _visible(self, n)
        if i1 <= i0:
            return
        if g == 1:
            o, h, l, c = self.o[i0:i1], self.h[i0:i1], self.l[i0:i1], self.c[i0:i1]
            xs = np.arange(i0, i1, dtype=float)
            w = 0.36
        else:
            edges = np.arange(i0, i1, g)
            last = np.minimum(edges + g, n) - 1
            o, c = self.o[edges], self.c[last]
            h = np.maximum.reduceat(self.h[i0:i1], edges - i0)
            l = np.minimum.reduceat(self.l[i0:i1], edges - i0)
            xs = edges + (g - 1) / 2.0
            w = 0.36 * g
        up = c >= o
        hollow = self.style == "hollow"
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        for is_up, color in ((True, theme.UP), (False, theme.DOWN)):
            m = up == is_up
            if not m.any():
                continue
            pen = pg.mkPen(color)
            p.setPen(pen)
            p.setBrush(QtGui.QBrush(QtCore.Qt.BrushStyle.NoBrush) if (hollow and is_up) else pg.mkBrush(color))
            x, hh, ll, oo, cc = xs[m], h[m], l[m], o[m], c[m]
            lines = [QtCore.QLineF(a, b, a, d) for a, b, d in zip(x, ll, hh)]
            p.drawLines(lines)
            if g == 1 or True:
                for a, b, d in zip(x, oo, cc):
                    p.drawRect(QtCore.QRectF(a - w, b, 2 * w, d - b))


class VolumeItem(pg.GraphicsObject):
    def __init__(self, df=None):
        super().__init__()
        self.limit = None
        self.overlay = None      # ViewBox of the price pane: draw as a thin strip at its bottom (TradingView style)
        self.v = np.zeros(0)
        self.up = np.zeros(0, bool)
        if df is not None:
            self.set_data(df)

    def set_data(self, df):
        self.v = df["Volume"].to_numpy(float)
        self.up = (df["Close"] >= df["Open"]).to_numpy()
        self.prepareGeometryChange()
        self.informViewBoundsChanged()
        self.update()

    def boundingRect(self):
        n = len(self.v)
        if n == 0:
            return QtCore.QRectF()
        if self.overlay is not None:
            return QtCore.QRectF(-1, -1e9, n + 2, 2e9)
        return QtCore.QRectF(-1, 0, n + 2, max(float(np.nanmax(self.v)), 1.0))

    def paint(self, p, *args):
        n = len(self.v)
        i0, i1, g = _visible(self, n)
        if i1 <= i0:
            return
        if g == 1:
            xs, v, up, w = np.arange(i0, i1, dtype=float), self.v[i0:i1], self.up[i0:i1], 0.4
        else:
            edges = np.arange(i0, i1, g)
            v = np.add.reduceat(self.v[i0:i1], edges - i0)
            last = np.minimum(edges + g, n) - 1
            up = self.up[last]
            xs, w = edges + (g - 1) / 2.0, 0.4 * g
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        base, k = 0.0, 1.0
        if self.overlay is not None:                   # scale so the tallest visible bar is ~16% of the pane
            (y0, y1) = self.overlay.viewRange()[1]
            vmax = float(np.nanmax(v)) or 1.0
            base, k = y0, 0.16 * (y1 - y0) / vmax
        for is_up, col in ((True, QtGui.QColor(8, 153, 129, 110)), (False, QtGui.QColor(242, 54, 69, 110))):
            p.setBrush(col)
            m = up == is_up
            for a, b in zip(xs[m], v[m]):
                p.drawRect(QtCore.QRectF(a - w, base, 2 * w, b * k))
