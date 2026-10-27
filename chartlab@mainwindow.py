"""Main application window."""
from __future__ import annotations

import contextlib
import io
import sys
import traceback
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from . import backtest as bt
from . import data as datamod
from . import theme
from .chart import ChartWidget, DateAxis
from .editor import CodeEditor

FALLBACK_STRATEGY = '''def strategy(df):
    fast = ta.sma(df["Close"], 20)
    slow = ta.sma(df["Close"], 50)
    return {"buy": ta.crossover(fast, slow), "sell": ta.crossunder(fast, slow),
            "plot_SMA 20": fast, "plot_SMA 50": slow}
'''

INTERVALS = ["1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo"]
RANGES = ["5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "max"]
INDICATORS = [("sma20", "SMA 20"), ("sma50", "SMA 50"), ("sma200", "SMA 200"),
              ("ema20", "EMA 20"), ("ema50", "EMA 50"),
              ("bb", "Bollinger Bands (20, 2)"), ("rsi", "RSI (14)  —  separate pane")]


class LoadWorker(QtCore.QThread):
    done = QtCore.Signal(object, str)
    failed = QtCore.Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            df, label = self.fn()
            self.done.emit(df, label)
        except Exception as e:  # noqa: BLE001 - report anything to the UI
            self.failed.emit(f"{type(e).__name__}: {e}")


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ChartLab")
        self.df = None
        self.label = ""
        self.result = None
        self._worker = None
        self.settings = QtCore.QSettings("ChartLab", "ChartLab")

        self.chart = ChartWidget()
        self.setCentralWidget(self.chart)
        self._build_toolbar()
        self._build_strategy_dock()
        self._build_results_dock()
        self._build_menu()
        self.setStatusBar(QtWidgets.QStatusBar())
        self._restore()

    # ------------------------------------------------------------------ UI construction
    def _build_toolbar(self):
        tb = self.addToolBar("Data")
        tb.setMovable(False)

        self.source = QtWidgets.QComboBox()
        self.source.addItem("Yahoo Finance  —  NSE / BSE / US / crypto", "yahoo")
        self.source.addItem("Local file  —  CSV / Parquet", "file")
        for name in datamod.list_providers():
            self.source.addItem(f"Provider  —  {name}", f"provider:{name}")
        self.source.currentIndexChanged.connect(self._source_changed)
        tb.addWidget(self.source)

        self.symbol = QtWidgets.QLineEdit()
        self.symbol.setPlaceholderText("RELIANCE, TCS, NIFTY, BSE:SBIN, US:AAPL, BTC-USD")
        self.symbol.setFixedWidth(300)
        self.symbol.returnPressed.connect(self.on_load)
        tb.addWidget(self.symbol)

        self.interval = QtWidgets.QComboBox()
        self.interval.addItems(INTERVALS)
        self.range = QtWidgets.QComboBox()
        self.range.addItems(RANGES)
        tb.addWidget(self.interval)
        tb.addWidget(self.range)

        self.load_btn = QtWidgets.QPushButton("Load")
        self.load_btn.setObjectName("primary")
        self.load_btn.clicked.connect(self.on_load)
        tb.addWidget(self.load_btn)

        tb.addSeparator()
        menu = QtWidgets.QMenu(self)
        for key, text in INDICATORS:
            act = menu.addAction(text)
            act.setCheckable(True)
            act.toggled.connect(lambda on, k=key: self.chart.set_indicator(k, on))
        btn = QtWidgets.QToolButton()
        btn.setText("Indicators")
        btn.setMenu(menu)
        btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        tb.addWidget(btn)

    def _build_strategy_dock(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)

        row = QtWidgets.QHBoxLayout()
        self.templates = QtWidgets.QComboBox()
        self.templates.addItem("Load a template…", None)
        sdir = datamod.ROOT / "strategies"
        for f in sorted(sdir.glob("*.py")) if sdir.exists() else []:
            self.templates.addItem(f.stem.replace("_", " "), str(f))
        self.templates.activated.connect(self._template_chosen)
        row.addWidget(self.templates, 1)
        b_open, b_save = QtWidgets.QPushButton("Open…"), QtWidgets.QPushButton("Save…")
        b_open.clicked.connect(self.open_strategy)
        b_save.clicked.connect(self.save_strategy)
        row.addWidget(b_open)
        row.addWidget(b_save)
        v.addLayout(row)

        self.editor = CodeEditor()
        v.addWidget(self.editor, 1)

        row2 = QtWidgets.QHBoxLayout()
        row2.addWidget(QtWidgets.QLabel("Capital"))
        self.capital = QtWidgets.QDoubleSpinBox()
        self.capital.setRange(1000, 1e10)
        self.capital.setDecimals(0)
        self.capital.setSingleStep(10000)
        self.capital.setValue(100000)
        row2.addWidget(self.capital, 1)
        row2.addWidget(QtWidgets.QLabel("Fee % / side"))
        self.fee = QtWidgets.QDoubleSpinBox()
        self.fee.setRange(0, 5)
        self.fee.setDecimals(3)
        self.fee.setSingleStep(0.01)
        self.fee.setValue(0.05)
        row2.addWidget(self.fee)
        v.addLayout(row2)

        self.run_btn = QtWidgets.QPushButton("▶  Run backtest")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self.run_backtest)
        v.addWidget(self.run_btn)

        self.strategy_dock = QtWidgets.QDockWidget("Strategy (Python)", self)
        self.strategy_dock.setWidget(w)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, self.strategy_dock)

    def _build_results_dock(self):
        self.tabs = QtWidgets.QTabWidget()

        self.summary = QtWidgets.QTableWidget(0, 4)  # two metric columns side by side
        self.summary.horizontalHeader().setVisible(False)
        self.summary.verticalHeader().setVisible(False)
        self.summary.verticalHeader().setDefaultSectionSize(30)
        self.summary.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.summary.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.summary.setShowGrid(False)
        self.tabs.addTab(self.summary, "Summary")

        cols = ["#", "Entry time", "Exit time", "Entry", "Exit", "Qty", "P&L", "P&L %", "Bars"]
        self.trades_tbl = QtWidgets.QTableWidget(0, len(cols))
        self.trades_tbl.setHorizontalHeaderLabels(cols)
        self.trades_tbl.verticalHeader().setVisible(False)
        self.trades_tbl.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.trades_tbl.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.trades_tbl.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.trades_tbl.setAlternatingRowColors(True)
        self.trades_tbl.cellClicked.connect(self._trade_clicked)
        self.tabs.addTab(self.trades_tbl, "Trades")

        self.equity_axis = DateAxis("bottom")
        self.equity_plot = pg.PlotWidget(axisItems={"bottom": self.equity_axis})
        self.equity_plot.showGrid(x=True, y=True, alpha=0.12)
        self.equity_plot.addLegend(offset=(10, 10))
        self.tabs.addTab(self.equity_plot, "Equity curve")

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.tabs.addTab(self.log, "Log")

        self.results_dock = QtWidgets.QDockWidget("Backtest results", self)
        self.results_dock.setWidget(self.tabs)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, self.results_dock)
        self.resizeDocks([self.strategy_dock], [520], QtCore.Qt.Orientation.Horizontal)
        self.resizeDocks([self.results_dock], [290], QtCore.Qt.Orientation.Vertical)

    def _build_menu(self):
        mb = self.menuBar()
        f = mb.addMenu("File")
        f.addAction("Open data file…", self.open_data_file, QtGui.QKeySequence("Ctrl+O"))
        f.addAction("Open strategy…", self.open_strategy)
        f.addAction("Save strategy…", self.save_strategy, QtGui.QKeySequence("Ctrl+S"))
        f.addSeparator()
        f.addAction("Quit", self.close, QtGui.QKeySequence("Ctrl+Q"))
        s = mb.addMenu("Strategy")
        run = QtGui.QAction("Run backtest", self)
        run.setShortcuts([QtGui.QKeySequence("Ctrl+R"), QtGui.QKeySequence("Ctrl+Return")])
        run.triggered.connect(self.run_backtest)
        s.addAction(run)
        v = mb.addMenu("View")
        v.addAction(self.strategy_dock.toggleViewAction())
        v.addAction(self.results_dock.toggleViewAction())

    # ------------------------------------------------------------------ settings
    def _restore(self):
        st = self.settings
        self.symbol.setText(st.value("symbol", "RELIANCE"))
        self.interval.setCurrentText(st.value("interval", "1d"))
        self.range.setCurrentText(st.value("range", "2y"))
        i = self.source.findData(st.value("source", "yahoo"))
        self.source.setCurrentIndex(max(i, 0))
        code = st.value("strategy", "")
        if not code:
            f = datamod.ROOT / "strategies" / "sma_cross.py"
            code = f.read_text() if f.exists() else FALLBACK_STRATEGY
        self.editor.setPlainText(code)
        geo = st.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        self._source_changed()

    def closeEvent(self, e):
        st = self.settings
        st.setValue("symbol", self.symbol.text())
        st.setValue("interval", self.interval.currentText())
        st.setValue("range", self.range.currentText())
        st.setValue("source", self.source.currentData())
        st.setValue("strategy", self.editor.toPlainText())
        st.setValue("geometry", self.saveGeometry())
        super().closeEvent(e)

    # ------------------------------------------------------------------ data loading
    def _source_changed(self, *_):
        is_file = self.source.currentData() == "file"
        for w in (self.symbol, self.interval, self.range):
            w.setEnabled(not is_file)
        self.load_btn.setText("Open file…" if is_file else "Load")

    def on_load(self, silent=False):
        src = self.source.currentData()
        if src == "file":
            self.open_data_file()
            return
        sym = self.symbol.text().strip()
        if not sym:
            self.statusBar().showMessage("Type a symbol first")
            return
        interval, rng = self.interval.currentText(), self.range.currentText()
        if src == "yahoo":
            fn = lambda: datamod.load_yahoo(sym, interval, rng)  # noqa: E731
        else:
            name = src.split(":", 1)[1]
            fn = lambda: datamod.load_provider(name, sym, interval, rng)  # noqa: E731
        self._start_load(fn, silent)

    def open_data_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open price data", str(Path.home()), "Price data (*.csv *.tsv *.txt *.parquet);;All files (*)")
        if not path:
            return
        self.source.setCurrentIndex(self.source.findData("file"))
        self._start_load(lambda: datamod.load_file(path), False)

    def _start_load(self, fn, silent=False):
        self.load_btn.setEnabled(False)
        self.statusBar().showMessage("Loading…")
        self._worker = LoadWorker(fn, self)
        self._worker.done.connect(self._on_loaded)
        self._worker.failed.connect(lambda msg: self._on_load_failed(msg, silent))
        self._worker.start()

    def _on_loaded(self, df, label):
        self.load_btn.setEnabled(True)
        self.df, self.label, self.result = df, label, None
        self.chart.set_data(df, label)
        self.summary.setRowCount(0)
        self.trades_tbl.setRowCount(0)
        self.equity_plot.clear()
        self.statusBar().showMessage(
            f"{label}: {len(df):,} bars  |  {df.index[0]:%d %b %Y} → {df.index[-1]:%d %b %Y}")

    def _on_load_failed(self, msg, silent):
        self.load_btn.setEnabled(True)
        self.statusBar().showMessage(msg)
        self._log(msg)
        if not silent:
            QtWidgets.QMessageBox.warning(self, "Could not load data", msg)

    # ------------------------------------------------------------------ strategy files
    def _template_chosen(self, idx):
        path = self.templates.itemData(idx)
        if path:
            self.editor.setPlainText(Path(path).read_text())
        self.templates.setCurrentIndex(0)

    def open_strategy(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open strategy", str(Path.home()), "Python (*.py);;All files (*)")
        if path:
            self.editor.setPlainText(Path(path).read_text())

    def save_strategy(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save strategy", str(Path.home() / "my_strategy.py"), "Python (*.py)")
        if path:
            Path(path).write_text(self.editor.toPlainText())
            self.statusBar().showMessage(f"Saved {path}")

    # ------------------------------------------------------------------ backtest
    def _log(self, text):
        self.log.appendPlainText(text.rstrip())

    def run_backtest(self):
        if self.df is None:
            self.statusBar().showMessage("Load some data first")
            return
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                sig = bt.run_user_strategy(self.editor.toPlainText(), self.df)
            res = bt.backtest(self.df, sig.buy, sig.sell, self.capital.value(), self.fee.value())
        except Exception:  # noqa: BLE001 - show user-code errors in the Log tab
            self._log(buf.getvalue() + traceback.format_exc())
            self.tabs.setCurrentWidget(self.log)
            self.statusBar().showMessage("Strategy error — see the Log tab")
            return

        if buf.getvalue():
            self._log(buf.getvalue())
        self.result = res
        self.chart.set_strategy_overlays(sig.overlays)
        self.chart.set_trades(res.trades)
        self._fill_summary(res)
        self._fill_trades(res.trades)
        self._fill_equity(res)
        self.tabs.setCurrentWidget(self.summary)
        m = res.metrics
        self.statusBar().showMessage(
            f"Backtest: {m['trades']} trades, net {m['total_return'] * 100:+.2f}% "
            f"(buy & hold {m['buy_hold'] * 100:+.2f}%), max drawdown {m['max_drawdown'] * 100:.2f}%")

    def _fill_summary(self, res):
        rows = bt.format_metrics(res.metrics)
        half = (len(rows) + 1) // 2
        self.summary.setRowCount(half)
        for n, (k, v) in enumerate(rows):
            r, c = n % half, 0 if n < half else 2
            ki, vi = QtWidgets.QTableWidgetItem(k), QtWidgets.QTableWidgetItem(v)
            ki.setForeground(QtGui.QColor(theme.MUTED))
            if k == "Net profit":
                vi.setForeground(QtGui.QColor(theme.UP if res.metrics["net_profit"] >= 0 else theme.DOWN))
            self.summary.setItem(r, c, ki)
            self.summary.setItem(r, c + 1, vi)

    def _fill_trades(self, trades):
        intraday = self.chart.axes[0].intraday
        tf = "%d %b %y %H:%M" if intraday else "%d %b %Y"
        self.trades_tbl.setRowCount(len(trades))
        for r, t in enumerate(trades.itertuples(index=False)):
            vals = [str(r + 1), f"{t.entry_time:{tf}}", f"{t.exit_time:{tf}}" + (" (open)" if t.open else ""),
                    f"{t.entry_price:,.2f}", f"{t.exit_price:,.2f}", f"{t.qty:,.2f}",
                    f"{t.pnl:+,.2f}", f"{t.pnl_pct * 100:+.2f}%", str(t.bars)]
            for c, text in enumerate(vals):
                item = QtWidgets.QTableWidgetItem(text)
                if c in (6, 7):
                    item.setForeground(QtGui.QColor(theme.UP if t.pnl >= 0 else theme.DOWN))
                self.trades_tbl.setItem(r, c, item)

    def _fill_equity(self, res):
        self.equity_axis.set_index(self.df.index)
        self.equity_plot.clear()
        x = np.arange(len(res.equity))
        self.equity_plot.plot(x, res.buy_hold.to_numpy(), pen=pg.mkPen(theme.MUTED, width=1.2), name="Buy & hold")
        self.equity_plot.plot(x, res.equity.to_numpy(), pen=pg.mkPen(theme.ACCENT, width=2), name="Strategy")

    def _trade_clicked(self, row, _col):
        if self.result is None or row >= len(self.result.trades):
            return
        t = self.result.trades.iloc[row]
        self.chart.focus_range(t.entry_idx - 25, t.exit_idx + 25)


def main():
    theme.apply_pg_config()
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("ChartLab")
    app.setStyleSheet(theme.STYLESHEET)
    win = MainWindow()
    win.resize(1560, 940)
    win.show()
    QtCore.QTimer.singleShot(300, lambda: win.on_load(silent=True))
    sys.exit(app.exec())
