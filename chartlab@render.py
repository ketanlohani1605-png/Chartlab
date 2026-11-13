"""Viewport-aware renderers: only the visible bars are painted, and when bars are narrower than ~3 px they are
merged (OHLC aggregation), so a chart with 1,000,000 bars pans and zooms as smoothly as one with 100."""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from . import settings, theme

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
    def __init__(self, df=None, style="candles", tf=None):
        super().__init__()
        self.style = style
        self.tf = tf
        self.limit = None
        self.o = self.h = self.l = self.c = np.zeros(0)
        if df is not None:
            self.set_data(df, style)

    def set_data(self, df, style="candles", tf=None):
        self.style = style
        if tf is not None:
            self.tf = tf
        self.o, self.h, self.l, self.c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
        if style == "heikin":
            c = (self.o + self.h + self.l + self.c) / 4.0
            o = np.empty_like(c)
            o[0] = (self.o[0] + self.c[0]) / 2.0
            for i in range(1, len(c)):
                o[i] = (o[i - 1] + c[i - 1]) / 2.0
            self.h, self.l = np.maximum.reduce([self.h, o, c]), np.minimum.reduce([self.l, o, c])
            self.o, self.c = o, c
        if self.tf is not None:
            self.o, self.h, self.l, self.c = (np.asarray(self.tf(a), float) for a in (self.o, self.h, self.l, self.c))
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
        S = settings.get()
        if S["prev_close"]:
            if g == 1:
                pc = np.concatenate(([self.c[i0 - 1] if i0 > 0 else o[0]], self.c[i0:i1 - 1]))
            else:
                pc = self.c[np.maximum(edges - 1, 0)]
            up = c >= pc
        else:
            up = c >= o
        hollow = self.style == "hollow"
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        for is_up, body, brd, wick in ((True, S["up"], S["border_up"], S["wick_up"]),
                                       (False, S["down"], S["border_down"], S["wick_down"])):
            m = up == is_up
            if not m.any():
                continue
            x, hh, ll, oo, cc = xs[m], h[m], l[m], o[m], c[m]
            if self.style == "bars":
                p.setPen(pg.mkPen(body))
                p.drawLines([QtCore.QLineF(a, lo, a, hi) for a, lo, hi in zip(x, ll, hh)])
                p.drawLines([QtCore.QLineF(a - w, b, a, b) for a, b in zip(x, oo)])
                p.drawLines([QtCore.QLineF(a, d, a + w, d) for a, d in zip(x, cc)])
                continue
            if S["wick"]:
                p.setPen(pg.mkPen(wick))
                QL = QtCore.QLineF
                p.drawLines([QL(a, b, a, d) for a, b, d in zip(x.tolist(), ll.tolist(), hh.tolist())])
            p.setPen(pg.mkPen(brd) if S["border"] else QtCore.Qt.PenStyle.NoPen)
            fill = QtGui.QBrush(QtCore.Qt.BrushStyle.NoBrush) if ((hollow and is_up) or not S["body"]) else pg.mkBrush(body)
            p.setBrush(fill)
            QR = QtCore.QRectF
            p.drawRects([QR(a - w, b, 2 * w, d - b) for a, b, d in zip(x.tolist(), oo.tolist(), cc.tolist())])


class VolumeItem(pg.GraphicsObject):
    def __init__(self, df=None):
        super().__init__()
        self.limit = None
        self.overlay = None      # ViewBox of the price pane: draw as a thin strip at its bottom (TradingView style)
        self.v = np.zeros(0)
        self.up = np.zeros(0, bool)
        if df is not None:
            self.set_data(df)

    _cache: dict = {}

    @staticmethod
    def _c(up):
        """Volume colour = candle colour at ~43% over the chart background, pre-blended (opaque = fast)."""
        S = settings.get()
        key = (S['up' if up else 'down'], S.get('bg', '#000000'))
        col = VolumeItem._cache.get(key)
        if col is None:
            a, b, al = QtGui.QColor(key[0]), QtGui.QColor(key[1]), 110 / 255
            col = QtGui.QColor(int(a.red() * al + b.red() * (1 - al)), int(a.green() * al + b.green() * (1 - al)),
                               int(a.blue() * al + b.blue() * (1 - al)))
            VolumeItem._cache[key] = col
        return col

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
        QR = QtCore.QRectF
        for is_up, col in ((True, self._c(True)), (False, self._c(False))):
            p.setBrush(col)
            m = up == is_up
            if m.any():
                p.drawRects([QR(a - w, base, 2 * w, b * k) for a, b in zip(xs[m].tolist(), v[m].tolist())])


class TradeMarkers(pg.GraphicsObject):
    """Strategy fills drawn TradingView-style in screen pixels: an arrow under the low (buy / cover) or above the
    high (sell / short) with a small label ('Long', 'Short', 'Close'). One item for all trades = cheap."""
    ARROW = [(0, 0), (6, 6), (2.4, 6), (2.4, 13), (-2.4, 13), (-2.4, 6), (-6, 6)]

    def __init__(self):
        super().__init__()
        self.x = np.zeros(0)
        self.y = np.zeros(0)
        self.up = np.zeros(0, bool)
        self.colors: list = []
        self.labels: list = []
        self.font = QtGui.QFont()
        self.font.setPixelSize(11)
        self.font.setWeight(QtGui.QFont.Weight.DemiBold)
        self._vb = None

    def set_points(self, x, y, up, colors, labels):
        self.x, self.y, self.up = np.asarray(x, float), np.asarray(y, float), np.asarray(up, bool)
        self.colors, self.labels = list(colors), list(labels)
        self.prepareGeometryChange()
        self.update()

    def itemChange(self, change, value):
        if change == QtWidgets.QGraphicsItem.GraphicsItemChange.ItemParentHasChanged:
            vb = self.getViewBox()
            if isinstance(vb, pg.ViewBox) and vb is not self._vb:
                self._vb = vb
                vb.sigRangeChanged.connect(lambda *_: self.prepareGeometryChange())
        return super().itemChange(change, value)

    def boundingRect(self):
        vb = self.getViewBox()
        return vb.viewRect() if isinstance(vb, pg.ViewBox) else QtCore.QRectF()

    def paint(self, p, *args):
        if not len(self.x):
            return
        vb = self.getViewBox()
        if not isinstance(vb, pg.ViewBox):
            return
        (x0, x1), _ = vb.viewRange()
        vis = np.nonzero((self.x >= x0 - 1) & (self.x <= x1 + 1))[0]
        if not len(vis):
            return
        tr = p.transform()
        show_text = len(vis) <= 120
        p.save()
        p.resetTransform()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        p.setFont(self.font)
        fm = QtGui.QFontMetrics(self.font)
        for k in vis.tolist():
            pt = tr.map(QtCore.QPointF(self.x[k], self.y[k]))
            s = 1 if self.up[k] else -1                  # up arrow sits below the point, down arrow above
            base = QtCore.QPointF(pt.x(), pt.y() + s * 4)
            poly = QtGui.QPolygonF([QtCore.QPointF(base.x() + ax, base.y() + s * ay) for ax, ay in self.ARROW])
            col = QtGui.QColor(self.colors[k])
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawPolygon(poly)
            if show_text and self.labels[k]:
                t = self.labels[k]
                w = fm.horizontalAdvance(t)
                ty = base.y() + s * 15 + (fm.ascent() if s > 0 else -fm.descent())
                p.setPen(col)
                p.drawText(QtCore.QPointF(pt.x() - w / 2, ty), t)
        p.restore()
