"""Option chain side panel: calls on the left, puts on the right, strikes in the middle, at-the-money row marked.
Click a price to open that contract's chart. Prices come from the connected Dhan account: the whole chain every few
seconds, and the rows on screen tick-by-tick through the live feed."""
from __future__ import annotations

import datetime as dt
import time

from PySide6 import QtCore, QtGui, QtWidgets

from . import dhan_core, theme
from .live import _Call

INDEX = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]
COLS = ["OI", "Chg%", "Call", "Strike", "Put", "Chg%", "OI"]
C_OI, C_CHG, C_LTP, STRIKE, P_LTP, P_CHG, P_OI = range(7)
ROLE = QtCore.Qt.ItemDataRole.UserRole


def fmt_oi(v: float) -> str:
    if not v:
        return "—"
    if v >= 1e7:
        return f"{v / 1e7:.2f}Cr"
    if v >= 1e5:
        return f"{v / 1e5:.1f}L"
    if v >= 1e3:
        return f"{v / 1e3:.1f}K"
    return f"{v:.0f}"


class OptionChain(QtWidgets.QWidget):
    picked = QtCore.Signal(str)              # contract symbol to chart, e.g. NIFTY261124C25000
    changed = QtCore.Signal()                # the set of contracts on screen changed (live feed re-subscribes)
    needAccount = QtCore.Signal()

    REFRESH_MS = 5000                        # Dhan allows one option-chain request every 3 seconds
    SPAN = 12                                # strikes shown each side of the money

    def __init__(self, parent=None, account_fn=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)   # (not the window's #222 gutter)
        self.account_fn = account_fn or (lambda: None)
        self.und, self.expiry = "NIFTY", None
        self.spot = 0.0
        self._rows: list = []                # what is in the table, by row
        self._sids: dict = {}                # (seg, sid) -> (row, column)
        self._contracts: dict = {}
        self._job = None
        self._meta_job = None
        self._centered = None
        self._last_ok = 0.0
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 6)
        v.setSpacing(6)
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(6)
        self.und_box = QtWidgets.QComboBox()
        self.und_box.setEditable(True)
        self.und_box.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.und_box.addItems(INDEX)
        self.und_box.setToolTip("Index or F&O stock, e.g. NIFTY, BANKNIFTY, RELIANCE")
        self.und_box.setMinimumWidth(118)
        self.exp_box = QtWidgets.QComboBox()
        self.exp_box.setToolTip("Expiry")
        self.exp_box.setPlaceholderText("Expiry")                 # (empty until the contracts are in)
        self.exp_box.setMinimumWidth(104)
        top.addWidget(self.und_box, 1)
        top.addWidget(self.exp_box, 1)
        v.addLayout(top)
        self.info = QtWidgets.QLabel("")
        self.info.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.info.setWordWrap(True)
        self.info.setStyleSheet(f"color:{theme.MUTED};font-size:12px;background:transparent;padding:2px 4px;")
        v.addWidget(self.info)
        self.tbl = QtWidgets.QTableWidget(0, len(COLS))
        self.tbl.setObjectName("optchain")
        self.tbl.setHorizontalHeaderLabels(COLS)
        self.tbl.verticalHeader().hide()
        self.tbl.setShowGrid(False)
        self.tbl.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.tbl.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.tbl.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tbl.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.tbl.verticalHeader().setDefaultSectionSize(26)
        self.tbl.setMouseTracking(True)
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.tbl.setTextElideMode(QtCore.Qt.TextElideMode.ElideNone)
        self.tbl.setWordWrap(False)
        # no '::item' rule here: a stylesheet item rule would switch off the per-cell backgrounds (in-the-money shade)
        self.tbl.setStyleSheet(
            "QTableWidget#optchain{background:transparent;border:0;font-size:12px;}"
            "QHeaderView::section{background:transparent;color:#787b86;border:0;border-bottom:1px solid #2a2e39;"
            "font-size:11px;padding:4px 0;}")
        v.addWidget(self.tbl, 1)
        # no broker yet: say where the prices come from and offer the one step, instead of an empty table
        from .fx import Placeholder
        self.no_acct = Placeholder(
            "chain", "Connect Dhan for the option chain",
            "Every strike's LTP, change and open interest, live, come from your Dhan account (its Data API). "
            "Add the client id and access token once in Accounts.",
            (("Connect a Dhan account…", lambda: self.needAccount.emit()),))
        self.no_acct.hide()
        v.addWidget(self.no_acct, 1)
        self.connect_btn = self.no_acct.buttons[0]
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(self.REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self.und_box.activated.connect(lambda *_: self.set_underlying(self.und_box.currentText()))
        self.und_box.lineEdit().returnPressed.connect(lambda: self.set_underlying(self.und_box.currentText()))
        self.exp_box.activated.connect(self._expiry_chosen)
        self.tbl.cellClicked.connect(self._clicked)

    # ------------------------------------------------------------------ visibility drives the polling
    def showEvent(self, e):
        super().showEvent(e)
        self.timer.start()
        QtCore.QTimer.singleShot(0, self.reload)

    def hideEvent(self, e):
        super().hideEvent(e)
        self.timer.stop()
        self.changed.emit()

    def stop(self):
        self.timer.stop()
        for j in (self._job, self._meta_job):
            if j is not None:
                j.wait(3000)

    # ------------------------------------------------------------------ underlying / expiry
    def set_underlying(self, und: str):
        und = (und or "").strip().upper().split(":")[-1]
        if not und:
            return
        self.und, self.expiry, self._centered = und, None, None
        if self.und_box.currentText().upper() != und:
            self.und_box.setEditText(und)
        self.exp_box.clear()
        self.tbl.setRowCount(0)
        self._rows, self._sids = [], {}
        self.reload()

    def follow(self, chart_symbol: str):
        """The chart shows NIFTY or a NIFTY option: show that chain (only when it is an F&O name we know)."""
        if not self.isVisible():
            return
        s = (chart_symbol or "").upper().split(":")[-1]
        o = dhan_core.parse_option(s)
        und = o[0] if o else s
        if und and und != self.und and (und in INDEX or und in self._known_unds()):
            self.set_underlying(und)

    def _known_unds(self) -> set:
        try:
            return set(dhan_core.underlyings()) if dhan_core.options_ready() else set()
        except Exception:  # noqa: BLE001
            return set()

    def reload(self):
        """Expiry list + contract ids for the underlying (from Dhan's instrument list), then the prices."""
        if self._meta_job is not None or not self.isVisible():
            return
        acct = self.account_fn()
        self._show_no_account(acct is None)
        if acct is None:
            self.tbl.setRowCount(0)
            self._rows, self._sids = [], {}
            self.info.setText("")
            return
        if self.exp_box.count() and self.expiry is not None:
            self.refresh()
            return
        und = self.und
        self.info.setText(f"Loading {und} contracts…")

        def work():
            exps = dhan_core.expiries(und)
            if not exps:
                raise RuntimeError(f"{und} has no listed options on Dhan. Try NIFTY, BANKNIFTY or an F&O stock.")
            return und, exps
        job = _Call(work, self)
        self._meta_job = job
        job.done.connect(self._meta_in)
        job.failed.connect(self._meta_failed)
        job.finished.connect(lambda: setattr(self, "_meta_job", None))
        job.start()

    def _show_no_account(self, none: bool):
        self.no_acct.setVisible(none)
        for w in (self.tbl, self.info, self.exp_box):
            w.setVisible(not none)

    def _meta_failed(self, msg):
        self.info.setText(f"<span style='color:{theme.DOWN}'>{msg[:220]}</span>")

    def _meta_in(self, res):
        und, exps = res
        if und != self.und:
            QtCore.QTimer.singleShot(0, self.reload)
            return
        self.exp_box.blockSignals(True)
        self.exp_box.clear()
        for e in exps[:12]:
            self.exp_box.addItem(f"{e:%d %b %Y}".lstrip("0"), e)
        self.exp_box.blockSignals(False)
        self.expiry = exps[0]
        self._last_ok = 0.0
        QtCore.QTimer.singleShot(0, self.refresh)

    def _expiry_chosen(self, i):
        self.expiry, self._centered = self.exp_box.itemData(i), None
        self.tbl.setRowCount(0)
        self._rows, self._sids = [], {}
        self._last_ok = 0.0
        self.refresh()

    # ------------------------------------------------------------------ prices
    def refresh(self):
        if self._job is not None or self.expiry is None or not self.isVisible():
            return
        acct = self.account_fn()
        if acct is None:
            return
        und, exp = self.und, self.expiry

        def work():
            chain = dhan_core.option_chain(acct, und, exp)
            return und, exp, chain, dhan_core.contracts(und, exp)
        job = _Call(work, self)
        self._job = job
        job.done.connect(self._chain_in)
        job.failed.connect(self._chain_failed)
        job.finished.connect(lambda: setattr(self, "_job", None))
        job.start()

    def _chain_failed(self, msg):
        low = msg.lower()
        if any(k in low for k in ("806", "dh-902", "not subscribed")):
            msg = "This Dhan account has no Data API subscription. Dhan needs it for option chain, live prices and candles."
        elif any(k in low for k in ("401", "dh-901", "expired", "807", "808", "809")):
            msg = "The Dhan access token has expired or is wrong. Paste a fresh one in Accounts."
        if time.time() - self._last_ok > 20:                 # one slow answer does not blank a working chain
            self.info.setText(f"<span style='color:{theme.DOWN}'>{msg[:240]}</span>")

    def _chain_in(self, res):
        und, exp, chain, contracts = res
        if (und, exp) != (self.und, self.expiry):
            return
        self._last_ok = time.time()
        self.spot, rows = float(chain.get("spot") or 0), chain.get("rows") or []
        self._contracts = contracts
        if not rows:
            self.info.setText(f"Dhan sent no rows for {und} {exp:%d %b}. (Outside market days the chain can be empty.)")
            return
        strikes = [r["strike"] for r in rows]
        atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - self.spot)) if self.spot else len(strikes) // 2
        lo, hi = max(0, atm - self.SPAN), min(len(rows), atm + self.SPAN + 1)
        show = rows[lo:hi]
        days = (exp - dt.date.today()).days
        lot = next(iter(contracts.values()))["lot"] if contracts else "—"
        self.info.setText(
            f"<span style='color:#d1d4dc;font-size:13px'><b>{und}</b> &nbsp;{self.spot:,.2f}</span>"
            f" &nbsp;·&nbsp; {max(days, 0)} day{'s' if days != 1 else ''} to expiry"
            f" &nbsp;·&nbsp; lot {lot}")
        same = [r["strike"] for r in show] == [r["strike"] for r in self._rows]
        self._rows = show
        if not same:
            self._build(show)
        self._fill(show, strikes[atm] if strikes else 0)
        if self._centered != (und, exp):
            self._centered = (und, exp)
            self.tbl.scrollToItem(self.tbl.item(atm - lo, STRIKE), QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter)
        if not same:
            self.changed.emit()

    def _build(self, show):
        t = self.tbl
        t.setRowCount(len(show))
        self._sids = {}
        A = QtCore.Qt.AlignmentFlag
        for r, row in enumerate(show):
            for c in range(len(COLS)):
                it = QtWidgets.QTableWidgetItem("")
                it.setTextAlignment(A.AlignCenter if c == STRIKE else (A.AlignRight | A.AlignVCenter))
                t.setItem(r, c, it)
            for kind, col in (("CE", C_LTP), ("PE", P_LTP)):
                con = self._contracts.get((row["strike"], kind))
                side = row["ce" if kind == "CE" else "pe"] or {}
                sid = (con or {}).get("security_id") or side.get("sid")
                if sid:
                    self._sids[((con or {}).get("segment", "NSE_FNO"), str(sid))] = (r, col)
                it = t.item(r, col)
                it.setData(ROLE, (con or {}).get("symbol") or
                           (dhan_core.option_symbol(self.und, self.expiry, row["strike"], kind)))

    def _fill(self, show, atm_strike):
        t = self.tbl
        itm, strike_bg, atm_bg, none = (QtGui.QColor("#172036"), QtGui.QColor("#1e222d"), QtGui.QColor("#2962ff"),
                                        QtGui.QBrush())
        bold = QtGui.QFont(t.font())
        bold.setBold(True)
        for r, row in enumerate(show):
            k = row["strike"]
            st = t.item(r, STRIKE)
            st.setText(f"{k:g}")
            st.setFont(bold)
            st.setForeground(QtGui.QColor("#ffffff" if k == atm_strike else "#d1d4dc"))
            st.setBackground(atm_bg if k == atm_strike else strike_bg)     # the strike nearest the spot price
            st.setToolTip("At the money: nearest strike to the spot price" if k == atm_strike else "")
            for side, cols, in_money in ((row["ce"], (C_OI, C_CHG, C_LTP), self.spot and k < self.spot),
                                         (row["pe"], (P_OI, P_CHG, P_LTP), self.spot and k > self.spot)):
                bg = itm if in_money else none
                oi_c, chg_c, ltp_c = cols
                for c in cols:
                    t.item(r, c).setBackground(bg)
                if not side:
                    for c in cols:
                        t.item(r, c).setText("—")
                    continue
                t.item(r, oi_c).setText(fmt_oi(side["oi"]))
                t.item(r, oi_c).setForeground(QtGui.QColor("#9598a1"))
                self._price(r, ltp_c, chg_c, side["ltp"], side["prev"])
                t.item(r, ltp_c).setToolTip(
                    f"{t.item(r, ltp_c).data(ROLE)}\nIV {side['iv']:.1f}%   delta {side['delta']:.2f}\n"
                    f"Bid {side['bid']:,.2f}   Ask {side['ask']:,.2f}\nVolume {fmt_oi(side['volume'])}   "
                    f"OI {fmt_oi(side['oi'])}\nClick to open the chart")

    def _price(self, r, ltp_c, chg_c, ltp, prev):
        t = self.tbl
        it = t.item(r, ltp_c)
        it.setText(f"{ltp:,.2f}" if ltp else "—")
        it.setData(ROLE + 1, prev)
        chg = (ltp / prev - 1) * 100 if (ltp and prev) else None
        col = QtGui.QColor("#d1d4dc" if chg is None else (theme.UP if chg >= 0 else theme.DOWN))
        it.setForeground(col)
        ci = t.item(r, chg_c)
        ci.setText("" if chg is None else f"{chg:+.1f}%")
        ci.setForeground(col)

    # ------------------------------------------------------------------ live feed hooks
    def instruments(self) -> dict:
        """(segment, security id) -> callback(price) for every contract shown (empty when the panel is closed)."""
        if not self.isVisible():
            return {}
        return {key: (lambda px, rc=rc: self._tick(rc, px)) for key, rc in self._sids.items()}

    def _tick(self, rc, px):
        r, c = rc
        it = self.tbl.item(r, c)
        if it is None:
            return
        self._price(r, c, C_CHG if c == C_LTP else P_CHG, float(px), it.data(ROLE + 1) or 0)

    def _clicked(self, r, c):
        if c == STRIKE:
            return
        it = self.tbl.item(r, C_LTP if c < STRIKE else P_LTP)
        sym = it.data(ROLE) if it is not None else None
        if sym:
            self.picked.emit(sym)
