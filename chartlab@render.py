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

    def set_last(self, o, h, l, c) -> bool:
        """Live tick: change only the newest bar. False when the whole series must be rebuilt (Heikin Ashi)."""
        if self.style == "heikin" or not len(self.o):
            return False
        v = np.array([o, h, l, c], float)
        if self.tf is not None:
            v = np.asarray(self.tf(v), float)
        if not self.o.flags.writeable:
            self.o, self.h, self.l, self.c = (np.array(a, float) for a in (self.o, self.h, self.l, self.c))
        grow = v[1] > self.h[-1] or v[2] < self.l[-1]
        self.o[-1], self.h[-1], self.l[-1], self.c[-1] = v
        if grow:
            self.prepareGeometryChange()
            self.informViewBoundsChanged()
        self.update()
        return True

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
            solid = not ((hollow and is_up) or not S["body"])
            fill = pg.mkBrush(body) if solid else QtGui.QBrush(QtCore.Qt.BrushStyle.NoBrush)
            # an outline in the body's own colour adds nothing visible but doubles the drawing work: skip it
            same = solid and str(brd).lower() == str(body).lower()
            p.setPen(pg.mkPen(brd) if (S["border"] and not same) else QtCore.Qt.PenStyle.NoPen)
            p.setBrush(fill)
            QR = QtCore.QRectF
            hgt = cc - oo
            if same:                                       # no outline: a doji still needs a 1 px body line
                vb = self.getViewBox()
                ph = abs(vb.viewPixelSize()[1]) if vb is not None else 0.0
                hgt = np.where(np.abs(hgt) < ph, ph, hgt)
            p.drawRects([QR(a - w, b, 2 * w, d) for a, b, d in zip(x.tolist(), oo.tolist(), hgt.tolist())])


_TV_VOLUME = {"#089981": "#26a69a", "#f23645": "#ef5350"}   # TradingView's default candle -> volume colours


def vol_color(up: bool) -> QtGui.QColor:
    """Volume colour for an up / down bar: TradingView's own volume colours while the candles have the default
    colours, otherwise the candle colour."""
    c = str(settings.get()['up' if up else 'down']).lower()
    return QtGui.QColor(_TV_VOLUME.get(c, c))


VOL_FRAC = 0.25                  # TradingView: the tallest visible volume bar fills the bottom 25% of the pane


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
        """TradingView's volume bars: the volume colour at 50% over the chart background, pre-blended (opaque = fast).
        (Measured on TradingView: #26a69a / #ef5350 at half strength over black.)"""
        S = settings.get()
        base = vol_color(up)
        key = (base.name(), S.get('bg', '#000000'))
        col = VolumeItem._cache.get(key)
        if col is None:
            a, b, al = base, QtGui.QColor(key[1]), 0.5
            col = QtGui.QColor(int(round(a.red() * al + b.red() * (1 - al))),
                               int(round(a.green() * al + b.green() * (1 - al))),
                               int(round(a.blue() * al + b.blue() * (1 - al))))
            VolumeItem._cache[key] = col
        return col

    def set_data(self, df):
        self.v = df["Volume"].to_numpy(float)
        self.up = (df["Close"] >= df["Open"]).to_numpy()
        self.prepareGeometryChange()
        self.informViewBoundsChanged()
        self.update()

    def set_last(self, vol, up):
        if not len(self.v):
            return
        if not self.v.flags.writeable:
            self.v = np.array(self.v, float)
        if not self.up.flags.writeable:
            self.up = np.array(self.up, bool)
        self.v[-1], self.up[-1] = vol, up
        self.update()

    def boundingRect(self):
        n = len(self.v)
        if n == 0:
            return QtCore.QRectF()
        if self.overlay is not None:
            # the strip at the bottom of what is on screen. (Not an 'infinite' rect: Qt turns the item's rect into
            # whole device pixels, and a 2e9-unit rect overflows that on a Retina Mac - the bars were never painted.)
            (x0, x1), (y0, y1) = self.overlay.viewRange()
            span = (y1 - y0) or 1.0
            return QtCore.QRectF(x0 - 2, y0 - span * 0.01, max(x1 - x0, 1.0) + 4, span * (VOL_FRAC + 0.03))
        return QtCore.QRectF(-1, 0, n + 2, max(float(np.nanmax(self.v)), 1.0))

    def viewRangeChanged(self):
        """The ViewBox moved: the bottom strip (= this item's rect) moved with it."""
        if self.overlay is not None:
            self.prepareGeometryChange()
        super().viewRangeChanged()

    def last_value(self):
        """(y in the price pane, volume, up) of the newest bar's volume as drawn - where TradingView puts the volume
        label on the price scale. None when there is nothing to label."""
        n = len(self.v) if self.limit is None else min(len(self.v), self.limit)
        if n == 0 or self.overlay is None:
            return None
        i0, i1, g = _visible(self, n)
        if i1 <= i0:
            i0, i1, g = max(0, n - 1), n, 1
        vis = self.v[i0:i1]
        if g > 1:
            vis = np.add.reduceat(vis, np.arange(i0, i1, g) - i0)
        vmax = float(np.nanmax(vis)) if len(vis) else 0.0
        last = float(self.v[n - 1])
        if not np.isfinite(last) or last <= 0 or vmax <= 0:
            return None
        drawn = last
        if g > 1 and i1 >= n:                          # the newest drawn bar is a group of g bars
            e = i0 + ((n - 1 - i0) // g) * g
            drawn = float(np.nansum(self.v[e:n]))
        (y0, y1) = self.overlay.viewRange()[1]
        return y0 + min(drawn / vmax, 1.0) * VOL_FRAC * (y1 - y0), last, bool(self.up[n - 1])

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
        if self.overlay is not None:                   # the tallest visible bar fills the bottom 25% (measured)
            (y0, y1) = self.overlay.viewRange()[1]
            vmax = float(np.nanmax(v)) or 1.0
            base, k = y0, VOL_FRAC * (y1 - y0) / vmax
        QR = QtCore.QRectF
        for is_up, col in ((True, self._c(True)), (False, self._c(False))):
            p.setBrush(col)
            m = up == is_up
            if m.any():
                p.drawRects([QR(a - w, base, 2 * w, b * k) for a, b in zip(xs[m].tolist(), v[m].tolist())])


class BandFill(pg.GraphicsObject):
    """The shaded area between two lines (Bollinger bands, channels, the Ichimoku cloud). Only the part on screen
    is drawn, so a band over 100,000 bars costs the same as one over 100. With brush_dn the colour follows which
    line is on top (Ichimoku: green cloud / red cloud), split exactly where the lines cross."""

    def __init__(self, upper, lower, brush, offset: int = 0, brush_dn=None):
        super().__init__()
        self.u, self.l = np.asarray(upper, float), np.asarray(lower, float)
        self.brush, self.brush_dn = brush, brush_dn
        self.offset = int(offset)
        self.limit = None
        ok = np.isfinite(self.u) & np.isfinite(self.l)
        self._first = int(np.argmax(ok)) if ok.any() else len(self.u)
        if ok.any():
            lo = float(np.nanmin(np.minimum(self.l, self.u)))
            hi = float(np.nanmax(np.maximum(self.u, self.l)))
            self._rect = QtCore.QRectF(-1 + self.offset, lo, len(self.u) + 2, max(hi - lo, 1e-9))
        else:
            self._rect = QtCore.QRectF()

    def boundingRect(self):
        return self._rect

    def paint(self, p, *args):
        n = len(self.u)
        vb = self.getViewBox()
        if vb is None or n < 2:
            return
        lim = n if self.limit is None else min(n, self.limit)
        x0, x1 = vb.viewRange()[0]
        i0 = max(self._first, int(np.floor(x0)) - 2 - self.offset)
        i1 = min(lim, int(np.ceil(x1)) + 3 - self.offset)
        if i1 - i0 < 2:
            return
        px = max(vb.width(), 50.0)
        g = max(1, int(np.ceil((i1 - i0) * MIN_BAR_PX / px)))
        idx = np.arange(i0, i1, g)
        u, lo = self.u[idx], self.l[idx]
        ok = np.isfinite(u) & np.isfinite(lo)
        if ok.sum() < 2:
            return
        xs = idx[ok].astype(float) + self.offset
        u, lo = u[ok], lo[ok]
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        P = QtCore.QPointF
        if self.brush_dn is None:
            pts = [P(a, b) for a, b in zip(xs.tolist(), u.tolist())]
            pts += [P(a, b) for a, b in zip(xs[::-1].tolist(), lo[::-1].tolist())]
            p.setBrush(self.brush)
            p.drawPolygon(QtGui.QPolygonF(pts))
            return
        up = u >= lo
        start = 0
        m = len(xs)
        for k in range(1, m + 1):
            if k < m and up[k] == up[start]:
                continue
            seg_x, seg_u, seg_l = list(xs[start:k]), list(u[start:k]), list(lo[start:k])
            if start > 0:                                  # begin at the crossing with the previous run
                cx, cy = self._cross(xs, u, lo, start - 1)
                seg_x, seg_u, seg_l = [cx] + seg_x, [cy] + seg_u, [cy] + seg_l
            if k < m:                                      # end at the crossing with the next run
                cx, cy = self._cross(xs, u, lo, k - 1)
                seg_x, seg_u, seg_l = seg_x + [cx], seg_u + [cy], seg_l + [cy]
            if len(seg_x) >= 2:
                pts = [P(a, b) for a, b in zip(seg_x, seg_u)] + [P(a, b) for a, b in zip(seg_x[::-1], seg_l[::-1])]
                p.setBrush(self.brush if up[start] else self.brush_dn)
                p.drawPolygon(QtGui.QPolygonF(pts))
            start = k

    @staticmethod
    def _cross(xs, u, lo, k):
        """Where the two lines cross between point k and k + 1 (straight lines in between)."""
        d0, d1 = u[k] - lo[k], u[k + 1] - lo[k + 1]
        t = d0 / (d0 - d1) if d0 != d1 else 0.5
        return float(xs[k] + t * (xs[k + 1] - xs[k])), float(u[k] + t * (u[k + 1] - u[k]))


class HistItem(pg.GraphicsObject):
    """Columns from zero (MACD histogram). colour index per bar into palette; only visible bars are drawn."""

    def __init__(self, values, color_idx, palette, width: float = 0.32):
        super().__init__()
        self.v = np.asarray(values, float)
        self.ci = np.asarray(color_idx, int)
        self.palette = [QtGui.QColor(c) for c in palette]
        self.w = width
        self.limit = None
        fin = self.v[np.isfinite(self.v)]
        lo, hi = (min(0.0, float(fin.min())), max(0.0, float(fin.max()))) if len(fin) else (0.0, 1.0)
        self._rect = QtCore.QRectF(-1, lo, len(self.v) + 2, max(hi - lo, 1e-9))

    def boundingRect(self):
        return self._rect

    def paint(self, p, *args):
        n = len(self.v)
        i0, i1, g = _visible(self, n)
        if i1 <= i0:
            return
        xs = np.arange(i0, i1, g)
        v, ci = self.v[xs], self.ci[xs]
        ok = np.isfinite(v)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        w = self.w * g
        QR = QtCore.QRectF
        for k, col in enumerate(self.palette):
            m = ok & (ci == k)
            if m.any():
                p.setBrush(col)
                p.drawRects([QR(a - w, 0.0, 2 * w, b) for a, b in zip(xs[m].astype(float).tolist(), v[m].tolist())])


class DotsItem(pg.GraphicsObject):
    """Small round markers at (bar, value) - Parabolic SAR. Size in screen pixels, only visible bars."""

    def __init__(self, values, color, size_px: float = 3.0):
        super().__init__()
        self.v = np.asarray(values, float)
        self.color = QtGui.QColor(color)
        self.size = size_px
        self.limit = None
        fin = self.v[np.isfinite(self.v)]
        lo, hi = (float(fin.min()), float(fin.max())) if len(fin) else (0.0, 1.0)
        self._rect = QtCore.QRectF(-1, lo, len(self.v) + 2, max(hi - lo, 1e-9))

    def boundingRect(self):
        return self._rect

    def paint(self, p, *args):
        n = len(self.v)
        i0, i1, g = _visible(self, n)
        vb = self.getViewBox()
        if i1 <= i0 or vb is None:
            return
        xs = np.arange(i0, i1, g)
        v = self.v[xs]
        ok = np.isfinite(v)
        if not ok.any():
            return
        pw, ph = vb.viewPixelSize()
        rx, ry = self.size / 2 * pw, self.size / 2 * ph
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(self.color)
        for a, b in zip(xs[ok].astype(float).tolist(), v[ok].tolist()):
            p.drawEllipse(QtCore.QPointF(a, b), rx, ry)


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
