"""TradingView-style chart: candlesticks, volume, RSI pane, overlays, crosshair, trade markers."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from . import indicators as ta
from . import theme

_QWIDGETSIZE_MAX = 16777215


class CandlestickItem(pg.GraphicsObject):
    def __init__(self, df: pd.DataFrame):
        super().__init__()
        self.picture = QtGui.QPicture()
        self.set_data(df)

    def set_data(self, df: pd.DataFrame):
        o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
        pic = QtGui.QPicture()
        p = QtGui.QPainter(pic)
        w = 0.36
        pens = {True: pg.mkPen(theme.UP), False: pg.mkPen(theme.DOWN)}
        brushes = {True: pg.mkBrush(theme.UP), False: pg.mkBrush(theme.DOWN)}
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


class ChartWidget(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.df: pd.DataFrame | None = None
        self.label = ""
        self._enabled: set[str] = set()
        self._ind_items: dict[str, list] = {}      # key -> [(plot, item, legend_name|None)]
        self._strategy_items: list = []             # [(item, name)]
        self._marker_items: list = []
        self.candles = None
        self.volbars = None

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

        self.price.vb.sigXRangeChanged.connect(self._autoscale)
        self.glw.scene().sigMouseMoved.connect(self._mouse_moved)

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
        p.showGrid(x=True, y=True, alpha=0.12)
        p.vb.setMouseEnabled(x=True, y=False)
        dash = pg.mkPen(theme.MUTED, style=QtCore.Qt.PenStyle.DashLine)
        p.vline = pg.InfiniteLine(angle=90, movable=False, pen=dash)
        p.hline = pg.InfiniteLine(angle=0, movable=False, pen=dash)
        p.addItem(p.vline, ignoreBounds=True)
        p.addItem(p.hline, ignoreBounds=True)
        p.hline.hide()
        return p

    def _set_rsi_visible(self, on: bool):
        grid = self.glw.ci.layout
        self.rsi.setVisible(on)
        if on:
            grid.setRowMinimumHeight(2, 0)
            grid.setRowMaximumHeight(2, _QWIDGETSIZE_MAX)
        else:
            grid.setRowFixedHeight(2, 0)
        self.vol.getAxis("bottom").setStyle(showValues=not on)

    # ------------------------------------------------------------------ data
    def set_data(self, df: pd.DataFrame, label: str = ""):
        self._wipe()
        self.df, self.label = df, label
        n = len(df)
        for a in self.axes:
            a.set_index(df.index)

        self.candles = CandlestickItem(df)
        self.price.addItem(self.candles)

        up = (df["Close"] >= df["Open"]).to_numpy()
        b_up, b_dn = pg.mkBrush(38, 166, 154, 140), pg.mkBrush(239, 83, 80, 140)
        self.volbars = pg.BarGraphItem(
            x=np.arange(n), height=df["Volume"].to_numpy(float), width=0.7,
            brushes=[b_up if u else b_dn for u in up], pen=pg.mkPen(None))
        self.vol.addItem(self.volbars)

        self._lows = df["Low"].to_numpy(float)
        self._highs = df["High"].to_numpy(float)
        self._vols = df["Volume"].to_numpy(float)

        for key in list(self._enabled):
            self._build_indicator(key)
        self._rebuild_legend()

        for p in (self.price, self.vol, self.rsi):
            p.vb.setLimits(xMin=-5, xMax=n + 80, minXRange=8)
        self.price.setXRange(max(-2, n - 160), n + 6, padding=0)
        self._autoscale()
        self._update_header(n - 1)

    def _wipe(self):
        for item, plot in ((self.candles, self.price), (self.volbars, self.vol)):
            if item is not None:
                plot.removeItem(item)
        self.candles = self.volbars = None
        for key in list(self._ind_items):
            self._remove_indicator(key)
        self.clear_strategy()

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
        lines = []  # (plot, series, colour, name)
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
        except Exception:
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

    # ------------------------------------------------------------------ interaction
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

    def _mouse_moved(self, pos):
        if self.df is None:
            return
        plots = (self.price, self.vol, self.rsi)
        hovered = next((p for p in plots if p.isVisible() and p.sceneBoundingRect().contains(pos)), None)
        if hovered is None:
            return
        mp = hovered.vb.mapSceneToView(pos)
        for p in plots:
            p.vline.setPos(mp.x())
            p.hline.setVisible(p is hovered)
        hovered.hline.setPos(mp.y())
        i = int(round(mp.x()))
        if 0 <= i < len(self.df):
            self._update_header(i)

    def _update_header(self, i: int):
        df = self.df
        r = df.iloc[i]
        prev = df["Close"].iloc[i - 1] if i > 0 else r["Open"]
        chg = (r["Close"] / prev - 1) * 100 if prev else 0.0
        col = theme.UP if r["Close"] >= r["Open"] else theme.DOWN
        fmt = "%a %d %b %Y %H:%M" if self.axes[0].intraday else "%a %d %b %Y"
        ts = df.index[i].strftime(fmt)
        v = lambda x: f"<span style='color:{col}'>{x:,.2f}</span>"
        self.header.setText(
            f"<b>{self.label}</b> &nbsp; <span style='color:{theme.MUTED}'>{ts}</span> &nbsp; "
            f"O {v(r['Open'])} &nbsp; H {v(r['High'])} &nbsp; L {v(r['Low'])} &nbsp; C {v(r['Close'])} "
            f"&nbsp; <span style='color:{col}'>{chg:+.2f}%</span> &nbsp; "
            f"<span style='color:{theme.MUTED}'>Vol {r['Volume']:,.0f}</span>")
