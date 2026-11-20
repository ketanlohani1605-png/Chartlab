"""Two things that went wrong at random.

1. The app froze for good or crashed minutes after an indicator pane was closed. The closed pane's plot is cyclic
   garbage holding a couple of hundred Qt objects; Python collected it in whichever thread happened to allocate next
   - often a data load or the price poll - and Qt objects were torn down off the GUI thread. Now only the GUI thread
   collects (chartlab/gcguard.py).
2. A strategy's lines stopped the chart when a candle had formed since the test ran (one bar more than the line has
   values for).
"""
import os as _os; _os.environ["CHARTLAB_OFFLINE"] = "1"      # no public price feed: a live tick would add bars of its own
import faulthandler
import gc
import os
import sys
import tempfile
import threading
import time
import weakref

faulthandler.enable()
faulthandler.dump_traceback_later(150, exit=True)             # a freeze is a failure, not a hung test run
HOME = tempfile.mkdtemp()
os.environ["CHARTLAB_HOME"] = HOME
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from PySide6 import QtCore, QtTest, QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from chartlab import gcguard, theme  # noqa: E402

theme.apply_pg_config()
app.setStyle(theme.make_style())
app.setStyleSheet(theme.STYLESHEET)
from chartlab.mainwindow import MainWindow  # noqa: E402


def pump(ms=50):
    QtTest.QTest.qWait(ms)


def make_df(n=300, seed=3, start=24000.0, end="2026-10-30"):
    r = np.random.default_rng(seed)
    c = start * np.exp(np.cumsum(r.normal(0.0004, 0.011, n)))
    o = np.r_[c[0], c[:-1]] * (1 + r.normal(0, 0.002, n))
    h = np.maximum(o, c) * (1 + np.abs(r.normal(0, 0.005, n)))
    l = np.minimum(o, c) * (1 - np.abs(r.normal(0, 0.005, n)))
    v = r.lognormal(15, 0.4, n)
    return pd.DataFrame({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v},
                        index=pd.bdate_range(end=end, periods=n))


# ---------------------------------------------------------------- 1. garbage is collected on the GUI thread only
assert gc.isenabled(), "(Python's own collector, before the app starts)"
win = MainWindow(QtCore.QSettings(tempfile.mktemp(suffix=".ini"), QtCore.QSettings.Format.IniFormat))
g = gcguard.guard()
assert g is not None and not gc.isenabled(), "the automatic collector is off while the app runs"
assert gcguard.install() is g, "switched on once"
assert g.timer.isActive() and g.timer.interval() == gcguard.EVERY_MS
win.resize(1280, 820)
win.show()
pump(150)
df = make_df()
ch = win.active.chart
ch.set_data(df, "NIFTY")
win.active.label = "NIFTY"
pump(100)

MAIN = threading.get_ident()
where = []                                                    # (thread, generation) of every collection from here on


def note(phase, info):
    if phase == "start":
        where.append((threading.get_ident(), info["generation"]))


gc.callbacks.append(note)

# every indicator on, then off: the closed panes' plots stay behind as cyclic garbage
for a in win.ind_actions.values():
    a.setChecked(True)
    pump(10)
plots = [weakref.ref(pn.plot) for pn in ch.panes]
assert len(plots) >= 3, len(plots)
for a in win.ind_actions.values():
    a.setChecked(False)
pump(100)
assert not ch.panes
left_behind = sum(r() is not None for r in plots)

out = {}


def worker():
    # what a data load does: a few hundred thousand new containers, many in cycles. This used to start a
    # collection right here, in the worker.
    keep = []
    for i in range(300_000):
        a = [i]
        b = {"a": a}
        a.append(b)
        if i % 3 == 0:
            keep.append(b)
    out["asked"] = gc.collect()                               # ... and a library asking for one outright
    out["thread"] = threading.get_ident()


full_before = g.runs[2]
t = threading.Thread(target=worker)
t.start()
while t.is_alive():
    pump(20)
assert out["thread"] != MAIN
assert out["asked"] == 0, "a collection asked for from a worker is not done there"
assert all(tid == MAIN for tid, _ in where), f"a collection ran off the GUI thread: {where}"

t0 = time.time()                                              # the GUI thread's timer does it, within a tick or two
while (g.runs[2] == full_before or any(r() is not None for r in plots)) and time.time() - t0 < 10:
    pump(100)
assert g.runs[2] > full_before, "the worker's request is served on the GUI thread"
assert all(r() is None for r in plots), "closed panes are collected by the GUI thread"
assert where and all(tid == MAIN for tid, _ in where), where
win.ind_actions["rsi"].setChecked(True)                       # and the app carries on
pump(150)
assert len(ch.panes) == 1
win.ind_actions["rsi"].setChecked(False)
pump(50)

# the timer's own logic, driven by hand
g.timer.stop()
gc.collect()


class Node:
    pass


def garbage(n):
    refs = []
    for _ in range(n):
        a, b = Node(), Node()
        a.o, b.o = b, a
        refs.append(weakref.ref(a))
    return refs


kept = g.threshold
g.threshold = (10 ** 9, 10, 10)
refs = garbage(400)
assert g.due() is None
g.check()
assert all(r() is not None for r in refs), "nothing is collected before it is due (and never automatically)"
g.threshold = (50, 10, 10)
assert g.due() is not None
g.check()
assert all(r() is None for r in refs), "due: collected"
gc.enable()                                                   # a library switches the automatic collector back on
g.check()
assert not gc.isenabled(), "... and it is switched off again at the next tick"
g.threshold = kept
g.timer.start()

# ---------------------------------------------------------------- 2. a strategy line and a chart that moved on
ser = pd.Series(np.linspace(100.0, 110.0, len(df)), index=df.index)
ch.set_strategy_overlays({"ma": ser})
raw = ch._strategy_items[0][0]._raw
assert len(raw) == len(df) and raw[-1] == 110.0

nxt = df.iloc[[-1]].copy()                                    # a candle forms while the test is still running
nxt.index = [df.index[-1] + pd.Timedelta(days=3)]
df2 = pd.concat([df, nxt])
ch.set_data(df2, "NIFTY")
pump(50)
ch.set_strategy_overlays({"ma": ser})                         # (this raised: 301 bars, 300 values)
item = ch._strategy_items[0][0]
x, y = item._full
assert len(x) == len(y) == len(df2) == len(df) + 1
assert item._raw[-2] == 110.0 and np.isnan(item._raw[-1]), "the new bar has no value yet"
for mode in ("log", "percent", "regular"):                    # these redraw the stored lines on today's bars
    ch.set_scale_mode(mode)
    pump(20)
assert len(ch._strategy_items[0][0]._full[1]) == len(df2)

older = make_df(40, seed=9, end=str((df.index[0] - pd.Timedelta(days=3)).date()))
df3 = pd.concat([older, df2])                                 # older history arrives on the left
assert df3.index.is_unique and df3.index.is_monotonic_increasing
ch.set_data(df3, "NIFTY")
pump(50)
ch.set_strategy_overlays({"ma": ser})
raw = ch._strategy_items[0][0]._raw
assert len(raw) == len(df3)
assert np.isnan(raw[:40]).all() and raw[40] == 100.0 and raw[-2] == 110.0, "the line keeps its dates"

ch.set_strategy_overlays({"n": pd.Series(np.arange(10.0))})   # a script's plain numbers, no dates: from the left
raw = ch._strategy_items[0][0]._raw
assert len(raw) == len(df3) and list(raw[:10]) == list(np.arange(10.0)) and np.isnan(raw[10:]).all()
ch.set_strategy_overlays({"long": pd.Series(np.arange(len(df3) + 25.0))})      # longer than the chart: cut
assert len(ch._strategy_items[0][0]._raw) == len(df3)
ch.clear_strategy()

print(f"STABILITY OK  ({len(where)} collections, all on the GUI thread; {left_behind} closed panes were garbage)")
os._exit(0)
