"""One chart pane: owns its data, symbol/timeframe, indicators, drawings and last backtest result."""
from __future__ import annotations

import threading

from PySide6 import QtCore, QtWidgets

from . import data as datamod
from . import theme
from .chart import ChartWidget


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


class ChartPanel(QtWidgets.QFrame):
    activated = QtCore.Signal(object)
    loadStarted = QtCore.Signal(object)
    loadFinished = QtCore.Signal(object)
    loadFailed = QtCore.Signal(object, str)
    drawingsChanged = QtCore.Signal(object)
    toolChanged = QtCore.Signal(object, object)

    def __init__(self, pid: int, parent=None):
        super().__init__(parent)
        self.pid = pid
        self.params = {"source": "yahoo", "symbol": "RELIANCE", "interval": "1d", "range": "2y", "path": ""}
        self.df = None
        self.label = ""
        self.result = None
        self.signals = None
        self.loading = False
        self.last_error = ""
        self.load_event = threading.Event()
        self.load_event.set()
        self._worker = None

        self.chart = ChartWidget()
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.addWidget(self.chart)
        self.chart.activated.connect(lambda: self.activated.emit(self))
        self.chart.drawingsChanged.connect(lambda: self.drawingsChanged.emit(self))
        self.chart.toolChanged.connect(lambda t: self.toolChanged.emit(self, t))
        self.set_active(False)

    def set_active(self, on: bool):
        self.setStyleSheet(
            f"ChartPanel {{ border: 2px solid {theme.ACCENT if on else theme.GRID}; border-radius: 2px; }}")

    @property
    def symbol_key(self) -> str:
        sym = self.params.get("symbol", "")
        if self.params.get("source") == "file":
            return "file:" + self.params.get("path", "")
        return datamod.normalize_symbol(sym) if self.params.get("source") == "yahoo" else sym.upper()

    # ------------------------------------------------------------------ loading
    def load(self, source: str, symbol: str, interval: str, rng: str, path: str = ""):
        self._prev_params = dict(self.params)
        self.params = {"source": source, "symbol": symbol, "interval": interval, "range": rng, "path": path}
        if source == "file":
            fn = lambda: datamod.load_file(path)  # noqa: E731
        elif source == "yahoo":
            fn = lambda: datamod.load_yahoo(symbol, interval, rng)  # noqa: E731
        elif source.startswith("provider:"):
            name = source.split(":", 1)[1]
            fn = lambda: datamod.load_provider(name, symbol, interval, rng)  # noqa: E731
        else:
            self._fail(f"Unknown data source '{source}'")
            return
        self.loading = True
        self.last_error = ""
        self.load_event.clear()
        self.chart.header.setText(f"Loading {symbol} …")
        self.loadStarted.emit(self)
        self._worker = LoadWorker(fn, self)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._fail)
        self._worker.start()

    def set_dataframe(self, df, label: str):
        """Synchronously show an already-loaded DataFrame (tests, API, demo data)."""
        self._on_done(df, label)

    def _on_done(self, df, label):
        self.loading = False
        self.df, self.label, self.result, self.signals = df, label, None, None
        self.chart.set_data(df, label)
        self.last_error = ""
        self.load_event.set()
        self.loadFinished.emit(self)

    def _fail(self, msg: str):
        self.loading = False
        self.last_error = msg
        if self.df is not None:
            self.params = getattr(self, "_prev_params", self.params)   # keep describing what is on screen
            self.chart._update_header(len(self.df) - 1)
        else:
            self.chart.header.setText("Could not load data")
        short = msg if len(msg) < 160 else msg[:157] + "\u2026"
        self.chart.header.setText(
            f"<span style='color:{theme.DOWN}'>\u26a0 {short}</span> &nbsp; "
            f"<span style='color:{theme.MUTED}'>{self.chart.label}</span>")
        self.load_event.set()
        self.loadFailed.emit(self, msg)
