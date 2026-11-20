"""One chart pane: owns its data, symbol/timeframe, indicators, drawings and last backtest result."""
from __future__ import annotations

import re
import threading
import time

import numpy as np
import pandas as pd
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
            col = "#d1d4dc" if on else theme.BORDER
            self.setStyleSheet(f"ChartPanel {{ border: 1px solid {col}; }}")

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
        """Source Auto: your own file, else every logged-in broker account in turn (the healthy ones first, one
        that just failed last), else Yahoo."""
        from . import accounts
        from .dhan_core import is_option
        opt = is_option(symbol)                            # option prices only come from a broker
        loaders = accounts.data_loaders(include_failed=opt)
        if opt:
            return ChartPanel._load_option(symbol, interval, rng, loaders)
        return datamod.load_auto(symbol, interval, rng, loaders)

    @staticmethod
    def _load_option(symbol, interval, rng, loaders):
        from . import store
        errors = []
        for name, fn in loaders:
            try:
                return fn(symbol, interval, rng)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {e}")
        got = store.option_bars(symbol, interval) if store.active() else None
        if got is not None:                                # an expired contract (or no broker right now): the archive
            return got
        if not loaders:
            raise RuntimeError("Option charts need a Dhan account: Accounts \u2192 Manage accounts, paste the client id "
                               "and access token. (Yahoo has no NSE option prices.)")
        raise RuntimeError("  |  ".join(str(x)[:160] for x in errors))

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
        from . import store
        from . import timeframes as TF
        stored = store.active() and source != "file" and not source.startswith("provider:")

        def kept(loader):
            """The loader, with the data store behind it: what it fetched is kept, and the chart gets the stored
            older bars in front (a 1-minute chart is not cut to the source's last few days)."""
            if not stored:
                return loader

            def run(s, i, r):
                df, label = loader(s, i, r)
                if str(label).endswith("my data"):          # your own file is yours: it is shown as it is
                    return df, label
                return store.keep(s, i, r, df, label)
            return run

        if source == "file":
            fn = lambda: self._load_local(path, interval)  # noqa: E731
        elif source == "auto" or (source == "yahoo" and self._is_option(symbol)):   # (Yahoo has no NSE options)
            fn = lambda: TF.load(kept(self._load_auto), symbol, interval, rng)  # noqa: E731
        elif source == "yahoo" and stored:
            fn = lambda: TF.load(kept(datamod.load_yahoo), symbol, interval, rng, TF.YAHOO, TF.YAHOO)  # noqa: E731
        elif source == "yahoo":
            fn = lambda: datamod.load_yahoo(symbol, interval, rng)  # noqa: E731
        elif source.startswith("provider:"):
            name = source.split(":", 1)[1]
            fn = lambda: TF.load(lambda s, i, r: datamod.load_provider(name, s, i, r), symbol, interval, rng)  # noqa: E731
        elif source.startswith("api:"):
            from . import accounts
            acct = source.split(":", 1)[1]
            fn = lambda: TF.load(kept(lambda s, i, r: accounts.load_api(acct, s, i, r)), symbol, interval, rng)  # noqa: E731
        else:
            self._fail(f"Unknown data source '{source}'")
            return
        if stored:
            fresh = fn

            def fn():                                       # no source answers (offline, login gone): the stored bars
                try:
                    return fresh()
                except Exception:
                    got = store.saved(symbol, interval, rng)
                    if got is None:
                        raise
                    return got[0], got[1] + " (saved copy)"
        self.loading = True
        self.last_error = ""
        self.load_event.clear()
        self._quiet = quiet
        if not quiet:
            self.chart.header.setText(f"Loading {symbol} …")
            self.loadStarted.emit(self)
        early = None
        if self._is_option(symbol):
            pass
        elif not quiet and source == "yahoo":
            early = lambda: ((store.saved(symbol, interval, rng) if stored else None)  # noqa: E731
                             or datamod.peek_cache(symbol, interval, rng))
        elif not quiet and source == "auto":
            early = lambda: (None if datamod.find_local(symbol) is not None  # noqa: E731
                             else ((store.saved(symbol, interval, rng) if stored else None)
                                   or datamod.peek_cache(symbol, interval, rng)))
        elif not quiet and stored and source.startswith("api:"):
            early = lambda: store.saved(symbol, interval, rng)  # noqa: E731
        self._t0 = time.perf_counter()
        self.load_ms, self.from_cache = None, False
        self._worker = LoadWorker(fn, self, early)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._fail)
        self._worker.early.connect(self._on_early)
        self._worker.start()

    @staticmethod
    def _is_option(symbol) -> bool:
        from .dhan_core import is_option
        return is_option(symbol)

    def _canon(self, label):
        """'NIFTY 25000 CE' was typed, the contract that loaded is NIFTY261124C25000: remember that exact one, so
        drawings, alerts and the live feed belong to this contract and not to next week's."""
        first = str(label).split(" \u00b7 ")[0].strip()
        if self.params.get("source") != "file" and first and self._is_option(self.params.get("symbol", "")) \
                and " " not in first and self._is_option(first):
            self.params["symbol"] = first

    def _stale(self) -> bool:
        """True when this result belongs to an older request (the symbol was changed again meanwhile)."""
        s = self.sender()
        return isinstance(s, LoadWorker) and s is not getattr(self, "_worker", None)

    def _on_early(self, df, label):
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
        self._canon(label)
        self._tick, self._day_vol, self._live_bars = None, None, set()
        self._tf_state(df)
        self.chart.set_data(df, label)
        self.last_error = ""
        self.load_event.set()
        self.loadFinished.emit(self)

    def _tf_state(self, df):
        """How many trading days the newest bar of an N-day chart already holds (so a tick knows when to open a bar)."""
        at = getattr(df, "attrs", {}) or {}
        if "tf_days" in at:
            self._tf_days, self._tf_last_day = int(at["tf_days"]), at.get("tf_last_day")
        elif not getattr(self, "_live_bars", None):
            self._tf_days, self._tf_last_day = 1, (df.index[-1].normalize() if df is not None and len(df) else None)

    def _apply_refresh(self, df, label):
        old, ch = self.df, self.chart
        if not (getattr(self, "_live_bars", None) and "tf_days" not in (getattr(df, "attrs", {}) or {})):
            self._tf_state(df)
        try:
            same = (len(df) == len(old) and df.index[-1] == old.index[-1]
                    and bool((df.iloc[-1] == old.iloc[-1]).all()))
        except Exception:  # noqa: BLE001
            same = False
        if same or df is None or not len(df) or ch.replay["active"] or ch.layer._drag or ch.layer.pending:
            if same and label and label != self.label:     # the same bars from somewhere else ("saved copy")
                self.label = ch.label = label
            self.changed = False
            self.refreshed.emit(self)
            return
        live = getattr(self, "_live_bars", None)
        if live and len(old) and old.index[-1] > df.index[-1]:
            # bars the live feed opened that the broker's candle list does not have yet: they stay
            keep = old[(old.index > df.index[-1]) & old.index.isin(list(live))]
            if len(keep):
                df = pd.concat([df, keep])
        if live:
            self._live_bars = {t for t in live if t > df.index[0] and t >= df.index[-1]}
        self.changed = True
        if self._same_bars(old, df) and ch.replace_values(df):
            # the same bars with newer numbers: put them in place - no rebuild, the view and drawings are not touched
            self.df, self.label = df, label
            ch.label = label
            tk = getattr(self, "_tick", None)
            if tk and time.time() - tk[0] < 5:             # the live feed is ahead of the candle list: keep its price
                ch.update_last(tk[1])
            self.refreshed.emit(self)
            return
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

    @staticmethod
    def _same_bars(old, df) -> bool:
        try:
            return len(df) == len(old) and list(df.columns) == list(old.columns) and df.index.equals(old.index)
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ live ticks
    def bar_step(self) -> float:
        """Seconds in one bar: 0 = daily or longer."""
        iv = str(self.params.get("interval", ""))
        m = re.fullmatch(r"(\d+)(m|h)", iv)
        if m:
            return int(m[1]) * (60.0 if m[2] == "m" else 3600.0)
        if re.fullmatch(r"\d+(d|wk|mo)", iv):
            return 0.0
        s = self.chart.bar_seconds()
        return s if 0 < s < 80000 else 0.0

    def on_tick(self, ltp: float, t: float | None, day_volume: float | None = None) -> str:
        """One live price. t = trade time as seconds on the chart's own (exchange wall-clock) time axis, or None.
        -> 'same' (newest bar changed), 'new' (a bar was added), '' (ignored)."""
        from . import timeframes as TF
        ch = self.chart
        df = self.df
        if (df is None or not len(df) or self.loading or ch.replay["active"] or ch.candles is None
                or not ltp or ltp <= 0 or ch._limit is not None):
            return ""
        last_t = float(ch._times[-1])
        step = self.bar_step()
        iv = str(self.params.get("interval", ""))
        prev_dv, self._day_vol = getattr(self, "_day_vol", None), day_volume
        add = 0.0
        if day_volume and prev_dv and day_volume > prev_dv:
            add = float(day_volume - prev_dv)
        new_t = None
        if t is not None:
            if step > 0:
                anc = getattr(self, "_anchor", None)
                if anc is None or anc[0] is not df.index:
                    anc = self._anchor = (df.index, TF.anchor(df.index))
                start = TF.bar_start(t, step, anc[1])
                if start > last_t + 1e-6:
                    new_t = start                            # bars are counted from the session's start (09:15)
                elif t < last_t - step:
                    return ""                                # an old trade: not this bar
            else:
                a, b = pd.Timestamp(t, unit="s"), df.index[-1]
                code = iv if TF.valid(iv) else "1d"
                seen = getattr(self, "_tf_last_day", None)
                if seen is not None and a.normalize() <= seen:
                    newer = False
                elif TF.same_period(b, a, code, int(getattr(self, "_tf_days", 1) or 1)):
                    newer = False
                    if a.normalize() > (seen if seen is not None else b.normalize()):
                        self._tf_days = int(getattr(self, "_tf_days", 1) or 1) + 1      # one more day in this bar
                        self._tf_last_day = a.normalize()
                else:
                    newer = True
                if newer:
                    new_t = float(a.normalize().value // 10 ** 9)
                    self._tf_days, self._tf_last_day = 1, a.normalize()
        self._tick = (time.time(), float(ltp))
        if new_t is None:
            vol = float(ch._vols[-1]) + add if add else None
            return "same" if ch.update_last(float(ltp), volume=vol) else ""
        if ch.layer._drag or ch.layer.pending or ch._panning() \
                or QtWidgets.QApplication.mouseButtons() != QtCore.Qt.MouseButton.NoButton:
            return ""                                        # mid-gesture: the bar is added on the next tick
        row = {c: float(ltp) for c in ("Open", "High", "Low", "Close")}
        row["Volume"] = add
        extra = pd.DataFrame([{c: row.get(c, np.nan) for c in df.columns}],
                             index=pd.DatetimeIndex([pd.Timestamp(new_t, unit="s")], name=df.index.name))
        df2 = pd.concat([df, extra.astype(df.dtypes.to_dict(), errors="ignore")])
        if not hasattr(self, "_live_bars"):
            self._live_bars = set()
        self._live_bars.add(df2.index[-1])
        self._apply_refresh(df2, self.label)
        return "new"

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
