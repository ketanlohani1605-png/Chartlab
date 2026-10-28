"""Broker accounts: saved credentials, API data fetching (Dhan) and the account manager dialog.

Credentials live in ~/ChartLab/accounts.json (file mode 600, readable only by you). Nothing is sent anywhere
except to the broker's own API host when you load data or test a connection."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pandas as pd
from PySide6 import QtCore, QtGui, QtWidgets

from . import data as datamod
from . import theme

PATH = datamod.USER_DIR / "accounts.json"
BROKERS = {"dhan": "Dhan"}
FIELDS = {"dhan": [("client_id", "Client ID"), ("access_token", "Access token")]}


# ---------------------------------------------------------------------- store
def load_accounts(path: Path | None = None) -> list[dict]:
    p = path or PATH
    try:
        return json.loads(p.read_text()).get("accounts", [])
    except Exception:  # noqa: BLE001
        return []


def save_accounts(accts: list[dict], path: Path | None = None):
    p = path or PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"accounts": accts}, f, indent=2)
    os.replace(tmp, p)


def get_account(name: str, path: Path | None = None) -> dict:
    for a in load_accounts(path):
        if a["name"] == name:
            return a
    raise KeyError(f"No account named '{name}'. Add it in Accounts -> Manage accounts.")


def account_env(a: dict) -> dict:
    """Environment variables handed to a script that runs on this account."""
    env = {"CHARTLAB_ACCOUNT": a["name"], "CHARTLAB_BROKER": a["broker"],
           "CHARTLAB_LIVE": "1" if a.get("live") else "0"}
    for k, _ in FIELDS.get(a["broker"], []):
        env["CHARTLAB_" + k.upper()] = str(a.get(k, ""))
    return env


# ---------------------------------------------------------------------- Dhan
_EPOCH_1980 = 315532800


from .dhan_core import DHAN, INDEX_IDS, MASTER_URL, _http, _master, place_order, resolve_symbol  # noqa: F401


_DAYS = {"5d": 5, "1mo": 31, "3mo": 93, "6mo": 186, "1y": 366, "2y": 731, "5y": 1827, "10y": 3653, "max": 3653}
_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}


def _frame(js: dict) -> pd.DataFrame:
    legacy = not js.get("timestamp") and bool(js.get("start_Time"))     # old API counts seconds from 1980
    ts = pd.Series(js.get("timestamp") or js.get("start_Time") or [], dtype="float64")
    if ts.empty:
        return pd.DataFrame()
    if legacy:
        ts = ts + _EPOCH_1980
    t = pd.to_datetime(ts, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    return pd.DataFrame({"Date": t, "Open": js["open"], "High": js["high"], "Low": js["low"],
                         "Close": js["close"], "Volume": js.get("volume", [0] * len(t))})


def fetch_dhan(acct: dict, symbol: str, interval: str, period: str) -> pd.DataFrame:
    hdr = {"access-token": acct["access_token"], "client-id": acct["client_id"], "Accept": "application/json"}
    sid, seg, inst, _ = resolve_symbol(symbol)
    end = dt.date.today() + dt.timedelta(days=1)
    start = end - dt.timedelta(days=_DAYS.get(period, 731))
    if interval in _MIN:
        frames, step = [], 85                    # Dhan allows ~90 days of intraday per request
        start = max(start, end - dt.timedelta(days=370))
        cur = start
        while cur < end:
            nxt = min(cur + dt.timedelta(days=step), end)
            body = {"securityId": sid, "exchangeSegment": seg, "instrument": inst, "interval": str(_MIN[interval]),
                    "oi": False, "fromDate": f"{cur} 09:00:00", "toDate": f"{nxt} 15:30:00"}
            frames.append(_frame(json.loads(_http(f"{DHAN}/charts/intraday", body, hdr))))
            cur = nxt
        df = pd.concat([f for f in frames if len(f)] or [pd.DataFrame()])
    else:
        body = {"securityId": sid, "exchangeSegment": seg, "instrument": inst, "expiryCode": 0, "oi": False,
                "fromDate": str(start), "toDate": str(end)}
        df = _frame(json.loads(_http(f"{DHAN}/charts/historical", body, hdr)))
        if interval in ("1wk", "1mo") and len(df):
            df = df.set_index("Date").resample("W-FRI" if interval == "1wk" else "ME").agg(
                {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna().reset_index()
    if df.empty:
        raise RuntimeError(f"Dhan returned no candles for {symbol} ({interval}).")
    return df.drop_duplicates("Date").sort_values("Date").reset_index(drop=True)


def test_account(acct: dict) -> str:
    """Cheap authenticated call; returns a human message or raises."""
    if acct["broker"] == "dhan":
        raw = _http(f"{DHAN}/profile", None, {"access-token": acct["access_token"],
                                              "client-id": acct["client_id"]})
        js = json.loads(raw)
        return f"Connected. Client {js.get('dhanClientId', acct['client_id'])}, token valid until " \
               f"{js.get('tokenValidity', '?')}"
    raise RuntimeError("Unsupported broker")


def load_api(account: str, symbol: str, interval: str, period: str):
    """Entry point used by the chart panel: -> (DataFrame cleaned like other sources, label)."""
    a = get_account(account)
    if a["broker"] != "dhan":
        raise RuntimeError(f"Broker '{a['broker']}' has no data adapter")
    raw = fetch_dhan(a, symbol, interval, period)
    return datamod.standardise(raw), f"{symbol.upper()} · {interval} · {a['name']}"


# ---------------------------------------------------------------------- UI
class _AccountCard(QtWidgets.QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QtCore.QSize(option.rect.width(), 62)

    def paint(self, p, option, index):
        a = index.data(QtCore.Qt.ItemDataRole.UserRole)
        p.save()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(4, 3, -4, -3)
        sel = bool(option.state & QtWidgets.QStyle.StateFlag.State_Selected)
        hov = bool(option.state & QtWidgets.QStyle.StateFlag.State_MouseOver)
        p.setPen(QtGui.QPen(QtGui.QColor(theme.ACCENT if sel else theme.BORDER), 1.3 if sel else 1))
        p.setBrush(QtGui.QColor("#1e2a55" if sel else ("#222633" if hov else "#1a1e2a")))
        p.drawRoundedRect(r, 10, 10)
        av = QtCore.QRect(r.left() + 12, r.center().y() - 16, 32, 32)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(theme.ACCENT))
        p.drawEllipse(av)
        f = QtGui.QFont(option.font)
        f.setBold(True)
        f.setPointSize(13)
        p.setFont(f)
        p.setPen(QtGui.QColor("white"))
        p.drawText(av, QtCore.Qt.AlignmentFlag.AlignCenter, (a["name"][:1] or "?").upper())
        f.setPointSize(12)
        p.setFont(f)
        tx = r.adjusted(54, 9, -74, 0)
        p.drawText(tx, QtCore.Qt.AlignmentFlag.AlignTop | QtCore.Qt.AlignmentFlag.AlignLeft, a["name"])
        f.setBold(False)
        f.setPointSize(10)
        p.setFont(f)
        p.setPen(QtGui.QColor(theme.MUTED))
        cid = str(a.get("client_id", ""))
        p.drawText(r.adjusted(54, 0, -74, -9), QtCore.Qt.AlignmentFlag.AlignBottom | QtCore.Qt.AlignmentFlag.AlignLeft,
                   f"{BROKERS.get(a['broker'], a['broker'])}  ·  {cid[:4]}…{cid[-3:]}" if len(cid) > 8 else
                   f"{BROKERS.get(a['broker'], a['broker'])}  ·  {cid}")
        live = bool(a.get("live"))
        col = QtGui.QColor(theme.DOWN if live else "#787b86")
        badge = QtCore.QRect(r.right() - 64, r.center().y() - 10, 54, 20)
        fill = QtGui.QColor(col)
        fill.setAlpha(50)
        p.setBrush(fill)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.drawRoundedRect(badge, 10, 10)
        f.setBold(True)
        f.setPointSize(9)
        p.setFont(f)
        p.setPen(col)
        p.drawText(badge, QtCore.Qt.AlignmentFlag.AlignCenter, "LIVE" if live else "PAPER")
        p.restore()


class AccountsDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, path: Path | None = None):
        super().__init__(parent)
        self.path = path
        self.setWindowTitle("Broker accounts")
        self.resize(860, 540)
        self.setStyleSheet(f"QDialog {{ background: {theme.BG}; }} QLabel#h {{ font-size: 15px; font-weight: 700; color: white; }} "
                           f"QLabel#sub {{ color: {theme.MUTED}; font-size: 12px; }} QLabel#lbl {{ color: {theme.MUTED}; font-size: 11px; }} "
                           f"QLineEdit, QDoubleSpinBox, QComboBox {{ background: #1e222d; border: 1px solid {theme.BORDER}; "
                           f"border-radius: 8px; padding: 8px 10px; min-height: 18px; }} "
                           f"QLineEdit:focus, QDoubleSpinBox:focus {{ border-color: {theme.ACCENT}; }} "
                           f"QPushButton#danger {{ background: transparent; color: {theme.DOWN}; }} "
                           f"QPushButton#danger:hover {{ background: rgba(242,54,69,40); }}")
        self.accts = load_accounts(path)
        self.editing = -1
        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        left = QtWidgets.QFrame()
        left.setFixedWidth(300)
        left.setObjectName("leftpane")
        left.setStyleSheet(f"QFrame#leftpane {{ background: #0f121a; border-right: 1px solid {theme.BORDER}; }} QLabel {{ border: 0; background: transparent; }} "
                           "QListWidget { border: 0; background: transparent; }")
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(14, 16, 14, 14)
        head = QtWidgets.QHBoxLayout()
        t = QtWidgets.QLabel("Accounts")
        t.setObjectName("h")
        head.addWidget(t)
        head.addStretch(1)
        add = QtWidgets.QPushButton("+  Add")
        add.setObjectName("primary")
        add.clicked.connect(self._new)
        head.addWidget(add)
        lv.addLayout(head)
        self.list = QtWidgets.QListWidget()
        self.list.setItemDelegate(_AccountCard(self.list))
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.currentRowChanged.connect(self._select)
        lv.addWidget(self.list, 1)
        self.empty = QtWidgets.QLabel("No accounts yet.\nClick  + Add  to connect your\nfirst broker account.")
        self.empty.setObjectName("sub")
        self.empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        lv.addWidget(self.empty, 1)
        root.addWidget(left)

        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(28, 22, 28, 20)
        rv.setSpacing(6)
        self.title = QtWidgets.QLabel("New account")
        self.title.setObjectName("h")
        rv.addWidget(self.title)
        sub = QtWidgets.QLabel("Used for API data and for running scripts on many accounts at once. Saved only on this "
                               "Mac (~/ChartLab/accounts.json, private to your user).")
        sub.setObjectName("sub")
        sub.setWordWrap(True)
        rv.addWidget(sub)
        rv.addSpacing(8)

        def field(label, w):
            l = QtWidgets.QLabel(label.upper())
            l.setObjectName("lbl")
            rv.addWidget(l)
            rv.addWidget(w)
            rv.addSpacing(4)
        self.name = QtWidgets.QLineEdit()
        self.name.setPlaceholderText("e.g. Main, Papa, Fund 2")
        self.broker = QtWidgets.QComboBox()
        for k, tt in BROKERS.items():
            self.broker.addItem(tt, k)
        self.client = QtWidgets.QLineEdit()
        self.client.setPlaceholderText("Dhan client ID")
        self.token = QtWidgets.QLineEdit()
        self.token.setPlaceholderText("Paste access token")
        self.token.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.show_tok = QtWidgets.QCheckBox("Show token")
        self.show_tok.toggled.connect(lambda on: self.token.setEchoMode(
            QtWidgets.QLineEdit.EchoMode.Normal if on else QtWidgets.QLineEdit.EchoMode.Password))
        self.mult = QtWidgets.QDoubleSpinBox()
        self.mult.setRange(0.1, 1000)
        self.mult.setDecimals(2)
        self.mult.setValue(1.0)
        self.mult.setToolTip("2 = double the quantity/lots your script sends on this account")
        field("Account name", self.name)
        row = QtWidgets.QHBoxLayout()
        c1, c2 = QtWidgets.QVBoxLayout(), QtWidgets.QVBoxLayout()
        for lay, lab, w in ((c1, "Broker", self.broker), (c2, "Client ID", self.client)):
            l = QtWidgets.QLabel(lab.upper())
            l.setObjectName("lbl")
            lay.addWidget(l)
            lay.addWidget(w)
        row.addLayout(c1, 1)
        row.addLayout(c2, 2)
        rv.addLayout(row)
        rv.addSpacing(4)
        field("Access token", self.token)
        rv.insertWidget(rv.count() - 1, self.show_tok)
        field("Quantity multiplier", self.mult)

        self.live = QtWidgets.QCheckBox("  Live orders  -  REAL money")
        self.live.setStyleSheet(f"QCheckBox {{ color: {theme.DOWN}; font-weight: 600; }}")
        self.live.setToolTip("Off = paper: chartlab.order() only prints what it would send")
        rv.addWidget(self.live)
        note = QtWidgets.QLabel("Leave off to test safely. Paper accounts never send an order.")
        note.setObjectName("sub")
        rv.addWidget(note)
        rv.addStretch(1)
        self.msg = QtWidgets.QLabel("")
        self.msg.setWordWrap(True)
        rv.addWidget(self.msg)
        btns = QtWidgets.QHBoxLayout()
        d = QtWidgets.QPushButton("Delete")
        d.setObjectName("danger")
        d.clicked.connect(self.delete)
        btns.addWidget(d)
        btns.addStretch(1)
        tb = QtWidgets.QPushButton("Test connection")
        tb.clicked.connect(self.test)
        btns.addWidget(tb)
        sb = QtWidgets.QPushButton("Save")
        sb.setObjectName("primary")
        sb.setDefault(True)
        sb.clicked.connect(self.save)
        btns.addWidget(sb)
        rv.addLayout(btns)
        root.addWidget(right, 1)
        self._fill()

    def _say(self, text: str, ok: bool | None = None):
        col = theme.UP if ok else (theme.DOWN if ok is False else theme.MUTED)
        self.msg.setText(f"<span style='color:{col}'>{text}</span>")

    def _fill(self, keep: str = ""):
        self.list.blockSignals(True)
        self.list.clear()
        for a in self.accts:
            it = QtWidgets.QListWidgetItem()
            it.setData(QtCore.Qt.ItemDataRole.UserRole, a)
            self.list.addItem(it)
        self.list.blockSignals(False)
        self.empty.setVisible(not self.accts)
        self.list.setVisible(bool(self.accts))
        if self.accts:
            i = next((k for k, a in enumerate(self.accts) if a["name"] == keep), 0)
            self.list.setCurrentRow(i)
            self._select(i)
        else:
            self._new()

    def _select(self, i):
        if 0 <= i < len(self.accts):
            self.editing = i
            a = self.accts[i]
            self.title.setText(a["name"])
            self.name.setText(a["name"])
            self.broker.setCurrentIndex(max(0, self.broker.findData(a["broker"])))
            self.client.setText(a.get("client_id", ""))
            self.token.setText(a.get("access_token", ""))
            self.live.setChecked(bool(a.get("live")))
            self.mult.setValue(float(a.get("qty_mult", 1) or 1))
            self._say("")

    def _new(self):
        self.editing = -1
        self.list.blockSignals(True)
        self.list.setCurrentRow(-1)
        self.list.blockSignals(False)
        self.title.setText("New account")
        for w in (self.name, self.client, self.token):
            w.clear()
        self.live.setChecked(False)
        self.mult.setValue(1.0)
        self._say("")
        self.name.setFocus()

    def _current(self) -> dict:
        return {"name": self.name.text().strip(), "broker": self.broker.currentData(),
                "client_id": self.client.text().strip(), "access_token": self.token.text().strip(),
                "live": self.live.isChecked(), "qty_mult": self.mult.value()}

    def save(self):
        a = self._current()
        if not a["name"] or not a["client_id"] or not a["access_token"]:
            self._say("Name, Client ID and Access token are all required.", False)
            return
        dup = [i for i, x in enumerate(self.accts) if x["name"] == a["name"]]
        if self.editing >= 0:
            self.accts[self.editing] = a
        elif dup:
            self.accts[dup[0]] = a
        else:
            self.accts.append(a)
        save_accounts(self.accts, self.path)
        self._fill(a["name"])
        self._say("Saved.", True)

    def delete(self):
        if 0 <= self.editing < len(self.accts):
            del self.accts[self.editing]
            save_accounts(self.accts, self.path)
            self._fill()
            self._say("Deleted.", True)

    def test(self):
        self._say("Testing connection…")
        QtWidgets.QApplication.processEvents()
        try:
            self._say(test_account(self._current()), True)
        except Exception as e:  # noqa: BLE001
            self._say(str(e), False)


class PickAccountsDialog(QtWidgets.QDialog):
    """Choose which accounts a script should run on; shows a clear warning that it may place real orders."""

    def __init__(self, parent, accts: list[dict]):
        super().__init__(parent)
        self.setWindowTitle("Run on accounts")
        v = QtWidgets.QVBoxLayout(self)
        v.addWidget(QtWidgets.QLabel(
            "The script runs <b>once per ticked account, all at the same time</b>. Each copy can read its own "
            "credentials with <code>chartlab.account()</code>.<br>"
            f"<span style='color:{theme.DOWN}'>If your script places orders, they will be REAL orders on every "
            "ticked account.</span>"))
        self.one = QtWidgets.QRadioButton("One script, orders sent to ALL ticked accounts at the same instant (fastest)")
        self.one.setChecked(True)
        self.sep = QtWidgets.QRadioButton("A separate copy of the script per account (independent runs)")
        v.addWidget(self.one)
        v.addWidget(self.sep)
        self.list = QtWidgets.QListWidget()
        for a in accts:
            it = QtWidgets.QListWidgetItem(f"{a['name']}  ({BROKERS.get(a['broker'], a['broker'])})  -  "
                                        + ("LIVE orders" if a.get("live") else "paper"))
            it.setFlags(it.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.CheckState.Unchecked)
            it.setData(QtCore.Qt.ItemDataRole.UserRole, a["name"])
            self.list.addItem(it)
        v.addWidget(self.list)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        ok = bb.addButton("Run on selected", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setObjectName("primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def broadcast(self) -> bool:
        return self.one.isChecked()

    def chosen(self) -> list[str]:
        return [self.list.item(i).data(QtCore.Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == QtCore.Qt.CheckState.Checked]
