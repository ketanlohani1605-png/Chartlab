"""TradingView-style chart: candles / hollow / line / area, volume, RSI pane, overlays,
crosshair with price+date tags, last-price tag, drawing tools and trade markers."""
from __future__ import annotations

import math
import re

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from . import icons
from . import indicators as ta
from . import settings, theme
from . import studies as STD
from .drawings import DrawingLayer
from .gestures import TVViewBox

# pyqtgraph drops mouse-move events that arrive faster than 100/s; Mac trackpads / ProMotion screens send
# ~120/s, so crosshair and drags would skip. Qt already compresses moves when busy, so take every one.
pg.setConfigOption("mouseRateLimit", 0)
from .render import BandFill, CandleItem, DotsItem, HistItem, VolumeItem, vol_color

_QWIDGETSIZE_MAX = 16777215
STYLES = ["candles", "hollow", "bars", "heikin", "line", "linemark", "step", "area", "baseline"]
INDICATOR_KEYS = list(STD.KEYS)


# Measured pixel by pixel on TradingView (dark theme, Retina): the crosshair is #9c9c9c, 1 point wide, dashed 5 on /
# 6 off; its labels are #303030 with white 12 px text; the '+' box turns #636363 under the mouse; scale text #b8b8b8.
XHAIR_COLOR = "#9c9c9c"
XHAIR_FILL = "#303030"
XHAIR_HOT = "#636363"
AXIS_TEXT = "#b8b8b8"
LEGEND_TEXT = "#dbdbdb"          # legend titles and the O H L C letters
PANE_LINE = "#2e2e2e"            # the line between the price chart and an indicator pane
PRICE_TAG_H = 17                 # last-price / alert labels on the price scale
XHAIR_TAG_H = 21                 # the crosshair's price label (and the '+' box next to it)
TIME_TAG_H = 24                  # the crosshair's time label, flush with the top of the time scale
TIME_TEXT_TOP = 7                # time-scale text sits this far below the top of the scale (labels line up with it)
TIME_LABEL_PX = 51.2             # time-scale labels: centres at least this far apart (TradingView: 50.8 no, 51.6 yes)
PRICE_TEXT_LEFT = 10             # price-scale text starts this far right of the scale's edge
_PEN_STYLES = {"solid": QtCore.Qt.PenStyle.SolidLine, "dash": QtCore.Qt.PenStyle.DashLine,
               "dot": QtCore.Qt.PenStyle.DotLine}


def _plus_pixmap(hot: bool, size: int = XHAIR_TAG_H, dpr: float = 2.0) -> QtGui.QPixmap:
    """TradingView's '+' left of the price scale: a #303030 box against the scale (rounded on its left side only),
    a white 15 px ring with an 8 px plus in it - all lines 1 point. Under the mouse the box turns #636363."""
    dpr = max(2.0, float(dpr))
    pm = QtGui.QPixmap(int(round(size * dpr)), int(round(size * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    box = QtGui.QColor(XHAIR_HOT if hot else XHAIR_FILL)
    path = QtGui.QPainterPath()
    path.addRoundedRect(QtCore.QRectF(0, 0, size, size), 2, 2)
    p.fillPath(path, box)
    p.fillRect(QtCore.QRectF(size / 2, 0, size / 2, size), box)      # square on the side that touches the scale
    pen = QtGui.QPen(QtGui.QColor("#ffffff"), 1.0)
    pen.setCapStyle(QtCore.Qt.PenCapStyle.FlatCap)
    p.setPen(pen)
    p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    c = size / 2
    p.drawEllipse(QtCore.QPointF(c, c), 7.0, 7.0)
    p.drawLine(QtCore.QPointF(c - 4, c), QtCore.QPointF(c + 4, c))
    p.drawLine(QtCore.QPointF(c, c - 4), QtCore.QPointF(c, c + 4))
    p.end()
    return pm


def _project_times(idx: pd.DatetimeIndex, count: int) -> pd.DatetimeIndex:
    """The times of the next `count` bars after the last one, following the market's own hours: an NSE 1-minute
    chart goes 15:28, 15:29, then the next weekday 09:15 (TradingView keeps the time scale going like this)."""
    if len(idx) < 3 or count <= 0:
        return pd.DatetimeIndex([])
    d = np.diff(idx.values[-300:]).astype("timedelta64[s]").astype(np.int64)
    d = d[d > 0]
    if not len(d):
        return pd.DatetimeIndex([])
    step = int(np.median(d))
    last = idx[-1]
    tail = idx[-3000:]
    weekends = bool((tail.dayofweek >= 5).any())
    out = []
    if step >= 27 * 86400:                                  # monthly
        out = [last + pd.DateOffset(months=k) for k in range(1, count + 1)]
    elif step >= 6 * 86400:                                 # weekly
        out = [last + pd.Timedelta(days=7 * k) for k in range(1, count + 1)]
    elif step >= int(86400 * 0.9):                          # daily
        t = last
        while len(out) < count:
            t = t + pd.Timedelta(days=1)
            if weekends or t.dayofweek < 5:
                out.append(t)
    else:                                                   # intraday: inside the usual session hours
        tod = np.asarray((tail - tail.normalize()).total_seconds(), dtype=np.int64)
        start_s, end_s = int(tod.min()), int(tod.max())
        t = last
        one = pd.Timedelta(seconds=step)
        while len(out) < count:
            t2 = t + one
            if t2.normalize() != t.normalize() or (t2 - t2.normalize()).total_seconds() > end_s:
                nd = t.normalize() + pd.Timedelta(days=1)
                while not weekends and nd.dayofweek >= 5:
                    nd = nd + pd.Timedelta(days=1)
                t2 = nd + pd.Timedelta(seconds=start_s)
            out.append(t2)
            t = t2
    return pd.DatetimeIndex(out)


class _DotGridMixin:
    """pyqtgraph draws grid lines with the axis pen + alpha. Translucent / dotted lines are very slow to paint
    on Retina screens, so the grid is drawn with an opaque, pre-blended colour (same look, ~10x cheaper)."""
    grid_bg = "#000000"

    def generateDrawSpecs(self, p):
        specs = super().generateDrawSpecs(p)
        try:
            axisSpec, tickSpecs, textSpecs = specs
            out = []
            bg = QtGui.QColor(self.grid_bg)
            for pen, a, b in tickSpecs:
                col = pen.color()
                al = col.alphaF()
                if 0 < al < 1:
                    mixed = QtGui.QColor(int(col.red() * al + bg.red() * (1 - al)),
                                         int(col.green() * al + bg.green() * (1 - al)),
                                         int(col.blue() * al + bg.blue() * (1 - al)))
                    pen = QtGui.QPen(mixed, 0)
                    pen.setCosmetic(True)
                out.append((pen, a, b))
            return axisSpec, out, textSpecs
        except Exception:  # noqa: BLE001
            return specs


class _BoldLabelsMixin:
    """TradingView draws some scale labels bold (measured on its canvas: same colour, ~1.5x thicker strokes):
    every other price label (the even multiples of the step: 25,400 / 25,440 / 25,480 bold, 25,420 / 25,460 not),
    and on the time scale the most important boundaries in view (the day '17' among hours, 'Aug' among days).
    tickStrings() fills self._bold; the strings in it are painted with the bold font."""
    _bold: frozenset = frozenset()
    sel_band = None        # (a, b) in view units: the light blue band between a selected drawing's anchors (TradingView)

    def _paint_sel_band(self, p):
        """TradingView marks the span of the selected drawing on the scales: #2962FF at 25 % behind the numbers."""
        vb = self.linkedView()
        if vb is None:
            return
        a, b = self.sel_band
        if self.orientation in ("left", "right"):
            ya = self.mapFromScene(vb.mapViewToScene(QtCore.QPointF(0.0, a))).y()
            yb = self.mapFromScene(vb.mapViewToScene(QtCore.QPointF(0.0, b))).y()
            r = QtCore.QRectF(0.0, min(ya, yb), float(self.width()), abs(yb - ya))
        else:
            xa = self.mapFromScene(vb.mapViewToScene(QtCore.QPointF(a, 0.0))).x()
            xb = self.mapFromScene(vb.mapViewToScene(QtCore.QPointF(b, 0.0))).x()
            r = QtCore.QRectF(min(xa, xb), 0.0, abs(xb - xa), float(self.height()))
        r = r.intersected(QtCore.QRectF(0.0, 0.0, float(self.width()), float(self.height())))
        if r.width() > 0 and r.height() > 0:
            p.fillRect(r, QtGui.QColor(41, 98, 255, 64))

    def drawPicture(self, p, axisSpec, tickSpecs, textSpecs):
        if self.sel_band is not None:
            self._paint_sel_band(p)
        bold = self._bold
        heavy = [t for t in textSpecs if t[2] in bold] if bold else []
        if not heavy:
            return super().drawPicture(p, axisSpec, tickSpecs, textSpecs)
        super().drawPicture(p, axisSpec, tickSpecs, [t for t in textSpecs if t[2] not in bold])
        f = QtGui.QFont(self.style["tickFont"]) if self.style.get("tickFont") is not None else QtGui.QFont(p.font())
        f.setWeight(QtGui.QFont.Weight.Bold)
        p.setFont(f)
        p.setPen(self.textPen())
        for rect, flags, text in heavy:
            p.drawText(rect, int(flags), text)


def _signed(x: float, decimals: int = 2) -> str:
    """'+0.61' / '−0.33': TradingView writes changes with a real minus sign (U+2212)."""
    return f"{x:+,.{decimals}f}".replace("-", "−")


def price_decimals(df, default: int = 2) -> int:
    """How many decimals the symbol's prices have - TradingView shows a symbol in its own tick size (RELIANCE moves in
    0.10 steps on NSE, so its scale reads 1,358.5 / 1,380.0; an index has two decimals). Read from the newest bars:
    the fewest decimals that (nearly) all of them fit."""
    try:
        tail = df[["Open", "High", "Low", "Close"]].tail(20).to_numpy(float).ravel()
    except Exception:  # noqa: BLE001
        return default
    tail = tail[np.isfinite(tail) & (tail != 0)]
    if len(tail) < 8:
        return default
    big = float(np.nanmedian(np.abs(tail)))
    top = 2 if big >= 1 else (4 if big >= 0.01 else 8)
    for d in range(0, top + 1):
        x = tail * (10 ** d)
        if np.mean(np.abs(x - np.round(x)) < 0.05) >= 0.9:   # data vendors store prices as floats: allow the noise
            return d
    return min(max(default, 0), top) if top <= 2 else top


def tv_price_step(raw: float, decimals: int | None = None) -> float:
    """TradingView's price-scale step: the smallest 'nice' step that keeps labels >= 30 px apart. From 1 up the
    steps are 1, 2, 2.5, 4, 5 x 10^n (measured on its scale: 0.1 0.2 0.5 1 2 2.5 10 20 40 100 200); below 1 only
    1, 2, 5 x 10^n, and never finer than the symbol's own tick."""
    if not np.isfinite(raw) or raw <= 0:
        return 1.0
    mag = 10.0 ** np.floor(np.log10(raw))
    mults = (1.0, 2.0, 2.5, 4.0, 5.0, 10.0) if mag >= 1 else (1.0, 2.0, 5.0, 10.0)
    step = mag * 10.0
    for m in mults:
        s = m * mag
        if decimals == 0 and abs(s - round(s)) > 1e-9:
            continue
        if s >= raw * (1 - 1e-9):
            step = s
            break
    if decimals is not None:
        step = max(step, 10.0 ** -decimals)
    return float(step)


def tv_bold_step(step: float) -> float:
    """Which price labels TradingView draws bold (measured): with a 1 / 2 / 4 x 10^n step every 5th one (1,300 and
    1,200 when the step is 20), with a 2.5 / 5 x 10^n step every 2nd one (1,385 / 1,380 / 1,375 at step 2.5)."""
    if step <= 0:
        return 0.0
    mag = 10.0 ** np.floor(np.log10(step) + 1e-9)
    m = step / mag
    return step * (2.0 if min(abs(m - 2.5), abs(m - 5.0)) < 1e-6 else 5.0)


class PriceAxis(_BoldLabelsMixin, _DotGridMixin, pg.AxisItem):
    chart = None
    is_price = True
    _blocked: tuple = ()          # (top, bottom) strips of this axis covered by a series label (last price, volume)

    def tickStrings(self, values, scale, spacing):
        out = self._tick_strings(values, scale, spacing)
        ch = self.chart
        b = tv_bold_step(float(spacing)) if spacing else 0.0
        if b <= 0 or (ch is not None and self.is_price and ch.scale_mode != "regular"):
            self._bold = frozenset()
        else:
            self._bold = frozenset(s for v, s in zip(values, out)
                                   if s and abs(float(v) / b - round(float(v) / b)) < 1e-6)
        return out

    def drawPicture(self, p, axisSpec, tickSpecs, textSpecs):
        """TradingView never lets a scale number peek out from under the last-price / volume label: a number that
        touches one of those labels is not drawn at all."""
        if self._blocked:
            keep = []
            for spec in textSpecs:
                r = spec[0]
                top, bot = r.top() + 2.0, r.bottom() - 2.0          # the glyphs, not the line's leading
                if not any(bot > a and top < b for a, b in self._blocked):
                    keep.append(spec)
            textSpecs = keep
        super().drawPicture(p, axisSpec, tickSpecs, textSpecs)

    def _tick_strings(self, values, scale, spacing):
        ch = self.chart
        if ch is not None and ch.df is not None:
            if not self.is_price:                          # an indicator pane: its own number format
                pn = next((pn for pn in ch.panes if pn.plot.getAxis("right") is self), None)
                st = STD.STUDIES.get(pn.keys[0]) if pn is not None and pn.keys else None
                return [ch._fmt_study(st, v) if st is not None else f"{v:,.2f}" for v in values]
            if ch.scale_mode == "percent":
                return [ch.axis_label(v) for v in values]
            return [ch.axis_price(float(ch.iy(v))) for v in values]
        return super().tickStrings(values, scale, spacing)

    """Right axis with TradingView's tick density (a label every 30+ px, measured ~33 px on its chart)."""

    def tickValues(self, minVal, maxVal, size):
        levels = super().tickValues(minVal, maxVal, size)
        if maxVal <= minVal or size <= 0:
            return levels
        edge = 9.0 * (maxVal - minVal) / size                # 9 px from the top / bottom of the pane
        return [(sp, [v for v in vals if minVal + edge <= v <= maxVal - edge]) for sp, vals in levels]

    def tickSpacing(self, minVal, maxVal, size):
        span = maxVal - minVal
        if span <= 0 or size <= 0:
            return super().tickSpacing(minVal, maxVal, size)
        raw = span / max(size / 30.0, 2)        # TradingView: labels at least 2.5 x the font size (30 px) apart
        ch = self.chart
        dec = None
        if ch is not None and self.is_price and ch.df is not None and ch.scale_mode == "regular":
            dec = ch.decimals(max(abs(minVal), abs(maxVal)))
        return [(tv_price_step(raw, dec), 0)]


class DateAxis(_BoldLabelsMixin, _DotGridMixin, pg.AxisItem):
    """X axis that plots bar numbers (no weekend gaps, like TradingView) but shows dates."""

    def __init__(self, orientation="bottom", **kw):
        super().__init__(orientation, **kw)
        self._index = None
        self._future = None
        self.intraday = False
        self.h24 = True
        self.dow = True
        self.datefmt = "%d %b '%y"

    # natural weight of a bar as a label place (the most important ones in view are drawn bold)
    W_YEAR, W_MONTH, W_WEEK, W_DAY = 13, 12, 11, 10
    TIME_STRIDES = (1, 5, 15, 30, 60, 180, 360, 720)         # minutes: weights 1 .. 8

    def set_index(self, index: pd.DatetimeIndex, future: pd.DatetimeIndex | None = None):
        self.intraday = bool(len(index)) and bool(((index.hour != 0) | (index.minute != 0)).any())
        real = index
        self._future = future if future is not None and len(future) else None
        if self._future is not None:                        # label the empty space right of the newest bar too
            try:
                index = index.append(self._future)
            except (TypeError, ValueError):                 # (time zones that do not mix): no future labels
                self._future = None
        self._index = index
        n = len(index)
        T = {"n": n}
        w = np.zeros(n, dtype=np.int8)
        if n:
            y, mo, d = index.year.to_numpy(), index.month.to_numpy(), index.day.to_numpy()
            chg = lambda a: np.r_[True, a[1:] != a[:-1]]  # noqa: E731
            newyear = chg(y)
            newmonth = newyear | chg(mo)
            newday = newmonth | chg(d)
            # bar interval in minutes (from the real bars, not the projected ones)
            dd = np.diff(real.values[-300:]).astype("timedelta64[s]").astype(np.int64) if len(real) > 2 else np.array([])
            dd = dd[dd > 0]
            T["bar_min"] = max(1, int(round(float(np.median(dd)) / 60))) if len(dd) else 1440
            if self.intraday:
                mins = index.hour.to_numpy().astype(np.int64) * 60 + index.minute.to_numpy()
                prev = np.r_[-1, mins[:-1]]
                for k, s in enumerate(self.TIME_STRIDES, start=1):   # the biggest clock boundary crossed
                    w[(mins // s) != (prev // s)] = k
                T["mins"] = mins
                # a new week (TradingView bolds the first day of a week among days); a Sunday session opens the week
                iso = (index.normalize() + pd.Timedelta(days=1)).isocalendar()
                wk = iso["year"].to_numpy().astype(np.int64) * 100 + iso["week"].to_numpy().astype(np.int64)
                newweek = newday & chg(wk)
                w[newday] = self.W_DAY
                w[newweek] = self.W_WEEK
                T["newweek"] = newweek
            else:
                w[:] = self.W_DAY                                # every daily / weekly bar is a day label place
            w[newmonth] = self.W_MONTH
            w[newyear] = self.W_YEAR
            if n > 1:                                            # the very first bar is not a real boundary
                newyear[0] = newmonth[0] = newday[0] = False
                if "newweek" in T:
                    T["newweek"][0] = False
            starts = np.flatnonzero(np.r_[True, (mo[1:] != mo[:-1]) | (y[1:] != y[:-1])])
            T["pos"] = np.arange(n) - np.repeat(starts, np.diff(np.r_[starts, n]))   # bar number inside its month
            T.update(newyear=newyear, newmonth=newmonth, newday=newday, year=y, month=mo,
                     day_id=np.cumsum(chg(d) | chg(mo) | chg(y)), cross={})
        self._weight = w
        self._ti = T

    def share_index(self, other):
        self._index, self.intraday = other._index, other.intraday
        self._weight, self._ti = other._weight, getattr(other, "_ti", {"n": 0})
        self._future = getattr(other, "_future", None)

    def tickValues(self, minVal, maxVal, size):
        """TradingView's time labels (measured on its scale at many zoom levels):
        - label centres at least ~51 px apart (50.8 px: no, 51.6 px: yes);
        - years, then months: every month, every 2nd (Jan, Mar, May ...) or only Jan / Jul - the finest step at which
          3 in 4 gaps fit (months that still do not fit are skipped);
        - daily / weekly bars: a date every 1, 2, 3 or 4 bars counted from the month's first bar, or no dates at all
          (at 12.6 px a bar: only months; at 13.5 px: every 4th bar);
        - intraday: each day's first bar (left to right, skipping days too close), then the clock: every 1, 5, 15,
          30 min, 1 h (and 3 / 6 / 12 h for bars of 15 min and up) - the finest step that fits; a time too close to a
          day label is dropped."""
        idx, T = self._index, getattr(self, "_ti", None)
        if idx is None or not len(idx) or maxVal <= minVal or size <= 0 or not T or not T.get("n"):
            return super().tickValues(minVal, maxVal, size)
        n = T["n"]
        spx = size / (maxVal - minVal)                       # pixels per bar
        k = TIME_LABEL_PX / spx                              # bars two labels must be apart
        pad = int(np.ceil(k)) + 2
        lo, hi = max(int(np.ceil(minVal)) - pad, 0), min(int(np.floor(maxVal)) + pad, n - 1)
        if hi < lo:
            return []
        span = hi - lo
        # everything is worked out over a wider stretch than the screen, so the labels stay put while panning
        elo, ehi = max(lo - max(span, 300), 0), min(hi + max(span, 300), n - 1)
        chosen: list[int] = []
        import bisect

        def free(i):
            j = bisect.bisect_left(chosen, i)
            return (j == 0 or i - chosen[j - 1] >= k - 1e-9) and (j == len(chosen) or chosen[j] - i >= k - 1e-9)

        def place(cands):
            for i in cands:
                i = int(i)
                if free(i):
                    bisect.insort(chosen, i)

        def fits(seq) -> float:
            seq = np.unique(np.asarray(seq, dtype=np.int64))
            if len(seq) < 2:
                return 1.0
            return float(np.mean(np.diff(seq) >= k - 1e-9))

        def marks(mask, a, b):
            return np.flatnonzero(mask[a:b + 1]) + a

        # years: every year, every 2nd, 5th ... (the finest step at which 3 in 4 gaps fit)
        ys_e = marks(T["newyear"], elo, ehi)
        if len(ys_e):
            yv = T["year"][ys_e]
            for s in (1, 2, 5, 10, 25, 50, 100):
                c = ys_e[yv % s == 0]
                if fits(c) >= 0.75 or s == 100:
                    place(c)
                    break
        # months (January is the year's label): every month, every 2nd, or Jan / Jul only
        ms_e = marks(T["newmonth"] & ~T["newyear"], elo, ehi)
        if len(ms_e):
            mv = T["month"][ms_e]
            for s in (1, 2, 6):
                c = ms_e[(mv - 1) % s == 0]
                if fits(np.r_[c, ys_e]) >= 0.75:
                    place(c)
                    break
        if not self.intraday:
            # dates: every 1, 2, 3 or 4 bars from the month's first bar - or none at all
            stride = next((s for s in (1, 2, 3, 4) if s * spx >= TIME_LABEL_PX - 1e-9), None)
            if stride is not None:
                pos = T["pos"][elo:ehi + 1]
                place(np.flatnonzero((pos > 0) & (pos % stride == 0)) + elo)
        else:
            place(marks(T["newday"] & ~T["newmonth"], elo, ehi))      # each day, left to right (a new week is
            #                                                          only drawn bold, it is not placed first)
            bm = int(T.get("bar_min", 1))
            cap = 60 if bm < 15 else 720
            for s in self.TIME_STRIDES:
                if s < bm or s % bm or s > cap:
                    continue
                cross = T["cross"].get(s)
                if cross is None:                                        # bars that start a new 5 / 15 / 60 min ...
                    mins = T["mins"]
                    cross = T["cross"][s] = ((mins // s) != (np.r_[-1, mins[:-1]] // s)) & ~T["newday"]
                c = marks(cross, elo, ehi)
                if not len(c):
                    continue
                day = T["day_id"][c]
                gaps = np.diff(c)[day[1:] == day[:-1]]                   # within a day
                if not len(gaps) or np.mean(gaps >= k - 1e-9) >= 0.75:
                    place(c)
                    break
        lo_v, hi_v = minVal - 0.5, maxVal + 0.5
        return [(max(k, 1.0), [float(v) for v in chosen if lo_v <= v <= hi_v])]

    def tickStrings(self, values, scale, spacing):
        idx, out, ks = self._index, [], []
        w = getattr(self, "_weight", None)
        for v in values:
            i = int(round(v))
            if idx is None or i < 0 or i >= len(idx):
                out.append("")
                ks.append(-1)
                continue
            t = idx[i]
            k = int(w[i]) if w is not None and i < len(w) else 0
            ks.append(k)
            if k >= self.W_YEAR:
                out.append(str(t.year))
            elif k == self.W_MONTH:
                out.append(t.strftime("%b"))
            elif self.intraday and k < self.W_DAY:
                out.append(t.strftime("%H:%M" if self.h24 else "%I:%M %p"))
            else:
                out.append(str(t.day))
        # TradingView: the most important boundaries in view are bold - a month among days, a year among months, a
        # day among hours, the first day of a week among days, every round hour among quarter hours (all hours alike)
        cls = [min(k, 5) if 0 <= k < self.W_DAY else k for k in ks]
        top = max((c for c, s in zip(cls, out) if s), default=-1)
        self._bold = frozenset(s for c, s in zip(cls, out) if s and c >= top) if top >= 0 else frozenset()
        return out


class CornerOverlay(QtWidgets.QWidget):
    """Paints rounded panel corners (gutter colour outside a rounded rect) like TradingView's panes."""
    R = 10

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground)

    def resizeEvent(self, ev):
        # only the four corner squares exist for the window system: the overlay never repaints the chart area
        r, w, h = self.R, self.width(), self.height()
        reg = QtGui.QRegion()
        for x, y in ((0, 0), (w - r, 0), (0, h - r), (w - r, h - r)):
            reg = reg.united(QtGui.QRegion(x, y, r, r))
        self.setMask(reg)
        super().resizeEvent(ev)

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        path = QtGui.QPainterPath()
        path.addRect(QtCore.QRectF(self.rect()))
        rr = QtGui.QPainterPath()
        rr.addRoundedRect(QtCore.QRectF(self.rect()), self.R, self.R)
        p.fillPath(path.subtracted(rr), QtGui.QColor(theme.GUTTER))


class OverlayLabel(QtWidgets.QLabel):
    """Transparent text drawn on top of the chart (legend / pane titles) - never steals the mouse."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent; font-size: 13px;")

    def setText(self, t):
        if t == self.text():                      # rich-text re-layout is the costly part of a hover update
            return
        super().setText(t)
        self.adjustSize()


class ReplayBar(QtWidgets.QFrame):
    """Floating replay toolbar at the bottom-centre of the chart (TradingView): select bar / play / step / speed /
    jump to real-time / exit."""

    SPEEDS = [("10x", 10.0), ("5x", 5.0), ("3x", 3.0), ("1x", 1.0), ("0.5x", 0.5), ("0.2x", 0.2)]

    def __init__(self, chart):
        super().__init__(chart.glw)
        self.chart = chart
        self.setObjectName("replaybar")
        self.setStyleSheet("QFrame#replaybar { background: #1e1e1e; border: 1px solid #2e2e2e; border-radius: 10px; }"
                           " QToolButton { padding: 6px 10px; border-radius: 6px; color: #dbdbdb; font-size: 13px; }"
                           " QToolButton:hover { background: #2e2e2e; } QLabel { background: transparent; color: #dbdbdb; }"
                           " QComboBox { background: transparent; border: 0; color: #dbdbdb; padding: 4px 6px; }"
                           " QComboBox::drop-down { width: 0; } QComboBox::down-arrow { image: none; width: 0; }"
                           " QComboBox:hover { background: #2e2e2e; border-radius: 6px; }"
                           " QFrame#rsep { background: #2e2e2e; }")
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(8, 4, 6, 4)
        h.setSpacing(2)
        self.info = QtWidgets.QLabel("")
        self.btn_select = QtWidgets.QToolButton()
        self.btn_select.setText("Select bar")
        self.btn_select.setIcon(icons.get("replay_icon"))
        self.btn_select.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_select.setToolTip("Pick a new starting bar")
        self.btn_select.clicked.connect(chart.replay_begin)
        self.btn_live = QtWidgets.QToolButton()
        self.btn_live.setIcon(icons.get("latest"))
        self.btn_live.setToolTip("Jump to real-time (exit replay)")
        self.btn_live.clicked.connect(chart.replay_exit)
        self.btn_play = QtWidgets.QToolButton()
        self.btn_play.clicked.connect(self._toggle)
        self.btn_step = QtWidgets.QToolButton()
        self.btn_step.setIcon(icons.get("step"))
        self.btn_step.setToolTip("Step forward one bar  (Right arrow)")
        self.btn_step.clicked.connect(lambda: chart.replay_step(1))
        self.speed = QtWidgets.QComboBox()
        for label, v in self.SPEEDS:
            self.speed.addItem(label, v)
        self.speed.setCurrentIndex(3)
        self.speed.setToolTip("Bars per second")
        self.speed.activated.connect(lambda _i: chart.replay_set_speed(self.speed.currentData()))
        self.btn_close = QtWidgets.QToolButton()
        self.btn_close.setIcon(icons.get("close"))
        self.btn_close.setToolTip("Exit replay  (Esc)")
        self.btn_close.clicked.connect(chart.replay_exit)
        def sep():
            s = QtWidgets.QFrame()
            s.setObjectName("rsep")
            s.setFixedSize(1, 20)
            return s
        for w in (self.info, self.btn_select, sep(), self.btn_play, self.btn_step, self.speed, sep(),
                  self.btn_live, self.btn_close):
            h.addWidget(w)
        shadow = QtWidgets.QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 4)
        shadow.setColor(QtGui.QColor(0, 0, 0, 200))
        self.setGraphicsEffect(shadow)
        self.hide()

    def _toggle(self):
        if self.chart.replay["playing"]:
            self.chart.replay_pause()
        else:
            self.chart.replay_play()

    def show_state(self):
        r = self.chart.replay
        picking = r["picking"]
        self.btn_play.setVisible(not picking)
        self.btn_step.setVisible(not picking)
        self.speed.setVisible(not picking)
        self.btn_select.setVisible(not picking)
        self.info.setText("&nbsp;<span style='color:#b2b5be'>Click a bar on the chart to start the replay from there</span>&nbsp;"
                          if picking else "")
        self.info.setVisible(picking)
        self.btn_play.setIcon(icons.get("pause" if r["playing"] else "play"))
        self.btn_play.setToolTip("Pause  (Space)" if r["playing"] else "Play  (Space)")
        self.show()
        self.adjustSize()
        self.place()
        self.raise_()

    def place(self):
        vr = self.chart.glw.mapFromScene(self.chart._bottom_plot().vb.sceneBoundingRect().bottomLeft())
        x = int((self.chart.glw.width() - self.width()) / 2) - 30
        self.move(max(x, 8), max(int(vr.y()) - self.height() - 14, 8))


def _scale_font(S=None) -> QtGui.QFont:
    """The price / time scale font: TradingView uses the system font at 12 px, regular weight."""
    S = S or settings.get()
    f = QtGui.QFont()
    f.setPixelSize(int(S.get("text_size", 12)))
    f.setWeight(QtGui.QFont.Weight.Normal)
    return f


_LEGEND_IMG_DIR = None


def _legend_img(key: str, w: float, h: float, draw) -> str:
    """A small picture for the legend's rich text (logo circle, status pill): written once as name.png and
    name@2x.png (Qt picks the sharp one on Retina); returns the <img> tag."""
    import hashlib
    import os
    import tempfile
    global _LEGEND_IMG_DIR
    if _LEGEND_IMG_DIR is None:
        _LEGEND_IMG_DIR = os.path.join(tempfile.gettempdir(), "chartlab_legend")
        os.makedirs(_LEGEND_IMG_DIR, exist_ok=True)
    name = hashlib.md5(key.encode()).hexdigest()[:16]
    base = os.path.join(_LEGEND_IMG_DIR, name)
    if not os.path.exists(base + "@2x.png"):
        for scale, suffix in ((1.0, ""), (2.0, "@2x")):
            img = QtGui.QImage(int(round(w * scale)), int(round(h * scale)), QtGui.QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(QtCore.Qt.GlobalColor.transparent)
            img.setDevicePixelRatio(scale)
            qp = QtGui.QPainter(img)
            qp.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            qp.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
            draw(qp)
            qp.end()
            img.save(base + suffix + ".png")
    return f"<img src='{base}.png' width='{int(round(w))}' height='{int(round(h))}' style='vertical-align: middle;'>"


def _blend(fg: str, alpha: float, bg: str) -> QtGui.QColor:
    a, b = QtGui.QColor(fg), QtGui.QColor(bg)
    return QtGui.QColor(int(round(a.red() * alpha + b.red() * (1 - alpha))),
                        int(round(a.green() * alpha + b.green() * (1 - alpha))),
                        int(round(a.blue() * alpha + b.blue() * (1 - alpha))))


def legend_logo_html(sym: str) -> str:
    """The 18 px symbol circle TradingView shows before the legend title (ChartLab's own letter logo, the same
    one the watchlist uses)."""
    from .watchlist import draw_logo, logo_color
    s = (sym or "?").upper()

    def draw(qp):
        draw_logo(qp, QtCore.QRectF(0, 0, 18, 18), s, QtGui.QFont())
    return _legend_img(f"logo|{s}|{logo_color(s)}", 18, 18, draw)


def status_pill_html(state: str, bg: str = "#000000") -> str:
    """TradingView's market-status pill after the legend title (its DOM: 18 px tall capsule, 2 px / 6 px padding, an
    18 px icon, 12 px text #b8b8b8 on rgba(184,184,184,0.2)). 'closed' -> a dash and 'Closed'; 'open' -> a teal dot
    and 'Open' (#42bda8 on rgba(34,171,148,0.2), measured); 'pre' -> 'Pre-market' in orange."""
    look = {"closed": (AXIS_TEXT, "Closed", AXIS_TEXT), "open": ("#42bda8", "Open", "#22ab94"),
            "pre": ("#ff9100", "Pre-market", "#ff9100")}.get(state)
    if look is None:
        return ""
    col, text, tint = look
    f = QtGui.QFont()
    f.setPixelSize(12)
    tw = QtGui.QFontMetricsF(f).horizontalAdvance(text)
    w, h = 2 + 18 + 2 + tw + 6, 18.0
    fill = _blend(tint, 0.2, bg)

    def draw(qp):
        path = QtGui.QPainterPath()
        path.addRoundedRect(QtCore.QRectF(0, 0, w, h), h / 2, h / 2)
        qp.fillPath(path, fill)
        qp.setPen(QtCore.Qt.PenStyle.NoPen)
        qp.setBrush(QtGui.QColor(col))
        if state == "closed":                       # a dash
            qp.drawRoundedRect(QtCore.QRectF(2 + 4, 7, 10, 4), 2, 2)
        else:                                       # a dot (radius 4 in the 18 px icon)
            qp.drawEllipse(QtCore.QPointF(2 + 9, 9), 4, 4)
        qp.setPen(QtGui.QColor(col))
        qp.setFont(f)
        qp.drawText(QtCore.QRectF(2 + 18 + 2, 0, tw + 2, h), QtCore.Qt.AlignmentFlag.AlignLeft |
                    QtCore.Qt.AlignmentFlag.AlignVCenter, text)
    return _legend_img(f"pill|{state}|{bg}|{QtGui.QFontInfo(f).family()}", w, h, draw)


def axis_rect(ax) -> QtCore.QRectF:
    """Scene rect of an axis' own strip. (sceneBoundingRect() also covers the grid lines = the whole plot.)"""
    return QtCore.QRectF(ax.mapToScene(QtCore.QPointF(0, 0)), QtCore.QSizeF(ax.width(), ax.height()))


class AxisTag(QtWidgets.QLabel):
    """A price / time label drawn ON the scale (like TradingView), not inside the plot.
    kind: 'y' = on the price scale at a price, 'below' = under the last-price tag (countdown),
    'x' = on the time scale at a bar."""

    def __init__(self, chart, plot, kind: str, fill: str, under=None, height: int | None = None):
        super().__init__(chart.glw)
        self.chart, self.plot, self.kind, self.under = chart, plot, kind, under
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setIndent(0)            # Qt adds half an 'x' of indent on the aligned side otherwise: the text would sit
        self.setMargin(0)            # ~3 px off the scale's own numbers
        self._fill = fill
        self._pos = None
        self._h = int(height or (TIME_TAG_H if kind == "x" else PRICE_TAG_H))
        self._joined = False                     # a countdown label sits right under this one (one box, TradingView)
        self._restyle()
        self.hide()

    def _restyle(self):
        """TradingView's scale labels: regular-weight 12 px text; a price label is square where it touches the scale
        and rounded on the other side, a time label hangs from the top of the time scale, rounded at the bottom."""
        q = QtGui.QColor(self._fill)
        light = 0.2126 * q.redF() + 0.7152 * q.greenF() + 0.0722 * q.blueF() > 0.7   # e.g. a yellow / white line
        txt = "#000000" if light else "#ffffff"
        if self.kind == "x":
            shape = (f"padding: {TIME_TEXT_TOP}px 10px 0px 10px; border-bottom-left-radius: 2px;"
                     " border-bottom-right-radius: 2px;")
            align = QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignTop
        else:
            pl = PRICE_TEXT_LEFT - 1                       # the label starts 1 px inside the scale
            tr = "0px" if self.kind == "below" else "2px"
            br = "0px" if self._joined else "2px"
            shape = (f"padding: 0px 6px 1px {pl}px; border-top-right-radius: {tr}; border-bottom-right-radius: {br};")
            align = QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
        self.setStyleSheet(f"QLabel {{ background: {self._fill}; color: {txt}; font-size: 12px; font-weight: 400;"
                           f" {shape} }}")
        self.setAlignment(align)
        self.setFixedHeight(self._h)

    def set_joined(self, on: bool):
        """Square off the bottom-right corner while the bar countdown hangs under this label."""
        if bool(on) != self._joined:
            self._joined = bool(on)
            self._restyle()

    @property
    def fill(self):
        return self._fill

    @fill.setter
    def fill(self, v):
        col = v.color().name() if isinstance(v, QtGui.QBrush) else str(v)
        if col != self._fill:
            self._fill = col
            self._restyle()

    def setText(self, t):
        t = t.strip()
        if t == self.text():
            return
        n = len(t)
        super().setText(t)
        if self.kind == "x" or n != getattr(self, "_n", -1):   # digits are fixed-width; dates / names are not
            self._n = n
            self.adjustSize()
        if self._pos is not None:
            self.setPos(*self._pos)

    def toPlainText(self) -> str:                 # same API as the pyqtgraph TextItem it replaces
        return self.text()

    @property
    def textItem(self):
        return self

    def setPos(self, x, y):
        self._pos = (x, y)
        g = self.chart.glw
        vb = self.plot.vb
        vr = vb.sceneBoundingRect()
        vt = g.viewportTransform()
        if self.kind in ("y", "below"):
            ax = axis_rect(self.plot.getAxis("right"))
            h = self.height()
            sy = vb.mapViewToScene(QtCore.QPointF(x, y)).y()
            sy = min(max(sy, vr.top() + h / 2), vr.bottom() - h / 2)
            pt = vt.map(QtCore.QPointF(ax.left(), sy))
            self.setFixedWidth(self.sizeHint().width())          # as wide as its text, like TradingView
            # centred on the pixel row the line is drawn on: the label's middle row IS the line
            top = int(math.floor(pt.y())) - (h - 1) // 2
            if self.kind == "below" and self.under is not None:  # the countdown: lower half of the price label
                top = self.under.y() + self.under.height()
                w = max(self.width(), self.under.width())
                self.setFixedWidth(w)
                if self.under.width() < w:
                    self.under.setFixedWidth(w)
            self.move(int(round(pt.x())) + 1, top)
        else:
            ax = axis_rect(self.plot.getAxis("bottom"))
            sx = vb.mapViewToScene(QtCore.QPointF(x, y)).x()
            pt = vt.map(QtCore.QPointF(sx, ax.top()))
            left = vt.map(vr.topLeft()).x()
            right = vt.map(vr.topRight()).x()
            tx = int(round(pt.x() - self.width() / 2))
            tx = max(int(left) + 1, min(tx, int(right) - self.width() - 1))   # never cut off at the pane edges
            self.move(tx, int(round(pt.y())))
        self.raise_()


class _TableModel(QtCore.QAbstractTableModel):
    """Bars (and indicator values) for Table view, read straight from the arrays: fast for any length."""

    def __init__(self, df, cols, extra, chart):
        super().__init__()
        self.idx = df.index
        self.cols = cols
        self.data_ = [df[c].to_numpy(float) for c in cols] + [y for _n, y in extra]
        self.heads = ["Date"] + cols + [n for n, _y in extra]
        self.chart = chart
        self.intraday = len(df) > 1 and (df.index[0] - df.index[1]) < pd.Timedelta(days=1)

    def rowCount(self, parent=QtCore.QModelIndex()):
        return len(self.idx)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return len(self.heads)

    def headerData(self, sec, orient, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if role == QtCore.Qt.ItemDataRole.DisplayRole and orient == QtCore.Qt.Orientation.Horizontal:
            return self.heads[sec]
        return None

    def data(self, ix, role=QtCore.Qt.ItemDataRole.DisplayRole):
        r, c = ix.row(), ix.column()
        if role == QtCore.Qt.ItemDataRole.TextAlignmentRole:
            return int((QtCore.Qt.AlignmentFlag.AlignLeft if c == 0 else QtCore.Qt.AlignmentFlag.AlignRight)
                       | QtCore.Qt.AlignmentFlag.AlignVCenter)
        if role != QtCore.Qt.ItemDataRole.DisplayRole:
            return None
        if c == 0:
            t = self.idx[r]
            return t.strftime("%d %b %Y %H:%M" if self.intraday else "%d %b %Y")
        v = self.data_[c - 1][r]
        if v != v:
            return "\u2014"
        if self.heads[c] == "Volume":
            return theme.fmt_vol(v)
        return self.chart.fp(float(v))


class TradeLabel(QtWidgets.QFrame):
    """The box on an order / position line: [qty] [what] [x], like TradingView's."""

    def __init__(self, chart, it: dict, color: str):
        super().__init__(chart.glw)
        self.chart, self.it = chart, it
        self.setObjectName("tradelabel")
        order = it.get("kind") == "order"
        qty = abs(int(it.get("qty") or 0))
        if order:
            word = {"LIMIT": "LIMIT", "SL": "STOP LIMIT", "SL-M": "STOP", "MARKET": "MARKET"}.get(it.get("type"), "")
            text = f"{it.get('side', '').title()} {word}".strip()
            tcol = "#dbdbdb"
        else:
            pnl = float(it.get("pnl") or 0)
            text = ("+" if pnl >= 0 else "\u2212") + f"{abs(pnl):,.2f}"
            tcol = theme.UP if pnl >= 0 else theme.DOWN
        self.setStyleSheet(f"QFrame#tradelabel {{ background: #000; border: 1px solid {color}; border-radius: 2px; }}"
                           f" QLabel {{ background: transparent; font-size: 11px; padding: 0 6px; }}"
                           f" QLabel#q {{ background: {color}; color: white; font-weight: 600; }}"
                           f" QToolButton {{ background: transparent; border: 0; border-left: 1px solid {color};"
                           f" color: #dbdbdb; padding: 0 5px; font-size: 11px; }}"
                           f" QToolButton:hover {{ background: {color}; color: white; }}")
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        q = QtWidgets.QLabel(f"{qty:,}")
        q.setObjectName("q")
        h.addWidget(q)
        t = QtWidgets.QLabel(text)
        t.setStyleSheet(f"color: {tcol};")
        h.addWidget(t)
        x = QtWidgets.QToolButton()
        x.setText("\u2715")
        x.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        x.setToolTip("Cancel this order" if order else "Close this position at market")
        x.clicked.connect(self._x)
        h.addWidget(x)
        self.setFixedHeight(20)
        self.adjustSize()
        tip = (f"{it.get('side', '')} {qty:,} \u00b7 {it.get('type', '')} @ {float(it['price']):,.2f}"
               + ("  (drag the line to change the price)" if it.get("movable") else "")) if order else \
            f"Position {int(it.get('qty') or 0):+,} @ {float(it['price']):,.2f}"
        self.setToolTip(tip)
        self.hide()

    def _x(self):
        if self.it.get("kind") == "order":
            self.chart.orderCancelRequested.emit(self.it.get("obj"))
        else:
            self.chart.positionCloseRequested.emit(self.it.get("obj"))


def _watermark_pixmap(size: int = 64, opacity: float = 0.22) -> QtGui.QPixmap:
    """The SN logo, faded, drawn in the bottom-left of every chart (TradingView puts its own logo there)."""
    from .data import ROOT
    src = QtGui.QPixmap(str(ROOT / "assets" / "logo.png"))
    out = QtGui.QPixmap(size, size)
    out.fill(QtCore.Qt.GlobalColor.transparent)
    if src.isNull():
        return out
    p = QtGui.QPainter(out)
    p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
    p.setOpacity(opacity)
    p.drawPixmap(0, 0, src.scaled(size, size, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                  QtCore.Qt.TransformationMode.SmoothTransformation))
    p.end()
    return out


IND_DEFAULTS = {k: STD.defaults(k) for k in STD.KEYS}      # (kept for older callers)
IND_TITLES = {k: STD.STUDIES[k].title for k in STD.KEYS}


class _IndButtons(QtWidgets.QFrame):
    """Hide / settings / remove, shown at the end of a legend row while the mouse is on it (TradingView)."""

    def __init__(self, chart):
        super().__init__(chart.glw)
        self.chart, self.key = chart, None
        self.setStyleSheet("QFrame{background:#1f1f1f;border:1px solid #303030;border-radius:5px;}"
                           "QToolButton{background:transparent;border:0;border-radius:3px;padding:1px;}"
                           "QToolButton:hover{background:#2e2e2e;}")
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(3, 2, 3, 2)
        h.setSpacing(1)
        self.btns = {}
        for name, icon, tip, fn in (("eye", "eye", "Hide / show", self._eye), ("gear", "gear", "Settings…", self._gear),
                                    ("x", "close", "Remove", self._remove)):
            b = QtWidgets.QToolButton()
            b.setIcon(icons.get(icon))
            b.setIconSize(QtCore.QSize(15, 15))
            b.setToolTip(tip)
            b.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
            b.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            b.clicked.connect(fn)
            h.addWidget(b)
            self.btns[name] = b
        self.adjustSize()
        self.hide()
        self._off = QtCore.QTimer(self)
        self._off.setSingleShot(True)
        self._off.setInterval(350)
        self._off.timeout.connect(lambda: None if self.underMouse() else self.hide())

    def show_for(self, key, x, y):
        self._off.stop()
        self.key = key
        st = STD.STUDIES.get(key)
        self.btns["gear"].setVisible(bool(st and (st.inputs or st.colors)))
        self.adjustSize()
        self.move(int(x), int(y))
        self.show()
        self.raise_()

    def leave(self):
        if not self.isHidden() and not self._off.isActive():
            self._off.start()

    def leaveEvent(self, ev):
        self._off.start()

    def _eye(self):
        ch = self.chart
        ch.set_indicator_hidden(self.key, self.key not in ch._ind_hidden)

    def _gear(self):
        self.hide()
        self.chart.edit_indicator(self.key)

    def _remove(self):
        self.hide()
        self.chart.set_indicator(self.key, False)
        self.chart.indicatorToggled.emit(self.key, False)


class _IndicatorSettings(QtWidgets.QDialog):
    """TradingView's indicator settings: Inputs (lengths, factors) and Style (one colour per line)."""

    def __init__(self, chart, key: str):
        super().__init__(chart)
        from .drawbar import COLORS, swatch
        self.chart, self.key = chart, key
        self.st = st = STD.STUDIES[key]
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(st.title)
        self.setMinimumWidth(360)
        P = chart._P(key)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 14)
        v.setSpacing(10)
        self._swatch, self._colors_list = swatch, COLORS
        self.inputs, self.colors, self.color_btns = {}, {}, {}
        if st.inputs:
            v.addWidget(self._head("Inputs"))
            form = QtWidgets.QFormLayout()
            form.setSpacing(8)
            for name, lab, dflt, lo, hi in st.inputs:
                if isinstance(dflt, int):
                    w = QtWidgets.QSpinBox()
                    w.setRange(int(lo), int(hi))
                    w.setValue(int(P[name]))
                else:
                    w = QtWidgets.QDoubleSpinBox()
                    w.setDecimals(3 if dflt < 0.1 else 2)
                    w.setSingleStep(0.01 if dflt < 0.1 else 0.1)
                    w.setRange(float(lo), float(hi))
                    w.setValue(float(P[name]))
                self.inputs[name] = w
                form.addRow(lab, w)
            v.addLayout(form)
        if st.colors:
            v.addWidget(self._head("Style"))
            form = QtWidgets.QFormLayout()
            form.setSpacing(8)
            for name, lab, _dflt in st.colors:
                b = QtWidgets.QPushButton()
                m = QtWidgets.QMenu(b)
                for cname, c in COLORS:
                    m.addAction(swatch(c), cname, lambda c=c, n=name: self._pick(n, c))
                m.addSeparator()
                m.addAction("Other colour…", lambda n=name: self._custom(n))
                b.setMenu(m)
                self.color_btns[name] = b
                self._pick(name, P[name])
                form.addRow(lab, b)
            v.addLayout(form)
        row = QtWidgets.QHBoxLayout()
        dflt = QtWidgets.QPushButton("Defaults")
        dflt.setAutoDefault(False)
        dflt.clicked.connect(self._defaults)
        row.addWidget(dflt)
        row.addStretch(1)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        v.addLayout(row)

    @staticmethod
    def _head(text):
        lab = QtWidgets.QLabel(text)
        lab.setStyleSheet("color:#9c9c9c;font-size:12px;font-weight:600;padding-top:4px;")
        return lab

    # (older tests / callers used .len, ._pick(colour), ._ok())
    @property
    def len(self):
        return self.inputs.get("len")

    def _pick(self, name, c=None):
        if c is None:                                       # _pick("#rrggbb"): the first colour
            name, c = self.st.colors[0][0], name
        self.colors[name] = c
        b = self.color_btns[name]
        b.setIcon(self._swatch(c))
        b.setText("  " + c)

    def _custom(self, name):
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.colors[name]), self, "Colour")
        if c.isValid():
            self._pick(name, c.name())

    def _defaults(self):
        D = STD.defaults(self.key)
        for name, w in self.inputs.items():
            w.setValue(D[name])
        for name in self.color_btns:
            self._pick(name, D[name])

    def _ok(self):
        kw = {name: (int(w.value()) if isinstance(w, QtWidgets.QSpinBox) else float(w.value()))
              for name, w in self.inputs.items()}
        kw.update(self.colors)
        self.chart.set_indicator_params(self.key, **kw)
        self.accept()


class _PaneGrip(QtWidgets.QWidget):
    """The thin strip on top of an indicator pane: drag it to give the pane (or the one above) more room."""

    def __init__(self, chart, pane):
        super().__init__(chart.glw)
        self.chart, self.pane = chart, pane
        self.setFixedHeight(7)
        self.setCursor(QtCore.Qt.CursorShape.SplitVCursor)
        self.setToolTip("Drag to resize the panes")
        self._y = None
        self.hide()

    def place(self):
        pl = self.pane.plot
        if pl.isVisible() and pl.scene() is not None:
            r = pl.sceneBoundingRect()
            tl = self.chart.glw.mapFromScene(r.topLeft())
            ax = axis_rect(pl.getAxis("right"))
            right = self.chart.glw.mapFromScene(ax.topRight()).x()
            self.setGeometry(int(tl.x()), int(tl.y()) - 4, int(right - tl.x()), 7)
            self.show()
            self.raise_()
        else:
            self.hide()

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        if self.underMouse() or self._y is not None:
            p.fillRect(0, 2, self.width(), 3, QtGui.QColor("#2962ff"))
        else:                                              # the hairline TradingView draws between two panes
            p.fillRect(0, 3, self.width(), 1, QtGui.QColor(PANE_LINE))

    def enterEvent(self, ev):
        self.update()

    def leaveEvent(self, ev):
        self.update()

    def mousePressEvent(self, ev):
        self._y = ev.globalPosition().y()
        self._h = self.pane.plot.height()
        self.update()

    def mouseMoveEvent(self, ev):
        if self._y is not None:
            self.chart.set_pane_height(self.pane, self._h - (ev.globalPosition().y() - self._y))

    def mouseReleaseEvent(self, ev):
        self._y = None
        self.update()


class Pane:
    """One indicator pane under the price chart (TradingView: RSI, MACD, ...) - its plot, legend and grip."""

    def __init__(self, chart):
        self.chart = chart
        self.plot = chart._make_plot(None)
        self.keys: list[str] = []
        self.label = OverlayLabel(chart.glw)
        self.label.hide()
        self.grip = _PaneGrip(chart, self)
        self.px = None                     # height in px once the user dragged it
        self.rows: list = []               # legend rows: [(key, title, [(values, offset, colour)], study)]

    def dispose(self):
        for w in (self.label, self.grip, self.plot.ytag, self.plot.dtag):
            w.hide()
            w.deleteLater()


class ChartWidget(QtWidgets.QWidget):
    trading_ready = False
    replayChanged = QtCore.Signal()
    alertRequested = QtCore.Signal(float)
    activated = QtCore.Signal()
    toolChanged = QtCore.Signal(object)
    drawingsChanged = QtCore.Signal()
    tradeRequested = QtCore.Signal(str, float)
    orderCancelRequested = QtCore.Signal(object)            # an order line's x
    orderMoveRequested = QtCore.Signal(object, float)       # an order line dragged to a new price
    positionCloseRequested = QtCore.Signal(object)          # a position line's x
    orderAtPrice = QtCore.Signal(str, str, float, bool)    # side, order type, price, at once (else: order panel)
    settingsRequested = QtCore.Signal()
    gotoDateRequested = QtCore.Signal()
    indicatorsChanged = QtCore.Signal()                      # inputs changed (saved by the window)
    indicatorToggled = QtCore.Signal(str, bool)              # removed / added from the legend
    typed = QtCore.Signal(str)
    barHovered = QtCore.Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.df: pd.DataFrame | None = None
        self.label = ""
        self.style = "candles"
        self.magnet = True
        self.scale_mode = "regular"      # regular | log | percent
        self._base = 1.0
        self._last_overlays = None
        self._last_trades = None
        self.auto_y = True
        self._limit = None            # replay: only bars [0, _limit) exist yet
        self._ind_timer = QtCore.QTimer(self)                # live ticks: indicators follow at most once a second
        self._ind_timer.setSingleShot(True)
        self._ind_timer.setInterval(1000)
        self._ind_timer.timeout.connect(self._refresh_indicators)
        self.replay = {"active": False, "picking": False, "playing": False, "speed": 1.0}
        self._enabled: set[str] = set()
        self._ind_order: list[str] = []      # the order indicators were added in (panes keep it)
        self._ind_items: dict[str, list] = {}
        self._strategy_items: list = []
        self._marker_items: list = []
        self.candles = self.volbars = self.line_item = None
        self._times = np.array([])
        self._lows = self._highs = self._vols = self._opens = self._closes = np.array([])

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.glw = pg.GraphicsLayoutWidget()
        QGV = QtWidgets.QGraphicsView
        # (DontSavePainterState is NOT safe with pyqtgraph: painter state leaks between items and e.g. the RSI
        #  line stops drawing)
        self.glw.setOptimizationFlag(QGV.OptimizationFlag.DontAdjustForAntialiasing, True)
        self.glw.viewport().setAttribute(QtCore.Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.glw.viewport().setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.glw.ci.layout.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.glw, 1)
        self.header = OverlayLabel(self.glw)
        self.header.setText(f"<span style='color:{theme.MUTED}'>Open a data file or type a symbol and press Enter</span>")
        self.header.move(10, 6)
        self.vol_label = OverlayLabel(self.glw)
        self.ind_label = OverlayLabel(self.glw)
        self.ind_label.hide()
        self.vol_label.hide()
        self.header.raise_()
        self.sell_btn = self._trade_button("SELL", theme.DOWN)
        self.buy_btn = self._trade_button("BUY", "#2962ff")
        self.spread_lbl = OverlayLabel(self.glw)
        self.spread_lbl.hide()
        self.collapse_btn = QtWidgets.QToolButton(self.glw)
        self.collapse_btn.setText("\u2303")
        self.collapse_btn.setFixedSize(28, 19)                # TradingView's legend toggle: 28 x 19
        self.collapse_btn.setCheckable(True)
        self.collapse_btn.setToolTip("Hide / show the legend")
        self.collapse_btn.setStyleSheet("QToolButton { background:#000; border:1px solid #3d3d3d; border-radius:4px; color:#dbdbdb; font-size:12px; padding:0; } QToolButton:hover { background:#1a1a1a; }")
        self.collapse_btn.toggled.connect(lambda _c: self._place_overlays())
        self.collapse_btn.hide()
        self.plus_btn = QtWidgets.QLabel(self.glw)
        self.plus_btn.setFixedSize(XHAIR_TAG_H, XHAIR_TAG_H)
        self.plus_btn.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.plus_btn.setPixmap(_plus_pixmap(False))
        self.plus_btn.hide()
        self._plus_y = 0.0
        self.watermark = QtWidgets.QLabel(self.glw)
        self.watermark.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.watermark.setStyleSheet("background: transparent;")
        self.watermark.setPixmap(_watermark_pixmap())
        self.watermark.adjustSize()
        self.watermark.hide()

        self.axes: list[DateAxis] = []
        self.panes: list[Pane] = []                         # indicator panes under the price chart
        self.plus_btn_hot = False
        self.price = self._make_plot(0)
        self.vol = self._make_plot(1)
        self.vol.setXLink(self.price)

        grid = self.glw.ci.layout
        grid.setSpacing(0)                                  # panes touch; a 1 px line separates them (TradingView)
        grid.setRowStretchFactor(0, 14)
        self.vol.setVisible(False)                      # volume is an overlay inside the price pane (like TradingView)
        grid.setRowFixedHeight(1, 0)
        self._refresh_axes()
        for pl in (self.price, self.vol):                   # no meaningless 0..1 labels before data arrives
            pl.getAxis("right").setStyle(showValues=False)

        self._legend_rows: list = []                    # [(name, [(item, colour), ...])] shown under "Vol"
        self._legend_keys: list = []                    # indicator key of each legend row (None = strategy line)
        self._ind_hidden: set = set()                   # switched off with the eye, still listed
        self.ind_params = {k: dict(v) for k, v in IND_DEFAULTS.items()}
        self.ind_buttons = _IndButtons(self)

        # tags: last price, crosshair price (per pane) and crosshair date
        self.vol_tag = AxisTag(self, self.price, "y", "#26a69a")   # TradingView: the newest bar's volume on the scale
        self.last_tag = AxisTag(self, self.price, "y", theme.UP)
        self.cd_tag = AxisTag(self, self.price, "below", "#2a2a2a", under=self.last_tag)
        self.name_tag = QtWidgets.QLabel(self.glw)
        self.name_tag.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.name_tag.hide()
        self._cd_timer = QtCore.QTimer(self)
        self._cd_timer.timeout.connect(self._update_countdown)
        self._cd_timer.start(1000)
        self.last_line = pg.InfiniteLine(angle=0, movable=False,
                                         pen=pg.mkPen(theme.UP, style=QtCore.Qt.PenStyle.DotLine))
        self.price.addItem(self.last_line, ignoreBounds=True)
        self.last_line.hide()

        self.stay_in_drawing = False                        # keep the tool after a drawing is placed
        self.trading_ready = False                          # a broker account is connected (Buy / Sell buttons)
        self.layer = DrawingLayer(self)
        self.layer.changed.connect(self.drawingsChanged)
        from .drawbar import DrawBar
        self.draw_bar = DrawBar(self)
        # TradingView's "scroll to the most recent bar": appears when the newest bar is off screen
        self.latest_btn = QtWidgets.QToolButton(self.glw)
        self.latest_btn.setIcon(icons.get("latest"))
        self.latest_btn.setIconSize(QtCore.QSize(18, 18))
        self.latest_btn.setFixedSize(30, 30)
        self.latest_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.latest_btn.setToolTip(f"Scroll to the most recent bar  ({theme.sc('Alt+Shift+Right')})")
        self.latest_btn.setStyleSheet("QToolButton{background:#1f1f1f;border:1px solid #303030;border-radius:15px;}"
                                      "QToolButton:hover{background:#2e2e2e;}")
        self.latest_btn.clicked.connect(self.scroll_to_latest)
        self.latest_btn.hide()
        # TradingView: a settings button in the corner where the price and time scales meet
        self.axis_gear = QtWidgets.QToolButton(self.glw)
        self.axis_gear.setIcon(icons.get("gear"))
        self.axis_gear.setIconSize(QtCore.QSize(18, 18))
        self.axis_gear.setFixedSize(26, 26)
        self.axis_gear.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.axis_gear.setToolTip("Chart settings")
        self.axis_gear.setStyleSheet("QToolButton{background:transparent;border:0;border-radius:4px;padding:0;}"
                                     "QToolButton:hover{background:#2e2e2e;}")
        self.axis_gear.clicked.connect(self.settingsRequested.emit)
        self.axis_gear.hide()

        self.price.vb.sigXRangeChanged.connect(self._autoscale)
        self.price.vb.sigXRangeChanged.connect(lambda *_: self._sync_latest_btn())
        self.price.vb.sigRangeChanged.connect(lambda *_: (self._place_last_tag(), self._place_alert_tags(),
                                                          self._place_trade_tags(), self.layer.place_tags(),
                                                          self._refresh_crosshair()))
        # labels on the scales are QWidgets: re-place them once pyqtgraph has finished laying the plots out
        # (a resize / layout change moves the axes AFTER the viewport's resize event)
        self._relayout_pending = False
        for sig in (self.price.getAxis("right").geometryChanged, self.price.vb.sigResized, self.vol.geometryChanged):
            sig.connect(self._schedule_relayout)
        self.glw.scene().sigMouseMoved.connect(self._mouse_moved)
        self.glw.scene().sigMouseClicked.connect(self._mouse_clicked)
        self.corners = CornerOverlay(self.glw)
        self.corners.show()
        self.glw.viewport().installEventFilter(self)
        self.glw.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.glw.viewport().setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

    def _trade_button(self, text, color):
        """TradingView's legend Sell / Buy button: 85 x 34, 1 px border, the price in 12 px semibold over the side
        in 11 px (measured on its page)."""
        b = QtWidgets.QPushButton("", self.glw)
        b.setFixedSize(85, 34)
        b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        b.setStyleSheet(f"QPushButton {{ background:#000000; border:1px solid {color}; border-radius:6px; padding:0; }}"
                        f" QPushButton:hover {{ background:{color}26; }}")
        b.price_lbl = QtWidgets.QLabel("", b)
        b.side_lbl = QtWidgets.QLabel(text, b)
        for lab, top, h, css in ((b.price_lbl, 3, 16, "font-size:12px; font-weight:600;"),
                                 (b.side_lbl, 18, 13, "font-size:11px; font-weight:400;")):
            lab.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            lab.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            lab.setStyleSheet(f"QLabel {{ background:transparent; color:{color}; {css} }}")
            lab.setGeometry(1, top, 83, h)
        b.clicked.connect(lambda _=False, side=text: self.tradeRequested.emit(side, float(self.df["Close"].iloc[-1]) if self.df is not None else 0.0))
        b.hide()
        return b

    def _update_trade_buttons(self):
        if self.df is None:                        # (in replay they follow the replayed price, like TradingView)
            return
        last = float(self.df["Close"].iloc[max(len(self.df) - 1 if self._limit is None else self._limit - 1, 0)])
        if last == getattr(self, "_tb_last", None):
            return
        self._tb_last = last
        # TradingView without a bid / ask feed (an index, no broker): the last price on both buttons, no spread
        # ('0.00' for an index, '0.0' for a 0.10-tick stock - the symbol's own precision)
        fp = self.fp(last)
        for b in (self.sell_btn, self.buy_btn):
            b.price_lbl.setText(fp)
        self.spread_lbl.setText(f"<span style='color:#dbdbdb;font-size:10px'>{self.fp(0.0, ref=last)}</span>")

    # ------------------------------------------------------------------ construction helpers
    def _make_plot(self, row) -> pg.PlotItem:
        """A plot with TradingView's scales. row None = an indicator pane, added to the layout later."""
        axis = DateAxis("bottom")
        self.axes.append(axis)
        kw = dict(axisItems={"bottom": axis, "right": PriceAxis("right")}, viewBox=TVViewBox(self, price=(row == 0)))
        p = self.glw.addPlot(row=row, col=0, **kw) if row is not None else pg.PlotItem(**kw)
        p.setMenuEnabled(False)
        p.hideButtons()
        p.showAxis("right")
        p.hideAxis("left")
        p.getAxis("right").chart = self
        p.getAxis("right").is_price = row == 0
        p.getAxis("right").setWidth(72 if not self.axes[1:] else self.price.getAxis("right").width())
        S = settings.get()
        f = _scale_font(S)
        for name in ("bottom", "right"):
            ax = p.getAxis(name)
            ax.setPen(pg.mkPen(S.get("bg", "#000000")))      # TradingView draws no line between chart and scales
            ax.setTextPen(pg.mkPen(S.get("text_color", AXIS_TEXT)))
            ax.setStyle(tickFont=f, tickLength=0,
                        tickTextOffset=TIME_TEXT_TOP if name == "bottom" else PRICE_TEXT_LEFT)
        p.getAxis('bottom').setHeight(28)
        p.getAxis('right').setTickDensity(2.2)
        p.getAxis('bottom').setTickDensity(1.2)
        p.showGrid(x=False, y=False)                 # the grid is drawn by the ViewBox (cheaper, see gestures.py)
        p.vb.plot_item = p
        dash = self._xhair_pen()
        p.vline = pg.InfiniteLine(angle=90, movable=False, pen=dash)
        p.hline = pg.InfiniteLine(angle=0, movable=False, pen=dash)
        for ln in (p.vline, p.hline):
            ln.setZValue(50)                       # above candles and indicator lines, like TradingView
            p.addItem(ln, ignoreBounds=True)
        p.hline.hide()
        p.vline.hide()                             # (shown when the mouse is over the chart)
        p.ytag = AxisTag(self, p, "y", XHAIR_FILL, height=XHAIR_TAG_H)
        p.dtag = AxisTag(self, p, "x", XHAIR_FILL)
        return p

    def _xhair_pen(self) -> QtGui.QPen:
        """TradingView's crosshair: 1 screen point wide (2 pixels on a Retina Mac), #9c9c9c, dashed 5 on / 6 off
        (measured on TradingView itself). A plain 1-pixel cosmetic pen is half a point on Retina: a faint hairline."""
        S = settings.get()
        dpr = max(1.0, float(self.glw.devicePixelRatioF())) if hasattr(self, "glw") else 1.0
        pen = QtGui.QPen(QtGui.QColor(S.get("crosshair", XHAIR_COLOR)))
        pen.setCosmetic(True)
        pen.setWidthF(dpr)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.FlatCap)
        style = S.get("crosshair_style", "dashed")
        if style == "solid":
            pen.setStyle(QtCore.Qt.PenStyle.SolidLine)
        elif style == "dotted":
            pen.setDashPattern([1.0, 2.0])
        else:
            pen.setDashPattern([5.0, 6.0])         # in line widths (= points): 5 on, 6 off
        self._xh_dpr = dpr
        return pen

    def _apply_xhair_pen(self):
        pen = self._xhair_pen()
        for pl in self._plots():
            pl.vline.setPen(pen)
            pl.hline.setPen(pen)

    def _plots(self) -> list:
        return [self.price, self.vol] + [pn.plot for pn in self.panes]

    @property
    def rsi(self):
        """(older callers) the plot of the RSI pane, or None."""
        pn = self._pane_of("rsi")
        return pn.plot if pn is not None else None

    # ------------------------------------------------------------------ indicator panes
    def _pane_of(self, key: str):
        return next((pn for pn in self.panes if key in pn.keys), None)

    def _pane_for_plot(self, plot):
        return next((pn for pn in self.panes if pn.plot is plot), None)

    def _add_pane(self, key: str) -> "Pane":
        pn = Pane(self)
        pn.keys.append(key)
        pl = pn.plot
        pl.setXLink(self.price)
        if self.df is not None:
            pl.getAxis("bottom").share_index(self.axes[0])
            pl.getAxis("right").setStyle(showValues=True)
            xmax = len(self._lows) + 80 if self._limit is None else self._n() + 60
            pl.vb.setLimits(xMin=-5, xMax=xmax, minXRange=8)
        else:
            pl.getAxis("right").setStyle(showValues=False)
        pl.geometryChanged.connect(self._schedule_relayout)
        self.panes.append(pn)
        self._layout_panes()
        self.apply_settings_to_plot(pl)
        return pn

    def _drop_pane(self, pn):
        if pn not in self.panes:
            return
        self.panes.remove(pn)
        pl = pn.plot
        try:
            pl.geometryChanged.disconnect(self._schedule_relayout)
        except (RuntimeError, TypeError):
            pass
        pl.setXLink(None)
        ax = pl.getAxis("bottom")
        if ax in self.axes:
            self.axes.remove(ax)
        if pl in self.glw.ci.items:
            self.glw.ci.removeItem(pl)
        pn.dispose()
        self._layout_panes()

    def _layout_panes(self):
        """Panes in rows 2, 3, ... of the layout with no gaps (a pane removed from the middle closes up)."""
        ci = self.glw.ci
        for k, pn in enumerate(self.panes):
            where = ci.items.get(pn.plot)
            if where and where[0][0] != 2 + k:
                ci.removeItem(pn.plot)
        for k, pn in enumerate(self.panes):
            if pn.plot not in ci.items:
                ci.addItem(pn.plot, row=2 + k, col=0)
        self._apply_row_heights()
        self._refresh_axes()
        self._fit_axis_width()
        self._schedule_relayout()

    def _apply_row_heights(self):
        grid = self.glw.ci.layout
        for r in range(2, 2 + len(self.panes) + 8):
            grid.setRowStretchFactor(r, 0)
            grid.setRowMinimumHeight(r, 0)
            grid.setRowMaximumHeight(r, _QWIDGETSIZE_MAX)
        for k, pn in enumerate(self.panes):
            r = 2 + k
            if pn.px:
                for setter in (grid.setRowMinimumHeight, grid.setRowPreferredHeight, grid.setRowMaximumHeight):
                    setter(r, pn.px)
            else:
                grid.setRowStretchFactor(r, 4)              # a new pane gets about a fifth of the chart
        grid.invalidate()

    def set_pane_height(self, pane, h: float):
        """Drag the line on top of a pane: that pane gets h pixels, the price chart (or the pane above) the rest."""
        if pane not in self.panes:
            return
        k = self.panes.index(pane)
        above = self.panes[k - 1] if k > 0 else None
        cur = pane.plot.height()
        room = cur + (above.plot.height() if above is not None else self.price.height())
        h = max(50.0, min(float(h), room - (50.0 if above is not None else 140.0)))
        if above is not None and above.px:
            above.px = max(50.0, above.px - (h - cur))
        pane.px = h
        self._apply_row_heights()
        self._schedule_relayout()

    def set_rsi_height(self, h: float):                     # (older callers)
        pn = self._pane_of("rsi")
        if pn is not None:
            self.set_pane_height(pn, h)

    def eventFilter(self, obj, ev):
        if obj is self.glw.viewport() and ev.type() == QtCore.QEvent.Type.Resize:
            self.corners.setGeometry(self.glw.rect())
            self.corners.raise_()
            QtCore.QTimer.singleShot(0, self._place_overlays)
            QtCore.QTimer.singleShot(0, self._refresh_header)
        if obj is self.glw.viewport() and ev.type() == QtCore.QEvent.Type.MouseButtonPress:
            self.activated.emit()
        if obj is self.glw.viewport() and ev.type() == QtCore.QEvent.Type.Leave:
            QtCore.QTimer.singleShot(0, self._crosshair_off)
        if obj is self.glw.viewport() and ev.type() == QtCore.QEvent.Type.Enter:
            for p in self._plots():
                p.vline.show()
            # mouse came back (e.g. a dialog / menu closed over the chart): put the whole crosshair under it now,
            # not where it was before
            pos = getattr(ev, "position", None)
            self._cross_vp = (pos().toPoint() if callable(pos)
                              else self.glw.viewport().mapFromGlobal(QtGui.QCursor.pos()))
            self._cross_key = None
            QtCore.QTimer.singleShot(0, self._refresh_crosshair)
        if obj is self.glw.viewport() and ev.type() == QtCore.QEvent.Type.Wheel and self.df is not None:
            if self._trackpad_scroll(ev):
                return True
        if obj is self.glw.viewport() and ev.type() == QtCore.QEvent.Type.NativeGesture:
            if ev.gestureType() == QtCore.Qt.NativeGestureType.ZoomNativeGesture:
                pos = self.price.vb.mapSceneToView(self.glw.mapToScene(ev.position().toPoint()))
                self.price.vb._stop_anim()
                self.price.vb.zoom_x(float(np.exp(-ev.value() * 1.6)), pos.x())    # pinch: around the fingers
                return True
        if obj is self.glw.viewport() and ev.type() == QtCore.QEvent.Type.KeyPress:
            if self.handle_key(ev):
                return True
        return super().eventFilter(obj, ev)

    def _trackpad_scroll(self, ev) -> bool:
        """Two fingers on a trackpad, like TradingView on a Mac: sideways drags the chart 1:1 with the fingers
        (macOS adds the glide after you let go), up / down zooms continuously. Pixel-exact and applied at once -
        no stepping, no restarted animations. A real mouse wheel has no pixel delta and keeps its eased zoom."""
        pd_ = ev.pixelDelta()
        if pd_.isNull():
            return False
        pt = self.glw.mapToScene(ev.position().toPoint())
        plot = next((p for p in self._plots() if p.isVisible() and p.vb.sceneBoundingRect().contains(pt)), None)
        if plot is None:
            return False                                   # over a scale: the normal handler scales that axis
        vb = self.price.vb
        mods = ev.modifiers()
        if mods & QtCore.Qt.KeyboardModifier.ShiftModifier:
            return False
        vb._stop_anim()
        vb._vel = 0.0
        (x0, x1) = vb.viewRange()[0]
        w = max(vb.width(), 1.0)
        dx, dy = pd_.x(), pd_.y()
        if abs(dx) >= abs(dy):                              # one direction at a time, whichever the fingers mean
            dy = 0
        else:
            dx = 0
        if dx:
            vb.pan_x(-dx * (x1 - x0) / w)
        if dy:
            focused = bool(mods & (QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.MetaModifier))
            anchor = vb.mapSceneToView(pt).x() if focused else min(x1, self.x_limit() + 2)
            vb.zoom_x(float(np.exp(-dy / 240.0)), anchor)
        ev.accept()
        return True

    # ------------------------------------------------------------------ replay (TradingView "Bar Replay")
    def _apply_limit(self):
        n = self._n()
        for it in (self.candles, self.volbars):
            if it is not None:
                it.limit = self._limit
                it.update()
        if self.line_item is not None:
            c = self.ty(self.df["Close"].to_numpy(float))
            self.line_item.setData(np.arange(n), c[:n])
        for items in self._ind_items.values():
            for _pl, it in items:
                self._limit_item(it, n)
        for it, _name in self._strategy_items:
            f = getattr(it, "_full", None)
            if f is not None:
                it.setData(f[0][:n], f[1][:n])
        lim = len(self._lows) + 80 if self._limit is None else n + 60
        for p in self._plots():
            p.vb.setLimits(xMin=-5, xMax=lim, minXRange=8)
        self._update_last_tag()
        self._update_header(max(n - 1, 0))
        self._autoscale()

    def _limit_item(self, it, n: int):
        """Replay: an indicator item shows only what the first n bars produce."""
        f = getattr(it, "_full", None)
        if f is not None:
            x, y = f
            if getattr(it, "_step", False):                  # pivot segments: cut at the replay position
                it.setData(np.where(np.isnan(x), np.nan, np.minimum(x, n - 0.5)), y)
            else:
                off = int(getattr(it, "_offset", 0))
                it.setData(x[:n], y[:n]) if off == 0 else it.setData(x[:max(n, 0)], y[:max(n, 0)])
        elif hasattr(it, "limit"):                           # fills / histograms / dots follow the replay too
            it.limit = self._limit
            it.update()

    def replay_begin(self):
        """Step 1: the user picks the start bar by clicking on the chart."""
        if self.df is None or len(self.df) < 20:
            return
        if self.replay.get("playing"):
            self.replay_pause()
        if self._limit is not None:                      # picking again mid-replay: show the whole history
            self._limit = None
            self._apply_limit()
        self.replay.update(active=True, picking=True, playing=False)
        self._ensure_replay_bar()
        self.replay_bar.show_state()
        self.replayChanged.emit()

    def replay_start(self, k: int):
        k = int(max(5, min(k, len(self.df) - 1)))
        self._limit = k + 1
        self.replay.update(picking=False, playing=False)
        self._apply_limit()
        self.price.setXRange(max(-2, k - 120), k + 25, padding=0)
        self._autoscale()
        self.replay_bar.show_state()
        self.replayChanged.emit()

    def replay_step(self, n: int = 1) -> bool:
        if not self.replay["active"] or self._limit is None:
            return False
        if self._limit >= len(self.df):
            self.replay_pause()
            return False
        self._limit = min(self._limit + n, len(self.df))
        self._apply_limit()
        k = self._limit - 1
        x0, x1 = self.price.vb.viewRange()[0]
        if k > x1 - 6:                                     # keep the newest bar in view
            self.price.vb.pan_x(k - (x1 - 6))
        self.replayChanged.emit()
        return True

    def replay_play(self):
        if self._limit is None:
            return
        self.replay["playing"] = True
        if not hasattr(self, "_replay_timer"):
            self._replay_timer = QtCore.QTimer(self)
            self._replay_timer.timeout.connect(self.replay_step)
        self._replay_timer.start(int(1000 / self.replay["speed"]))
        self.replay_bar.show_state()
        self.replayChanged.emit()

    def replay_pause(self):
        self.replay["playing"] = False
        if hasattr(self, "_replay_timer"):
            self._replay_timer.stop()
        if hasattr(self, "replay_bar"):
            self.replay_bar.show_state()
        self.replayChanged.emit()

    def replay_set_speed(self, per_second: float):
        self.replay["speed"] = per_second
        if self.replay["playing"]:
            self._replay_timer.start(int(1000 / per_second))

    def replay_exit(self):
        self.replay_pause()
        self.replay.update(active=False, picking=False)
        self._limit = None
        if self.df is not None:
            self._apply_limit()
            self.go_latest()
        if hasattr(self, "replay_bar"):
            self.replay_bar.hide()
        self.replayChanged.emit()

    def _ensure_replay_bar(self):
        if not hasattr(self, "replay_bar"):
            self.replay_bar = ReplayBar(self)

    def _schedule_relayout(self, *_):
        if self.df is not None:                       # scale labels: right away (cheap) ...
            self._place_last_tag()
            self._place_alert_tags()
            self._place_trade_tags()
            self.layer.place_tags()
        if self._relayout_pending:                    # ... legend / crosshair once the layout pass is over
            return
        self._relayout_pending = True

        def run():
            self._relayout_pending = False
            self._place_overlays()
            self._refresh_crosshair()
        QtCore.QTimer.singleShot(0, run)

    def _refresh_crosshair(self, *_):
        """The chart moved under a still mouse (wheel / trackpad zoom, arrow keys, replay, a resize): keep the
        crosshair, its price / date labels and the legend on what is under the cursor now (TradingView does)."""
        vp = getattr(self, "_cross_vp", None)
        if vp is None or self.df is None or self._panning():
            return
        (x0, x1), (y0, y1) = self.price.vb.viewRange()
        key = (vp.x(), vp.y(), x0, x1, y0, y1, self.glw.width(), self.glw.height())
        if key == getattr(self, "_cross_key", None):
            return
        self._cross_key = key
        self._mouse_moved(self.glw.mapToScene(vp), refresh=True)

    def _place_axis_gear(self):
        g = self.axis_gear
        bp = self._bottom_plot() if self.df is not None else None
        if bp is None or not bp.getAxis("bottom").isVisible():
            g.hide()
            return
        vt = self.glw.viewportTransform()
        ra = axis_rect(self.price.getAxis("right"))
        ba = axis_rect(bp.getAxis("bottom"))
        x0, x1 = vt.map(QtCore.QPointF(ra.left(), 0)).x(), vt.map(QtCore.QPointF(ra.right(), 0)).x()
        y0, y1 = vt.map(QtCore.QPointF(0, ba.top())).y(), vt.map(QtCore.QPointF(0, ba.bottom())).y()
        g.move(int((x0 + x1 - g.width()) / 2), int((y0 + y1 - g.height()) / 2))
        g.show()
        g.raise_()

    def _place_overlays(self):
        self.corners.setGeometry(self.glw.rect())
        self.corners.raise_()
        self._sync_latest_btn()
        self._place_axis_gear()
        if not self.draw_bar.isHidden():
            self.draw_bar.place()
            self.draw_bar.raise_()
        self._place_last_tag()
        self._place_alert_tags()
        self._place_trade_tags()
        self.layer.place_tags()
        if hasattr(self, "replay_bar") and not self.replay_bar.isHidden():
            self.replay_bar.place()
        if self.df is not None:                                  # volume legend sits under the OHLC header
            col = not self.collapse_btn.isChecked() and bool(settings.get()['sl_trade'])   # shown like TradingView
            self.sell_btn.setVisible(col)
            self.buy_btn.setVisible(col)
            self.spread_lbl.setVisible(col)
            self.collapse_btn.show()
            self._apply_watermark_vis()
            y = self.header.y() + self.header.height() + 4
            if col:                                       # TradingView: 39 px between the buttons, spread centred
                self.sell_btn.move(10, y)
                gx = 10 + self.sell_btn.width()
                self.spread_lbl.move(gx + (39 - self.spread_lbl.width()) // 2, y + (34 - self.spread_lbl.height()) // 2)
                self.buy_btn.move(gx + 39, y)
                y += 34 + 8
            folded = self.collapse_btn.isChecked()
            show_vol = self._show_vol_legend()
            self.vol_label.setVisible(show_vol)
            if show_vol:                                  # (isVisible() is False while the pane is hidden)
                self.vol_label.move(10, y)
                y += self.vol_label.height() + 2
            if self._legend_rows and not folded and settings.get().get("sl_ind_titles", True):
                self.ind_label.move(10, y)
                self.ind_label.show()
                self.ind_label.raise_()
                y += self.ind_label.height() + 4
            else:
                self.ind_label.hide()
            self.collapse_btn.move(10, y + 2)
            gh = self.glw.height()
            self.watermark.move(14, max(gh - 100, 0))
            for w in (self.sell_btn, self.buy_btn, self.spread_lbl, self.vol_label, self.collapse_btn):
                w.raise_()
        folded = self.collapse_btn.isChecked()
        for pn in self.panes:                                    # each pane's legend sits in its top-left corner
            pl, lab = pn.plot, pn.label
            vis = (self.df is not None and pl.scene() is not None and bool(lab.text()) and not folded
                   and settings.get().get("sl_ind_titles", True))
            lab.setVisible(vis)
            if vis:
                tl = self.glw.mapFromScene(pl.vb.sceneBoundingRect().topLeft())
                lab.move(int(tl.x()) + 10, int(tl.y()) + 4)
                lab.raise_()
            pn.grip.place()
        self.header.raise_()

    def _show_vol_legend(self) -> bool:
        """'Vol' in the legend only when the symbol trades volume - an index (NIFTY, SENSEX) has none, and
        TradingView shows nothing there rather than 'Vol 0'."""
        if self.df is None or self.collapse_btn.isChecked() or not settings.get()['sl_volume']:
            return False
        key = (id(self.df), len(self._vols))
        if getattr(self, "_has_vol_key", None) != key:
            self._has_vol_key = key
            v = self._vols
            self._has_vol = bool(len(v)) and bool(np.nanmax(np.abs(v[-min(len(v), 2000):])) > 0)
        return self._has_vol

    def set_range_preset(self, key: str):
        """TradingView's bottom range buttons: show the last 1D / 5D / 1M ... / All."""
        n = len(self._times)
        if n < 2:
            return
        end = self._times[-1]
        days = {"1d": 1, "5d": 5, "1mo": 30, "3mo": 91, "6mo": 182, "1y": 365, "5y": 1826}
        if key == "all":
            x0 = 0
        elif key == "ytd":
            jan1 = pd.Timestamp(year=pd.Timestamp(end, unit="s").year, month=1, day=1).timestamp()
            x0 = int(np.searchsorted(self._times, jan1))
        else:
            x0 = int(np.searchsorted(self._times, end - days[key] * 86400))
        x0 = min(x0, max(n - 10, 0))
        self.auto_y = True
        self.price.setXRange(x0 - 1, n + 4, padding=0)
        self._autoscale()

    def handle_key(self, ev) -> bool:
        vb, K = self.price.vb, QtCore.Qt.Key
        x0, x1 = vb.viewRange()[0]
        step, k = (x1 - x0) * 0.1, ev.key()
        if self.replay["active"] and not self.replay["picking"]:
            if k == K.Key_Space:
                self.replay_bar._toggle()
                return True
            if k == K.Key_Right:
                self.replay_step(1)
                return True
        mods = ev.modifiers()
        M = QtCore.Qt.KeyboardModifier
        txt = ev.text()
        if not (mods & (M.ControlModifier | M.AltModifier | M.MetaModifier)) and len(txt) == 1 and \
                (txt.isalnum() or txt == ","):
            self.typed.emit("" if txt == "," else txt)
            return True
        if mods & M.ControlModifier:
            if k == K.Key_Left:
                vb.pan_x(-step * 5); return True
            if k == K.Key_Right:
                vb.pan_x(step * 5); return True
            if k == K.Key_Up:
                vb.zoom_x(0.8); return True
            if k == K.Key_Down:
                vb.zoom_x(1.25); return True
        if (mods & M.AltModifier) and (mods & M.ShiftModifier):
            if k == K.Key_Left:
                vb.pan_x(-x0 - 2); return True
            if k == K.Key_Right:
                self.go_latest(); return True
        if self.replay["active"] and k == K.Key_Escape:
            self.replay_exit()
            return True
        if k == K.Key_Left:
            vb.pan_x(-1)
        elif k == K.Key_Right:
            vb.pan_x(1)
        elif k in (K.Key_Plus, K.Key_Equal, K.Key_Up):
            vb.zoom_x(0.8)
        elif k in (K.Key_Minus, K.Key_Down):
            vb.zoom_x(1.25)
        elif k in (K.Key_Home,):
            vb.pan_x(-x0 - 2)
        elif k in (K.Key_End,):
            self.go_latest()
        else:
            return False
        return True

    # ------------------------------------------------------------------ settings
    def ty(self, y):
        """price -> view coordinate for the current scale mode (works on scalars and arrays)."""
        if self.scale_mode == "log":
            return np.log10(np.maximum(y, 1e-9))
        if self.scale_mode == "percent":
            return (np.asarray(y, float) / (self._base or 1.0) - 1.0) * 100.0 if not np.isscalar(y) else (y / (self._base or 1.0) - 1.0) * 100.0
        return y

    def iy(self, v):
        if self.scale_mode == "log":
            return 10 ** v
        if self.scale_mode == "percent":
            return (self._base or 1.0) * (1.0 + v / 100.0)
        return v

    def decimals(self, at: float | None = None) -> int:
        """Decimals of a price on this chart. 'Default' = the symbol's own tick, like TradingView: an NSE stock's
        step depends on its price (RELIANCE 1,358.5, HDFCBANK 809.20, BAJAJ-AUTO 10,587), an index 25,447.92.
        `at`: the price being written (None: the newest close)."""
        p = settings.get()["precision"]
        if p == "Integer":
            return 0
        if p != "Default":
            return int(p.split()[0])
        if getattr(self, "_var_tick", False):
            from .symbols import band_decimals
            if at is None:
                at = float(self._closes[self._n() - 1]) if getattr(self, "_closes", None) is not None and self._n() else 0.0
            return band_decimals(at)
        return int(getattr(self, "_auto_dec", 2))

    def axis_price(self, x: float) -> str:
        return f"{x:,.{self.decimals(x)}f}"                # 25,282.50 / 1,358.5 like TradingView's scale

    def axis_label(self, v: float) -> str:
        if self.scale_mode == "percent":
            return f"{v:+.2f}%"
        return self.axis_price(float(self.iy(v)))

    def set_scale_mode(self, mode: str):
        if mode == self.scale_mode:
            return
        self.scale_mode = mode
        if self.df is None:
            return
        self._base = float(self.df["Close"].iloc[0])
        n = len(self.df)
        self.candles.set_data(self.df, self.style, self.ty)
        close = self.ty(self.df["Close"].to_numpy(float))
        self.line_item.setData(np.arange(n), close[:self._n()])
        for key in list(self._enabled):
            self._remove_indicator(key)
            self._build_indicator(key)
        self._rebuild_legend()
        if self._last_overlays is not None:
            self.set_strategy_overlays(self._last_overlays)
        if self._last_trades is not None:
            self.set_trades(self._last_trades)
        self._apply_style()
        self.layer.rebuild()
        if getattr(self, "_alert_items", None):
            self.set_alert_lines(self._alert_items)
        self.price.getAxis("right").picture = None
        self.price.getAxis("right").update()
        self._update_last_tag()
        self.go_latest()

    def fp(self, x: float, ref: float | None = None) -> str:
        """Format a price with the precision chosen in Settings (Default: the symbol's own tick at that price;
        `ref`: write it in the precision of that price instead - a spread, a change)."""
        return f"{x:,.{self.decimals(x if ref is None else ref)}f}"

    def fd(self, x: float, ref: float | None = None) -> str:
        """A signed price change in the symbol's precision, with TradingView's real minus sign: '−8.5' / '+7.9' for
        RELIANCE, '−117.63' for NIFTY."""
        return f"{x:+,.{self.decimals(ref)}f}".replace("-", "−")

    def apply_settings_to_plot(self, pl):
        S = settings.get()
        pl.vb._gp_key = None
        pl.vb.update()
        f = _scale_font(S)
        for name in ("bottom", "right"):
            ax = pl.getAxis(name)
            ax.setPen(pg.mkPen(S["bg"]))
            ax.setTextPen(pg.mkPen(S["text_color"]))
            ax.setStyle(tickFont=f)
            ax.picture = None
            ax.update()
            if name == "bottom":
                ax.dow, ax.h24, ax.datefmt = bool(S["dow"]), bool(S["h24"]), S["date_format"]
        pen = self._xhair_pen()
        pl.vline.setPen(pen)
        pl.hline.setPen(pen)

    def apply_settings(self):
        S = settings.get()
        self.glw.setBackground(S["bg"])
        for pl in self._plots():
            self.apply_settings_to_plot(pl)
        for ax in self.axes:
            ax.dow = bool(S["dow"])
            ax.h24 = bool(S["h24"])
            ax.datefmt = S["date_format"]
        self.last_line.setVisible(bool(S["last_line"]) and self.df is not None)
        self.last_tag.setVisible(bool(S["last_label"]) and self.df is not None)
        if self.candles is not None:
            self.candles.update()
        if self.volbars is not None:
            self.volbars.update()
        self._apply_watermark()
        self._fit_axis_width()
        if self.df is not None:
            self._update_last_tag()
            self._autoscale()
            self._update_header(self._n() - 1 if self._n() else 0)
        self._place_overlays()

    def _apply_watermark_vis(self):
        self.watermark.setVisible(settings.get()['watermark'] != 'Hidden')

    def _apply_watermark(self):
        S = settings.get()
        mode = S["watermark"]
        if mode == "Hidden" or self.df is None:
            self.watermark.hide()
            return
        if mode == "Logo":
            self.watermark.setPixmap(_watermark_pixmap())
        else:
            if mode == "Ticker":
                txt = (self.label or "").split(" ")[0]
            elif mode == "Interval":
                txt = self.interval_tag()
            else:
                txt = self.label or ""
            big = mode != "Ticker and description"
            f = QtGui.QFont()
            f.setPixelSize(64 if big else 28)
            f.setWeight(QtGui.QFont.Weight.Bold)
            fm = QtGui.QFontMetrics(f)
            w, h = fm.horizontalAdvance(txt) + 8, fm.height() + 4
            pm = QtGui.QPixmap(max(w, 4), h)
            pm.fill(QtCore.Qt.GlobalColor.transparent)
            p = QtGui.QPainter(pm)
            col = QtGui.QColor(S["wm_color"])
            col.setAlpha(60)
            p.setPen(col)
            p.setFont(f)
            p.drawText(pm.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, txt)
            p.end()
            self.watermark.setPixmap(pm)
        self.watermark.adjustSize()
        self.watermark.show()

    def _fit_axis_width(self, df=None):
        df = self.df if df is None else df
        if df is None or not len(df):
            return
        fm = QtGui.QFontMetrics(_scale_font())
        widest = max(fm.horizontalAdvance(self.axis_price(float(np.nanmax(df["High"])) * 1.2)),
                     fm.horizontalAdvance("-100.00%"))
        w = int(max(64, widest + PRICE_TEXT_LEFT + 12))      # TradingView: 10 px before the text, ~12 after
        for pl in self._plots():
            if pl.getAxis("right").width() != w:
                pl.getAxis("right").setWidth(w)

    def _refresh_axes(self):
        """Only the lowest pane carries the time scale; the others end flush against the pane below (TradingView)."""
        owner = self._bottom_plot()
        for pl in self._plots():
            if pl is owner:
                pl.showAxis("bottom")
                pl.getAxis("bottom").setStyle(showValues=True)
            elif pl.getAxis("bottom").isVisible():
                pl.hideAxis("bottom")

    def _bottom_plot(self):
        return self.panes[-1].plot if self.panes else self.price

    # ------------------------------------------------------------------ coordinate helpers (used by drawings)
    def _n(self) -> int:
        return len(self._lows) if self._limit is None else min(self._limit, len(self._lows))

    def has_data(self) -> bool:
        return self.df is not None and len(self._times) > 1

    def x_limit(self) -> float:
        return len(self._times) + 80

    def y_range(self):
        return self.price.vb.viewRange()[1]

    def time_to_x(self, ts: float) -> float:
        t, n = self._times, len(self._times)
        if n < 2:
            return 0.0
        if ts <= t[0]:
            return (ts - t[0]) / max(t[1] - t[0], 1)
        if ts >= t[-1]:
            return n - 1 + (ts - t[-1]) / max(t[-1] - t[-2], 1)
        i = int(np.searchsorted(t, ts))
        return i - 1 + (ts - t[i - 1]) / max(t[i] - t[i - 1], 1)

    def x_to_time(self, x: float) -> float:
        t, n = self._times, len(self._times)
        if n < 2:
            return 0.0
        if x <= 0:
            return float(t[0] + x * (t[1] - t[0]))
        if x >= n - 1:
            return float(t[-1] + (x - (n - 1)) * (t[-1] - t[-2]))
        i = int(np.floor(x))
        return float(t[i] + (x - i) * (t[i + 1] - t[i]))

    def set_cursor_for_tool(self, on: bool):
        self.glw.viewport().setCursor(QtCore.Qt.CursorShape.CrossCursor)

    def snap(self, x: float, y: float):
        """Magnet: snap to the nearest O/H/L/C of the bar under the cursor if close enough."""
        n = self._n()
        if not self.magnet or n == 0:
            return x, y
        i = int(min(max(round(x), 0), n - 1))
        r = self.df.iloc[i]
        vb = self.price.vb
        py = vb.mapViewToScene(QtCore.QPointF(x, y)).y()
        best, best_d = None, 16.0
        for v in (r["Open"], r["High"], r["Low"], r["Close"]):
            tv = float(self.ty(float(v)))
            d = abs(vb.mapViewToScene(QtCore.QPointF(x, tv)).y() - py)
            if d < best_d:
                best, best_d = tv, d
        return (float(i), best) if best is not None else (x, y)

    # ------------------------------------------------------------------ data
    def set_data(self, df: pd.DataFrame, label: str = ""):
        if self.replay["active"]:
            self.replay_exit()
        self._limit = None
        self._alert_lines = []
        self._wipe()
        self.df, self.label = df, label
        n = len(df)
        self._fut = None
        self.axes[0].set_index(df.index, _project_times(df.index, 160))
        for a in self.axes[1:]:                              # same bars on every pane: work the labels out once
            a.share_index(self.axes[0])
        self._times = df.index.to_numpy().astype("datetime64[s]").astype("int64").astype(float)

        self._base = float(df["Close"].iloc[0])
        self._auto_dec = price_decimals(df)                  # the symbol's tick size, TradingView's precision
        try:
            from .symbols import variable_tick
            self._var_tick = variable_tick(self.ticker()) and not self._option()
        except Exception:  # noqa: BLE001
            self._var_tick = False
        self._tb_last = None                                 # (Buy / Sell prices are re-written in that precision)
        self._fit_axis_width(df)
        for pl in self._plots():
            pl.getAxis("right").setStyle(showValues=True)
        self.candles = CandleItem(df, self.style, self.ty)
        self.price.addItem(self.candles)
        close = df["Close"].to_numpy(float)
        self.line_item = pg.PlotDataItem(x=np.arange(n), y=self.ty(close), pen=pg.mkPen(theme.ACCENT, width=2))
        self.price.addItem(self.line_item)
        self.line_item.setClipToView(True)
        self.line_item.setDownsampling(auto=True, method='peak')

        self.volbars = VolumeItem(df)
        self.volbars.overlay = self.price.vb
        self.volbars.setZValue(-5)                     # behind candles, like TradingView
        self.price.addItem(self.volbars, ignoreBounds=True)

        self._lows = df["Low"].to_numpy(float)
        self._highs = df["High"].to_numpy(float)
        self._vols = df["Volume"].to_numpy(float)
        self._opens = df["Open"].to_numpy(float)
        self._closes = close
        self._itag = None                              # interval tag is recomputed for the new data
        self._apply_style()

        for key in self._ordered_enabled():
            self._build_indicator(key)
        self._rebuild_legend()

        for p in self._plots():
            p.vb.setLimits(xMin=-5, xMax=n + 80, minXRange=8)
        self.layer.rebuild()
        self.go_latest()
        self._update_last_tag()
        self._update_header(n - 1)

    def update_last(self, close, high=None, low=None, volume=None, open_=None) -> bool:
        """A live price for the newest bar. Only that bar, the last-price label, the legend and (at most once a
        second) the indicators change: no rebuild, the view and the drawings are not touched."""
        df = self.df
        if df is None or not len(df) or self._limit is not None or self.candles is None:
            return False
        n = len(df) - 1
        for name in ("_opens", "_highs", "_lows", "_closes", "_vols"):
            a = getattr(self, name)
            if not a.flags.writeable:
                setattr(self, name, np.array(a, float))
        o = float(self._opens[n] if open_ is None else open_)
        c = float(close)
        h = max(float(self._highs[n] if high is None else high), c, o)
        lo = min(float(self._lows[n] if low is None else low), c, o)
        v = float(self._vols[n] if volume is None else volume)
        if (o, h, lo, c, v) == (self._opens[n], self._highs[n], self._lows[n], self._closes[n], self._vols[n]):
            return False
        cp = getattr(self, "_colpos", None)
        if cp is None or cp[0] is not df:                    # column positions: looked up once per data set
            pos = {k: df.columns.get_loc(k) for k in ("Open", "High", "Low", "Close", "Volume")}
            cp = self._colpos = (df, pos, {k: df.dtypes.iloc[j].kind in "iu" for k, j in pos.items()})
        _, pos, ints = cp
        for col, val, arr in (("Open", o, self._opens), ("High", h, self._highs), ("Low", lo, self._lows),
                              ("Close", c, self._closes), ("Volume", v, self._vols)):
            if arr[n] != val:
                arr[n] = val
                df.iat[n, pos[col]] = int(val) if ints[col] else val
        if self.candles.isVisible():
            if not self.candles.set_last(o, h, lo, c):
                self.candles.set_data(df, self.style, self.ty)
        else:
            self._apply_style()                              # line / area / baseline: cheap redraw of the line
        if self.volbars is not None:
            self.volbars.set_last(v, c >= o)
        self._update_last_tag()
        if self.auto_y:
            y0, y1 = self.price.vb.viewRange()[1]
            x1 = self.price.vb.viewRange()[0][1]
            if x1 >= n and not (y0 <= float(self.ty(lo)) and float(self.ty(h)) <= y1):
                self._autoscale()                            # the bar grew out of the pane
        hi = getattr(self, "_hover_i", None)
        if hi is None or hi >= n:
            self._update_header(n)
        if self._ind_items and not self._ind_timer.isActive():
            # indicators follow once a second; on very long histories less often, so the chart never stutters
            self._ind_timer.start(int(max(1000, 25 * getattr(self, "_ind_cost", 0.0))))
        return True

    def replace_values(self, df) -> bool:
        """Same bars, newer numbers (a quiet refresh): swap the data under the items that are already on screen."""
        if (self.df is None or self.candles is None or self._limit is not None or len(df) != len(self.df)
                or self.replay["active"]):
            return False
        self.df = df
        self._lows = df["Low"].to_numpy(float)
        self._highs = df["High"].to_numpy(float)
        self._vols = df["Volume"].to_numpy(float)
        self._opens = df["Open"].to_numpy(float)
        self._closes = df["Close"].to_numpy(float)
        self._apply_style()
        if self.volbars is not None:
            self.volbars.set_data(df)
        self._ind_timer.stop()
        self._refresh_indicators()
        self._update_last_tag()
        if self.auto_y:
            self._autoscale()
        return True

    def _ordered_enabled(self) -> list:
        return [k for k in self._ind_order if k in self._enabled] + sorted(k for k in self._enabled if k not in self._ind_order)

    def _refresh_indicators(self):
        if self.df is None or self._limit is not None:
            return
        import time as _time
        t0 = _time.perf_counter()
        for key in self._ordered_enabled():
            if key in self._ind_items:
                self._remove_indicator(key)
                self._build_indicator(key)
        self._rebuild_legend()
        self._ind_cost = (_time.perf_counter() - t0) * 1000
        hi = getattr(self, "_hover_i", None)
        self._update_header(len(self.df) - 1 if hi is None else min(hi, len(self.df) - 1))

    # ------------------------------------------------------------------ drawings: selection, settings
    def selection_changed(self):
        bar = getattr(self, "draw_bar", None)
        if bar is not None:
            bar.sync()

    def edit_drawing(self, did):
        d = self.layer.get(did) if did is not None else None
        if d is None:
            return
        from .drawbar import DrawingSettings
        DrawingSettings(self, d).exec()

    # ------------------------------------------------------------------ newest bar
    def _latest_visible(self) -> bool:
        return self.df is None or self.price.vb.viewRange()[0][1] >= self._n() - 0.5

    def _sync_latest_btn(self):
        b = getattr(self, "latest_btn", None)
        if b is None:
            return
        show = self.df is not None and not self._latest_visible()
        if show != (not b.isHidden()):
            b.setVisible(show)
        if show:
            r = self.price.vb.sceneBoundingRect()
            br = self.glw.mapFromScene(r.bottomRight())
            b.move(int(br.x()) - b.width() - 12, int(br.y()) - b.height() - 12)
            b.raise_()

    def scroll_to_latest(self):
        """Back to the newest bar without changing the zoom (TradingView's round arrow button)."""
        n = self._n()
        if not n:
            return
        vb = self.price.vb
        x0, x1 = vb.viewRange()[0]
        right = n + int(settings.get()['scale_right_bars'])
        vb._goal = (right - (x1 - x0), right)
        vb._animate_x(right - (x1 - x0), right, ms=260)

    def go_latest(self):
        n = self._n()
        self.auto_y = True
        if n:
            self.price.setXRange(max(-2, n - 150), n + int(settings.get()['scale_right_bars']), padding=0)
            self._autoscale()

    def _wipe(self):
        for item, plot in ((self.candles, self.price), (self.line_item, self.price), (self.volbars, self.price)):
            if item is not None:
                plot.removeItem(item)
        self.candles = self.volbars = self.line_item = None
        for key in list(self._ind_items):
            self._remove_indicator(key)
        self.clear_strategy()
        self.layer.clear_silent()

    # ------------------------------------------------------------------ style
    def set_style(self, style: str):
        if style not in STYLES:
            return
        self.style = style
        if self.df is not None:
            self._apply_style()

    def _apply_style(self):
        if self.candles is None:
            return
        is_candle = self.style in ("candles", "hollow", "bars", "heikin")
        if is_candle:
            self.candles.set_data(self.df, self.style, self.ty)
        self.candles.setVisible(is_candle)
        self.line_item.setVisible(not is_candle)
        n = self._n()
        close = self.ty(self.df["Close"].to_numpy(float))[:n]
        self.line_item.setSymbol("o" if self.style == "linemark" else None)
        if self.style == "linemark":
            self.line_item.setSymbolSize(5)
            self.line_item.setSymbolBrush(pg.mkBrush(theme.ACCENT))
            self.line_item.setSymbolPen(pg.mkPen(theme.ACCENT))
        if self.style == "step":
            x = np.repeat(np.arange(n), 2)[1:]
            y = np.repeat(close, 2)[:-1]
            self.line_item.setData(x, y)
        else:
            self.line_item.setData(np.arange(n), close)
        if self.style == "area":
            self.line_item.setFillLevel(float(self.ty(np.nanmin(self._lows))))
            self.line_item.setBrush(pg.mkBrush(41, 98, 255, 55))
        elif self.style == "baseline":
            base = float(self.ty(float(self.df["Close"].iloc[0])))
            self.line_item.setFillLevel(base)
            self.line_item.setBrush(pg.mkBrush(8, 153, 129, 60))
        else:
            self.line_item.setFillLevel(None)

    # ------------------------------------------------------------------ indicators
    def _P(self, key: str) -> dict:
        """An indicator's inputs and colours: defaults with what the user changed on top."""
        return STD.params(key, self.ind_params.get(key))

    def set_indicator(self, key: str, on: bool):
        if key not in STD.STUDIES:
            return
        if on:
            self._enabled.add(key)
            if key not in self._ind_order:
                self._ind_order.append(key)
            if self.df is not None:
                self._build_indicator(key)
        else:
            self._enabled.discard(key)
            self._remove_indicator(key)
            pn = self._pane_of(key)
            if pn is not None:
                pn.keys.remove(key)
                if not pn.keys:
                    self._drop_pane(pn)
        self._rebuild_legend()
        if self.df is not None:
            self._autoscale()

    def set_indicator_params(self, key: str, **params):
        """Change an indicator's inputs / colours. The lines are rebuilt at once."""
        if key not in STD.STUDIES:
            return
        cur = self.ind_params.setdefault(key, STD.defaults(key))
        for k, v in params.items():
            if v is None:
                cur.pop(k, None)
            else:
                cur[k] = v
        if key in self._ind_items:
            self._remove_indicator(key)
            self._build_indicator(key)
            self._rebuild_legend()
            if self.df is not None and self._n():
                self._update_header(min(getattr(self, "_hdr_i", self._n() - 1), self._n() - 1))
                self._autoscale()
        self.indicatorsChanged.emit()

    def set_indicator_hidden(self, key: str, hidden: bool):
        (self._ind_hidden.add if hidden else self._ind_hidden.discard)(key)
        self._rebuild_legend()
        self.indicatorsChanged.emit()                        # (the window remembers hidden ones too)
        if self.df is not None:
            self._autoscale()

    def edit_indicator(self, key: str):
        if key in STD.STUDIES:
            _IndicatorSettings(self, key).exec()

    def _legend_hover(self, vp_pos):
        """Mouse over a legend row: show its hide / settings / remove buttons at the end of the row."""
        b = self.ind_buttons
        if vp_pos is None or self.df is None:
            b.leave()
            return
        targets = [(self.ind_label, list(self._legend_keys))] + [(pn.label, [r[0] for r in pn.rows]) for pn in self.panes]
        for label, keys in targets:
            if label.isHidden() or not keys:
                continue
            g = label.geometry()
            zone = QtCore.QRect(g.x() - 6, g.y(), g.width() + 110, g.height())
            if zone.contains(vp_pos):
                row = min(len(keys) - 1, max(0, int((vp_pos.y() - g.y()) * len(keys) / max(g.height(), 1))))
                key = keys[row]
                if key is None:
                    break
                h = g.height() / len(keys)
                b.show_for(key, g.x() + g.width() + 8, int(g.y() + row * h + (h - b.height()) / 2))
                return
        b.leave()

    def _build_indicator(self, key: str):
        st = STD.STUDIES.get(key)
        if st is None or self.df is None or key in self._ind_items:
            return
        P = self._P(key)
        try:
            outs, fills = st.calc(self.df, P)
        except Exception as e:  # noqa: BLE001 - a bad input value must not break the chart
            import logging
            logging.getLogger("chartlab").warning("indicator %s failed: %s", key, e)
            return
        if st.pane:
            pn = self._pane_of(key)
            if pn is None:
                pn = self._add_pane(key)
            plot = pn.plot
        else:
            plot = self.price
        n = len(self.df)
        lim = self._limit
        items = []
        for o in outs:
            raw = np.asarray(o.y, float)
            y = raw if st.pane else np.asarray(self.ty(raw), float)
            if o.kind == "hist":
                it = HistItem(y, o.idx, o.palette)
                it.limit = lim
            elif o.kind == "dots":
                it = DotsItem(y, o.color)
                it.limit = lim
            else:
                x = np.asarray(o.x, float) if o.kind == "step" else np.arange(n, dtype=float) + o.offset
                pen = pg.mkPen(o.color, width=o.width, style=_PEN_STYLES.get(o.style, QtCore.Qt.PenStyle.SolidLine))
                it = pg.PlotDataItem(x=x, y=y, pen=pen, connect="finite")
                it._full = (x, y)
                it._step = o.kind == "step"
            it._raw = raw if o.vals is None else np.asarray(o.vals, float)
            it._offset = int(o.offset)
            it._legend = bool(o.legend)
            it._legend_color = o.color
            plot.addItem(it)
            if isinstance(it, pg.PlotDataItem):
                it.setClipToView(True)                       # only the part on screen is turned into a path:
                it.setDownsampling(auto=True, method="peak")  # 100,000 bars pan as smoothly as 500
                if lim is not None:
                    self._limit_item(it, self._n())
            items.append((plot, it))
        for f in fills:
            ia, ib = items[f.a][1], items[f.b][1]
            if not (hasattr(ia, "_full") and hasattr(ib, "_full")):
                continue
            c = QtGui.QColor(f.color)
            c.setAlpha(f.alpha)
            dn = None
            if f.color_dn:
                dn = QtGui.QColor(f.color_dn)
                dn.setAlpha(f.alpha)
                dn = pg.mkBrush(dn)
            fill = BandFill(ia._full[1], ib._full[1], pg.mkBrush(c), offset=outs[f.a].offset, brush_dn=dn)
            fill.limit = lim
            fill.setZValue(-8)
            plot.addItem(fill, ignoreBounds=True)
            items.append((plot, fill))
        if st.pane:
            if st.band:
                bc = QtGui.QColor(st.band_color)
                bc.setAlpha(24)
                band = pg.LinearRegionItem(values=st.band, orientation="horizontal", movable=False,
                                           brush=pg.mkBrush(bc), pen=pg.mkPen(None))
                band.setZValue(-10)
                plot.addItem(band, ignoreBounds=True)
                items.append((plot, band))
            for lvl, style in st.levels:
                g = pg.InfiniteLine(pos=lvl, angle=0, movable=False,
                                    pen=pg.mkPen(STD.GREY, width=1, style=_PEN_STYLES.get(style)))
                g.setZValue(-9)
                plot.addItem(g, ignoreBounds=True)
                items.append((plot, g))
        self._ind_items[key] = items

    def _remove_indicator(self, key: str):
        for plot, item in self._ind_items.pop(key, []):
            plot.removeItem(item)

    @staticmethod
    def _legend_vals(items) -> list:
        return [(it._raw, it._offset, it._legend_color) for _pl, it in items
                if getattr(it, "_legend", False) and getattr(it, "_raw", None) is not None]

    def _rebuild_legend(self):
        """Price-chart indicators and strategy lines are listed under 'Vol'; each pane lists its own in its corner.
        TradingView style: the name, then the value(s) at the bar under the crosshair in the line colours."""
        rows = []
        for key in self._ordered_enabled():
            items = self._ind_items.get(key)
            if not items:
                continue
            st = STD.STUDIES[key]
            hidden = key in self._ind_hidden
            for _pl, it in items:
                it.setVisible(not hidden)
            if not st.pane:
                rows.append((key, st.legend(self._P(key)), self._legend_vals(items), st))
        for item, name in self._strategy_items:
            col = item.opts["pen"].color().name() if hasattr(item.opts.get("pen"), "color") else "#dbdbdb"
            raw = getattr(item, "_raw", None)
            rows.append((None, name, [(raw, 0, col)] if raw is not None else [], None))
        self._legend_rows = rows
        self._legend_keys = [r[0] for r in rows]
        for pn in self.panes:
            pn.rows = [(k, STD.STUDIES[k].legend(self._P(k)), self._legend_vals(self._ind_items.get(k, [])),
                        STD.STUDIES[k]) for k in pn.keys if k in self._ind_items]
        self._update_ind_legend(getattr(self, "_hdr_i", None))
        self._hdr_shape = None
        self._place_overlays()

    def _fmt_study(self, st, v: float) -> str:
        return self.fp(float(v)) if st is None else STD.fmt_value(st, float(v), self.fp)

    def _legend_html(self, rows, i: int) -> str:
        S = settings.get()
        show_vals = S.get("sl_ind_values", True)
        out = []
        for key, title, vals, st in rows:
            off = key in self._ind_hidden
            parts = [f"<span style='color:{'#636363' if off else LEGEND_TEXT}'>{title}</span>"]
            if show_vals and not off:
                for arr, offs, col in vals:
                    j = i - offs
                    v = arr[j] if arr is not None and 0 <= j < len(arr) else np.nan
                    if np.isfinite(v):
                        parts.append(f"<span style='color:{col}'>{self._fmt_study(st, v)}</span>")
            out.append("&nbsp;&nbsp;".join(parts))
        return "<br>".join(out)

    def _update_ind_legend(self, i):
        S = settings.get()
        if self.df is None:
            self.ind_label.hide()
            return
        if i is None:
            i = self._n() - 1
        folded = self.collapse_btn.isChecked()
        titles = S.get("sl_ind_titles", True)
        if self._legend_rows and titles:
            self.ind_label.setText(self._legend_html(self._legend_rows, i))
            self.ind_label.setVisible(not folded)
        else:
            self.ind_label.hide()
        for pn in self.panes:
            pn.label.setText(self._legend_html(pn.rows, i) if pn.rows else "")
            if pn.label.isHidden() == (bool(pn.rows) and titles and not folded):
                self._hdr_shape = None                       # a pane legend appeared / vanished: re-place

    def _autoscale_panes(self):
        """Each pane fits what is on screen, levels (30 / 70 ...) included, like TradingView."""
        if not self.panes or self.df is None:
            return
        x0, x1 = self.price.vb.viewRange()[0]
        n = self._n()
        for pn in self.panes:
            lo, hi = np.inf, -np.inf
            for key in pn.keys:
                if key in self._ind_hidden:
                    continue
                st = STD.STUDIES[key]
                for _pl, it in self._ind_items.get(key, []):
                    raw = getattr(it, "_raw", None)
                    if raw is None:
                        continue
                    offs = int(getattr(it, "_offset", 0))
                    a = max(0, int(np.floor(x0)) - offs)
                    b = min(n, len(raw), int(np.ceil(x1)) + 1 - offs)
                    if b <= a:
                        continue
                    seg = raw[a:b]
                    seg = seg[np.isfinite(seg)]
                    if len(seg):
                        lo, hi = min(lo, float(seg.min())), max(hi, float(seg.max()))
                    if isinstance(it, HistItem):
                        lo, hi = min(lo, 0.0), max(hi, 0.0)
                for lvl, _style in st.levels:
                    lo, hi = min(lo, float(lvl)), max(hi, float(lvl))
            if not (np.isfinite(lo) and np.isfinite(hi)):
                continue
            pad = (hi - lo) * 0.1 if hi > lo else (abs(hi) * 0.05 or 1.0)
            pn.plot.vb.setYRange(lo - pad, hi + pad, padding=0)

    # ------------------------------------------------------------------ strategy output
    def clear_strategy(self):
        for item, _ in self._strategy_items:
            self.price.removeItem(item)
        self._strategy_items = []
        for item in self._marker_items:
            self.price.removeItem(item)
        self._marker_items = []

    def set_strategy_overlays(self, overlays: dict):
        for item, _ in self._strategy_items:
            self.price.removeItem(item)
        self._strategy_items = []
        self._last_overlays = overlays
        if self.df is None:
            return
        x = np.arange(len(self.df))
        for i, (name, series) in enumerate(overlays.items()):
            color = theme.LINE_COLORS[(i + 2) % len(theme.LINE_COLORS)]
            sy = self.ty(series.to_numpy(float))
            item = pg.PlotDataItem(x=x, y=sy, pen=pg.mkPen(color, width=1.4), connect="finite")
            item._full = (x, sy)
            item._raw = series.to_numpy(float)
            self.price.addItem(item)
            if self._limit is not None:
                item.setData(x[:self._limit], item._full[1][:self._limit])
            self._strategy_items.append((item, name))
        self._rebuild_legend()

    def set_trades(self, trades: pd.DataFrame):
        """Strategy fills on the chart like TradingView: entry arrows with 'Long' / 'Short', exit arrows with 'Close'."""
        from .render import TradeMarkers
        self._last_trades = trades
        for item in self._marker_items:
            self.price.removeItem(item)
        self._marker_items = []
        if self.df is None or trades is None or trades.empty:
            return
        n = self._n()
        side = trades["side"].astype(str).to_numpy() if "side" in trades else np.array(["long"] * len(trades))
        ei = trades["entry_idx"].to_numpy(int)
        closed = ~trades["open"].astype(bool).to_numpy()
        xi = trades["exit_idx"].to_numpy(float)
        BLUE, RED, MAG = "#2962ff", "#f23645", "#e040fb"
        X, Y, UP, COL, LAB = [], [], [], [], []

        def add(x, low_side, color, label):
            X.append(x)
            Y.append(float(self.ty(self._lows[x] if low_side else self._highs[x])))
            UP.append(low_side)
            COL.append(color)
            LAB.append(label)
        for k in range(len(trades)):
            long_ = side[k] != "short"
            e = int(ei[k])
            if 0 <= e < n:
                add(e, long_, BLUE if long_ else RED, "Long" if long_ else "Short")
            if closed[k] and np.isfinite(xi[k]) and 0 <= int(xi[k]) < n:
                add(int(xi[k]), not long_, MAG, "Close")
        m = TradeMarkers()
        m.setZValue(30)
        self.price.addItem(m, ignoreBounds=True)
        m.set_points(X, Y, UP, COL, LAB)
        self._marker_items.append(m)

    def focus_range(self, x0: float, x1: float):
        self.price.setXRange(x0, x1, padding=0)

    # ------------------------------------------------------------------ view / tags
    def set_auto_y(self, on: bool):
        if on == self.auto_y:
            return
        self.auto_y = on
        if on:
            self._autoscale()

    def _autoscale(self, *_):
        if self.df is None:
            return
        n = self._n()
        self._autoscale_panes()
        if not self.auto_y:
            x0, x1 = self.price.vb.viewRange()[0]
            lo, hi = max(0, int(x0)), min(n, int(x1) + 1)
            if hi > lo:
                self.vol.vb.setYRange(0, max(float(np.nanmax(self._vols[lo:hi])) * 1.15, 1.0), padding=0)
            self._place_last_tag()
            return
        x0, x1 = self.price.vb.viewRange()[0]
        lo, hi = max(0, int(np.floor(x0))), min(n, int(np.ceil(x1)) + 1)
        if hi <= lo:
            return
        ymin, ymax = float(np.nanmin(self.ty(self._lows[lo:hi]))), float(np.nanmax(self.ty(self._highs[lo:hi])))
        S = settings.get()
        rng = (ymax - ymin) or ymax * 0.02
        self.price.vb.setYRange(ymin - rng * (S['margin_bottom'] + 10) / 100, ymax + rng * S['margin_top'] / 100, padding=0)   # room for the volume strip
        if self.volbars is not None:
            self.volbars.update()
        vmax = float(np.nanmax(self._vols[lo:hi]))
        self.vol.vb.setYRange(0, max(vmax * 1.15, 1.0), padding=0)
        self._place_last_tag()

    def _update_last_tag(self):
        if self.df is None or self._n() == 0:
            return
        last, prev = float(self._closes[self._n() - 1]), float(self._opens[self._n() - 1])
        color = theme.UP if last >= prev else theme.DOWN
        self.last_tag.fill = pg.mkBrush(color)
        self.cd_tag.fill = color                            # the countdown is the lower half of the same label
        self.last_tag.setText(f" {self.axis_label(float(self.ty(last)))} ")
        self.last_line.setPen(pg.mkPen(color, style=QtCore.Qt.PenStyle.DotLine))
        self.last_line.setPos(float(self.ty(last)))
        self.last_tag.setVisible(bool(settings.get()['last_label']))
        self.last_line.setVisible(bool(settings.get()['last_line']))
        nt = self.name_tag                                 # TradingView: the symbol name sits left of the price
        name = self.ticker()
        if (name, color) != getattr(self, "_nt_key", None):   # live ticks come many times a second: restyle only on change
            self._nt_key = (name, color)
            nt.setText(name)
            nt.setStyleSheet(f"QLabel {{ background: {color}; color: #ffffff; font-size: 12px; font-weight: 400;"
                             f" padding: 0px 6px 1px 6px; border-radius: 2px; }}")
            nt.adjustSize()
        self._place_last_tag()

    def _place_vol_tag(self):
        """The newest bar's volume as a label on the price scale, level with the top of its bar (TradingView)."""
        vt = self.vol_tag
        info = None
        if (self.df is not None and self.volbars is not None and bool(settings.get().get("last_label", True))
                and getattr(self.volbars, "overlay", None) is not None and self.volbars.isVisible()):
            try:
                info = self.volbars.last_value()
            except Exception:  # noqa: BLE001
                info = None
        if info is None:
            vt.hide()
            return
        y, v, up = info
        vt.fill = vol_color(up).name()
        vt.setText(theme.fmt_vol(v))
        vt.setPos(self.price.vb.viewRange()[0][1], y)
        vt.show()
        for tg in (self.last_tag, self.cd_tag, self.name_tag):     # the price's own labels stay on top
            if not tg.isHidden():
                tg.raise_()

    def _place_last_tag(self):
        try:
            self._place_last_tag_()
        finally:
            self._sync_scale_blocks()

    def _sync_scale_blocks(self):
        """Tell the price scale where the series labels sit, so the numbers under them are not drawn (TradingView)."""
        ax = self.price.getAxis("right")
        spans = []
        try:
            inv, ok = self.glw.viewportTransform().inverted()
            if ok:
                lay = getattr(self, "layer", None)                  # drawings' labels too (h line, selected anchors)
                extra = (list(lay._tags.values()) + list(lay._sel_y)) if lay is not None else []
                for tg in (self.last_tag, self.cd_tag, self.vol_tag, *extra):
                    if tg.isHidden() or tg.parent() is None:
                        continue
                    g = tg.geometry()
                    a = ax.mapFromScene(inv.map(QtCore.QPointF(0, g.top()))).y()
                    b = ax.mapFromScene(inv.map(QtCore.QPointF(0, g.top() + g.height()))).y()
                    spans.append((float(round(min(a, b))), float(round(max(a, b)))))
        except RuntimeError:                                   # widgets already gone (closing)
            return
        spans = tuple(sorted(spans))
        if spans != ax._blocked:
            ax._blocked = spans
            ax.picture = None
            ax.update()

    def _place_last_tag_(self):
        self._place_vol_tag()
        if self.df is None or self.last_tag.isHidden():
            return
        xr = self.price.vb.viewRange()[0][1]
        y = float(self.ty(float(self._closes[self._n() - 1])))
        self.last_tag.setPos(xr, y)
        self.cd_tag.setPos(xr, y)
        nt = self.name_tag
        show = not self.last_tag.isHidden() and bool(settings.get().get("sym_label", False)) and bool(nt.text())
        nt.setVisible(show)
        if show:
            g = self.last_tag.geometry()
            nt.setFixedHeight(g.height())
            nt.move(g.x() - nt.width() - 1, g.y())

    def bar_seconds(self) -> float:
        if self.df is None or len(self.df) < 3:
            return 0.0
        d = np.diff(self._times[-30:])
        d = d[d > 0]
        return float(np.median(d)) if len(d) else 0.0

    def _update_countdown(self):
        """TradingView's 'time left in this bar' under the last-price label (only while the bar is live)."""
        if self.df is None or self._limit is not None or not self.isVisible() or not settings.get()['countdown']:
            self._cd_hide()
            return
        step = self.bar_seconds()
        if step <= 0 or step > 86400 * 2:
            self._cd_hide()
            return
        import datetime as _dt
        last = self.df.index[-1]
        close = self._session_close()                       # when today's session ends (None: market closed)
        if step >= 86400 * 0.9:
            # a daily bar closes with the session (TradingView: 02:20:43 left at 13:09 IST on NSE), not at midnight
            ok = close is not None and pd.Timestamp(last).date() == self._exchange_today()
            left = (close - _dt.datetime.now(_dt.timezone.utc)).total_seconds() if ok else -1.0
            if not 0 <= left <= 86400:
                self._cd_hide()
                return
        else:
            end = last + _dt.timedelta(seconds=step)
            now = _dt.datetime.now(tz=end.tzinfo) if getattr(end, "tzinfo", None) else _dt.datetime.now()
            left = (end - now).total_seconds()
            if close is not None:                            # the day's last bar is cut short by the close
                left = min(left, (close - _dt.datetime.now(_dt.timezone.utc)).total_seconds())
            if not 0 <= left <= step:
                self._cd_hide()
                return
        h, rem = divmod(int(left), 3600)
        m, sec = divmod(rem, 60)
        self.cd_tag.setText(f" {h:02d}:{m:02d}:{sec:02d} " if h else f" {m:02d}:{sec:02d} ")   # TV: 02:20:43
        self.last_tag.set_joined(not self.last_tag.isHidden())
        self.cd_tag.setVisible(not self.last_tag.isHidden())
        self._place_last_tag()

    def _cd_hide(self):
        was = not self.cd_tag.isHidden()
        self.cd_tag.hide()
        self.last_tag.set_joined(False)
        if was:
            self._sync_scale_blocks()

    def _mouse_moved(self, pos, refresh: bool = False):
        if self.df is None:
            return
        # the horizontal line sits exactly on one pixel row (crisp, 1 point), and its label / '+' are centred on it
        pos = QtCore.QPointF(pos.x(), math.floor(pos.y()) + 0.5)
        self._cross_vp = self.glw.mapFromScene(pos)
        if abs(float(self.glw.devicePixelRatioF()) - getattr(self, "_xh_dpr", 1.0)) > 0.01:
            self._apply_xhair_pen()                         # window moved to a screen with another pixel ratio
        if not refresh:
            self._legend_hover(self._cross_vp)
        plots = self._plots()
        hovered = next((p for p in plots if p.isVisible() and p.sceneBoundingRect().contains(pos)), None)
        if hovered is None:
            for p in plots:
                p.ytag.hide()
                p.dtag.hide()
                p.hline.hide()
            self._set_plus_hot(False)
            self.plus_btn.hide()
            return
        mp = hovered.vb.mapSceneToView(pos)
        over_plus = self._over_plus(self._cross_vp)
        self._set_plus_hot(over_plus)
        if hovered is self.price and not self.layer.tool and not self._panning():
            if over_plus:
                cur = QtCore.Qt.CursorShape.PointingHandCursor
            else:
                cur = (QtCore.Qt.CursorShape.PointingHandCursor if self.layer.drawings and self.layer.hover_hit(mp.x(), mp.y())
                       else QtCore.Qt.CursorShape.CrossCursor)
            if cur != getattr(self, "_cursor_shape", None):
                self._cursor_shape = cur
                self.glw.viewport().setCursor(cur)
        i = int(round(mp.x()))
        if getattr(self, "_vlock", None) is not None:
            i = int(self._vlock)                         # locked by time: the line stays on that bar
        n = self._n()
        ix = float(i)                                    # TradingView: the vertical line snaps to the bar
        new_bar = i != getattr(self, "_hover_i", None)
        self._hover_i = i
        for p in plots:
            if new_bar:
                p.vline.setPos(ix)
            p.vline.show()
            p.hline.setVisible(p is hovered)
            p.ytag.setVisible(p is hovered)
        yv = self._plus_y if over_plus else mp.y()       # on the "+" the line stays where the "+" is
        hovered.hline.setPos(yv)
        xr = hovered.vb.viewRange()[0][1]
        hovered.ytag.setText(f" {self._scale_text(hovered, yv)} ")
        hovered.ytag.setPos(xr, yv)
        if not over_plus:
            self._place_plus(hovered, yv)
        bp = self._bottom_plot()
        t = self._time_at(i) if i >= 0 else None
        if t is not None:
            if new_bar or bp.dtag.isHidden() or refresh:  # header / date tag only change when the bar changes
                if 0 <= i < n:
                    self._update_header(i)
                elif new_bar:
                    self._update_header(n - 1)          # right of the newest bar: the legend shows the last bar
                for o in plots:
                    if o is not bp:
                        o.dtag.hide()
                bp.dtag.setText(f" {self._crosshair_time(t)} ")
                bp.dtag.setPos(ix, bp.vb.viewRange()[1][0])
                bp.dtag.show()
        else:
            for o in plots:
                o.dtag.hide()
        if hovered is self.price and not over_plus:
            x, y = (self.snap(mp.x(), mp.y()) if self.layer.tool else (mp.x(), mp.y()))
            self.layer.handle_move(x, y)

    def _scale_text(self, plot, v: float) -> str:
        if plot is self.price:
            return self.axis_label(v)
        pn = self._pane_for_plot(plot)
        st = STD.STUDIES.get(pn.keys[0]) if pn is not None and pn.keys else None
        return self._fmt_study(st, v) if st is not None else self.axis_price(v)

    def _crosshair_time(self, t) -> str:
        """TradingView's time label: 'Tue 17 Nov '26   14:29' (the date the way Settings says, 24 / 12 h)."""
        ax = self.axes[0]
        datefmt = getattr(ax, "datefmt", "%d %b '%y")
        d = t.strftime(("%a " if getattr(ax, "dow", True) else "") + datefmt)
        if ax.intraday:
            return d + "   " + t.strftime("%H:%M" if getattr(ax, "h24", True) else "%I:%M %p")
        return d

    def _time_at(self, i: int):
        """Time of bar i - also to the right of the newest bar, where TradingView keeps the time scale going."""
        if self.df is None or i < 0:
            return None
        n = self._n()
        if i < n:
            return self.df.index[i]
        if self._limit is not None:                      # replay: the future is the real next bars
            return self.df.index[i] if i < len(self.df) else None
        fut = getattr(self.axes[0], "_future", None)
        k = i - len(self.df)
        return fut[k] if fut is not None and 0 <= k < len(fut) else None

    # ------------------------------------------------------------------ the "+" next to the price scale
    def _over_plus(self, vp_pos) -> bool:
        b = self.plus_btn
        return vp_pos is not None and not b.isHidden() and b.geometry().adjusted(-2, -2, 2, 2).contains(vp_pos)

    def _set_plus_hot(self, on: bool):
        if on != self.plus_btn_hot:
            self.plus_btn_hot = on
            self.plus_btn.setPixmap(_plus_pixmap(on))

    def _plus_menu(self, price: float):
        """TradingView's '+': alert / horizontal line at this price."""
        menu = QtWidgets.QMenu(self)
        fp = self.fp(price)
        menu.addAction(icons.get("bell"), f"Add alert on {self.ticker() or 'symbol'} at {fp}",
                       lambda: self.alertRequested.emit(float(price)))
        menu.addAction(icons.get("hline"), f"Draw horizontal line at {fp}",
                       lambda: self.layer.add("hline", [[self.x_to_time(self._n() - 1), float(price)]]))
        menu.addSeparator()
        menu.addAction(f"Copy price {fp}", lambda: QtWidgets.QApplication.clipboard().setText(fp.replace(",", "")))
        menu.exec(QtGui.QCursor.pos())

    def set_alert_lines(self, items):
        """items: [(price, text)] drawn as dashed orange lines with a bell label."""
        for ln in getattr(self, "_alert_lines", []):
            self.price.removeItem(ln)
        for tg in getattr(self, "_alert_tags", []):
            tg.deleteLater()
        self._alert_lines, self._alert_tags = [], []
        self._alert_items = list(items)
        for price, text in items:
            ln = pg.InfiniteLine(pos=float(self.ty(price)), angle=0, movable=False,
                                 pen=pg.mkPen("#ff9800", width=1, style=QtCore.Qt.PenStyle.DashLine))
            ln.setZValue(20)
            ln.setToolTip(f"Alert: {text}")
            self.price.addItem(ln, ignoreBounds=True)
            self._alert_lines.append(ln)
            tg = AxisTag(self, self.price, "y", "#ff9800")       # TradingView: an orange label on the price scale
            tg._price = price
            tg.setToolTip(text)
            tg.setText(f"\u23F0 {self.axis_price(price)}")
            self._alert_tags.append(tg)
        self._place_alert_tags()

    def make_axis_tag(self, fill: str) -> "AxisTag":
        """A label on the price scale (used by drawings: a horizontal line shows its price there)."""
        return AxisTag(self, self.price, "y", fill)

    def _place_alert_tags(self):
        tags = getattr(self, "_alert_tags", [])
        if not tags or self.df is None:
            return
        xr = self.price.vb.viewRange()[0][1]
        (y0, y1) = self.price.vb.viewRange()[1]
        for tg in tags:
            y = float(self.ty(tg._price))
            inside = y0 <= y <= y1
            tg.setVisible(inside)
            if inside:
                tg.setPos(xr, y)
        self.last_tag.raise_()

    # ------------------------------------------------------------------ orders and positions on the chart
    def set_trade_lines(self, items):
        """TradingView's order / position lines. items: dicts with kind ('order' | 'position'), price, qty, side,
        type, pnl, obj (the broker's Order / Position), movable. An order line can be dragged to a new price and
        cancelled with its x; a position line shows the open P&L and closes with its x."""
        sig = [(it.get("kind"), it.get("id"), round(float(it["price"]), 6), it.get("qty"), it.get("type"),
                round(float(it.get("pnl") or 0), 2), bool(it.get("movable"))) for it in items]
        if sig == getattr(self, "_trade_sig", None):
            return
        if any(ln.moving for ln, _t, _l, _i in getattr(self, "_trade_objs", [])):
            return                                           # never pull a line away from under the mouse
        self._trade_sig = sig
        for ln, tag, lab, _it in getattr(self, "_trade_objs", []):
            self.price.removeItem(ln)
            tag.deleteLater()
            lab.deleteLater()
        self._trade_objs = []
        for it in items:
            order = it.get("kind") == "order"
            buy = (it.get("side") == "BUY") if order else (it.get("qty", 0) > 0)
            col = "#2962ff" if buy else theme.DOWN
            pen = pg.mkPen(col, width=1, style=QtCore.Qt.PenStyle.DashLine if order else QtCore.Qt.PenStyle.SolidLine)
            ln = pg.InfiniteLine(pos=float(self.ty(float(it["price"]))), angle=0,
                                 movable=bool(order and it.get("movable")), pen=pen, hoverPen=pg.mkPen(col, width=2))
            ln.setZValue(25)
            ln.sigPositionChanged.connect(lambda *_a, l=ln: self._trade_line_dragging(l))
            ln.sigPositionChangeFinished.connect(lambda *_a, l=ln, i=it: self._trade_line_moved(l, i))
            self.price.addItem(ln, ignoreBounds=True)
            tag = AxisTag(self, self.price, "y", col)
            tag._price = float(it["price"])
            tag.setText(self.axis_price(float(it["price"])))
            lab = TradeLabel(self, it, col)
            self._trade_objs.append((ln, tag, lab, it))
        self._place_trade_tags()

    def _trade_line_dragging(self, ln):
        for l2, tag, lab, _it in getattr(self, "_trade_objs", []):
            if l2 is ln:
                px = float(self.iy(ln.value()))
                tag._price = px
                tag.setText(self.axis_price(px))
        self._place_trade_tags()

    def _trade_line_moved(self, ln, it):
        px = float(self.iy(ln.value()))
        if abs(px - float(it["price"])) > 1e-9:
            self.orderMoveRequested.emit(it.get("obj"), px)

    def _place_trade_tags(self):
        objs = getattr(self, "_trade_objs", [])
        if not objs or self.df is None:
            return
        g = self.glw
        vb = self.price.vb
        vr = vb.sceneBoundingRect()
        vt = g.viewportTransform()
        xr = vb.viewRange()[0][1]
        (y0, y1) = vb.viewRange()[1]
        ax = axis_rect(self.price.getAxis("right"))
        for ln, tag, lab, _it in objs:
            y = float(ln.value())
            inside = y0 <= y <= y1
            tag.setVisible(inside)
            lab.setVisible(inside)
            if not inside:
                continue
            tag._price = float(self.iy(y))
            tag.setPos(xr, y)
            sy = vb.mapViewToScene(QtCore.QPointF(xr, y)).y()
            sy = min(max(sy, vr.top() + lab.height() / 2), vr.bottom() - lab.height() / 2)
            pt = vt.map(QtCore.QPointF(ax.left(), sy))
            lab.move(int(pt.x()) - lab.width() - 56, int(math.floor(pt.y())) - (lab.height() - 1) // 2)
            lab.raise_()
        self.last_tag.raise_()

    def effective_df(self):
        """Data a strategy is allowed to see: everything, or only the bars revealed so far in replay."""
        return self.df if (self._limit is None or self.df is None) else self.df.iloc[: self._limit]

    def _panning(self) -> bool:
        return bool(QtWidgets.QApplication.mouseButtons() & QtCore.Qt.MouseButton.LeftButton)

    def _time_menu(self):
        """Right-click on the time scale."""
        menu = QtWidgets.QMenu(self)
        menu.addAction("Go to date\u2026\t" + theme.sc("Alt+G"), self.gotoDateRequested.emit)
        menu.addAction("Scroll to the most recent bar\t" + theme.sc("Alt+Shift+Right"), self.scroll_to_latest)
        menu.addAction("Reset time scale", self.go_latest)
        menu.addSeparator()
        menu.addAction("Settings\u2026", self.settingsRequested.emit)
        menu.exec(QtGui.QCursor.pos())

    def _context_menu(self, ev):
        mp = self.price.vb.mapSceneToView(ev.scenePos())
        menu = QtWidgets.QMenu(self)
        hit = self.layer._hit_test(mp.x(), mp.y())
        if hit is not None:
            from .drawbar import COLORS, NO_LINE, STYLE_NAMES, WIDTHS, line_icon, swatch
            self.layer.selected = hit
            self.layer.rebuild()
            d = self.layer.get(hit)
            menu.addAction("Settings\u2026", lambda h=hit: self.edit_drawing(h))
            menu.addAction("Clone", self.layer.duplicate_selected)
            menu.addAction("Copy\t" + theme.sc("Ctrl+C"), self.layer.copy_selected)
            col = menu.addMenu("Colour")
            for name, c in COLORS:
                col.addAction(swatch(c), name, lambda c=c, h=hit: self.layer.set_color(h, c))
            if d["type"] not in NO_LINE:
                wm = menu.addMenu("Line width")
                for w in WIDTHS:
                    wm.addAction(line_icon(w), f"{w:g} px", lambda w=w, h=hit: self.layer.set_width(h, w))
                sm = menu.addMenu("Line style")
                for key, lab in STYLE_NAMES:
                    sm.addAction(line_icon(2, key), lab, lambda k=key, h=hit: self.layer.set_style(h, k))
            lk = menu.addAction("Lock")
            lk.setCheckable(True)
            lk.setChecked(bool(d.get("lock")))
            lk.toggled.connect(lambda on, h=hit: self.layer.set_locked(h, on))
            menu.addAction("Remove\tDel", self.layer.delete_selected)
            menu.addSeparator()
        price = float(self.iy(mp.y()))
        ptxt = self.fp(price)                                 # in the symbol's own precision (TradingView)
        self._menu_tv(menu, price, ptxt, mp)
        menu.exec(QtGui.QCursor.pos())

    def _menu_tv(self, menu, price: float, ptxt: str, mp):
        """TradingView's chart menu, item for item (measured on tradingview.com)."""
        from .drawings import _CLIP
        menu.addAction(f"Copy price {ptxt}", lambda: QtWidgets.QApplication.clipboard().setText(ptxt.replace(",", "")))
        pa = menu.addAction("Paste\t" + theme.sc("Ctrl+V"), self.layer.paste)
        pa.setEnabled(bool(_CLIP))
        menu.addSeparator()
        sym = self.ticker() or "symbol"
        menu.addAction(icons.get("bell"), f"Add alert on {sym} at {ptxt}\u2026\t{theme.sc('Alt+A')}",
                       lambda: self.alertRequested.emit(float(price)))
        last = float(self.df["Close"].iloc[-1]) if self.df is not None and len(self.df) else price
        below = price < last
        buy_t, sell_t = ("LIMIT", "SL-M") if below else ("SL-M", "LIMIT")
        word = {"LIMIT": "limit", "SL-M": "stop"}
        menu.addAction(icons.get("buy_order"), f"Buy 1 {sym} @ {ptxt} {word[buy_t]}\t{theme.sc('Alt+Shift+B')}",
                       lambda: self.orderAtPrice.emit("BUY", buy_t, float(price), True))
        menu.addAction(icons.get("sell_order"), f"Sell 1 {sym} @ {ptxt} {word[sell_t]}\t{theme.sc('Alt+Shift+S')}",
                       lambda: self.orderAtPrice.emit("SELL", sell_t, float(price), True))
        menu.addAction(icons.get("order_add"), f"Add order on {sym} at {ptxt}\u2026\t{theme.sc('Shift+T')}",
                       lambda: self.orderAtPrice.emit("BUY", buy_t, float(price), False))
        menu.addSeparator()
        locked = getattr(self, "_vlock", None) is not None
        menu.addAction("Unlock vertical cursor line" if locked else "Lock vertical cursor line by time",
                       lambda: self.lock_vline(None if locked else int(round(mp.x()))))
        menu.addSeparator()
        menu.addAction("Table view", self.table_view)
        menu.addAction("Object tree", self.object_tree)
        n_ind = len(getattr(self, "_enabled", ()))
        n_draw = len(self.layer.drawings)
        if n_ind or n_draw:
            menu.addSeparator()
        if n_ind:
            menu.addAction(f"Remove {n_ind} indicator" + ("s" if n_ind > 1 else ""), self.remove_all_indicators)
        if n_draw:
            menu.addAction(f"Remove {n_draw} drawing" + ("s" if n_draw > 1 else ""), self.layer.clear)
        menu.addSeparator()
        menu.addAction(icons.get("gear"), "Settings\u2026", self.settingsRequested.emit)

    def lock_vline(self, i):
        """TradingView's 'Lock vertical cursor line by time': the vertical line stays on that bar."""
        self._vlock = i
        if i is not None:
            for pl in self._plots():
                pl.vline.setPos(float(i))
                pl.vline.show()
            self._hover_i = None

    def remove_all_indicators(self):
        for k in list(getattr(self, "_ind_order", [])):
            if k in self._enabled:
                self.set_indicator(k, False)
                self.indicatorToggled.emit(k, False)

    def table_view(self):
        """TradingView's Table view: every bar of the chart in a table, newest first."""
        if self.df is None or not len(self.df):
            return
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"Table view \u00b7 {self.ticker() or ''}")
        dlg.resize(760, 560)
        v = QtWidgets.QVBoxLayout(dlg)
        v.setContentsMargins(10, 10, 10, 10)
        df = self.df.iloc[::-1]
        cols = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df.columns]
        extra = []
        for k in getattr(self, "_ind_order", []):
            if k in getattr(self, "_enabled", ()):
                for _name, item in getattr(self, "_ind_items", {}).get(k, []):
                    y = getattr(item, "yData", None)
                    if y is not None and len(y) == len(self.df):
                        extra.append((STD.STUDIES[k].title if k in STD.STUDIES else k, np.asarray(y)[::-1]))
        tbl = QtWidgets.QTableView()
        model = _TableModel(df, cols, extra, self)
        tbl.setModel(model)
        tbl.verticalHeader().hide()
        tbl.verticalHeader().setDefaultSectionSize(26)
        tbl.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        tbl.setAlternatingRowColors(True)
        tbl.setShowGrid(False)
        v.addWidget(tbl)
        dlg.exec()

    def object_tree(self):
        """TradingView's Object tree: the symbol, its indicators and drawings; each can be removed here."""
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Object tree")
        dlg.resize(380, 460)
        v = QtWidgets.QVBoxLayout(dlg)
        v.setContentsMargins(12, 12, 12, 12)
        tree = QtWidgets.QTreeWidget()
        tree.setHeaderHidden(True)
        tree.setColumnCount(2)
        tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        tree.header().setStretchLastSection(False)
        v.addWidget(tree)

        def fill():
            tree.clear()
            root = QtWidgets.QTreeWidgetItem([self.display_name() if self.df is not None else "Chart", ""])
            tree.addTopLevelItem(root)
            for k in list(getattr(self, "_ind_order", [])):
                if k not in getattr(self, "_enabled", ()):
                    continue
                it = QtWidgets.QTreeWidgetItem([STD.STUDIES[k].title if k in STD.STUDIES else k, ""])
                root.addChild(it)
                b = QtWidgets.QToolButton()
                b.setIcon(icons.get("trash"))
                b.setToolTip("Remove")
                b.setAutoRaise(True)
                b.clicked.connect(lambda _c=False, key=k: (self.set_indicator(key, False),
                                                           self.indicatorToggled.emit(key, False), fill()))
                tree.setItemWidget(it, 1, b)
            for d in list(self.layer.drawings):
                it = QtWidgets.QTreeWidgetItem([str(d.get("type", "drawing")).replace("hline", "Horizontal line")
                                                .replace("trend", "Trend line").title(), ""])
                root.addChild(it)
                b = QtWidgets.QToolButton()
                b.setIcon(icons.get("trash"))
                b.setToolTip("Remove")
                b.setAutoRaise(True)

                def rm(_c=False, did=d.get("id")):
                    self.layer.selected = did
                    self.layer.delete_selected()
                    fill()
                b.clicked.connect(rm)
                tree.setItemWidget(it, 1, b)
            root.setExpanded(True)
        fill()
        dlg.exec()

    def _crosshair_off(self):
        """Mouse left the chart: hide the crosshair and its tags, legend shows the last bar again (TradingView)."""
        if self.glw.viewport().underMouse():
            return
        self._set_plus_hot(False)
        for p in self._plots():
            p.vline.hide()
            p.hline.hide()
            p.ytag.hide()
            p.dtag.hide()
        self.plus_btn.hide()
        self._hover_i = None
        self._cross_vp = self._cross_key = None
        if self.df is not None and self._n():
            self._update_header(self._n() - 1)

    def _place_plus(self, hovered, y):
        """The '+' box sits against the price scale, on the same row as the crosshair's price label (1 px apart),
        both centred on the line - TradingView draws them as one unit."""
        if hovered is not self.price or not settings.get().get("plus_button", True) or self.layer.tool:
            self.plus_btn.hide()
            return
        tag = self.price.ytag
        ax = axis_rect(self.price.getAxis("right"))
        axl = int(round(self.glw.viewportTransform().map(QtCore.QPointF(ax.left(), 0)).x()))
        self._plus_y = y
        b = self.plus_btn
        b.move(axl - b.width(), tag.y() + (tag.height() - b.height()) // 2)
        b.show()
        self.plus_btn.raise_()
        self.corners.raise_()

    def _axis_menu(self):
        """TradingView's price-scale right-click menu."""
        S = settings.get()
        vb = self.price.vb
        menu = QtWidgets.QMenu(self)
        a = menu.addAction("Auto (fits data to screen)")
        a.setCheckable(True)
        a.setChecked(self.auto_y)
        a.toggled.connect(lambda on: (self.set_auto_y(on), self._autoscale()))
        menu.addSeparator()
        for lbl, md, sc in (("Regular", "regular", ""), ("Percent", "percent", "Alt+P"), ("Logarithmic", "log", "Alt+L")):
            ma = menu.addAction(lbl + (f"\t{theme.sc(sc)}" if sc else ""))
            ma.setCheckable(True)
            ma.setChecked(self.scale_mode == md)
            ma.triggered.connect(lambda _=False, md=md: self.set_scale_mode(md))
        menu.addSeparator()
        inv = menu.addAction("Invert scale\t" + theme.sc("Alt+I"))
        inv.setCheckable(True)
        inv.setChecked(bool(vb.state["yInverted"]))
        inv.toggled.connect(lambda on: vb.invertY(on))
        menu.addSeparator()
        for key, label in (("last_label", "Symbol last value label"), ("last_line", "Symbol last price line"),
                           ("countdown", "Countdown to bar close")):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(bool(S[key]))
            act.toggled.connect(lambda on, k=key: (S.__setitem__(k, on), self.apply_settings()))
        menu.addSeparator()
        menu.addAction("More settings\u2026", self.settingsRequested.emit)
        menu.exec(QtGui.QCursor.pos())

    def _mouse_clicked(self, ev):
        if self.df is not None and ev.button() == QtCore.Qt.MouseButton.LeftButton and \
                self._over_plus(self.glw.mapFromScene(ev.scenePos())):
            price = float(self.iy(self._plus_y))
            QtCore.QTimer.singleShot(0, lambda: self._plus_menu(price))
            return
        if self.df is not None and ev.button() == QtCore.Qt.MouseButton.RightButton and \
                axis_rect(self.price.getAxis("right")).contains(ev.scenePos()):
            self._axis_menu()
            return
        if self.df is not None and ev.button() == QtCore.Qt.MouseButton.RightButton and \
                any(axis_rect(a).contains(ev.scenePos()) for a in self.axes if a.isVisible()):
            self._time_menu()
            return
        if self.df is not None and ev.button() == QtCore.Qt.MouseButton.RightButton and \
                self.price.sceneBoundingRect().contains(ev.scenePos()):
            self._context_menu(ev)
            return
        if self.df is None or ev.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        if ev.double():                                  # double-click an axis = reset that scale
            ax = axis_rect(self.price.getAxis("right"))
            if ax.contains(ev.scenePos()):
                self.set_auto_y(True)
                self._autoscale()
                return
            if any(axis_rect(a).contains(ev.scenePos()) for a in self.axes if a.isVisible()):
                self.go_latest()
                return
        if not self.price.sceneBoundingRect().contains(ev.scenePos()):
            return
        mp = self.price.vb.mapSceneToView(ev.scenePos())
        if self.replay["picking"]:
            self.replay_start(int(round(mp.x())))
            return
        if ev.double() and not self.layer.tool:              # double-click a drawing: its settings
            hit = self.layer._hit_test(mp.x(), mp.y())
            if hit is not None:
                self.layer.selected = hit
                self.layer.rebuild()
                QtCore.QTimer.singleShot(0, lambda h=hit: self.edit_drawing(h))
                return
        if (ev.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier) and not self.layer.tool \
                and self.layer._hit_test(mp.x(), mp.y()) is None:
            self.layer.set_tool("measure")                   # TradingView: Shift + click measures from here
            self.toolChanged.emit("measure")
        x, y = self.snap(mp.x(), mp.y()) if self.layer.tool else (mp.x(), mp.y())
        self.layer.handle_click(x, y)

    def interval_tag(self) -> str:
        if self.df is None or len(self.df) < 2:
            return "1D"
        key = (id(self.df), len(self.df))
        hit = getattr(self, "_itag", None)
        if hit is not None and hit[0] == key:
            return hit[1]
        tag = self._interval_tag()
        self._itag = (key, tag)
        return tag

    def _interval_tag(self) -> str:
        sec = float(np.median(np.diff(self.df.index.values[-50:]).astype("timedelta64[s]").astype(float)))
        if sec < 3600:
            return f"{max(int(round(sec / 60)), 1)}"          # TradingView's legend: 'Reliance … · 5 · NSE'
        if sec < 86400 * 0.9:
            return f"{int(round(sec / 3600))}h"
        if sec < 86400 * 3:
            return "1D"
        if sec < 86400 * 20:
            return "1W"
        return "1M"

    def _raw_symbol(self) -> str:
        raw = (self.label or "").split(" \u00b7 ")[0].strip()
        return raw.split()[0] if raw else ""

    def ticker(self) -> str:
        """What you typed, not the data vendor's code: NIFTY for '^NSEI', RELIANCE for 'RELIANCE.NS'."""
        t = self._raw_symbol()
        from . import data as _d
        u = t.upper()
        for name, code in _d._INDEX.items():
            if code == u:
                return name
        for suf in (".NS", ".BO"):
            if u.endswith(suf):
                return t[: -len(suf)]
        return t.lstrip("^")

    def display_name(self) -> str:
        """TradingView's legend shows the description: 'Nifty 50 Index', 'Reliance Industries Ltd'."""
        t = self.ticker()
        o = self._option()
        if o:
            from .dhan_core import option_name
            return option_name(*o)
        try:
            from .tvui import symbol_name
            return symbol_name(t) or t
        except Exception:  # noqa: BLE001
            return t

    def _option(self):
        """(underlying, expiry, strike, kind) when this chart is an option contract, else None."""
        u = self._raw_symbol().upper()
        hit = getattr(self, "_opt_memo", None)
        if hit and hit[0] == u:
            return hit[1]
        out = None
        try:
            from .dhan_core import _TV_OPT
            import datetime as _dt
            m = _TV_OPT.match(u)
            if m:
                out = (m[1], _dt.date(2000 + int(m[2]), int(m[3]), int(m[4])), float(m[6]), "CE" if m[5] == "C" else "PE")
        except Exception:  # noqa: BLE001
            out = None
        self._opt_memo = (u, out)
        return out

    def exchange_tag(self) -> str:
        u = self._raw_symbol().upper()
        if not u:
            return ""
        o = self._option()
        if o:
            return "BSE" if o[0] in ("SENSEX", "BANKEX") else "NSE"
        if u.endswith(".BO") or u == "^BSESN":
            return "BSE"
        if u.endswith("=X"):
            return "FX"
        if u.endswith("=F"):
            return "FUTURES"
        if re.fullmatch(r"[A-Z0-9]{2,10}-(USD|USDT|INR|EUR|BTC)", u):
            return "CRYPTO"
        if "." in u and not u.endswith(".NS"):
            return u.split(".")[-1]
        return "NSE"

    def data_window_html(self, i: int) -> str:
        """TradingView's Data Window: the bar under the crosshair and every indicator / strategy value at it,
        labels left, values right-aligned to the panel edge, values in the same colours as the legend."""
        df = self.df
        if df is None or not 0 <= i < self._n():
            return ""
        o, h, lo, c = (float(a[i]) for a in (self._opens, self._highs, self._lows, self._closes))
        prev = float(self._closes[i - 1]) if i > 0 else o
        d = c - prev
        ccol = theme.UP if c >= o else theme.DOWN            # same rule as the legend at the top of the chart
        dcol = theme.UP if d >= 0 else theme.DOWN
        vol = float(self._vols[i])
        vs = theme.fmt_vol(vol)
        fmt = "%a %d %b '%y %H:%M" if self.axes[0].intraday else "%a %d %b '%y"
        rows = [("Date", df.index[i].strftime(fmt), "#dbdbdb"),
                ("Open", self.fp(o), ccol), ("High", self.fp(h), ccol), ("Low", self.fp(lo), ccol),
                ("Close", self.fp(c), ccol),
                ("Change", f"{self.fd(d, ref=c)} ({_signed((c / prev - 1) * 100 if prev else 0)}%)", dcol),
                ("Volume", vs, "#dbdbdb")]
        title = f"{self.ticker()} \u00b7 {self.interval_tag()} \u00b7 {self.exchange_tag()}"
        sections = [(title, rows)]
        ind = []
        rows = list(getattr(self, "_legend_rows", []))
        for pn in self.panes:
            rows += pn.rows
        for _key, name, vals, st in rows:
            parts = []
            for arr, offs, col in vals:
                j = i - offs
                v = arr[j] if arr is not None and 0 <= j < len(arr) else np.nan
                if np.isfinite(v):
                    parts.append(f"<span style='color:{col}'>{self._fmt_study(st, v)}</span>")
            ind.append((name, "&nbsp;&nbsp;".join(parts) or "<span style='color:#5d606b'>\u2014</span>", None))
        if ind:
            sections.append(("Indicators", ind))
        out = ["<table width='100%' cellspacing=0 cellpadding=3>"]
        for k, (head, rws) in enumerate(sections):
            pad = "" if k == 0 else " style='padding-top:12px'"
            out.append(f"<tr><td colspan=2{pad}><b style='color:#ffffff'>{head}</b></td></tr>")
            for lab, val, col in rws:
                v = val if col is None else f"<span style='color:{col}'>{val}</span>"
                out.append(f"<tr><td style='color:#787b86'>{lab}</td><td align=right>{v}</td></tr>")
        out.append("</table>")
        return "".join(out)

    def market_status(self) -> str:
        """'open' / 'pre' / 'closed' for the charted market ('' when unknown)."""
        ex = self.exchange_tag()
        probe = {"NSE": "X", "BSE": "BSE:X", "CRYPTO": "X-USD"}.get(ex)
        if probe is None:
            return ""
        from .watchlist import market_state
        return market_state(probe)[0]

    def _session_close(self):
        """UTC time the charted market's session ends today (None: closed / unknown)."""
        probe = {"NSE": "X", "BSE": "BSE:X", "CRYPTO": "X-USD"}.get(self.exchange_tag())
        if probe is None:
            return None
        from .watchlist import session_close
        return session_close(probe)

    def _exchange_today(self):
        """Today's date where the charted market trades (a daily bar is dated in exchange time)."""
        import datetime as _dt
        utc = _dt.datetime.now(_dt.timezone.utc)
        if self.exchange_tag() == "CRYPTO":
            return utc.date()
        return (utc + _dt.timedelta(hours=5, minutes=30)).date()

    def _market_dot(self) -> str:
        """TradingView's market-status pill after the title ('Closed' / 'Pre-market'; nothing while open)."""
        pill = status_pill_html(self.market_status(), settings.get().get("bg", "#000000"))
        return f"&nbsp;&nbsp;{pill}" if pill else ""

    def _refresh_header(self):
        if self.df is not None and self._n():
            self._update_header(min(getattr(self, '_hdr_i', self._n() - 1), self._n() - 1))

    def _update_header(self, i: int):
        self._hdr_i = i
        self.barHovered.emit(i)
        self._update_trade_buttons()
        o, h, lo, c = (float(a[i]) for a in (self._opens, self._highs, self._lows, self._closes))
        r = {"Open": o, "High": h, "Low": lo, "Close": c, "Volume": float(self._vols[i])}
        prev = float(self._closes[i - 1]) if i > 0 else o
        d = c - prev
        chg = (c / prev - 1) * 100 if prev else 0.0
        col = theme.UP if c >= o else theme.DOWN
        S = settings.get()
        fp = self.fp
        m = theme.MUTED
        v = lambda k, x: (f"<span style='color:{LEGEND_TEXT}'>{k}</span><span style='color:{col}'>{fp(x)}</span>")  # noqa: E731
        title = (f"{legend_logo_html(self.ticker())}&nbsp;"
                 f"<span style='font-size:16px;color:{LEGEND_TEXT}'>{self.display_name()}</span>"
                 f"<span style='font-size:16px;color:{LEGEND_TEXT}'> \u00b7 {self.interval_tag()} \u00b7 {self.exchange_tag()}</span>"
                 f"{self._market_dot()}&nbsp;&nbsp;&nbsp;") if S["sl_title"] else ""
        vals = (f"{v('O', r['Open'])}&nbsp;&nbsp;{v('H', r['High'])}&nbsp;&nbsp;{v('L', r['Low'])}&nbsp;&nbsp;{v('C', r['Close'])}"
                if S["sl_values"] else "")
        chgt = (f"&nbsp;&nbsp;<span style='color:{col}'>{self.fd(d, ref=c)} ({_signed(chg)}%)</span>"
                if S["sl_change"] else "")
        self.header.setText(title + vals + chgt)
        avail = int(self.price.vb.sceneBoundingRect().width()) - 24
        if avail > 50 and self.header.width() > avail:          # narrow pane: drop parts like TradingView
            self.header.setText(title + (f"{v('C', r['Close'])}" if S["sl_values"] else "") + chgt)
            if self.header.width() > avail:
                self.header.setText(title + (f"{v('C', r['Close'])}" if S["sl_values"] else ""))
            if self.header.width() > avail:
                self.header.setText(title)
        self.header.setVisible(bool(title or vals or chgt))
        vol = float(r["Volume"])
        vs = theme.fmt_vol(vol)
        vcol = vol_color(c >= o).name()                       # TradingView: the volume's own colour
        self.vol_label.setText(f"<span style='color:{LEGEND_TEXT}'>Vol</span> <span style='color:{vcol}'>{vs}</span>")
        self.vol_label.setVisible(self._show_vol_legend())
        self._update_ind_legend(i)
        hs = (self.header.height(), self.header.isHidden(), self.vol_label.isHidden(), self.ind_label.height(),
              self.ind_label.isHidden(), tuple(pn.label.height() for pn in self.panes))
        if hs != getattr(self, "_hdr_shape", None):        # only re-layout overlays when the legend's shape changed
            self._hdr_shape = hs
            self._place_overlays()
