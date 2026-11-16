"""TradingView-style chart: candles / hollow / line / area, volume, RSI pane, overlays,
crosshair with price+date tags, last-price tag, drawing tools and trade markers."""
from __future__ import annotations

import re

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from . import icons
from . import indicators as ta
from . import settings, theme
from .drawings import DrawingLayer
from .gestures import TVViewBox

# pyqtgraph drops mouse-move events that arrive faster than 100/s; Mac trackpads / ProMotion screens send
# ~120/s, so crosshair and drags would skip. Qt already compresses moves when busy, so take every one.
pg.setConfigOption("mouseRateLimit", 0)
from .render import CandleItem, VolumeItem

_QWIDGETSIZE_MAX = 16777215
STYLES = ["candles", "hollow", "bars", "heikin", "line", "linemark", "step", "area", "baseline"]
INDICATOR_KEYS = ["sma20", "sma50", "sma200", "ema20", "ema50", "bb", "rsi"]


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


class PriceAxis(_DotGridMixin, pg.AxisItem):
    chart = None

    def tickStrings(self, values, scale, spacing):
        ch = self.chart
        if ch is not None and ch.df is not None:
            if ch.scale_mode == "percent":
                return [ch.axis_label(v) for v in values]
            return [ch.axis_price(float(ch.iy(v))) for v in values]
        return super().tickStrings(values, scale, spacing)

    """Right axis with TradingView-like tick density (a label every ~40 px)."""

    def tickSpacing(self, minVal, maxVal, size):
        span = maxVal - minVal
        if span <= 0 or size <= 0:
            return super().tickSpacing(minVal, maxVal, size)
        raw = span / max(size / 40.0, 2)
        mag = 10 ** np.floor(np.log10(raw))
        step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
        return [(step, 0)]


class DateAxis(_DotGridMixin, pg.AxisItem):
    """X axis that plots bar numbers (no weekend gaps, like TradingView) but shows dates."""

    def __init__(self, orientation="bottom", **kw):
        super().__init__(orientation, **kw)
        self._index = None
        self.intraday = False
        self.h24 = True
        self.dow = True
        self.datefmt = "%d %b '%y"

    def set_index(self, index: pd.DatetimeIndex):
        self._index = index
        self.intraday = bool(len(index)) and bool(((index.hour != 0) | (index.minute != 0)).any())
        # how "important" each bar is as a place for a time label (TradingView puts labels ON calendar boundaries:
        # a new year beats a new month beats a new day beats a round hour ...)
        n = len(index)
        w = np.zeros(n, dtype=np.int8)
        if n:
            y, mo, d = index.year.to_numpy(), index.month.to_numpy(), index.day.to_numpy()
            ch = lambda a: np.r_[True, a[1:] != a[:-1]]  # noqa: E731
            if self.intraday:
                h, mi = index.hour.to_numpy(), index.minute.to_numpy()
                w[mi % 5 == 0] = 1
                w[mi % 15 == 0] = 2
                w[mi % 30 == 0] = 3
                w[ch(h) | (mi == 0)] = 4
            newday = ch(d) | ch(mo) | ch(y)
            if self.intraday:
                w[newday] = 5
            w[ch(mo) | ch(y)] = 6
            w[ch(y)] = 7
            w[0] = max(int(w[0]), 0) if n > 1 else 0
            if n > 1:                                    # the very first bar is not a real boundary
                w[0] = 0
        self._weight = w
        self._levels = {int(k): np.flatnonzero(w >= k) for k in range(1, 8)}

    def tickValues(self, minVal, maxVal, size):
        idx = self._index
        if idx is None or not len(idx) or maxVal <= minVal or size <= 0:
            return super().tickValues(minVal, maxVal, size)
        n = len(idx)
        gap = max(1.0, 82.0 * (maxVal - minVal) / size)          # bars between two labels (~82 px apart)
        lo, hi = max(int(np.ceil(minVal)), 0), min(int(np.floor(maxVal)), n - 1)
        if hi < lo:
            return []
        chosen: list[int] = []
        for k in range(7, 0, -1):                                 # most important boundaries first
            cand = self._levels[k]
            a, b = np.searchsorted(cand, lo), np.searchsorted(cand, hi, side="right")
            if b - a == 0:
                continue
            self._place(chosen, cand[a:b], gap)
        if not self.intraday or not chosen:                       # fill what is left with ordinary bars
            self._place(chosen, None, gap, lo, hi)
        return [(gap, [float(v) for v in chosen])]

    @staticmethod
    def _place(chosen: list, cand, gap: float, lo: int = 0, hi: int = 0):
        """Add candidates that keep at least `gap` bars from every label already chosen (chosen stays sorted)."""
        import bisect
        pos = None
        j, m = 0, (len(cand) if cand is not None else 0)
        cur = lo
        while True:
            if cand is not None:
                if pos is not None:
                    j = int(np.searchsorted(cand, pos + gap))
                if j >= m:
                    return
                x = int(cand[j])
            else:
                x = cur if pos is None else int(np.ceil(pos + gap))
                if x > hi:
                    return
            i = bisect.bisect_left(chosen, x)
            left_ok = i == 0 or x - chosen[i - 1] >= gap
            right_ok = i == len(chosen) or chosen[i] - x >= gap
            if left_ok and right_ok:
                chosen.insert(i, x)
                pos = x
            else:                                               # blocked: jump past the label in the way
                nxt = (chosen[i - 1] if not left_ok else chosen[i]) + gap
                pos = max(nxt - gap, x + 1 - gap) if nxt > x else x + 1 - gap
                if cand is not None:
                    j = int(np.searchsorted(cand, max(nxt, x + 1)))
                    pos = None
                    if j >= m:
                        return

    def tickStrings(self, values, scale, spacing):
        idx, out = self._index, []
        w = getattr(self, "_weight", None)
        for v in values:
            i = int(round(v))
            if idx is None or i < 0 or i >= len(idx):
                out.append("")
                continue
            t = idx[i]
            k = int(w[i]) if w is not None and i < len(w) else 0
            if k >= 7:
                out.append(str(t.year))
            elif k == 6:
                out.append(t.strftime("%b"))
            elif self.intraday:
                out.append(str(t.day) if k == 5 else t.strftime("%H:%M" if self.h24 else "%I:%M %p"))
            else:
                out.append(str(t.day))
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
        self.setStyleSheet("background: transparent; font-size: 14px;")

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


def axis_rect(ax) -> QtCore.QRectF:
    """Scene rect of an axis' own strip. (sceneBoundingRect() also covers the grid lines = the whole plot.)"""
    return QtCore.QRectF(ax.mapToScene(QtCore.QPointF(0, 0)), QtCore.QSizeF(ax.width(), ax.height()))


class AxisTag(QtWidgets.QLabel):
    """A price / time label drawn ON the scale (like TradingView), not inside the plot.
    kind: 'y' = on the price scale at a price, 'below' = under the last-price tag (countdown),
    'x' = on the time scale at a bar."""

    def __init__(self, chart, plot, kind: str, fill: str, under=None):
        super().__init__(chart.glw)
        self.chart, self.plot, self.kind, self.under = chart, plot, kind, under
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._fill = fill
        self._pos = None
        self._restyle()
        self.hide()

    def _restyle(self):
        q = QtGui.QColor(self._fill)
        light = 0.2126 * q.redF() + 0.7152 * q.greenF() + 0.0722 * q.blueF() > 0.7   # e.g. a yellow / white line
        self.setStyleSheet(f"QLabel {{ background: {self._fill}; color: {'#000000' if light else '#ffffff'};"
                           f" font-size: 12px; font-weight: 600; padding: 2px 4px; border-radius: 2px; }}")

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
        if n != getattr(self, "_n", -1):          # digits are fixed-width: only re-measure when length changes
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
        if self.kind in ("y", "below"):
            ax = axis_rect(self.plot.getAxis("right"))
            sy = vb.mapViewToScene(QtCore.QPointF(x, y)).y()
            sy = min(max(sy, vr.top() + self.height() / 2), vr.bottom() - self.height() / 2)
            pt = g.mapFromScene(QtCore.QPointF(ax.left(), sy))
            self.setFixedWidth(max(int(ax.width()) - 2, self.sizeHint().width()))
            top = int(pt.y() - self.height() / 2)
            if self.kind == "below" and self.under is not None:
                top += self.under.height() + 1
            self.move(int(pt.x()) + 1, top)
        else:
            ax = axis_rect(self.plot.getAxis("bottom"))
            sx = vb.mapViewToScene(QtCore.QPointF(x, y)).x()
            pt = g.mapFromScene(QtCore.QPointF(sx, ax.top()))
            self.move(int(pt.x() - self.width() / 2), int(pt.y()) + 1)
        self.raise_()


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


class ChartWidget(QtWidgets.QWidget):
    replayChanged = QtCore.Signal()
    alertRequested = QtCore.Signal(float)
    activated = QtCore.Signal()
    toolChanged = QtCore.Signal(object)
    drawingsChanged = QtCore.Signal()
    tradeRequested = QtCore.Signal(str, float)
    settingsRequested = QtCore.Signal()
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
        self.replay = {"active": False, "picking": False, "playing": False, "speed": 1.0}
        self._enabled: set[str] = set()
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
        self.rsi_label = OverlayLabel(self.glw)
        self.ind_label = OverlayLabel(self.glw)
        self.ind_label.hide()
        self.vol_label.hide()
        self.rsi_label.hide()
        self.header.raise_()
        self.sell_btn = self._trade_button("SELL", theme.DOWN)
        self.buy_btn = self._trade_button("BUY", "#2962ff")
        self.spread_lbl = OverlayLabel(self.glw)
        self.spread_lbl.hide()
        self.collapse_btn = QtWidgets.QToolButton(self.glw)
        self.collapse_btn.setText("\u2303")
        self.collapse_btn.setFixedSize(38, 22)
        self.collapse_btn.setCheckable(True)
        self.collapse_btn.setToolTip("Hide / show the legend")
        self.collapse_btn.setStyleSheet("QToolButton { background:#000; border:1px solid #2b2b2b; border-radius:4px; color:#d1d4dc; font-size:14px; } QToolButton:hover { background:#1a1a1a; }")
        self.collapse_btn.toggled.connect(lambda _c: self._place_overlays())
        self.collapse_btn.hide()
        self.plus_btn = QtWidgets.QToolButton(self.glw)
        self.plus_btn.setText("+")
        self.plus_btn.setFixedSize(20, 20)
        self.plus_btn.setToolTip("Add alert at this price")
        self.plus_btn.setStyleSheet("QToolButton{background:#2a2a2a;color:#fff;border:1px solid #4a4a4a;border-radius:10px;font-size:15px;padding:0;} QToolButton:hover{background:#2962ff;border-color:#2962ff;}")
        self.plus_btn.clicked.connect(lambda: self.alertRequested.emit(float(self.iy(self._plus_y))))
        self.plus_btn.hide()
        self._plus_y = 0.0
        self.watermark = QtWidgets.QLabel(self.glw)
        self.watermark.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.watermark.setStyleSheet("background: transparent;")
        self.watermark.setPixmap(_watermark_pixmap())
        self.watermark.adjustSize()
        self.watermark.hide()

        self.axes: list[DateAxis] = []
        self.price = self._make_plot(0)
        self.vol = self._make_plot(1)
        self.rsi = self._make_plot(2)
        self.vol.setXLink(self.price)
        self.rsi.setXLink(self.price)
        self.price.getAxis("bottom").setStyle(showValues=False)

        grid = self.glw.ci.layout
        grid.setSpacing(2)
        grid.setRowStretchFactor(0, 14)
        grid.setRowStretchFactor(1, 2)
        grid.setRowStretchFactor(2, 3)
        self._set_rsi_visible(False)
        self.vol.setVisible(False)                      # volume is an overlay inside the price pane (like TradingView)
        grid.setRowFixedHeight(1, 0)
        self._refresh_axes()
        for pl in (self.price, self.vol, self.rsi):          # no meaningless 0..1 labels before data arrives
            pl.getAxis("right").setStyle(showValues=False)

        self._legend_rows: list = []                    # [(name, [(item, colour), ...])] shown under "Vol"

        # tags: last price, crosshair price (per pane) and crosshair date
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
        for p in (self.price, self.vol, self.rsi):
            p.ytag = AxisTag(self, p, "y", "#363a45")
        for p in (self.price, self.vol, self.rsi):
            p.dtag = AxisTag(self, p, "x", "#363a45")

        self.layer = DrawingLayer(self)
        self.layer.changed.connect(self.drawingsChanged)

        self.price.vb.sigXRangeChanged.connect(self._autoscale)
        self.price.vb.sigRangeChanged.connect(lambda *_: (self._place_last_tag(), self._place_alert_tags(),
                                                          self.layer.place_tags(), self._refresh_crosshair()))
        # labels on the scales are QWidgets: re-place them once pyqtgraph has finished laying the plots out
        # (a resize / layout change moves the axes AFTER the viewport's resize event)
        self._relayout_pending = False
        for sig in (self.price.getAxis("right").geometryChanged, self.price.vb.sigResized,
                    self.rsi.geometryChanged, self.vol.geometryChanged):
            sig.connect(self._schedule_relayout)
        self.glw.scene().sigMouseMoved.connect(self._mouse_moved)
        self.glw.scene().sigMouseClicked.connect(self._mouse_clicked)
        self.corners = CornerOverlay(self.glw)
        self.corners.show()
        self.glw.viewport().installEventFilter(self)
        self.glw.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.glw.viewport().setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

    def _trade_button(self, text, color):
        b = QtWidgets.QPushButton(text, self.glw)
        b.setFixedSize(80, 40)
        b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        b.setStyleSheet(f"QPushButton {{ background:#000; color:{color}; border:1px solid {color}; border-radius:5px; "
                        f"font-size:11px; padding:0; }} QPushButton:hover {{ background:{color}33; }}")
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
        sp = max(round(last * 0.0005, 2), 0.01)
        for b, px, t in ((self.sell_btn, last - sp / 2, "SELL"), (self.buy_btn, last + sp / 2, "BUY")):
            b.setText(f"{px:,.2f}\n{t}")
        self.spread_lbl.setText(f"<span style='color:#b8b8b8;font-size:12px'>{sp:.2f}</span>")

    # ------------------------------------------------------------------ construction helpers
    def _make_plot(self, row: int) -> pg.PlotItem:
        axis = DateAxis("bottom")
        self.axes.append(axis)
        p = self.glw.addPlot(row=row, col=0, axisItems={"bottom": axis, "right": PriceAxis("right")}, viewBox=TVViewBox(self, price=(row == 0)))
        p.setMenuEnabled(False)
        p.hideButtons()
        p.showAxis("right")
        p.hideAxis("left")
        p.getAxis("right").chart = self
        p.getAxis("right").setWidth(72)
        f = QtGui.QFont()
        f.setPixelSize(12)
        f.setWeight(QtGui.QFont.Weight.DemiBold)
        for name in ("bottom", "right"):
            ax = p.getAxis(name)
            gp = pg.mkPen("#262626", style=QtCore.Qt.PenStyle.DotLine)
            ax.setPen(pg.mkPen("#1a1a1a"))
            ax.setTextPen(pg.mkPen("#d1d4dc"))
            ax.setStyle(tickFont=f, tickLength=0, tickTextOffset=6)
            ax._chartlab_grid = gp
        p.getAxis('bottom').setHeight(28)
        p.getAxis('right').setTickDensity(2.2)
        p.getAxis('bottom').setTickDensity(1.2)
        p.showGrid(x=False, y=False)                 # the grid is drawn by the ViewBox (cheaper, see gestures.py)
        p.vb.plot_item = p
        dash = pg.mkPen("#868993", width=1, style=QtCore.Qt.PenStyle.DashLine)
        p.vline = pg.InfiniteLine(angle=90, movable=False, pen=dash)
        p.hline = pg.InfiniteLine(angle=0, movable=False, pen=dash)
        p.addItem(p.vline, ignoreBounds=True)
        p.addItem(p.hline, ignoreBounds=True)
        p.hline.hide()
        p.vline.hide()                             # (shown when the mouse is over the chart)
        return p

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
            for p in (self.price, self.vol, self.rsi):
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
        plot = next((p for p in (self.price, self.vol, self.rsi)
                     if p.isVisible() and p.vb.sceneBoundingRect().contains(pt)), None)
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
            for _, it, _n in items:
                f = getattr(it, "_full", None)
                if f is not None:
                    it.setData(f[0][:n], f[1][:n])
        for it, _name in self._strategy_items:
            f = getattr(it, "_full", None)
            if f is not None:
                it.setData(f[0][:n], f[1][:n])
        lim = len(self._lows) + 80 if self._limit is None else n + 60
        for p in (self.price, self.vol, self.rsi):
            p.vb.setLimits(xMin=-5, xMax=lim, minXRange=8)
        self._update_last_tag()
        self._update_header(max(n - 1, 0))
        self._autoscale()

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

    def _place_overlays(self):
        self.corners.setGeometry(self.glw.rect())
        self.corners.raise_()
        self._place_last_tag()
        self._place_alert_tags()
        self.layer.place_tags()
        if hasattr(self, "replay_bar") and not self.replay_bar.isHidden():
            self.replay_bar.place()
        if self.df is not None:                                  # volume legend sits under the OHLC header
            col = not self.collapse_btn.isChecked() and bool(settings.get()['sl_trade'])
            self.sell_btn.setVisible(col)
            self.buy_btn.setVisible(col)
            self.spread_lbl.setVisible(col)
            self.collapse_btn.show()
            self._apply_watermark_vis()
            y = self.header.y() + self.header.height() + 4
            if col:
                self.sell_btn.move(10, y)
                self.spread_lbl.move(96, y + 10)
                self.buy_btn.move(96 + self.spread_lbl.width() + 6, y)
                y += 46
            folded = self.collapse_btn.isChecked()
            show_vol = not folded and bool(settings.get()['sl_volume'])
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
        for plot, lab in ((self.rsi, self.rsi_label),):
            vis = plot.isVisible() and self.df is not None
            lab.setVisible(vis)
            if vis:
                tl = self.glw.mapFromScene(plot.sceneBoundingRect().topLeft())
                lab.move(int(tl.x()) + 10, int(tl.y()) + 3)
                lab.raise_()
        self.header.raise_()

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

    def _set_rsi_visible(self, on: bool):
        grid = self.glw.ci.layout
        self.rsi.setVisible(on)
        if on:
            grid.setRowMinimumHeight(2, 0)
            grid.setRowMaximumHeight(2, _QWIDGETSIZE_MAX)
        else:
            grid.setRowFixedHeight(2, 0)
        self._refresh_axes()

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

    def decimals(self) -> int:
        p = settings.get()["precision"]
        if p == "Integer":
            return 0
        if p != "Default":
            return int(p.split()[0])
        return 2

    def axis_price(self, x: float) -> str:
        return f"{x:,.{self.decimals()}f}"                 # 25,282.50 like TradingView's scale

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

    def fp(self, x: float) -> str:
        """Format a price with the precision chosen in Settings."""
        p = settings.get()["precision"]
        if p == "Integer":
            return f"{x:,.0f}"
        if p != "Default":
            return f"{x:,.{int(p.split()[0])}f}"
        return f"{x:,.2f}"

    def apply_settings(self):
        S = settings.get()
        self.glw.setBackground(S["bg"])
        for pl in (self.price, self.vol, self.rsi):
            pl.vb._gp_key = None
            pl.vb.update()
            f = QtGui.QFont()
            f.setPixelSize(int(S["text_size"]))
            f.setWeight(QtGui.QFont.Weight.DemiBold)
            for name in ("bottom", "right"):
                ax = pl.getAxis(name)
                ax.setPen(pg.mkPen(S["grid_color"]))
                ax.setTextPen(pg.mkPen(S["text_color"]))
                ax.setStyle(tickFont=f)
                ax.picture = None
                ax.update()
            dash = pg.mkPen(S["crosshair"], width=1, style=QtCore.Qt.PenStyle.DashLine)
            pl.vline.setPen(dash)
            pl.hline.setPen(dash)
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
        S = settings.get()
        f = QtGui.QFont()
        f.setPixelSize(int(S.get("text_size", 12)))
        f.setWeight(QtGui.QFont.Weight.DemiBold)
        fm = QtGui.QFontMetrics(f)
        widest = max(fm.horizontalAdvance(self.axis_price(float(np.nanmax(df["High"])) * 1.2)),
                     fm.horizontalAdvance("-100.00%"))
        w = int(max(64, widest + 22))
        for pl in (self.price, self.vol, self.rsi):
            if pl.getAxis("right").width() != w:
                pl.getAxis("right").setWidth(w)

    def _refresh_axes(self):
        owner = self._bottom_plot()
        for pl in (self.price, self.vol, self.rsi):
            pl.getAxis("bottom").setStyle(showValues=pl is owner)

    def _bottom_plot(self):
        return self.rsi if self.rsi.isVisible() else self.price

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
        for a in self.axes:
            a.set_index(df.index)
        self._times = df.index.to_numpy().astype("datetime64[s]").astype("int64").astype(float)

        self._base = float(df["Close"].iloc[0])
        self._fit_axis_width(df)
        for pl in (self.price, self.vol, self.rsi):
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

        for key in list(self._enabled):
            self._build_indicator(key)
        self._rebuild_legend()

        for p in (self.price, self.vol, self.rsi):
            p.vb.setLimits(xMin=-5, xMax=n + 80, minXRange=8)
        self.layer.rebuild()
        self.go_latest()
        self._update_last_tag()
        self._update_header(n - 1)

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
    def set_indicator(self, key: str, on: bool):
        if on:
            self._enabled.add(key)
            if self.df is not None:
                self._build_indicator(key)
        else:
            self._enabled.discard(key)
            self._remove_indicator(key)
        if key == "rsi":
            self._set_rsi_visible(on)
        self._rebuild_legend()

    def _build_indicator(self, key: str):
        if self.df is None or key in self._ind_items:
            return
        c = self.df["Close"]
        lines = []
        if key == "sma20":
            lines = [(self.price, ta.sma(c, 20), "#2962ff", "SMA 20")]
        elif key == "sma50":
            lines = [(self.price, ta.sma(c, 50), "#ff9800", "SMA 50")]
        elif key == "sma200":
            lines = [(self.price, ta.sma(c, 200), "#e040fb", "SMA 200")]
        elif key == "ema20":
            lines = [(self.price, ta.ema(c, 20), "#00bcd4", "EMA 20")]
        elif key == "ema50":
            lines = [(self.price, ta.ema(c, 50), "#ffeb3b", "EMA 50")]
        elif key == "bb":
            bb = ta.bollinger(c, 20, 2)
            lines = [(self.price, bb["mid"], "#ff6d00", "BB 20 2"),
                     (self.price, bb["upper"], "#2962ff", None),
                     (self.price, bb["lower"], "#2962ff", None)]
        elif key == "vwap":
            lines = [(self.price, ta.vwap(self.df), "#2962ff", "VWAP")]
        elif key == "supertrend":
            st = ta.supertrend(self.df, 10, 3.0)
            upl = st["st"].where(st["dir"] > 0)
            dnl = st["st"].where(st["dir"] < 0)
            lines = [(self.price, upl, theme.UP, "Supertrend 10 3"), (self.price, dnl, theme.DOWN, None)]
        elif key == "rsi":
            r14 = ta.rsi(c, 14)
            self._rsi_arr = r14.to_numpy(float)
            lines = [(self.rsi, r14, "#7e57c2", None)]

        items = []
        x = np.arange(len(c))
        for plot, y, color, name in lines:
            yy = y.to_numpy(float)
            if plot is self.price:
                yy = self.ty(yy)
            item = pg.PlotDataItem(x=x, y=yy, pen=pg.mkPen(color, width=1.5 if key == "supertrend" else 1.3),
                                   connect="finite")
            item._full = (x, yy)
            item._legend_color = color
            plot.addItem(item)
            if self._limit is not None:
                item.setData(x[:self._limit], item._full[1][:self._limit])
            items.append((plot, item, name))
        if key == "bb" and len(items) == 3:              # TradingView fills between the bands
            fill = pg.FillBetweenItem(items[1][1], items[2][1], brush=pg.mkBrush(33, 150, 243, 22))
            fill.setZValue(-8)
            self.price.addItem(fill)
            items.append((self.price, fill, None))
        if key == "rsi":
            band = pg.LinearRegionItem(values=(30, 70), orientation="horizontal", movable=False,
                                       brush=pg.mkBrush(126, 87, 194, 22), pen=pg.mkPen(None))
            band.setZValue(-10)
            self.rsi.addItem(band, ignoreBounds=True)
            items.append((self.rsi, band, None))
            for lvl in (30, 70):
                g = pg.InfiniteLine(pos=lvl, angle=0, movable=False,
                                    pen=pg.mkPen(theme.MUTED, style=QtCore.Qt.PenStyle.DashLine))
                self.rsi.addItem(g, ignoreBounds=True)
                items.append((self.rsi, g, None))
        self._ind_items[key] = items

    def _remove_indicator(self, key: str):
        for plot, item, _ in self._ind_items.pop(key, []):
            plot.removeItem(item)

    def _rebuild_legend(self):
        """Indicator / strategy lines listed under 'Vol', TradingView style: name then value(s) at the bar."""
        rows = []
        for key, items in self._ind_items.items():
            name = next((n for _, _, n in items if n), None)
            vals = [(it, getattr(it, "_legend_color", "#d1d4dc")) for pl, it, _ in items
                    if pl is self.price and isinstance(it, pg.PlotDataItem)]
            if name and vals:
                rows.append((name, vals))
        for item, name in self._strategy_items:
            col = item.opts["pen"].color().name() if hasattr(item.opts.get("pen"), "color") else "#d1d4dc"
            rows.append((name, [(item, col)]))
        self._legend_rows = rows
        self._update_ind_legend(getattr(self, "_hdr_i", None))
        self._hdr_shape = None
        self._place_overlays()

    def _update_ind_legend(self, i):
        S = settings.get()
        if not self._legend_rows or not S.get("sl_ind_titles", True) or self.df is None:
            self.ind_label.hide()
            return
        if i is None:
            i = self._n() - 1
        out = []
        for name, vals in self._legend_rows:
            col0 = vals[0][1]
            parts = [f"<span style='color:#d1d4dc'>{name}</span>"]
            if S.get("sl_ind_values", True):
                for it, col in vals:
                    f = getattr(it, "_full", None)
                    y = f[1][i] if f is not None and 0 <= i < len(f[1]) else np.nan
                    if np.isfinite(y):
                        parts.append(f"<span style='color:{col}'>{self.fp(float(self.iy(y)))}</span>")
            out.append(f"<span style='color:{col0}'>\u25cf</span>&nbsp;" + "&nbsp;&nbsp;".join(parts))
        self.ind_label.setText("<br>".join(out))
        self.ind_label.setVisible(not self.collapse_btn.isChecked())

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
        if not self.auto_y:
            x0, x1 = self.price.vb.viewRange()[0]
            lo, hi = max(0, int(x0)), min(n, int(x1) + 1)
            if hi > lo:
                self.vol.vb.setYRange(0, max(float(np.nanmax(self._vols[lo:hi])) * 1.15, 1.0), padding=0)
            self.rsi.vb.setYRange(0, 100, padding=0)
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
        self.rsi.vb.setYRange(0, 100, padding=0)
        self._place_last_tag()

    def _update_last_tag(self):
        if self.df is None or self._n() == 0:
            return
        last, prev = float(self.df["Close"].iloc[self._n() - 1]), float(self.df["Open"].iloc[self._n() - 1])
        color = theme.UP if last >= prev else theme.DOWN
        self.last_tag.fill = pg.mkBrush(color)
        self.last_tag.setText(f" {self.axis_label(float(self.ty(last)))} ")
        self.last_line.setPen(pg.mkPen(color, style=QtCore.Qt.PenStyle.DotLine))
        self.last_line.setPos(float(self.ty(last)))
        self.last_tag.setVisible(bool(settings.get()['last_label']))
        self.last_line.setVisible(bool(settings.get()['last_line']))
        nt = self.name_tag                                 # TradingView: the symbol name sits left of the price
        name = self.ticker()
        nt.setText(name)
        nt.setStyleSheet(f"QLabel {{ background: {color}; color: #ffffff; font-size: 12px; font-weight: 500;"
                         f" padding: 2px 5px; border-radius: 2px; }}")
        nt.adjustSize()
        self._place_last_tag()

    def _place_last_tag(self):
        if self.df is None or self.last_tag.isHidden():
            return
        xr = self.price.vb.viewRange()[0][1]
        y = float(self.ty(float(self.df["Close"].iloc[self._n() - 1])))
        self.last_tag.setPos(xr, y)
        self.cd_tag.setPos(xr, y)
        nt = self.name_tag
        show = not self.last_tag.isHidden() and bool(settings.get().get("sym_label", True)) and bool(nt.text())
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
            self.cd_tag.hide()
            return
        step = self.bar_seconds()
        if step <= 0 or step > 86400 * 2:
            self.cd_tag.hide()
            return
        import datetime as _dt
        last = self.df.index[-1]
        end = last + _dt.timedelta(seconds=step)
        now = _dt.datetime.now(tz=end.tzinfo) if getattr(end, "tzinfo", None) else _dt.datetime.now()
        left = (end - now).total_seconds()
        if not 0 <= left <= step:
            self.cd_tag.hide()
            return
        h, rem = divmod(int(left), 3600)
        m, sec = divmod(rem, 60)
        self.cd_tag.setText(f" {h:d}:{m:02d}:{sec:02d} " if h else f" {m:02d}:{sec:02d} ")
        self.cd_tag.show()
        self._place_last_tag()

    def _mouse_moved(self, pos, refresh: bool = False):
        if self.df is None:
            return
        self._cross_vp = self.glw.mapFromScene(pos)
        plots = (self.price, self.vol, self.rsi)
        hovered = next((p for p in plots if p.isVisible() and p.sceneBoundingRect().contains(pos)), None)
        if hovered is None:
            for p in plots:
                p.ytag.hide()
            for o in plots:
                o.dtag.hide()
            if not self.plus_btn.underMouse():
                self.plus_btn.hide()
            return
        mp = hovered.vb.mapSceneToView(pos)
        if hovered is self.price and not self.layer.tool and not self._panning():
            cur = (QtCore.Qt.CursorShape.PointingHandCursor if self.layer.drawings and self.layer.hover_hit(mp.x(), mp.y())
                   else QtCore.Qt.CursorShape.CrossCursor)
            if cur != getattr(self, "_cursor_shape", None):
                self._cursor_shape = cur
                self.glw.viewport().setCursor(cur)
        i = int(round(mp.x()))
        ix = float(min(max(i, 0), max(self._n() - 1, 0))) if 0 <= i < self._n() else mp.x()
        new_bar = i != getattr(self, "_hover_i", None)
        self._hover_i = i
        for p in plots:
            if new_bar:
                p.vline.setPos(ix)                      # TradingView: the vertical line snaps to the bar
                p.vline.show()
            p.hline.setVisible(p is hovered)
            p.ytag.setVisible(p is hovered)
        hovered.hline.setPos(mp.y())
        self._place_plus(hovered, pos, mp.y())
        xr = hovered.vb.viewRange()[0][1]
        hovered.ytag.setText(f" {self.axis_label(mp.y()) if hovered is self.price else self.axis_price(mp.y())} ")
        hovered.ytag.setPos(xr, mp.y())
        if 0 <= i < self._n():
            bp = self._bottom_plot()
            if new_bar or not bp.dtag.isVisible():       # header / date tag only change when the bar changes
                self._update_header(i)
                fmt = "%a %d %b '%y %H:%M" if self.axes[0].intraday else "%a %d %b '%y"
                for o in plots:
                    if o is not bp:
                        o.dtag.hide()
                bp.dtag.setText(f" {self.df.index[i].strftime(fmt)} ")
                bp.dtag.setPos(ix, bp.vb.viewRange()[1][0])
                bp.dtag.show()
            elif refresh:                                  # same bar, but the chart moved under the mouse
                bp.dtag.setPos(ix, bp.vb.viewRange()[1][0])
        else:
            for o in plots:
                o.dtag.hide()
        if hovered is self.price:
            x, y = (self.snap(mp.x(), mp.y()) if self.layer.tool else (mp.x(), mp.y()))
            self.layer.handle_move(x, y)

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

    def effective_df(self):
        """Data a strategy is allowed to see: everything, or only the bars revealed so far in replay."""
        return self.df if (self._limit is None or self.df is None) else self.df.iloc[: self._limit]

    def _panning(self) -> bool:
        return bool(QtWidgets.QApplication.mouseButtons() & QtCore.Qt.MouseButton.LeftButton)

    def _context_menu(self, ev):
        mp = self.price.vb.mapSceneToView(ev.scenePos())
        menu = QtWidgets.QMenu(self)
        hit = self.layer._hit_test(mp.x(), mp.y())
        if hit is not None:
            self.layer.selected = hit
            self.layer.rebuild()
            menu.addAction("Duplicate", self.layer.duplicate_selected)
            col = menu.addMenu("Color")
            for name, c in (("Blue", "#2962ff"), ("Red", "#f23645"), ("Green", "#089981"), ("Orange", "#ff9800"),
                            ("Purple", "#9c27b0"), ("White", "#ffffff")):
                col.addAction(name, lambda c=c, h=hit: self.layer.set_color(h, c))
            menu.addAction("Delete", self.layer.delete_selected)
            menu.addSeparator()
        price = float(self.iy(mp.y()))
        menu.addAction(f"Copy price {self.fp(price)}", lambda: QtWidgets.QApplication.clipboard().setText(f"{price:.2f}"))
        menu.addAction(f"Add alert at {price:,.2f}\t{theme.sc('Alt+A')}", lambda: self.alertRequested.emit(float(price)))
        menu.addAction(f"Add horizontal line at {price:,.2f}",
                       lambda: self.layer.add("hline", [[self.x_to_time(mp.x()), price]]))
        menu.addSeparator()
        a = menu.addAction("Auto-scale price")
        a.setCheckable(True)
        a.setChecked(self.auto_y)
        a.toggled.connect(self.set_auto_y)
        menu.addAction("Reset chart view\t" + theme.sc("Alt+R"), lambda: (self.set_auto_y(True), self.go_latest()))
        menu.addAction("Undo\t" + theme.sc("Ctrl+Z"), self.layer.undo)
        menu.addAction("Redo\t" + theme.sc("Ctrl+Shift+Z"), self.layer.redo)
        menu.addAction("Remove all drawings", self.layer.clear)
        menu.addSeparator()
        menu.addAction("Settings…", self.settingsRequested.emit)
        menu.exec(QtGui.QCursor.pos())

    def _crosshair_off(self):
        """Mouse left the chart: hide the crosshair and its tags, legend shows the last bar again (TradingView)."""
        if self.plus_btn.underMouse() or self.glw.viewport().underMouse():
            return
        for p in (self.price, self.vol, self.rsi):
            p.vline.hide()
            p.hline.hide()
            p.ytag.hide()
            p.dtag.hide()
        self.plus_btn.hide()
        self._hover_i = None
        self._cross_vp = self._cross_key = None
        if self.df is not None and self._n():
            self._update_header(self._n() - 1)

    def _place_plus(self, hovered, pos, y):
        if hovered is not self.price or not settings.get().get("plus_button", True):
            self.plus_btn.hide()
            return
        r = self.price.vb.sceneBoundingRect()
        ax = axis_rect(self.price.getAxis("right"))
        pt = self.glw.mapFromScene(QtCore.QPointF(ax.left(), pos.y()))
        self._plus_y = y
        self.plus_btn.move(int(pt.x()) - 24, int(pt.y()) - 10)
        self.plus_btn.show()
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
        if self.df is not None and ev.button() == QtCore.Qt.MouseButton.RightButton and \
                axis_rect(self.price.getAxis("right")).contains(ev.scenePos()):
            self._axis_menu()
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
            return f"{max(int(round(sec / 60)), 1)}m"
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
        try:
            from .tvui import NAMES
            return NAMES.get(t.upper(), t)
        except Exception:  # noqa: BLE001
            return t

    def exchange_tag(self) -> str:
        u = self._raw_symbol().upper()
        if not u:
            return ""
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
        rows = [("Date", df.index[i].strftime(fmt), "#d1d4dc"),
                ("Open", self.fp(o), ccol), ("High", self.fp(h), ccol), ("Low", self.fp(lo), ccol),
                ("Close", self.fp(c), ccol),
                ("Change", f"{d:+,.2f} ({(c / prev - 1) * 100 if prev else 0:+.2f}%)", dcol),
                ("Volume", vs, "#d1d4dc")]
        title = f"{self.ticker()} \u00b7 {self.interval_tag()} \u00b7 {self.exchange_tag()}"
        sections = [(title, rows)]
        ind = []
        for name, vals in getattr(self, "_legend_rows", []):
            parts = []
            for it, col in vals:
                f = getattr(it, "_full", None)
                y = f[1][i] if f is not None and 0 <= i < len(f[1]) else np.nan
                if np.isfinite(y):
                    parts.append(f"<span style='color:{col}'>{self.fp(float(self.iy(y)))}</span>")
            ind.append((name, "&nbsp;&nbsp;".join(parts) or "<span style='color:#5d606b'>\u2014</span>", None))
        rs = getattr(self, "_rsi_arr", None)
        if "rsi" in self._enabled and rs is not None and i < len(rs):
            ind.append(("RSI 14", f"{rs[i]:.2f}" if np.isfinite(rs[i]) else "\u2014", "#b388ff"))
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
        v = lambda k, x: (f"<span style='color:#ffffff'>{k}</span><span style='color:{col}'>{fp(x)}</span>")  # noqa: E731
        title = (f"<span style='font-size:16px;color:#ffffff;font-weight:500'>{self.display_name()}</span>"
                 f"<span style='font-size:16px;color:#ffffff'> \u00b7 {self.interval_tag()} \u00b7 {self.exchange_tag()}</span>"
                 f"&nbsp;&nbsp;&nbsp;") if S["sl_title"] else ""
        vals = (f"{v('O', r['Open'])}&nbsp;&nbsp;{v('H', r['High'])}&nbsp;&nbsp;{v('L', r['Low'])}&nbsp;&nbsp;{v('C', r['Close'])}"
                if S["sl_values"] else "")
        chgt = f"&nbsp;&nbsp;<span style='color:{col}'>{d:+,.2f} ({chg:+.2f}%)</span>" if S["sl_change"] else ""
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
        self.vol_label.setText(f"<span style='color:#ffffff'>Vol</span> <span style='color:{col}'>{vs}</span>")
        self.vol_label.setVisible(bool(S["sl_volume"]) and not self.collapse_btn.isChecked())
        rs = getattr(self, "_rsi_arr", None)
        if rs is not None and i < len(rs) and np.isfinite(rs[i]):
            self.rsi_label.setText(f"<span style='color:#ffffff'>RSI 14</span> <span style='color:#b388ff'>{rs[i]:.2f}</span>")
        self._update_ind_legend(i)
        hs = (self.header.height(), self.header.isHidden(), self.vol_label.isHidden(), self.ind_label.height(),
              self.ind_label.isHidden())
        if hs != getattr(self, "_hdr_shape", None):        # only re-layout overlays when the legend's shape changed
            self._hdr_shape = hs
            self._place_overlays()
