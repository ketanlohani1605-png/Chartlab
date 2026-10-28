"""Broker accounts: saved credentials, API data fetching (Dhan) and the account manager dialog.

Credentials live in ~/ChartLab/accounts.json (file mode 600, readable only by you). Nothing is sent anywhere
except to the broker's own API host when you load data or test a connection."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd
from PySide6 import QtCore, QtWidgets

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
    env = {"CHARTLAB_ACCOUNT": a["name"], "CHARTLAB_BROKER": a["broker"]}
    for k, _ in FIELDS.get(a["broker"], []):
        env["CHARTLAB_" + k.upper()] = str(a.get(k, ""))
    return env


# ---------------------------------------------------------------------- Dhan
DHAN = "https://api.dhan.co/v2"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
INDEX_IDS = {"NIFTY": "13", "NIFTY50": "13", "BANKNIFTY": "25", "NIFTYBANK": "25", "FINNIFTY": "27",
             "SENSEX": "51", "MIDCPNIFTY": "442"}
_EPOCH_1980 = 315532800


def _http(url: str, body: dict | None, headers: dict, timeout=30) -> bytes:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **headers},
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Dhan API error {e.code}: {e.read().decode('utf8', 'replace')[:300]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach Dhan ({e.reason}). Check your internet.") from None


def _master() -> pd.DataFrame:
    cache = datamod.USER_DIR / "cache" / "dhan_master.csv"
    if not cache.exists() or time.time() - cache.stat().st_mtime > 7 * 86400:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(_http(MASTER_URL, None, {}, timeout=120))
    return pd.read_csv(cache, low_memory=False)


def resolve_symbol(symbol: str) -> tuple[str, str, str, str]:
    """-> (securityId, exchangeSegment, instrument, display). Accepts RELIANCE, NSE:RELIANCE, BSE:SBIN, NIFTY."""
    s = symbol.strip().upper()
    exch = "NSE"
    if ":" in s:
        exch, s = s.split(":", 1)
    if s in INDEX_IDS:
        return INDEX_IDS[s], "IDX_I", "INDEX", s
    m = _master()
    col = {c.upper(): c for c in m.columns}
    ex, seg = col.get("EXCH_ID"), col.get("SEGMENT")
    sid, sym = col.get("SECURITY_ID"), col.get("SYMBOL_NAME") or col.get("UNDERLYING_SYMBOL")
    tsym, inst = col.get("DISPLAY_NAME"), col.get("INSTRUMENT")
    ser = col.get("SERIES")
    if not (ex and sid and inst):
        raise RuntimeError("Dhan instrument list has an unexpected format; update ChartLab.")
    q = m[(m[ex] == exch) & (m[inst] == "EQUITY")]
    if seg:
        q = q[q[seg] == "E"]
    if ser:
        q = q[q[ser].isin(["EQ", "BE", "SM"]) | q[ser].isna()]
    hit = q[q[sym].astype(str).str.upper() == s] if sym else q.iloc[0:0]
    if hit.empty and tsym:
        hit = q[q[tsym].astype(str).str.upper() == s]
    if hit.empty:
        raise RuntimeError(f"'{symbol}' not found on Dhan {exch} equity. Try NSE:RELIANCE or BSE:SBIN.")
    return str(int(hit.iloc[0][sid])), f"{exch}_EQ", "EQUITY", s


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
class AccountsDialog(QtWidgets.QDialog):
    def __init__(self, parent=None, path: Path | None = None):
        super().__init__(parent)
        self.path = path
        self.setWindowTitle("Broker accounts")
        self.resize(620, 420)
        self.accts = load_accounts(path)
        v = QtWidgets.QVBoxLayout(self)
        v.addWidget(QtWidgets.QLabel(
            f"<span style='color:{theme.MUTED}'>Used for API data and for running a script on many accounts at once. "
            "Saved only on this Mac (~/ChartLab/accounts.json, private to your user).</span>"))
        h = QtWidgets.QHBoxLayout()
        self.list = QtWidgets.QListWidget()
        self.list.currentRowChanged.connect(self._select)
        h.addWidget(self.list, 1)
        form = QtWidgets.QFormLayout()
        self.name = QtWidgets.QLineEdit()
        self.broker = QtWidgets.QComboBox()
        for k, t in BROKERS.items():
            self.broker.addItem(t, k)
        self.client = QtWidgets.QLineEdit()
        self.token = QtWidgets.QLineEdit()
        self.token.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        form.addRow("Name", self.name)
        form.addRow("Broker", self.broker)
        form.addRow("Client ID", self.client)
        form.addRow("Access token", self.token)
        self.msg = QtWidgets.QLabel("")
        self.msg.setWordWrap(True)
        form.addRow(self.msg)
        row = QtWidgets.QHBoxLayout()
        for text, fn in (("Save", self.save), ("Test", self.test), ("Delete", self.delete)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            if text == "Save":
                b.setObjectName("primary")
            row.addWidget(b)
        form.addRow(row)
        h.addLayout(form, 2)
        v.addLayout(h, 1)
        new = QtWidgets.QPushButton("+ New account")
        new.clicked.connect(self._new)
        v.addWidget(new)
        self._fill()

    def _fill(self, keep: str = ""):
        self.list.blockSignals(True)
        self.list.clear()
        for a in self.accts:
            self.list.addItem(a["name"])
        self.list.blockSignals(False)
        if self.accts:
            i = next((k for k, a in enumerate(self.accts) if a["name"] == keep), 0)
            self.list.setCurrentRow(i)
            self._select(i)

    def _select(self, i):
        if 0 <= i < len(self.accts):
            a = self.accts[i]
            self.name.setText(a["name"])
            self.broker.setCurrentIndex(max(0, self.broker.findData(a["broker"])))
            self.client.setText(a.get("client_id", ""))
            self.token.setText(a.get("access_token", ""))

    def _new(self):
        self.list.clearSelection()
        self.list.setCurrentRow(-1)
        for w in (self.name, self.client, self.token):
            w.clear()
        self.name.setFocus()

    def _current(self) -> dict:
        return {"name": self.name.text().strip(), "broker": self.broker.currentData(),
                "client_id": self.client.text().strip(), "access_token": self.token.text().strip()}

    def save(self):
        a = self._current()
        if not a["name"] or not a["client_id"] or not a["access_token"]:
            self.msg.setText("Name, Client ID and Access token are all required.")
            return
        old = self.list.currentRow()
        same = [i for i, x in enumerate(self.accts) if x["name"] == a["name"]]
        if same:
            self.accts[same[0]] = a
        elif 0 <= old < len(self.accts) and self.list.currentItem() and self.list.currentItem().isSelected():
            self.accts[old] = a
        else:
            self.accts.append(a)
        save_accounts(self.accts, self.path)
        self._fill(a["name"])
        self.msg.setText("Saved.")

    def delete(self):
        i = self.list.currentRow()
        if 0 <= i < len(self.accts):
            del self.accts[i]
            save_accounts(self.accts, self.path)
            self._fill()
            self.msg.setText("Deleted.")

    def test(self):
        self.msg.setText("Testing…")
        QtWidgets.QApplication.processEvents()
        try:
            self.msg.setText(test_account(self._current()))
        except Exception as e:  # noqa: BLE001
            self.msg.setText(f"<span style='color:{theme.DOWN}'>{e}</span>")


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
        self.list = QtWidgets.QListWidget()
        for a in accts:
            it = QtWidgets.QListWidgetItem(f"{a['name']}  ({BROKERS.get(a['broker'], a['broker'])})")
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

    def chosen(self) -> list[str]:
        return [self.list.item(i).data(QtCore.Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == QtCore.Qt.CheckState.Checked]
