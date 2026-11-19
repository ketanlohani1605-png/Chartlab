"""Trading for every broker account, the TradingView way.

  * TradingManager - which account trades, its orders / positions / holdings / funds (refreshed in the background),
    placing, changing, cancelling and closing, the LIVE-order confirmation, and the paper broker's price feed.
  * OrderTicket - the order panel: Buy / Sell, Market / Limit / Stop / Stop-limit, quantity or lots, product,
    validity, take-profit / stop-loss, order value, margin from the broker, available funds.
  * TradingPanel - the bottom "Trading Panel": account summary, Positions, Orders, Holdings, Account, Notifications.

Accounts that are not switched to LIVE (and the built-in "Paper trading" account) never reach a broker: their orders
fill on ChartLab's paper broker against the prices on screen."""
from __future__ import annotations

import datetime as dt
import time

from PySide6 import QtCore, QtGui, QtWidgets

from . import brokers, theme
from .brokers import BrokerError, OrderRequest
from .brokers.paper import Paper, _key

PAPER_NAME = "Paper trading"
RUPEE = "₹"
MINUS = "−"

_CSS = (f"QDialog, QWidget#tpanel {{ background: #1e1e1e; }} QLabel {{ background: transparent; }} "
        f"QLabel#muted {{ color: {theme.MUTED}; font-size: 12px; }} QLabel#h {{ color: white; font-size: 15px; "
        f"font-weight: 700; }} QLabel#big {{ color: white; font-size: 13px; font-weight: 600; }} "
        f"QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox {{ background: #121212; border: 1px solid #3a3a3a; "
        f"border-radius: 6px; padding: 6px 8px; min-height: 18px; color: #dbdbdb; }} "
        f"QDoubleSpinBox:focus, QSpinBox:focus, QLineEdit:focus {{ border-color: {theme.ACCENT}; }} "
        f"QDoubleSpinBox:disabled, QSpinBox:disabled {{ color: #555; }} "
        f"QPushButton#seg {{ background: transparent; border: 0; border-bottom: 2px solid transparent; color: "
        f"{theme.MUTED}; padding: 6px 10px; font-size: 13px; }} QPushButton#seg:checked {{ color: white; "
        f"border-bottom: 2px solid white; }} QPushButton#seg:hover {{ color: white; }} "
        f"QPushButton#chip {{ background: #262626; border: 1px solid #333; border-radius: 6px; padding: 5px 10px; "
        f"color: #b2b5be; }} QPushButton#chip:checked {{ background: #2a2e39; border-color: #868993; color: white; }} "
        f"QCheckBox {{ color: #dbdbdb; }}")


def money(v, sign: bool = False) -> str:
    if v is None:
        return "—"
    s = f"{abs(v):,.2f}"
    if sign:
        return ("+" if v >= 0 else MINUS) + RUPEE + s
    return (MINUS if v < 0 else "") + RUPEE + s


def all_accounts() -> list[dict]:
    from . import accounts
    return [{"name": PAPER_NAME, "broker": "paper"}] + accounts.load_accounts()


class Worker(QtCore.QThread):
    done = QtCore.Signal(object)
    failed = QtCore.Signal(str, str)                 # message, kind (auth / access / rejected / unknown / ...)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except ValueError as e:                      # our own order checks
            self.failed.emit(str(e), "input")
        except BrokerError as e:
            self.failed.emit(str(e), e.kind)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e), "error")


# ==================================================================================================== manager
class TradingManager(QtCore.QObject):
    updated = QtCore.Signal()                       # new orders / positions / funds
    accountChanged = QtCore.Signal()
    notify = QtCore.Signal(str, str)                # text, level: ok | warn | error | info
    instrumentReady = QtCore.Signal(str)            # SYMBOL whose instrument was just looked up

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.settings = win.settings
        self.name = str(self.settings.value("trade_account", PAPER_NAME) or PAPER_NAME)
        self.snap = self._blank()
        self.log: list = []                         # (time, level, text) - the Notifications tab
        self._jobs: set = set()
        self._refreshing = None
        self._again = False
        self._insts: dict = {}                      # (account, SYMBOL) -> Instrument | None
        self._looking: set = set()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(4000)
        self.timer.timeout.connect(lambda: self.refresh(quiet=True))
        self.timer.start()
        QtCore.QTimer.singleShot(300, self.refresh)

    @staticmethod
    def _blank():
        return {"orders": [], "positions": [], "holdings": [], "funds": None, "error": "", "kind": "", "ts": 0.0}

    # ------------------------------------------------------------------ account
    def accounts(self) -> list[dict]:
        return all_accounts()

    def account(self) -> dict:
        accts = self.accounts()
        return next((a for a in accts if a["name"] == self.name), accts[0])

    def set_account(self, name: str):
        if name == self.name:
            return
        self.name = name
        self.settings.setValue("trade_account", name)
        self.snap = self._blank()
        self.accountChanged.emit()
        self.updated.emit()
        self.refresh()

    def broker(self):
        return brokers.trading_broker(self.account())

    def data_broker(self):
        """The real broker behind the account (instrument lists, margin), even while it trades on paper."""
        a = self.account()
        if a.get("broker") not in (None, "paper") and (a.get("access_token") or a.get("broker") == "dhan"):
            try:
                return brokers.get(a)
            except BrokerError:
                return None
        return None

    @property
    def live(self) -> bool:
        a = self.account()
        return a.get("broker") != "paper" and bool(a.get("live"))

    def label(self) -> str:
        a = self.account()
        if a.get("broker") == "paper":
            return PAPER_NAME
        return f"{a['name']} · {brokers.NAMES.get(a.get('broker'), a.get('broker'))}" + ("" if self.live else " (paper)")

    # ------------------------------------------------------------------ background work
    def _run(self, fn, done=None, failed=None):
        job = Worker(fn, self)
        self._jobs.add(job)
        if done:
            job.done.connect(done)
        if failed:
            job.failed.connect(failed)
        job.finished.connect(lambda j=job: self._jobs.discard(j))
        job.start()
        return job

    def refresh(self, quiet: bool = False):
        if self._refreshing is not None:
            self._again = True
            return
        b = self.broker()
        if quiet and not self._wanted(b):
            return

        def work():
            out = {"orders": [], "positions": [], "holdings": [], "funds": None, "error": "", "kind": ""}
            parts = [("funds", b.funds), ("positions", b.positions), ("orders", b.orders)]
            if "holdings" in b.caps:
                parts.append(("holdings", b.holdings))
            for key, fn in parts:
                try:
                    out[key] = fn()
                except BrokerError as e:
                    out["error"], out["kind"] = out["error"] or str(e), out["kind"] or e.kind
                    if e.kind in ("auth", "network"):
                        break
            return out
        self._refreshing = self._run(work, self._refreshed, lambda m, k: self._refreshed(
            {**self._blank(), "error": m, "kind": k}))

    def _wanted(self, b) -> bool:
        """Quiet refreshes: always for paper (local), for a real broker only while someone looks at the result."""
        if b.id == "paper":
            return True
        dock = getattr(self.win, "trade_dock", None)
        seen = dock is not None and dock.isVisible()
        busy = any(o.is_open for o in self.snap["orders"]) or any(p.qty for p in self.snap["positions"])
        return seen or busy

    def _refreshed(self, out):
        self._refreshing = None
        self.snap = {**self._blank(), **out, "ts": time.time()}
        self.updated.emit()
        if self._again:
            self._again = False
            QtCore.QTimer.singleShot(0, self.refresh)

    def say(self, text: str, level: str = "info"):
        self.log.insert(0, (dt.datetime.now().strftime("%H:%M:%S"), level, text))
        del self.log[300:]
        self.notify.emit(text, level)

    # ------------------------------------------------------------------ instruments
    def instrument(self, symbol: str):
        """Cached lookup of a symbol on the account's broker (None when unknown or not looked up yet)."""
        k = (self.name, _key(symbol))
        if k in self._insts:
            return self._insts[k]
        self.lookup(symbol)
        return None

    def lookup(self, symbol: str):
        k = (self.name, _key(symbol))
        if k in self._looking or k in self._insts:
            return
        self._looking.add(k)
        db = self.data_broker()

        def work():
            if db is None:
                return Paper({"name": "_lookup", "broker": "paper"}).resolve(symbol)
            return db.resolve(symbol)

        def done(inst):
            self._looking.discard(k)
            self._insts[k] = inst
            self.instrumentReady.emit(k[1])

        def failed(msg, kind):
            self._looking.discard(k)
            if kind != "network":
                self._insts[k] = None
            self.instrumentReady.emit(k[1])
        self._run(work, done, failed)

    def token_of(self, symbol: str) -> str:
        """What this account's orders / positions call the chart's symbol (to draw them on the right chart)."""
        if self.broker().id == "paper":
            return _key(symbol)
        inst = self.instrument(symbol)
        return str(inst.token) if inst is not None else ""

    # ------------------------------------------------------------------ actions
    def describe(self, req: OrderRequest, inst=None) -> str:
        typ = {"MARKET": "at market", "LIMIT": f"limit {req.price:,.2f}", "SL-M": f"stop {req.trigger:,.2f}",
               "SL": f"stop {req.trigger:,.2f} limit {req.price:,.2f}"}.get(req.order_type, req.order_type)
        name = inst.display if inst is not None else req.symbol
        extra = []
        if req.take_profit:
            extra.append(f"take profit {req.take_profit:,.2f} away")
        if req.stop_loss:
            extra.append(f"stop loss {req.stop_loss:,.2f} away")
        return f"{req.side} {req.qty} {name} {typ} · {req.product.lower()}" + (f" ({', '.join(extra)})" if extra else "")

    def confirm_live(self, parent, what: str) -> bool:
        if not self.live:
            return True
        if self.settings.value("confirm_live", "true") != "true":
            return True
        a = self.account()
        box = QtWidgets.QMessageBox(parent or self.win)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setWindowTitle("Real order")
        box.setText(f"<b>Send a REAL order to {brokers.NAMES.get(a.get('broker'), '')} ({a['name']})?</b>")
        box.setInformativeText(what + "\n\nThis uses real money. Paper accounts are not affected.")
        yes = box.addButton("Send order", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Cancel", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is yes

    def place(self, req: OrderRequest, parent=None, inst=None, done=None) -> bool:
        what = self.describe(req, inst)
        if not self.confirm_live(parent, what):
            return False
        b = self.broker()
        tag = "LIVE" if self.live else "Paper"

        def ok(oid):
            self.say(f"{tag} order sent: {what} (order {oid})", "ok")
            if done:
                done(oid)
            self.refresh()

        def bad(msg, kind):
            if kind == "unknown":
                self.say(msg, "warn")
            elif kind == "auth":
                self.say(f"{msg}. Open Accounts to log in again.", "error")
            else:
                self.say(f"Order not placed: {msg}", "error")
            self.refresh()
        self._run(lambda: b.place(req), ok, bad)
        return True

    def cancel(self, order, parent=None):
        if not self.confirm_live(parent, f"Cancel order {order.id}: {order.side} {order.qty} {order.symbol}"):
            return
        b = self.broker()
        self._run(lambda: b.cancel(order), lambda _r: (self.say(f"Cancelled order {order.id} ({order.symbol})", "ok"),
                                                        self.refresh()),
                  lambda m, k: (self.say(f"Could not cancel {order.id}: {m}", "error"), self.refresh()))

    def modify(self, order, parent=None, **changes):
        what = ", ".join(f"{k} {v:g}" if isinstance(v, (int, float)) else f"{k} {v}" for k, v in changes.items()
                         if v is not None)
        if not self.confirm_live(parent, f"Change order {order.id} ({order.symbol}): {what}"):
            return
        b = self.broker()
        self._run(lambda: b.modify(order, **changes),
                  lambda _r: (self.say(f"Changed order {order.id}: {what}", "ok"), self.refresh()),
                  lambda m, k: (self.say(f"Could not change {order.id}: {m}", "error"), self.refresh()))

    def close_position(self, pos, parent=None):
        side = "SELL" if pos.qty > 0 else "BUY"
        if not self.confirm_live(parent, f"Close {pos.symbol}: {side} {abs(pos.qty)} at market ({pos.product.lower()})"):
            return
        b = self.broker()
        self._run(lambda: b.exit_position(pos),
                  lambda oid: (self.say(f"Closing {pos.symbol}: {side} {abs(pos.qty)} at market (order {oid})", "ok"),
                               self.refresh()),
                  lambda m, k: (self.say(f"Could not close {pos.symbol}: {m}", "error"), self.refresh()))

    def close_all(self, parent=None):
        open_pos = [p for p in self.snap["positions"] if p.qty]
        if not open_pos:
            return
        if self.live and not self.confirm_live(parent, f"Close ALL {len(open_pos)} open positions at market"):
            return
        b = self.broker()

        def work():
            res = []
            for p in open_pos:
                try:
                    res.append((p.symbol, b.exit_position(p), ""))
                except BrokerError as e:
                    res.append((p.symbol, "", str(e)))
            return res

        def done(res):
            for sym, oid, err in res:
                self.say(f"Close {sym}: " + (f"order {oid}" if oid else f"failed - {err}"), "ok" if oid else "error")
            self.refresh()
        self._run(work, done)

    # ------------------------------------------------------------------ prices (paper broker)
    def on_price(self, symbol: str, price: float):
        if not price or price <= 0:
            return
        Paper.set_price(symbol, price)
        filled = []
        for b in self._papers():
            try:
                filled += [(b, oid) for oid in b.on_price(symbol, price)]
            except Exception:  # noqa: BLE001
                continue
        if filled:
            for b, oid in filled:
                o = next((x for x in b.orders() if x.id == oid), None)
                if o is not None:
                    self.say(f"Paper fill: {o.side} {o.qty} {o.symbol} @ {o.avg_price:,.2f} ({b.acct.get('name')})", "ok")
            self.refresh()

    def _papers(self):
        seen = {}
        cur = self.broker()
        if cur.id == "paper":
            seen[str(cur.path)] = cur
        for _sig, b in list(brokers._CACHE.values()):
            if isinstance(b, Paper):
                seen.setdefault(str(b.path), b)
        return list(seen.values())


# ==================================================================================================== order ticket
class _Seg(QtWidgets.QWidget):
    """TradingView's underlined tabs (Market / Limit / Stop / Stop limit)."""
    changed = QtCore.Signal(str)

    def __init__(self, items, parent=None):
        super().__init__(parent)
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(2)
        self.group = QtWidgets.QButtonGroup(self)
        self.btns = {}
        for key, text in items:
            b = QtWidgets.QPushButton(text)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            self.group.addButton(b)
            self.btns[key] = b
            b.clicked.connect(lambda _c=False, k=key: self.changed.emit(k))
            h.addWidget(b)
        h.addStretch(1)

    def set(self, key):
        if key in self.btns:
            self.btns[key].setChecked(True)

    def value(self):
        return next((k for k, b in self.btns.items() if b.isChecked()), None)


class _SideButton(QtWidgets.QPushButton):
    def __init__(self, side: str, color: str):
        super().__init__()
        self.side, self.color = side, color
        self.setCheckable(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(52)
        self.price = 0.0
        self.toggled.connect(lambda _c: self._style())
        self._style()

    def set_price(self, txt: str):
        self.setText(f"{self.side.title()}\n{txt}")

    def _style(self):
        on = self.isChecked()
        self.setStyleSheet(
            f"QPushButton {{ background: {self.color if on else '#262626'}; color: {'white' if on else self.color}; "
            f"border: 1px solid {self.color if on else '#333'}; border-radius: 6px; font-size: 13px; font-weight: 600; "
            f"padding: 4px; }} QPushButton:hover {{ border-color: {self.color}; }}")


class OrderTicket(QtWidgets.QDialog):
    """The order panel. Everything it offers follows the chosen account's broker."""

    def __init__(self, mgr: TradingManager, symbol: str, side: str = "BUY", price: float = 0.0,
                 order_type: str = "MARKET", parent=None):
        super().__init__(parent or mgr.win)
        self.mgr = mgr
        self.symbol = str(symbol or "").strip().upper()
        self.last = float(price or 0.0)
        self.inst = None
        self.setWindowTitle(f"Order · {self.symbol}")
        self.setStyleSheet(_CSS)
        self.setMinimumWidth(380)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(8)

        head = QtWidgets.QHBoxLayout()
        self.title = QtWidgets.QLabel(self.symbol)
        self.title.setObjectName("h")
        head.addWidget(self.title, 1)
        self.badge = QtWidgets.QLabel("")
        head.addWidget(self.badge)
        v.addLayout(head)
        self.sub = QtWidgets.QLabel("")
        self.sub.setObjectName("muted")
        v.addWidget(self.sub)

        ar = QtWidgets.QHBoxLayout()
        al = QtWidgets.QLabel("Account")
        al.setObjectName("muted")
        ar.addWidget(al)
        self.acct = QtWidgets.QComboBox()
        for a in mgr.accounts():
            self.acct.addItem(self._acct_text(a), a["name"])
        self.acct.setCurrentIndex(max(0, self.acct.findData(mgr.name)))
        self.acct.currentIndexChanged.connect(self._acct_changed)
        ar.addWidget(self.acct, 1)
        v.addLayout(ar)

        sides = QtWidgets.QHBoxLayout()
        sides.setSpacing(8)
        self.sell = _SideButton("SELL", theme.DOWN)
        self.buy = _SideButton("BUY", theme.ACCENT)
        grp = QtWidgets.QButtonGroup(self)
        grp.addButton(self.sell)
        grp.addButton(self.buy)
        for b in (self.sell, self.buy):
            b.clicked.connect(self._update)
            sides.addWidget(b)
        v.addLayout(sides)
        (self.buy if side.upper() == "BUY" else self.sell).setChecked(True)

        self.types = _Seg([("MARKET", "Market"), ("LIMIT", "Limit"), ("SL-M", "Stop"), ("SL", "Stop limit")])
        self.types.changed.connect(lambda _k: self._update())
        v.addWidget(self.types)

        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        r = 0

        def lab(t):
            x = QtWidgets.QLabel(t)
            x.setObjectName("muted")
            return x
        self.qty_lbl = lab("Quantity")
        self.qty = QtWidgets.QSpinBox()
        self.qty.setRange(1, 10_000_000)
        self.qty.setValue(1)
        self.qty.valueChanged.connect(self._update)
        self.lot_lbl = lab("")
        grid.addWidget(self.qty_lbl, r, 0)
        grid.addWidget(self.qty, r, 1)
        grid.addWidget(self.lot_lbl, r, 2)
        r += 1
        self.price_lbl = lab("Price")
        self.price = self._price_box()
        grid.addWidget(self.price_lbl, r, 0)
        grid.addWidget(self.price, r, 1, 1, 2)
        r += 1
        self.trig_lbl = lab("Trigger")
        self.trig = self._price_box()
        grid.addWidget(self.trig_lbl, r, 0)
        grid.addWidget(self.trig, r, 1, 1, 2)
        r += 1
        grid.addWidget(lab("Product"), r, 0)
        prow = QtWidgets.QHBoxLayout()
        prow.setSpacing(6)
        self.prod_group = QtWidgets.QButtonGroup(self)
        self.prods = {}
        for key, text in (("INTRADAY", "Intraday"), ("DELIVERY", "Delivery"), ("MARGIN", "Carry (NRML)"),
                          ("MTF", "MTF")):
            b = QtWidgets.QPushButton(text)
            b.setObjectName("chip")
            b.setCheckable(True)
            b.clicked.connect(self._update)
            self.prod_group.addButton(b)
            self.prods[key] = b
            prow.addWidget(b)
        prow.addStretch(1)
        grid.addLayout(prow, r, 1, 1, 2)
        r += 1
        grid.addWidget(lab("Validity"), r, 0)
        vrow = QtWidgets.QHBoxLayout()
        self.validity = QtWidgets.QComboBox()
        self.validity.addItem("Day", "DAY")
        self.validity.addItem("IOC (now or cancel)", "IOC")
        vrow.addWidget(self.validity)
        self.amo = QtWidgets.QCheckBox("After-market order")
        vrow.addWidget(self.amo)
        vrow.addStretch(1)
        grid.addLayout(vrow, r, 1, 1, 2)
        r += 1
        self.tp_on = QtWidgets.QCheckBox("Take profit")
        self.tp = self._price_box()
        self.tp_info = lab("")
        self.sl_on = QtWidgets.QCheckBox("Stop loss")
        self.sl = self._price_box()
        self.sl_info = lab("")
        for on, box, info in ((self.tp_on, self.tp, self.tp_info), (self.sl_on, self.sl, self.sl_info)):
            on.toggled.connect(self._update)
            grid.addWidget(on, r, 0)
            grid.addWidget(box, r, 1)
            grid.addWidget(info, r, 2)
            r += 1
        v.addLayout(grid)

        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        line.setStyleSheet("color: #2a2a2a;")
        v.addWidget(line)
        self.info = QtWidgets.QLabel("")
        self.info.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.info.setStyleSheet("color: #b2b5be; font-size: 12px;")
        v.addWidget(self.info)
        self.msg = QtWidgets.QLabel("")
        self.msg.setWordWrap(True)
        self.msg.setStyleSheet(f"color: {theme.DOWN}; font-size: 12px;")
        v.addWidget(self.msg)
        self.go = QtWidgets.QPushButton("")
        self.go.setMinimumHeight(40)
        self.go.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.go.clicked.connect(self.submit)
        self.go.setDefault(True)
        v.addWidget(self.go)

        self._margin = None
        self._margin_job = None
        self._mtimer = QtCore.QTimer(self)
        self._mtimer.setSingleShot(True)
        self._mtimer.setInterval(450)
        self._mtimer.timeout.connect(self._ask_margin)
        for box in (self.price, self.trig, self.tp, self.sl):
            box.valueChanged.connect(self._update)
        self.validity.currentIndexChanged.connect(self._update)
        self.amo.toggled.connect(self._update)
        mgr.instrumentReady.connect(self._inst_ready)
        self._acct_changed(rebuild=False)
        self.types.set(order_type if order_type in self.types.btns else "MARKET")
        if self.last:
            for box in (self.price, self.trig):
                box.setValue(self.last)
            self.tp.setValue(self.last * (1.01 if side.upper() == "BUY" else 0.99))
            self.sl.setValue(self.last * (0.99 if side.upper() == "BUY" else 1.01))
        self._update()

    # ------------------------------------------------------------------ pieces
    @staticmethod
    def _acct_text(a) -> str:
        if a.get("broker") == "paper":
            return PAPER_NAME
        live = " · LIVE" if a.get("live") else " · paper"
        return f"{a['name']} ({brokers.NAMES.get(a.get('broker'), a.get('broker'))}){live}"

    @staticmethod
    def _price_box():
        b = QtWidgets.QDoubleSpinBox()
        b.setRange(0, 10_000_000)
        b.setDecimals(2)
        b.setSingleStep(0.05)
        b.setGroupSeparatorShown(True)
        b.setKeyboardTracking(False)
        return b

    def _broker(self):
        return self.mgr.broker()

    def _acct_changed(self, *_a, rebuild: bool = True):
        name = self.acct.currentData()
        if name and name != self.mgr.name:
            self.mgr.set_account(name)
        b = self._broker()
        live = self.mgr.live
        self.badge.setText("LIVE" if live else "PAPER")
        col = theme.DOWN if live else "#787b86"
        self.badge.setStyleSheet(f"background: {col}33; color: {col}; border-radius: 9px; padding: 2px 10px; "
                                 "font-weight: 700; font-size: 11px;")
        types = [t for t in ("MARKET", "LIMIT", "SL-M", "SL") if t in b.order_types]
        for k, btn in self.types.btns.items():
            btn.setVisible(k in types)
        if self.types.value() not in types:
            self.types.set("MARKET")
        for k, btn in self.prods.items():
            btn.setVisible(k in b.products)
        self.amo_ok = "amo" in b.caps
        self.amo.setVisible(self.amo_ok)
        bracket = self.bracket_ok = "bracket" in b.caps
        for w in (self.tp_on, self.tp, self.tp_info, self.sl_on, self.sl, self.sl_info):
            w.setVisible(bracket)
        if not bracket:
            self.tp_on.setChecked(False)
            self.sl_on.setChecked(False)
        self.inst = self.mgr.instrument(self.symbol)
        self._apply_inst()
        if rebuild:
            self._update()

    def _inst_ready(self, sym):
        if sym == _key(self.symbol):
            self.inst = self.mgr.instrument(self.symbol)
            self._apply_inst()
            self._update()

    def _apply_inst(self):
        i = self.inst
        deriv = bool(i is not None and i.derivative)
        lot = i.lot if i is not None and i.lot > 1 else 1
        if i is not None:
            self.title.setText(i.display)
            kind = {"EQ": "Stock", "FUT": "Future", "CE": "Call option", "PE": "Put option", "IDX": "Index"}.get(i.kind, "")
            self.sub.setText(f"{kind} · {i.exchange}" + (f" · lot {lot}" if lot > 1 else "") +
                             (f" · tick {i.tick:g}" if i.tick else ""))
            for box in (self.price, self.trig, self.tp, self.sl):
                box.setSingleStep(i.tick or 0.05)
        else:
            self.sub.setText("Looking the symbol up…" if self.mgr.data_broker() is not None else "")
        if deriv and lot > 1:
            self.qty_lbl.setText("Lots")
            self.lot_lbl.setText(f"× {lot} = {self.qty.value() * lot:,} qty")
        else:
            self.qty_lbl.setText("Quantity")
            self.lot_lbl.setText("")
        b = self._broker()
        cur = next((k for k, x in self.prods.items() if x.isChecked()), None)
        if deriv:
            self.prods["DELIVERY"].setVisible(False)
            self.prods["MTF"].setVisible(False)
            if cur in (None, "DELIVERY", "MTF"):
                self.prods["INTRADAY"].setChecked(True)
        else:
            self.prods["MARGIN"].setVisible(False)
            for k in ("DELIVERY", "MTF"):
                self.prods[k].setVisible(k in b.products)
            if cur in (None, "MARGIN"):
                self.prods["INTRADAY"].setChecked(True)

    # ------------------------------------------------------------------ live state
    def side(self) -> str:
        return "BUY" if self.buy.isChecked() else "SELL"

    def qty_units(self) -> int:
        i = self.inst
        if i is not None and i.derivative and i.lot > 1:
            return self.qty.value() * i.lot
        return self.qty.value()

    def request(self) -> OrderRequest:
        t = self.types.value() or "MARKET"
        prod = next((k for k, x in self.prods.items() if x.isChecked()), "INTRADAY")
        ref = self.price.value() if t in ("LIMIT", "SL") else (self.trig.value() if t == "SL-M" else self.last)
        sgn = 1 if self.side() == "BUY" else -1
        tp = abs(self.tp.value() - ref) if self.tp_on.isChecked() and self.bracket_ok and ref else 0.0
        sl = abs(ref - self.sl.value()) if self.sl_on.isChecked() and self.bracket_ok and ref else 0.0
        if tp and (self.tp.value() - ref) * sgn <= 0:
            raise ValueError("take profit must be on the winning side of the entry")
        if sl and (ref - self.sl.value()) * sgn <= 0:
            raise ValueError("stop loss must be on the losing side of the entry")
        return OrderRequest(symbol=self.symbol, side=self.side(), qty=self.qty_units(), order_type=t, product=prod,
                            price=self.price.value() if t in ("LIMIT", "SL") else 0.0,
                            trigger=self.trig.value() if t in ("SL", "SL-M") else 0.0,
                            validity=self.validity.currentData(), amo=self.amo.isChecked() and self.amo_ok,
                            take_profit=round(tp, 4), stop_loss=round(sl, 4), instrument=self.inst, tag="chartlab")

    def _update(self, *_a):
        t = self.types.value() or "MARKET"
        for w in (self.price_lbl, self.price):
            w.setVisible(t in ("LIMIT", "SL"))
        for w in (self.trig_lbl, self.trig):
            w.setVisible(t in ("SL", "SL-M"))
        self.tp.setEnabled(self.tp_on.isChecked())
        self.sl.setEnabled(self.sl_on.isChecked())
        px = self.last
        txt = f"{px:,.2f}" if px else "—"
        self.sell.set_price(txt)
        self.buy.set_price(txt)
        i = self.inst
        if i is not None and i.derivative and i.lot > 1:
            self.lot_lbl.setText(f"× {i.lot} = {self.qty.value() * i.lot:,} qty")
        ref = self.price.value() if t in ("LIMIT", "SL") else (self.trig.value() if t == "SL-M" else px)
        for on, box, info, good in ((self.tp_on, self.tp, self.tp_info, 1), (self.sl_on, self.sl, self.sl_info, -1)):
            if on.isChecked() and ref:
                d = (box.value() - ref) * (1 if self.side() == "BUY" else -1)
                info.setText(f"{'+' if d >= 0 else MINUS}{abs(d):,.2f} ({abs(d) / ref * 100:.2f}%)")
                info.setStyleSheet(f"color: {theme.UP if d * good > 0 else theme.DOWN}; font-size: 12px;")
            else:
                info.setText("")
        q = self.qty_units()
        value = q * (ref or 0)
        rows = [f"Order value <b style='color:white'>{money(value) if ref else chr(8212)}</b>"]
        if self._margin and self._margin.get("required"):
            rows.append(f"Margin needed <b style='color:white'>{money(self._margin['required'])}</b>")
        f = self.mgr.snap.get("funds")
        if f is not None:
            rows.append(f"Available <b style='color:white'>{money(f.available)}</b>")
        self.info.setText(" &nbsp;·&nbsp; ".join(rows))
        name = i.display if i is not None else self.symbol
        word = {"MARKET": "Market", "LIMIT": "Limit", "SL-M": "Stop", "SL": "Stop limit"}[t]
        at = "MKT" if t == "MARKET" else f"{(self.price.value() if t in ('LIMIT', 'SL') else self.trig.value()):,.2f}"
        self.go.setText(f"{self.side().title()} {q:,} {name} @ {at} {'' if t == 'MARKET' else word}".strip())
        col = theme.ACCENT if self.side() == "BUY" else theme.DOWN
        self.go.setStyleSheet(f"QPushButton {{ background: {col}; color: white; border: 0; border-radius: 6px; "
                              f"font-size: 14px; font-weight: 600; }} QPushButton:hover {{ background: {col}cc; }} "
                              f"QPushButton:disabled {{ background: #333; color: #777; }}")
        self.msg.setText("")
        if "margin" in self._broker().caps or (self.mgr.data_broker() is not None and
                                               "margin" in self.mgr.data_broker().caps):
            self._mtimer.start()

    def _ask_margin(self):
        db = self.mgr.data_broker()
        if db is None or "margin" not in db.caps or self.inst is None or self._margin_job is not None:
            return
        try:
            req = self.request()
        except ValueError:
            return

        def done(m):
            self._margin_job = None
            self._margin = m
            self._update_info_only()

        def failed(_m, _k):
            self._margin_job = None
        self._margin_job = self.mgr._run(lambda: db.margin(req), done, failed)

    def _update_info_only(self):
        mt = self._mtimer
        mt.blockSignals(True)
        self._update()
        mt.stop()
        mt.blockSignals(False)

    def set_last(self, px: float):
        if px and px > 0:
            self.last = float(px)
            self._update_info_only()

    def submit(self):
        try:
            req = self.request()
            b = self._broker()
            if b.id != "paper":
                b.prepare(req)                      # every check that needs no network, before confirming
            elif req.order_type == "MARKET" and not Paper._prices.get(_key(self.symbol)) and self.last:
                Paper.set_price(self.symbol, self.last)
        except (ValueError, BrokerError) as e:
            self.msg.setText(str(e))
            return
        if self.mgr.place(req, self, self.inst):
            self.accept()


class ModifyDialog(QtWidgets.QDialog):
    def __init__(self, mgr: TradingManager, order, parent=None):
        super().__init__(parent or mgr.win)
        self.mgr, self.order = mgr, order
        self.setWindowTitle(f"Change order {order.id}")
        self.setStyleSheet(_CSS)
        f = QtWidgets.QFormLayout(self)
        f.setContentsMargins(18, 14, 18, 14)
        h = QtWidgets.QLabel(f"{order.side} {order.symbol} · {order.order_type}")
        h.setObjectName("big")
        f.addRow(h)
        self.qty = QtWidgets.QSpinBox()
        self.qty.setRange(1, 10_000_000)
        self.qty.setValue(max(1, int(order.qty)))
        f.addRow("Quantity", self.qty)
        self.price = OrderTicket._price_box()
        self.price.setValue(order.price)
        self.trig = OrderTicket._price_box()
        self.trig.setValue(order.trigger)
        if order.order_type in ("LIMIT", "SL"):
            f.addRow("Price", self.price)
        if order.order_type in ("SL", "SL-M"):
            f.addRow("Trigger", self.trig)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _ok(self):
        o = self.order
        ch = {}
        if self.qty.value() != o.qty:
            ch["qty"] = self.qty.value()
        if o.order_type in ("LIMIT", "SL") and abs(self.price.value() - o.price) > 1e-9:
            ch["price"] = self.price.value()
        if o.order_type in ("SL", "SL-M") and abs(self.trig.value() - o.trigger) > 1e-9:
            ch["trigger"] = self.trig.value()
        if ch:
            self.mgr.modify(o, self, **ch)
        self.accept()


# ==================================================================================================== trading panel
def _item(text, align=None, color=None, bold=False, data=None):
    it = QtWidgets.QTableWidgetItem(str(text))
    it.setFlags(it.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
    AL = QtCore.Qt.AlignmentFlag
    it.setTextAlignment((align or AL.AlignLeft) | AL.AlignVCenter)
    if color:
        it.setForeground(QtGui.QColor(color))
    if bold:
        f = it.font()
        f.setBold(True)
        it.setFont(f)
    if data is not None:
        it.setData(QtCore.Qt.ItemDataRole.UserRole, data)
    return it


class TradingPanel(QtWidgets.QWidget):
    """TradingView's Trading Panel."""

    def __init__(self, mgr: TradingManager, parent=None):
        super().__init__(parent)
        self.mgr = mgr
        self.setObjectName("tpanel")
        self.setStyleSheet(_CSS + " QTableWidget { background: #000; border: 0; gridline-color: #1a1a1a; } "
                           "QHeaderView::section { background: #000; color: #787b86; border: 0; padding: 4px 6px; }")
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(10, 6, 10, 6)
        v.setSpacing(6)
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(10)
        self.acct = QtWidgets.QComboBox()
        self.acct.setMinimumWidth(230)
        self.acct.currentIndexChanged.connect(self._acct_picked)
        top.addWidget(self.acct)
        self.state = QtWidgets.QLabel("")
        top.addWidget(self.state)
        self.summary = QtWidgets.QLabel("")
        self.summary.setTextFormat(QtCore.Qt.TextFormat.RichText)
        top.addWidget(self.summary, 1)
        self.login_btn = QtWidgets.QPushButton("Log in")
        self.login_btn.clicked.connect(lambda: self.mgr.win.manage_accounts(self.mgr.name))
        self.login_btn.hide()
        top.addWidget(self.login_btn)
        self.close_all_btn = QtWidgets.QPushButton("Close all")
        self.close_all_btn.setToolTip("Close every open position at market")
        self.close_all_btn.clicked.connect(lambda: self.mgr.close_all(self))
        top.addWidget(self.close_all_btn)
        ref = QtWidgets.QPushButton("⟳")
        ref.setToolTip("Refresh now")
        ref.setFixedWidth(34)
        ref.clicked.connect(self.mgr.refresh)
        top.addWidget(ref)
        new = QtWidgets.QPushButton("+ New order")
        new.setObjectName("primary")
        new.clicked.connect(lambda: self.mgr.win.open_order_ticket("BUY"))
        top.addWidget(new)
        v.addLayout(top)

        self.tabs = QtWidgets.QTabWidget()
        self.pos = self._table(["Symbol", "Side", "Qty", "Avg price", "Last", "Unrealised P&L", "Realised P&L",
                                "Product", ""])
        self.ords = self._table(["Time", "Symbol", "Side", "Type", "Qty", "Filled", "Price", "Trigger", "Avg fill",
                                 "Status", "", ""])
        self.hold = self._table(["Symbol", "Qty", "Avg price", "Last", "P&L", "Day %", "ISIN"])
        self.acct_info = QtWidgets.QLabel("")
        self.acct_info.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.acct_info.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft)
        self.acct_info.setWordWrap(True)
        self.acct_info.setStyleSheet("padding: 8px; color: #dbdbdb;")
        acct_scroll = QtWidgets.QScrollArea()
        acct_scroll.setWidget(self.acct_info)
        acct_scroll.setWidgetResizable(True)
        self.notes = QtWidgets.QListWidget()
        self.order_filter = QtWidgets.QComboBox()
        for k, t in (("working", "Working"), ("filled", "Filled"), ("cancelled", "Cancelled / rejected"),
                     ("all", "All")):
            self.order_filter.addItem(t, k)
        self.order_filter.currentIndexChanged.connect(lambda _i: self._fill())
        ow = QtWidgets.QWidget()
        ol = QtWidgets.QVBoxLayout(ow)
        ol.setContentsMargins(0, 0, 0, 0)
        orow = QtWidgets.QHBoxLayout()
        orow.addWidget(self.order_filter)
        orow.addStretch(1)
        ol.addLayout(orow)
        ol.addWidget(self.ords)
        self.tabs.addTab(self.pos, "Positions")
        self.tabs.addTab(ow, "Orders")
        self.tabs.addTab(self.hold, "Holdings")
        self.tabs.addTab(acct_scroll, "Account summary")
        self.tabs.addTab(self.notes, "Notifications log")
        v.addWidget(self.tabs, 1)
        mgr.updated.connect(self._fill)
        mgr.accountChanged.connect(self._accounts)
        mgr.notify.connect(self._note)
        self._accounts()
        self._fill()

    @staticmethod
    def _table(cols):
        t = QtWidgets.QTableWidget(0, len(cols))
        t.setHorizontalHeaderLabels(cols)
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(30)
        t.setShowGrid(False)
        t.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        t.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = t.horizontalHeader()
        hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        hh.setHighlightSections(False)
        t.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        return t

    def _accounts(self):
        self.acct.blockSignals(True)
        self.acct.clear()
        for a in self.mgr.accounts():
            self.acct.addItem(OrderTicket._acct_text(a), a["name"])
        self.acct.setCurrentIndex(max(0, self.acct.findData(self.mgr.name)))
        self.acct.blockSignals(False)
        self._fill()

    def _acct_picked(self, _i):
        name = self.acct.currentData()
        if name:
            self.mgr.set_account(name)

    def _note(self, text, level):
        col = {"ok": theme.UP, "error": theme.DOWN, "warn": "#ff9800"}.get(level, "#b2b5be")
        it = QtWidgets.QListWidgetItem(f"{dt.datetime.now():%H:%M:%S}   {text}")
        it.setForeground(QtGui.QColor(col))
        self.notes.insertItem(0, it)
        while self.notes.count() > 300:
            self.notes.takeItem(self.notes.count() - 1)

    def _btn(self, text, tip, fn, color="#b2b5be"):
        b = QtWidgets.QToolButton()
        b.setText(text)
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        b.setStyleSheet(f"QToolButton {{ color: {color}; border: 0; padding: 0 6px; font-size: 13px; }} "
                        f"QToolButton:hover {{ color: white; background: #2a2a2a; border-radius: 4px; }}")
        b.clicked.connect(fn)
        return b

    def _fill(self):
        s = self.mgr.snap
        AL = QtCore.Qt.AlignmentFlag
        R = AL.AlignRight
        live = self.mgr.live
        b = self.mgr.broker()
        err, kind = s.get("error", ""), s.get("kind", "")
        if err and kind in ("auth",):
            self.state.setText(f"<span style='color:{theme.DOWN}'>● Login needed</span>")
        elif err and kind == "network":
            self.state.setText(f"<span style='color:#ff9800'>● Offline</span>")
        elif b.id == "paper":
            self.state.setText(f"<span style='color:{theme.MUTED}'>● Paper</span>")
        else:
            self.state.setText(f"<span style='color:{theme.UP}'>● {'LIVE' if live else 'Connected'}</span>")
        self.state.setToolTip(err or "")
        self.login_btn.setVisible(kind == "auth")
        f = s.get("funds")
        pos = s.get("positions") or []
        upnl = sum(p.unrealised for p in pos)
        rpnl = sum(p.realised for p in pos)
        parts = []
        if f is not None:
            parts.append(f"Available <b>{money(f.available)}</b>")
            parts.append(f"Margin used <b>{money(f.used)}</b>")
            if b.id == "paper":
                parts.append(f"Equity <b>{money(f.total)}</b>")
        if pos:
            for lbl, val in (("Unrealised P&amp;L", upnl), ("Realised P&amp;L", rpnl)):
                c = theme.UP if val >= 0 else theme.DOWN
                parts.append(f"{lbl} <b style='color:{c}'>{money(val, True)}</b>")
        if err and kind not in ("auth",):
            parts.append(f"<span style='color:{theme.DOWN}'>{err[:120]}</span>")
        self.summary.setText(" &nbsp;&nbsp; ".join(parts))
        self.close_all_btn.setVisible(any(p.qty for p in pos))
        # positions
        rows = sorted(pos, key=lambda p: (p.qty == 0, p.symbol))
        self.pos.setRowCount(len(rows))
        for r, p in enumerate(rows):
            side = "Long" if p.qty > 0 else ("Short" if p.qty < 0 else "Flat")
            sc = theme.UP if p.qty > 0 else (theme.DOWN if p.qty < 0 else theme.MUTED)
            vals = [(p.symbol, None, None, True), (side, None, sc, False), (f"{abs(p.qty):,}", R, None, False),
                    (f"{p.avg:,.2f}" if p.avg else "—", R, None, False),
                    (f"{p.ltp:,.2f}" if p.ltp else "—", R, None, False),
                    (money(p.unrealised, True), R, theme.UP if p.unrealised >= 0 else theme.DOWN, False),
                    (money(p.realised, True), R, theme.UP if p.realised >= 0 else theme.DOWN, False),
                    (p.product.title(), None, theme.MUTED, False)]
            for c, (t, al, col, bold) in enumerate(vals):
                self.pos.setItem(r, c, _item(t, al, col, bold, p if c == 0 else None))
            if p.qty:
                self.pos.setCellWidget(r, 8, self._btn("✕ Close", "Close this position at market",
                                                       lambda _c=False, x=p: self.mgr.close_position(x, self)))
            else:
                self.pos.removeCellWidget(r, 8)
        # orders
        want = self.order_filter.currentData()
        ods = s.get("orders") or []
        if want == "working":
            ods = [o for o in ods if o.is_open]
        elif want == "filled":
            ods = [o for o in ods if o.status == "complete"]
        elif want == "cancelled":
            ods = [o for o in ods if o.status in ("cancelled", "rejected")]
        self.ords.setRowCount(len(ods))
        stc = {"complete": theme.UP, "rejected": theme.DOWN, "cancelled": theme.MUTED, "open": "#ff9800",
               "trigger_pending": "#ff9800", "pending": "#ff9800", "partial": "#ff9800"}
        for r, o in enumerate(ods):
            tm = o.time[11:19] if len(o.time) >= 19 else o.time
            vals = [(tm, None, theme.MUTED), (o.symbol, None, None), (o.side.title(), None,
                                                                       theme.UP if o.side == "BUY" else theme.DOWN),
                    (o.order_type, None, None), (f"{o.qty:,}", R, None), (f"{o.filled:,}", R, None),
                    (f"{o.price:,.2f}" if o.price else "—", R, None),
                    (f"{o.trigger:,.2f}" if o.trigger else "—", R, None),
                    (f"{o.avg_price:,.2f}" if o.avg_price else "—", R, None),
                    (o.status.replace("_", " ").title(), None, stc.get(o.status))]
            for c, (t, al, col) in enumerate(vals):
                it = _item(t, al, col, False, o if c == 0 else None)
                if c == 9 and o.message:
                    it.setToolTip(o.message)
                self.ords.setItem(r, c, it)
            if o.is_open and "modify" in b.caps:
                self.ords.setCellWidget(r, 10, self._btn("✎", "Change this order",
                                                         lambda _c=False, x=o: ModifyDialog(self.mgr, x, self).exec()))
            else:
                self.ords.removeCellWidget(r, 10)
            if o.is_open and "cancel" in b.caps:
                self.ords.setCellWidget(r, 11, self._btn("✕", "Cancel this order",
                                                         lambda _c=False, x=o: self.mgr.cancel(x, self), theme.DOWN))
            else:
                self.ords.removeCellWidget(r, 11)
        # holdings
        hs = s.get("holdings") or []
        self.hold.setRowCount(len(hs))
        for r, h in enumerate(hs):
            vals = [(h.symbol, None, None), (f"{h.qty:,}", R, None), (f"{h.avg:,.2f}", R, None),
                    (f"{h.ltp:,.2f}" if h.ltp else "—", R, None),
                    (money(h.pnl, True), R, theme.UP if h.pnl >= 0 else theme.DOWN),
                    (f"{h.day_change:+.2f}%" if h.day_change else "—", R,
                     theme.UP if h.day_change >= 0 else theme.DOWN), (h.isin, None, theme.MUTED)]
            for c, (t, al, col) in enumerate(vals):
                self.hold.setItem(r, c, _item(t, al, col))
        hi = self.tabs.indexOf(self.hold)
        self.tabs.setTabVisible(hi, "holdings" in b.caps)
        # account
        rows = [f"<b style='font-size:14px'>{self.mgr.label()}</b>"]
        if b.id == "paper":
            rows.append("Orders fill against the prices on your charts. No money moves.")
        if f is not None:
            rows.append(f"Available: <b>{money(f.available)}</b> &nbsp; Used: <b>{money(f.used)}</b> &nbsp; "
                        f"Total: <b>{money(f.total)}</b>")
            for k, val in (f.raw or {}).items():
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    rows.append(f"<span style='color:#787b86'>{k}</span> &nbsp; {val:,.2f}")
        caps = ", ".join(sorted(b.caps))
        rows.append(f"<span style='color:#787b86'>Can do: {caps}</span>")
        if getattr(b, "data_note", ""):
            rows.append(f"<span style='color:#787b86'>{b.data_note}</span>")
        self.acct_info.setText("<br>".join(rows))
