"""Auto-trade: the strategy in the editor (def strategy(df)) trades by itself on every chosen broker account.

Each time a bar closes on the armed chart the strategy is evaluated again; when its target position changes on
that just-closed bar, ONE order goes to all chosen accounts at the same instant (dhan_core.place_many).
Accounts marked paper only print the order. Nothing is ever sent for signals older than the moment you armed it."""
from __future__ import annotations

import io

import numpy as np
from PySide6 import QtCore, QtWidgets

from . import accounts as acctmod
from . import backtest as bt
from . import theme


def position_series(sig, n: int) -> np.ndarray:
    """Target position per bar (1 long, 0 flat, -1 short) implied by the strategy's signals."""
    if sig.position is not None:
        return np.sign(sig.position.to_numpy(float))
    z = np.zeros(n, bool)
    buy = sig.buy.to_numpy(bool) | (sig.long.to_numpy(bool) if sig.long is not None else z)
    short = sig.short.to_numpy(bool) if sig.short is not None else z
    sell = sig.sell.to_numpy(bool)
    exits = sig.exits.to_numpy(bool) if sig.exits is not None else z
    out = np.zeros(n)
    pos = 0
    for i in range(n):
        if exits[i]:
            pos = 0
        if buy[i]:
            pos = 1
        elif short[i]:
            pos = -1
        elif sell[i] and pos == 1:
            pos = 0
        out[i] = pos
    return out


def decide(code: str, df, live_pos: int, qty: int):
    """-> (side | None, order qty, new position, closed-bar time, log text). Looks only at the bar that just closed
    (the last row is still forming) and acts only if the strategy's target changed ON that bar."""
    buf = io.StringIO()
    if df is None or len(df) < 4:
        return None, 0, live_pos, None, ""
    sig = bt.run_user_strategy(code, df, out=buf)
    pos = position_series(sig, len(df))
    i = len(df) - 2
    now, before = int(pos[i]), int(pos[i - 1])
    if now == before or now == live_pos:
        return None, 0, live_pos, df.index[i], buf.getvalue()
    diff = (now - live_pos) * qty
    return ("BUY" if diff > 0 else "SELL"), abs(diff), now, df.index[i], buf.getvalue()


class AutoTradeDialog(QtWidgets.QDialog):
    def __init__(self, parent, accts: list[dict], symbol: str, interval: str):
        super().__init__(parent)
        self.setWindowTitle("Auto-trade")
        self.setMinimumWidth(560)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(22, 18, 22, 16)
        v.setSpacing(10)
        v.addWidget(QtWidgets.QLabel("<span style='font-size:17px;font-weight:600;color:#fff'>"
                                     f"Auto-trade the strategy on {symbol} · {interval}</span>"))
        info = QtWidgets.QLabel(
            "Every time a bar closes, the strategy in the editor runs again. When it gives a new buy / sell on that "
            "bar, <b>one order goes to every ticked account at the same moment</b>. Old signals are never traded.<br>"
            f"<span style='color:{theme.DOWN}'>Accounts marked LIVE get REAL orders with real money. "
            "PAPER accounts only write the order in the Log.</span>")
        info.setWordWrap(True)
        v.addWidget(info)
        self.list = QtWidgets.QListWidget()
        for a in accts:
            it = QtWidgets.QListWidgetItem(f"{a['name']}   —   " + ("LIVE orders" if a.get("live") else "paper"))
            it.setFlags(it.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.CheckState.Unchecked if a.get("live") else QtCore.Qt.CheckState.Checked)
            it.setSizeHint(QtCore.QSize(0, 34))
            it.setData(QtCore.Qt.ItemDataRole.UserRole, a["name"])
            self.list.addItem(it)
        v.addWidget(self.list)
        g = QtWidgets.QGridLayout()
        self.qty = QtWidgets.QSpinBox()
        self.qty.setRange(1, 1_000_000)
        self.qty.setValue(1)
        self.product = QtWidgets.QComboBox()
        self.product.addItem("Intraday (MIS)", "INTRADAY")
        self.product.addItem("Delivery (CNC)", "CNC")
        self.exchange = QtWidgets.QComboBox()
        self.exchange.addItems(["NSE", "BSE"])
        for r, (lab, w) in enumerate((("Quantity per order", self.qty), ("Product", self.product),
                                      ("Exchange", self.exchange))):
            g.addWidget(QtWidgets.QLabel(lab), r, 0)
            g.addWidget(w, r, 1)
        g.setColumnStretch(1, 1)
        v.addLayout(g)
        note = QtWidgets.QLabel("Each account's own quantity multiplier (Accounts) is applied on top of this quantity.")
        note.setStyleSheet(f"color:{theme.MUTED};font-size:12px;")
        note.setWordWrap(True)
        v.addWidget(note)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        ok = bb.addButton("Start auto-trade", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setObjectName("primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def chosen(self) -> list[str]:
        return [self.list.item(i).data(QtCore.Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == QtCore.Qt.CheckState.Checked]


class _OrderWorker(QtCore.QThread):
    done = QtCore.Signal(object, str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn(), "")
        except Exception as e:  # noqa: BLE001
            self.done.emit(None, f"{type(e).__name__}: {e}")


class AutoTrader(QtCore.QObject):
    """State of one armed auto-trade. on_bars(df) is called whenever the armed chart gets fresh data."""
    log = QtCore.Signal(str)
    changed = QtCore.Signal()
    traded = QtCore.Signal(str, str)          # title, text (for a toast)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.active = False
        self.pos = 0
        self._workers = []

    def start(self, panel, code: str, names: list[str], qty: int, product: str, exchange: str):
        self.panel, self.code, self.names = panel, code, list(names)
        self.qty, self.product, self.exchange = int(qty), product, exchange
        self.symbol = str(panel.params.get("symbol", "")).upper()
        self.key = (self.symbol, panel.params.get("interval"))
        df = panel.df
        self.last_bar = df.index[-2] if df is not None and len(df) > 1 else None   # nothing before now is traded
        self.pos = 0
        self.active = True
        self.log.emit(f"--- AUTO-TRADE ON: {self.symbol} {self.key[1]}, qty {self.qty}, accounts: "
                      + ", ".join(self.names) + "  (waiting for the next bar to close)")
        self.changed.emit()

    def stop(self, why: str = "stopped by you"):
        if self.active:
            self.active = False
            self.log.emit(f"--- AUTO-TRADE OFF ({why}); strategy position was {self.pos:+d}. "
                          "Open positions at the broker are NOT closed automatically.")
            self.changed.emit()

    def on_bars(self, panel):
        if not self.active or panel is not self.panel:
            return
        if (str(panel.params.get("symbol", "")).upper(), panel.params.get("interval")) != self.key:
            self.stop("the chart was changed to another symbol / interval")
            return
        df = panel.df
        if df is None or len(df) < 4 or df.index[-2] == self.last_bar:
            return
        self.last_bar = df.index[-2]
        try:
            side, n, new_pos, when, out = decide(self.code, df, self.pos, self.qty)
        except Exception as e:  # noqa: BLE001
            self.log.emit(f"[auto-trade] strategy error, nothing sent: {type(e).__name__}: {e}")
            return
        if out.strip():
            self.log.emit(out)
        if side is None:
            return
        self.pos = new_pos
        by = {a["name"]: a for a in acctmod.load_accounts()}
        accts = [by[x] for x in self.names if x in by]
        price = float(df["Close"].iloc[-2])
        self.log.emit(f"[auto-trade] {when:%d %b %H:%M} bar closed at {price:,.2f}: {side} {n} {self.symbol} "
                      f"-> {len(accts)} accounts (strategy position now {new_pos:+d})")
        sym, product, exchange = self.symbol.split(":")[-1], self.product, self.exchange

        def send():
            from . import dhan_core
            return dhan_core.place_many(accts, sym, side, n, product=product, exchange=exchange, tag="auto")
        w = _OrderWorker(send, self)
        self._workers.append(w)

        def finished(res, err, w=w):
            if w in self._workers:
                self._workers.remove(w)
            if err:
                self.log.emit(f"[auto-trade] ORDER NOT SENT: {err}")
                self.traded.emit("Auto-trade: order failed", err[:140])
                return
            lines, bad = [], 0
            for r in res:
                if r.get("paper"):
                    lines.append(f"[auto-trade] {r['account']}: PAPER {side} {r['request'].get('quantity', n)}")
                elif r.get("ok"):
                    lines.append(f"[auto-trade] {r['account']}: LIVE order sent in {r.get('ms', 0)} ms  {r.get('response')}")
                else:
                    bad += 1
                    lines.append(f"[auto-trade] {r['account']}: FAILED  {r.get('error')}")
            self.log.emit("\n".join(lines))
            self.traded.emit(f"Auto-trade: {side} {n} {self.symbol}",
                             f"{len(res) - bad} of {len(res)} accounts OK" + (f", {bad} failed (see Log)" if bad else ""))
        w.done.connect(finished)
        w.start()
