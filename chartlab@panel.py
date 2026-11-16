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
    early = QtCore.Signal(object, str)          # a saved copy, shown at once while the fresh bars are on their way

    def __init__(self, fn, parent=None, early_fn=None):
        super().__init__(parent)
        self.fn = fn
        self.early_fn = early_fn

    def run(self):
        if self.early_fn is not None:
            try:
                r = self.early_fn()
                if r is not None:
                    self.early.emit(r[0], r[1])
            except Exception:  # noqa: BLE001 - a bad saved copy only means: wait for the fresh one
                pass
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

    multi = False

    def set_active(self, on: bool):
        self._on = on
        if not self.multi:
            self.setStyleSheet("ChartPanel { border: 0; }")
        else:
            self.setStyleSheet(
                f"ChartPanel {{ border: 1px solid {"#d1d4dc" if on else theme.BORDER}; }}")

    @property
    def symbol_key(self) -> str:
        sym = self.params.get("symbol", "")
        if self.params.get("source") == "file":
            return "file:" + self.params.get("path", "")
        return datamod.normalize_symbol(sym) if self.params.get("source") == "yahoo" else sym.upper()

    # ------------------------------------------------------------------ loading
    refreshed = QtCore.Signal(object)

    @staticmethod
    def _load_auto(symbol, interval, rng):
        from . import accounts
        loaders = [(f"API {a['name']}", lambda s, i, r, n=a["name"]: accounts.load_api(n, s, i, r))
                   for a in accounts.load_accounts() if a.get("broker") == "dhan" and a.get("access_token")]
        return datamod.load_auto(symbol, interval, rng, loaders)

    def refresh(self) -> bool:
        """Quietly fetch the same symbol again (live / daily auto update). The view, drawings and zoom stay put;
        nothing happens on screen unless new bars or a changed last bar arrive."""
        ch = self.chart
        if (self.loading or self.df is None or ch.replay["active"] or ch._panning() or ch.layer.tool
                or ch.layer.pending or ch.layer._drag
                or QtWidgets.QApplication.mouseButtons() != QtCore.Qt.MouseButton.NoButton):
            return False
        prm = self.params
        self.load(prm["source"], prm["symbol"], prm["interval"], prm["range"], prm.get("path", ""), quiet=True)
        return True

    def load(self, source: str, symbol: str, interval: str, rng: str, path: str = "", quiet: bool = False):
        self._prev_params = dict(self.params)
        self.params = {"source": source, "symbol": symbol, "interval": interval, "range": rng, "path": path}
        if source == "file":
            fn = lambda: self._load_local(path, interval)  # noqa: E731
        elif source == "auto":
            fn = lambda: self._load_auto(symbol, interval, rng)  # noqa: E731
        elif source == "yahoo":
            fn = lambda: datamod.load_yahoo(symbol, interval, rng)  # noqa: E731
        elif source.startswith("provider:"):
            name = source.split(":", 1)[1]
            fn = lambda: datamod.load_provider(name, symbol, interval, rng)  # noqa: E731
        elif source.startswith("api:"):
            from . import accounts
            acct = source.split(":", 1)[1]
            fn = lambda: accounts.load_api(acct, symbol, interval, rng)  # noqa: E731
        else:
            self._fail(f"Unknown data source '{source}'")
            return
        self.loading = True
        self.last_error = ""
        self.load_event.clear()
        self._quiet = quiet
        if not quiet:
            self.chart.header.setText(f"Loading {symbol} …")
            self.loadStarted.emit(self)
        early = None
        if not quiet and source == "yahoo":
            early = lambda: datamod.peek_cache(symbol, interval, rng)  # noqa: E731
        elif not quiet and source == "auto":
            early = lambda: (None if datamod.find_local(symbol) is not None  # noqa: E731
                             else datamod.peek_cache(symbol, interval, rng))
        import time
        self._t0 = time.perf_counter()
        self.load_ms, self.from_cache = None, False
        self._worker = LoadWorker(fn, self, early)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._fail)
        self._worker.early.connect(self._on_early)
        self._worker.start()

    def _stale(self) -> bool:
        """True when this result belongs to an older request (the symbol was changed again meanwhile)."""
        s = self.sender()
        return isinstance(s, LoadWorker) and s is not getattr(self, "_worker", None)

    def _on_early(self, df, label):
        import time
        if self._stale() or getattr(self, "_quiet", False) or df is None or not len(df):
            return
        self.df, self.label, self.result, self.signals = df, label, None, None
        self.chart.set_data(df, label)
        self.last_error = ""
        self.load_ms, self.from_cache = (time.perf_counter() - self._t0) * 1000, True
        self._quiet = True                      # the fresh bars then slide in without disturbing the view
        self.loadFinished.emit(self)

    _raw_cache: dict = {}

    @classmethod
    def _load_local(cls, path, interval):
        import os
        key = (path, os.path.getmtime(path))
        if key not in cls._raw_cache:
            cls._raw_cache.clear()
            cls._raw_cache[key] = datamod.load_file(path)
        raw, label = cls._raw_cache[key]
        if interval in ("auto", "", None):
            return raw, label
        df, note = datamod.to_timeframe(raw, interval)
        return df, label + (f"  [{interval}]" if not note else f"  [{note}]")

    def set_dataframe(self, df, label: str):
        """Synchronously show an already-loaded DataFrame (tests, API, demo data)."""
        self._on_done(df, label)

    def _on_done(self, df, label):
        import time
        if self._stale():
            return
        self.loading = False
        if getattr(self, "_quiet", False) and self.df is not None:
            self._quiet = False
            self.load_event.set()
            if self.from_cache:
                self.from_cache = False
                self.fresh_ms = (time.perf_counter() - self._t0) * 1000
            self._apply_refresh(df, label)
            return
        if getattr(self, "_t0", None) is not None:
            self.load_ms = (time.perf_counter() - self._t0) * 1000
        self.df, self.label, self.result, self.signals = df, label, None, None
        self.chart.set_data(df, label)
        self.last_error = ""
        self.load_event.set()
        self.loadFinished.emit(self)

    def _apply_refresh(self, df, label):
        old, ch = self.df, self.chart
        try:
            same = (len(df) == len(old) and df.index[-1] == old.index[-1]
                    and bool((df.iloc[-1] == old.iloc[-1]).all()))
        except Exception:  # noqa: BLE001
            same = False
        if same or df is None or not len(df) or ch.replay["active"] or ch.layer._drag or ch.layer.pending:
            self.changed = False
            self.refreshed.emit(self)
            return
        self.changed = True
        (x0, x1), auto_y = ch.price.vb.viewRange()[0], ch.auto_y
        yr = ch.price.vb.viewRange()[1]
        grew = len(df) - len(old)
        at_edge = x1 >= len(old) - 1                       # looking at the newest bars -> keep following them
        drawings, sel = ch.layer.to_json(), ch.layer.selected
        self.df, self.label = df, label
        ch.set_data(df, label)
        ch.layer.load_json(drawings)
        ch.layer.selected = sel
        shift = grew if (at_edge and 0 < grew < 500) else 0
        ch.price.setXRange(x0 + shift, x1 + shift, padding=0)
        ch.auto_y = auto_y
        if auto_y:
            ch._autoscale()
        else:
            ch.price.setYRange(yr[0], yr[1], padding=0)
        self.refreshed.emit(self)

    def _fail(self, msg: str):
        if self._stale():
            return
        self.loading = False
        if getattr(self, "_quiet", False):                 # a background refresh that failed: keep the chart as is
            self._quiet = False
            self.load_event.set()
            return
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
