"""TradingView-style chart: candles / hollow / line / area, volume, RSI pane, overlays,
crosshair with price+date tags, last-price tag, drawing tools and trade markers."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from . import indicators as ta
from . import theme
from .drawings import DrawingLayer
from .gestures import TVViewBox
from .render import CandleItem, VolumeItem

_QWIDGETSIZE_MAX = 16777215
STYLES = ["candles", "hollow", "line", "area"]
INDICATOR_KEYS = ["sma20", "sma50", "sma200", "ema20", "ema50", "bb", "rsi"]


class DateAxis(pg.AxisItem):
    """X axis that plots bar numbers (no weekend gaps, like TradingView) but shows dates."""

    def __init__(self, orientation="bottom", **kw):
        super().__init__(orientation, **kw)
        self._index = None
        self.intraday = False

    def set_index(self, index: pd.DatetimeIndex):
        self._index = index
        self.intraday = bool(len(index)) and bool(((index.hour != 0) | (index.minute != 0)).any())

    def tickStrings(self, values, scale, spacing):
        idx, out = self._index, []
        for v in values:
            i = int(round(v))
            if idx is None or i < 0 or i >= len(idx):
                out.append("")
                continue
            t = idx[i]
            if self.intraday:
                out.append(t.strftime("%d %b %H:%M"))
            elif spacing > 60:
                out.append(t.strftime("%b %Y"))
            else:
                out.append(t.strftime("%d %b %y"))
        return out


class OverlayLabel(QtWidgets.QLabel):
    """Transparent text drawn on top of the chart (legend / pane titles) - never steals the mouse."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent; font-size: 12px;")

    def setText(self, t):
        super().setText(t)
        self.adjustSize()


def _tag(anchor, fill: str) -> pg.TextItem:
    t = pg.TextItem(anchor=anchor, color="#ffffff", fill=pg.mkBrush(fill))
    t.setZValue(100)
    return t


class ChartWidget(QtWidgets.QWidget):
    activated = QtCore.Signal()
    toolChanged = QtCore.Signal(object)
    drawingsChanged = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.df: pd.DataFrame | None = None
        self.label = ""
        self.style = "candles"
        self.magnet = True
        self.auto_y = True
        self._enabled: set[str] = set()
        self._ind_items: dict[str, list] = {}
        self._strategy_items: list = []
        self._marker_items: list = []
        self.candles = self.volbars = self.line_item = None
        self._times = np.array([])
        self._lows = self._highs = self._vols = np.array([])

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.ci.layout.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.glw, 1)
        self.header = OverlayLabel(self.glw)
        self.header.setText(f"<span style='color:{theme.MUTED}'>Open a data file or type a symbol and press Enter</span>")
        self.header.move(10, 6)
        self.vol_label = OverlayLabel(self.glw)
        self.rsi_label = OverlayLabel(self.glw)
        self.vol_label.hide()
        self.rsi_label.hide()
        self.header.raise_()

        self.axes: list[DateAxis] = []
        self.price = self._make_plot(0)
        self.vol = self._make_plot(1)
        self.rsi = self._make_plot(2)
        self.vol.setXLink(self.price)
        self.rsi.setXLink(self.price)
        self.price.getAxis("bottom").setStyle(showValues=False)

        grid = self.glw.ci.layout
        grid.setSpacing(2)
        grid.setRowStretchFactor(0, 10)
        grid.setRowStretchFactor(1, 2)
        grid.setRowStretchFactor(2, 3)
        self._set_rsi_visible(False)

        try:
            self.legend = self.price.addLegend(
                offset=(10, 46), labelTextColor=theme.TEXT,
                brush=pg.mkBrush(19, 23, 34, 170), pen=pg.mkPen(None))
        except TypeError:  # older pyqtgraph
            self.legend = self.price.addLegend(offset=(10, 46))

        # tags: last price, crosshair price (per pane) and crosshair date
        self.last_tag = _tag((1, 0.5), theme.UP)
        self.price.addItem(self.last_tag, ignoreBounds=True)
        self.last_tag.hide()
        self.last_line = pg.InfiniteLine(angle=0, movable=False,
                                         pen=pg.mkPen(theme.UP, style=QtCore.Qt.PenStyle.DotLine))
        self.price.addItem(self.last_line, ignoreBounds=True)
        self.last_line.hide()
        for p in (self.price, self.vol, self.rsi):
            p.ytag = _tag((1, 0.5), "#363a45")
            p.addItem(p.ytag, ignoreBounds=True)
            p.ytag.hide()
        for p in (self.vol, self.rsi):
            p.dtag = _tag((0.5, 1), "#363a45")
            p.addItem(p.dtag, ignoreBounds=True)
            p.dtag.hide()

        self.layer = DrawingLayer(self)
        self.layer.changed.connect(self.drawingsChanged)

        self.price.vb.sigXRangeChanged.connect(self._autoscale)
        self.price.vb.sigRangeChanged.connect(lambda *_: self._place_last_tag())
        self.glw.scene().sigMouseMoved.connect(self._mouse_moved)
        self.glw.scene().sigMouseClicked.connect(self._mouse_clicked)
        self.glw.viewport().installEventFilter(self)
        self.glw.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.glw.viewport().setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

    # ------------------------------------------------------------------ construction helpers
    def _make_plot(self, row: int) -> pg.PlotItem:
        axis = DateAxis("bottom")
        self.axes.append(axis)
        p = self.glw.addPlot(row=row, col=0, axisItems={"bottom": axis}, viewBox=TVViewBox(self, price=(row == 0)))
        p.setMenuEnabled(False)
        p.hideButtons()
        p.showAxis("right")
        p.hideAxis("left")
        p.getAxis("right").setWidth(72)
        f = QtGui.QFont()
        f.setPixelSize(11)
        for name in ("bottom", "right"):
            ax = p.getAxis(name)
            ax.setPen(pg.mkPen(theme.BORDER))
            ax.setTextPen(pg.mkPen("#b2b5be"))
            ax.setStyle(tickFont=f, tickLength=-4)
        p.showGrid(x=True, y=True, alpha=0.06)
        dash = pg.mkPen(theme.MUTED, style=QtCore.Qt.PenStyle.DashLine)
        p.vline = pg.InfiniteLine(angle=90, movable=False, pen=dash)
        p.hline = pg.InfiniteLine(angle=0, movable=False, pen=dash)
        p.addItem(p.vline, ignoreBounds=True)
        p.addItem(p.hline, ignoreBounds=True)
        p.hline.hide()
        return p

    def eventFilter(self, obj, ev):
        if obj is self.glw.viewport() and ev.type() == QtCore.QEvent.Type.Resize:
            QtCore.QTimer.singleShot(0, self._place_overlays)
        if obj is self.glw.viewport() and ev.type() == QtCore.QEvent.Type.MouseButtonPress:
            self.activated.emit()
        if obj is self.glw.viewport() and ev.type() == QtCore.QEvent.Type.NativeGesture:
            if ev.gestureType() == QtCore.Qt.NativeGestureType.ZoomNativeGesture:
                pos = self.price.vb.mapSceneToView(self.glw.mapToScene(ev.position().toPoint()))
                self.price.vb.zoom_x(max(0.2, 1.0 - ev.value() * 1.5), pos.x())
                return True
        if obj is self.glw.viewport() and ev.type() == QtCore.QEvent.Type.KeyPress:
            if self.handle_key(ev):
                return True
        return super().eventFilter(obj, ev)

    def _place_overlays(self):
        for plot, lab in ((self.vol, self.vol_label), (self.rsi, self.rsi_label)):
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
        if k == K.Key_Left:
            vb.pan_x(-step)
        elif k == K.Key_Right:
            vb.pan_x(step)
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
        self.vol.getAxis("bottom").setStyle(showValues=not on)

    def _bottom_plot(self):
        return self.rsi if self.rsi.isVisible() else self.vol

    # ------------------------------------------------------------------ coordinate helpers (used by drawings)
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
        self.glw.viewport().setCursor(QtCore.Qt.CursorShape.CrossCursor if on
                                      else QtCore.Qt.CursorShape.ArrowCursor)

    def snap(self, x: float, y: float):
        """Magnet: snap to the nearest O/H/L/C of the bar under the cursor if close enough."""
        n = len(self._lows)
        if not self.magnet or n == 0:
            return x, y
        i = int(min(max(round(x), 0), n - 1))
        r = self.df.iloc[i]
        vb = self.price.vb
        py = vb.mapViewToScene(QtCore.QPointF(x, y)).y()
        best, best_d = None, 16.0
        for v in (r["Open"], r["High"], r["Low"], r["Close"]):
            d = abs(vb.mapViewToScene(QtCore.QPointF(x, float(v))).y() - py)
            if d < best_d:
                best, best_d = float(v), d
        return (float(i), best) if best is not None else (x, y)

    # ------------------------------------------------------------------ data
    def set_data(self, df: pd.DataFrame, label: str = ""):
        self._wipe()
        self.df, self.label = df, label
        n = len(df)
        for a in self.axes:
            a.set_index(df.index)
        self._times = df.index.to_numpy().astype("datetime64[s]").astype("int64").astype(float)

        self.candles = CandleItem(df, self.style)
        self.price.addItem(self.candles)
        close = df["Close"].to_numpy(float)
        self.line_item = pg.PlotDataItem(x=np.arange(n), y=close, pen=pg.mkPen(theme.ACCENT, width=2))
        self.price.addItem(self.line_item)
        self.line_item.setClipToView(True)
        self.line_item.setDownsampling(auto=True, method='peak')

        self.volbars = VolumeItem(df)
        self.vol.addItem(self.volbars)

        self._lows = df["Low"].to_numpy(float)
        self._highs = df["High"].to_numpy(float)
        self._vols = df["Volume"].to_numpy(float)
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
        n = len(self._lows)
        self.auto_y = True
        if n:
            self.price.setXRange(max(-2, n - 160), n + 6, padding=0)
            self._autoscale()

    def _wipe(self):
        for item, plot in ((self.candles, self.price), (self.line_item, self.price), (self.volbars, self.vol)):
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
        is_candle = self.style in ("candles", "hollow")
        if is_candle:
            self.candles.set_data(self.df, self.style)
        self.candles.setVisible(is_candle)
        self.line_item.setVisible(not is_candle)
        if self.style == "area":
            self.line_item.setFillLevel(float(np.nanmin(self._lows)))
            self.line_item.setBrush(pg.mkBrush(41, 98, 255, 55))
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
        L = theme.LINE_COLORS
        lines = []
        if key == "sma20":
            lines = [(self.price, ta.sma(c, 20), L[0], "SMA 20")]
        elif key == "sma50":
            lines = [(self.price, ta.sma(c, 50), L[1], "SMA 50")]
        elif key == "sma200":
            lines = [(self.price, ta.sma(c, 200), L[2], "SMA 200")]
        elif key == "ema20":
            lines = [(self.price, ta.ema(c, 20), L[3], "EMA 20")]
        elif key == "ema50":
            lines = [(self.price, ta.ema(c, 50), L[4], "EMA 50")]
        elif key == "bb":
            bb = ta.bollinger(c, 20, 2)
            lines = [(self.price, bb["mid"], L[5], "BB (20, 2)"),
                     (self.price, bb["upper"], L[5], None),
                     (self.price, bb["lower"], L[5], None)]
        elif key == "rsi":
            r14 = ta.rsi(c, 14)
            self._rsi_arr = r14.to_numpy(float)
            lines = [(self.rsi, r14, "#b388ff", None)]

        items = []
        x = np.arange(len(c))
        for plot, y, color, name in lines:
            item = pg.PlotDataItem(x=x, y=y.to_numpy(float), pen=pg.mkPen(color, width=1.3), connect="finite")
            plot.addItem(item)
            items.append((plot, item, name))
        if key == "rsi":
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
        try:
            self.legend.clear()
        except Exception:  # noqa: BLE001
            return
        for items in self._ind_items.values():
            for _, item, name in items:
                if name:
                    self.legend.addItem(item, name)
        for item, name in self._strategy_items:
            self.legend.addItem(item, name)
        self.legend.setVisible(bool(self.legend.items))

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
        if self.df is None:
            return
        x = np.arange(len(self.df))
        for i, (name, series) in enumerate(overlays.items()):
            color = theme.LINE_COLORS[(i + 2) % len(theme.LINE_COLORS)]
            item = pg.PlotDataItem(x=x, y=series.to_numpy(float), pen=pg.mkPen(color, width=1.4), connect="finite")
            self.price.addItem(item)
            self._strategy_items.append((item, name))
        self._rebuild_legend()

    def set_trades(self, trades: pd.DataFrame):
        for item in self._marker_items:
            self.price.removeItem(item)
        self._marker_items = []
        if self.df is None or trades.empty:
            return
        ei = trades["entry_idx"].to_numpy(int)
        closed = trades[~trades["open"].astype(bool)]
        xi = closed["exit_idx"].to_numpy(int)
        buys = pg.ScatterPlotItem(x=ei, y=self._lows[ei] * 0.994, symbol="t1", size=13,
                                  brush=pg.mkBrush(theme.UP), pen=pg.mkPen("#0b3d38"))
        sells = pg.ScatterPlotItem(x=xi, y=self._highs[xi] * 1.006, symbol="t", size=13,
                                   brush=pg.mkBrush(theme.DOWN), pen=pg.mkPen("#4a1614"))
        for s in (buys, sells):
            self.price.addItem(s)
            self._marker_items.append(s)

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
        n = len(self._lows)
        if not self.auto_y:
            x0, x1 = self.price.vb.viewRange()[0]
            lo, hi = max(0, int(x0)), min(n, int(x1) + 1)
            if hi > lo:
                self.vol.vb.setYRange(0, max(float(np.nanmax(self._vols[lo:hi])) * 1.15, 1.0), padding=0)
            self.rsi.vb.setYRange(0, 100, padding=0)
            self._place_last_tag()
            return
        x0, x1 = self.price.vb.viewRange()[0]
        n = len(self._lows)
        lo, hi = max(0, int(np.floor(x0))), min(n, int(np.ceil(x1)) + 1)
        if hi <= lo:
            return
        ymin, ymax = float(np.nanmin(self._lows[lo:hi])), float(np.nanmax(self._highs[lo:hi]))
        pad = (ymax - ymin) * 0.08 or ymax * 0.01
        self.price.vb.setYRange(ymin - pad, ymax + pad, padding=0)
        vmax = float(np.nanmax(self._vols[lo:hi]))
        self.vol.vb.setYRange(0, max(vmax * 1.15, 1.0), padding=0)
        self.rsi.vb.setYRange(0, 100, padding=0)
        self._place_last_tag()

    def _update_last_tag(self):
        if self.df is None or len(self.df) == 0:
            return
        last, prev = float(self.df["Close"].iloc[-1]), float(self.df["Open"].iloc[-1])
        color = theme.UP if last >= prev else theme.DOWN
        self.last_tag.fill = pg.mkBrush(color)
        self.last_tag.setText(f" {last:,.2f} ")
        self.last_line.setPen(pg.mkPen(color, style=QtCore.Qt.PenStyle.DotLine))
        self.last_line.setPos(last)
        self.last_tag.show()
        self.last_line.show()
        self._place_last_tag()

    def _place_last_tag(self):
        if self.df is None or not self.last_tag.isVisible():
            return
        xr = self.price.vb.viewRange()[0][1]
        self.last_tag.setPos(xr, float(self.df["Close"].iloc[-1]))

    def _mouse_moved(self, pos):
        if self.df is None:
            return
        plots = (self.price, self.vol, self.rsi)
        hovered = next((p for p in plots if p.isVisible() and p.sceneBoundingRect().contains(pos)), None)
        if hovered is None:
            for p in plots:
                p.ytag.hide()
            self.vol.dtag.hide()
            self.rsi.dtag.hide()
            return
        mp = hovered.vb.mapSceneToView(pos)
        i = int(round(mp.x()))
        for p in plots:
            p.vline.setPos(mp.x())
            p.hline.setVisible(p is hovered)
            p.ytag.setVisible(p is hovered)
        hovered.hline.setPos(mp.y())
        xr = hovered.vb.viewRange()[0][1]
        hovered.ytag.setText(f" {mp.y():,.2f} ")
        hovered.ytag.setPos(xr, mp.y())
        if 0 <= i < len(self.df):
            self._update_header(i)
            fmt = "%a %d %b '%y %H:%M" if self.axes[0].intraday else "%a %d %b '%y"
            bp = self._bottom_plot()
            other = self.vol if bp is self.rsi else self.rsi
            other.dtag.hide()
            bp.dtag.setText(f" {self.df.index[i].strftime(fmt)} ")
            bp.dtag.setPos(mp.x(), bp.vb.viewRange()[1][0])
            bp.dtag.show()
        else:
            self.vol.dtag.hide()
            self.rsi.dtag.hide()
        if hovered is self.price:
            x, y = (self.snap(mp.x(), mp.y()) if self.layer.tool else (mp.x(), mp.y()))
            self.layer.handle_move(x, y)

    def _mouse_clicked(self, ev):
        if self.df is None or ev.button() != QtCore.Qt.MouseButton.LeftButton:
            return
        if ev.double():                                  # double-click an axis = reset that scale
            ax = self.price.getAxis("right").sceneBoundingRect()
            if ax.contains(ev.scenePos()):
                self.set_auto_y(True)
                self._autoscale()
                return
            if any(a.sceneBoundingRect().contains(ev.scenePos()) for a in self.axes):
                self.go_latest()
                return
        if not self.price.sceneBoundingRect().contains(ev.scenePos()):
            return
        mp = self.price.vb.mapSceneToView(ev.scenePos())
        x, y = self.snap(mp.x(), mp.y()) if self.layer.tool else (mp.x(), mp.y())
        self.layer.handle_click(x, y)

    def _update_header(self, i: int):
        df = self.df
        r = df.iloc[i]
        prev = df["Close"].iloc[i - 1] if i > 0 else r["Open"]
        d = r["Close"] - prev
        chg = (r["Close"] / prev - 1) * 100 if prev else 0.0
        col = theme.UP if r["Close"] >= r["Open"] else theme.DOWN
        fmt = "%a %d %b '%y %H:%M" if self.axes[0].intraday else "%a %d %b '%y"
        ts = df.index[i].strftime(fmt)
        m = theme.MUTED
        v = lambda k, x: (f"<span style='color:{m}'>{k}</span><span style='color:{col}'>{x:,.2f}</span>")  # noqa: E731
        self.header.setText(
            f"<span style='font-size:14px;color:#ffffff;font-weight:600'>{self.label}</span>"
            f" &nbsp;<span style='color:{m}'>{ts}</span><br>"
            f"{v('O', r['Open'])} &nbsp;{v('H', r['High'])} &nbsp;{v('L', r['Low'])} &nbsp;{v('C', r['Close'])}"
            f" &nbsp;<span style='color:{col}'>{d:+,.2f} ({chg:+.2f}%)</span>")
        vol = float(r["Volume"])
        vs = f"{vol / 1e7:.2f}Cr" if vol >= 1e7 else (f"{vol / 1e5:.2f}L" if vol >= 1e5 else f"{vol:,.0f}")
        self.vol_label.setText(f"<span style='color:#ffffff'>Vol</span> <span style='color:{col}'>{vs}</span>")
        rs = getattr(self, "_rsi_arr", None)
        if rs is not None and i < len(rs) and np.isfinite(rs[i]):
            self.rsi_label.setText(f"<span style='color:#ffffff'>RSI 14</span> <span style='color:#b388ff'>{rs[i]:.2f}</span>")
        self._place_overlays()
