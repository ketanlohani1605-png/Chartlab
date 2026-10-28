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

_QWIDGETSIZE_MAX = 16777215
STYLES = ["candles", "hollow", "line", "area"]
INDICATOR_KEYS = ["sma20", "sma50", "sma200", "ema20", "ema50", "bb", "rsi"]


class CandlestickItem(pg.GraphicsObject):
    def __init__(self, df: pd.DataFrame, style: str = "candles"):
        super().__init__()
        self.picture = QtGui.QPicture()
        self.set_data(df, style)

    def set_data(self, df: pd.DataFrame, style: str = "candles"):
        o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
        pic = QtGui.QPicture()
        p = QtGui.QPainter(pic)
        w = 0.36
        hollow = style == "hollow"
        pens = {True: pg.mkPen(theme.UP), False: pg.mkPen(theme.DOWN)}
        brushes = {True: QtGui.QBrush(QtCore.Qt.BrushStyle.NoBrush) if hollow else pg.mkBrush(theme.UP),
                   False: pg.mkBrush(theme.DOWN)}
        for i in range(len(o)):
            up = c[i] >= o[i]
            p.setPen(pens[up])
            p.setBrush(brushes[up])
            p.drawLine(QtCore.QPointF(i, l[i]), QtCore.QPointF(i, h[i]))
            p.drawRect(QtCore.QRectF(i - w, o[i], 2 * w, c[i] - o[i]))
        p.end()
        self.prepareGeometryChange()
        self.picture = pic
        self.informViewBoundsChanged()
        self.update()

    def paint(self, p, *args):
        p.drawPicture(0, 0, self.picture)

    def boundingRect(self):
        return QtCore.QRectF(self.picture.boundingRect())


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
        self.header = QtWidgets.QLabel("Load a symbol or open a CSV file to begin")
        self.header.setObjectName("chartHeader")
        self.header.setTextFormat(QtCore.Qt.TextFormat.RichText)
        lay.addWidget(self.header)
        self.glw = pg.GraphicsLayoutWidget()
        lay.addWidget(self.glw, 1)

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
                offset=(10, 10), labelTextColor=theme.TEXT,
                brush=pg.mkBrush(19, 23, 34, 170), pen=pg.mkPen(None))
        except TypeError:  # older pyqtgraph
            self.legend = self.price.addLegend(offset=(10, 10))

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

    # ------------------------------------------------------------------ construction helpers
    def _make_plot(self, row: int) -> pg.PlotItem:
        axis = DateAxis("bottom")
        self.axes.append(axis)
        p = self.glw.addPlot(row=row, col=0, axisItems={"bottom": axis})
        p.setMenuEnabled(False)
        p.hideButtons()
        p.showAxis("right")
        p.hideAxis("left")
        p.getAxis("right").setWidth(72)
        for name in ("bottom", "right"):
            p.getAxis(name).setPen(pg.mkPen(theme.GRID))
            p.getAxis(name).setTextPen(pg.mkPen(theme.TEXT))
        p.showGrid(x=True, y=True, alpha=0.10)
        p.vb.setMouseEnabled(x=True, y=False)
        dash = pg.mkPen(theme.MUTED, style=QtCore.Qt.PenStyle.DashLine)
        p.vline = pg.InfiniteLine(angle=90, movable=False, pen=dash)
        p.hline = pg.InfiniteLine(angle=0, movable=False, pen=dash)
        p.addItem(p.vline, ignoreBounds=True)
        p.addItem(p.hline, ignoreBounds=True)
        p.hline.hide()
        return p

    def eventFilter(self, obj, ev):
        if obj is self.glw.viewport() and ev.type() == QtCore.QEvent.Type.MouseButtonPress:
            self.activated.emit()
        return super().eventFilter(obj, ev)

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

        self.candles = CandlestickItem(df, self.style)
        self.price.addItem(self.candles)
        close = df["Close"].to_numpy(float)
        self.line_item = pg.PlotDataItem(x=np.arange(n), y=close, pen=pg.mkPen(theme.ACCENT, width=2))
        self.price.addItem(self.line_item)

        up = (df["Close"] >= df["Open"]).to_numpy()
        b_up, b_dn = pg.mkBrush(38, 166, 154, 140), pg.mkBrush(239, 83, 80, 140)
        self.volbars = pg.BarGraphItem(
            x=np.arange(n), height=df["Volume"].to_numpy(float), width=0.7,
            brushes=[b_up if u else b_dn for u in up], pen=pg.mkPen(None))
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
            lines = [(self.rsi, ta.rsi(c, 14), "#b388ff", None)]

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
    def _autoscale(self, *_):
        if self.df is None:
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
        if not self.price.sceneBoundingRect().contains(ev.scenePos()):
            return
        mp = self.price.vb.mapSceneToView(ev.scenePos())
        x, y = self.snap(mp.x(), mp.y()) if self.layer.tool else (mp.x(), mp.y())
        self.layer.handle_click(x, y)

    def _update_header(self, i: int):
        df = self.df
        r = df.iloc[i]
        prev = df["Close"].iloc[i - 1] if i > 0 else r["Open"]
        chg = (r["Close"] / prev - 1) * 100 if prev else 0.0
        col = theme.UP if r["Close"] >= r["Open"] else theme.DOWN
        fmt = "%a %d %b %Y %H:%M" if self.axes[0].intraday else "%a %d %b %Y"
        ts = df.index[i].strftime(fmt)
        v = lambda x: f"<span style='color:{col}'>{x:,.2f}</span>"  # noqa: E731
        self.header.setText(
            f"<b>{self.label}</b> &nbsp; <span style='color:{theme.MUTED}'>{ts}</span> &nbsp; "
            f"O {v(r['Open'])} &nbsp; H {v(r['High'])} &nbsp; L {v(r['Low'])} &nbsp; C {v(r['Close'])} "
            f"&nbsp; <span style='color:{col}'>{chg:+.2f}%</span> &nbsp; "
            f"<span style='color:{theme.MUTED}'>Vol {r['Volume']:,.0f}</span>")
