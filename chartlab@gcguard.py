"""Python's cycle collector, kept on the GUI thread.

Python collects cyclic garbage in whichever thread happens to make the allocation that tips its counter. Here that
was often a worker - a data load, the live-price poll, a request to the local API - and the garbage held Qt objects:
an indicator pane taken off the chart leaves its plot behind, with pyqtgraph's own option menu (a couple of hundred
Qt objects in one cycle). Their Qt side was then torn down from the wrong thread while the GUI thread was using it,
and the app froze for good or crashed, at random - minutes after the pane was closed. (pyqtgraph ships the same
remedy, util/garbage_collector.py; the idea is Erik Janssens'.)

So the automatic collector is switched off and a timer on the GUI thread does its work instead, generation by
generation, when the same counters say it is due. A library that asks for a collection from another thread has it
done here on the next tick.
"""
from __future__ import annotations

import gc
import threading

from PySide6 import QtCore

EVERY_MS = 1000
_state: dict = {"guard": None, "collect": None}


class Guard(QtCore.QObject):
    def __init__(self, parent=None, every_ms: int = EVERY_MS):
        super().__init__(parent)
        self.home = threading.get_ident()
        self.threshold = gc.get_threshold()
        self.asked: list = []                # generations other threads asked for
        self.runs = [0, 0, 0]                # collections done here, by generation
        gc.disable()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(int(every_ms))
        self.timer.timeout.connect(self.check)
        self.timer.start()

    def due(self) -> int | None:
        """The generation the automatic collector would take now (None: nothing is due)."""
        c0, c1, c2 = gc.get_count()
        t0, t1, t2 = self.threshold
        if c0 <= t0:
            return None
        if c1 > t1:
            return 2 if c2 > t2 else 1
        return 0

    def check(self):
        if gc.isenabled():                   # something switched it back on
            gc.disable()
        g = self.due()
        if self.asked:
            want = max(self.asked)
            del self.asked[:]
            g = want if g is None else max(g, want)
        if g is not None:
            self.collect(g)

    def collect(self, generation: int = 2) -> int:
        self.runs[generation] += 1
        return _state["collect"](generation)


def _collect(generation: int = 2) -> int:
    """gc.collect while the guard is on: at once on the GUI thread, on its next tick from any other."""
    g = _state["guard"]
    if g is None or threading.get_ident() == g.home:
        return _state["collect"](generation)
    g.asked.append(int(generation))
    return 0


def _alive(g) -> bool:
    try:
        from shiboken6 import isValid
        return g is not None and isValid(g)
    except Exception:  # noqa: BLE001
        return g is not None


def guard():
    return _state["guard"] if _alive(_state["guard"]) else None


def install(every_ms: int = EVERY_MS):
    """Switch it on (once; call on the GUI thread, with the application made)."""
    g = _state["guard"]
    if _alive(g):
        return g
    if _state["collect"] is None:
        _state["collect"] = gc.collect
        gc.collect = _collect
    g = _state["guard"] = Guard(QtCore.QCoreApplication.instance(), every_ms)
    return g


def uninstall():
    """Python's own collector again (tests)."""
    g = _state["guard"]
    _state["guard"] = None
    if _alive(g):
        g.timer.stop()
        g.deleteLater()
    if _state["collect"] is not None:
        gc.collect = _state["collect"]
        _state["collect"] = None
    gc.enable()
