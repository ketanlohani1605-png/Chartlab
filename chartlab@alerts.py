"""Price alerts: rules, evaluation, live polling, persistence, dialog, panel and toast."""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import uuid

from PySide6 import QtCore, QtGui, QtWidgets

from . import data as datamod
from . import icons, theme

CONDITIONS = [("cross", "Crossing"), ("cross_up", "Crossing up"), ("cross_down", "Crossing down"),
              ("gt", "Greater than"), ("lt", "Less than")]
COND_TEXT = dict(CONDITIONS)
COOLDOWN_S = 60


def _now() -> dt.datetime:
    return dt.datetime.now()


class AlertManager(QtCore.QObject):
    changed = QtCore.Signal()
    triggered = QtCore.Signal(dict, float)

    def __init__(self, settings: QtCore.QSettings | None = None):
        super().__init__()
        self.settings = settings
        self.alerts: list[dict] = []
        self.history: list[dict] = []
        if settings is not None:
            try:
                data = json.loads(settings.value("alerts_v1", "{}") or "{}")
                self.alerts = data.get("alerts", [])
                self.history = data.get("history", [])[-200:]
            except Exception:  # noqa: BLE001 - corrupt store: start clean
                self.alerts, self.history = [], []

    # ------------------------------------------------------------------ CRUD
    def add(self, symbol: str, label: str, cond: str, value: float, ref_price: float, message: str = "",
            once: bool = True, expires: str = "") -> dict:
        if cond not in COND_TEXT:
            raise ValueError(f"Unknown condition '{cond}'")
        a = {"id": uuid.uuid4().hex[:8], "symbol": symbol, "label": label, "cond": cond, "value": float(value),
             "message": message, "once": once, "expires": expires, "status": "active",
             "side": 1 if ref_price > value else -1, "created": _now().isoformat(timespec="seconds"),
             "last_fired": "", "count": 0}
        self.alerts.append(a)
        self._save()
        return a

    def remove(self, aid: str):
        self.alerts = [a for a in self.alerts if a["id"] != aid]
        self._save()

    def set_status(self, aid: str, status: str):
        for a in self.alerts:
            if a["id"] == aid:
                a["status"] = status
        self._save()

    def clear_done(self):
        self.alerts = [a for a in self.alerts if a["status"] in ("active", "paused")]
        self._save()

    def active_symbols(self) -> list[str]:
        return sorted({a["symbol"] for a in self.alerts if a["status"] == "active"})

    def for_symbol(self, symbol: str) -> list[dict]:
        return [a for a in self.alerts if a["symbol"] == symbol and a["status"] in ("active", "paused")]

    def _save(self):
        if self.settings is not None:
            self.settings.setValue("alerts_v1", json.dumps({"alerts": self.alerts, "history": self.history[-200:]}))
        self.changed.emit()

    # ------------------------------------------------------------------ evaluation
    def check(self, symbol: str, last: float, low: float | None = None, high: float | None = None,
              now: dt.datetime | None = None) -> list[dict]:
        """Evaluate every active alert on `symbol` against a price update (last price + range since last update).
        Crossing alerts are edge-triggered: they must first see the price on the starting side, so creating an
        alert never fires retroactively."""
        now = now or _now()
        low = last if low is None else min(low, last)
        high = last if high is None else max(high, last)
        fired = []
        for a in self.alerts:
            if a["symbol"] != symbol or a["status"] != "active":
                continue
            if a["expires"] and now.isoformat(timespec="seconds") > a["expires"]:
                a["status"] = "expired"
                continue
            v, c, hit = a["value"], a["cond"], False
            if a["last_fired"] and (now - dt.datetime.fromisoformat(a["last_fired"])).total_seconds() < COOLDOWN_S:
                continue
            if c == "gt":
                hit = last >= v
            elif c == "lt":
                hit = last <= v
            elif c == "cross_up":
                if a["side"] < 0 and high >= v:
                    hit = True
                elif last < v:
                    a["side"] = -1
                if hit:
                    a["side"] = 1
            elif c == "cross_down":
                if a["side"] > 0 and low <= v:
                    hit = True
                elif last > v:
                    a["side"] = 1
                if hit:
                    a["side"] = -1
            elif c == "cross":
                if a["side"] > 0 and low <= v:
                    hit, a["side"] = True, -1
                elif a["side"] < 0 and high >= v:
                    hit, a["side"] = True, 1
            if hit:
                self._fire(a, last, now)
                fired.append(a)
        if fired or any(a["status"] == "expired" for a in self.alerts):
            self._save()
        return fired

    def _fire(self, a: dict, price: float, now: dt.datetime):
        a["count"] += 1
        a["last_fired"] = now.isoformat(timespec="seconds")
        if a["once"]:
            a["status"] = "triggered"
        rec = {"time": a["last_fired"], "symbol": a["label"], "text": describe(a), "price": price,
               "message": a["message"]}
        self.history.append(rec)
        self.triggered.emit(a, price)


def describe(a: dict) -> str:
    return f"{a['label']} {COND_TEXT[a['cond']].lower()} {a['value']:,.2f}"


def notify_os(title: str, text: str):
    """macOS notification centre banner (no-op elsewhere)."""
    if sys.platform != "darwin":
        return
    try:
        t = text.replace('"', "'").replace("\\", "")
        subprocess.Popen(["osascript", "-e", f'display notification "{t}" with title "{title}" sound name "Glass"'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------- live polling
class PollWorker(QtCore.QThread):
    done = QtCore.Signal(dict)

    def __init__(self, symbols, parent=None):
        super().__init__(parent)
        self.symbols = list(symbols)

    def run(self):
        out = {}
        try:
            import yfinance as yf
            for sym in self.symbols:
                try:
                    h = yf.Ticker(sym).history(period="1d", interval="1m", auto_adjust=False).tail(6)
                    if len(h):
                        out[sym] = (float(h["Close"].iloc[-1]), float(h["Low"].min()), float(h["High"].max()))
                except Exception:  # noqa: BLE001 - one bad symbol must not block the others
                    continue
        finally:
            self.done.emit(out)


class Poller(QtCore.QObject):
    """Every 30 s fetch the latest price of each symbol with an active alert and feed the manager."""

    def __init__(self, manager: AlertManager, interval_ms: int = 30_000):
        super().__init__()
        self.mgr = manager
        self._worker = None
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(interval_ms)

    def poll(self):
        if self._worker is not None and self._worker.isRunning():
            return
        syms = [s for s in self.mgr.active_symbols() if not s.startswith("file:")]
        if not syms:
            return
        self._worker = PollWorker(syms, self)
        self._worker.done.connect(self._got)
        self._worker.start()

    def _got(self, quotes: dict):
        for sym, (last, lo, hi) in quotes.items():
            self.mgr.check(sym, last, lo, hi)


# ---------------------------------------------------------------------- UI
class AlertDialog(QtWidgets.QDialog):
    def __init__(self, parent, label: str, price: float, value: float | None = None):
        super().__init__(parent)
        self.setWindowTitle("Create alert")
        self.setMinimumWidth(380)
        f = QtWidgets.QFormLayout(self)
        f.setContentsMargins(18, 16, 18, 16)
        f.setSpacing(10)
        title = QtWidgets.QLabel(f"<b style='font-size:15px'>{label}</b> &nbsp;<span style='color:{theme.MUTED}'>"
                                 f"last {price:,.2f}</span>")
        f.addRow(title)
        self.cond = QtWidgets.QComboBox()
        for key, text in CONDITIONS:
            self.cond.addItem(text, key)
        self.value = QtWidgets.QDoubleSpinBox()
        self.value.setDecimals(2)
        self.value.setRange(0, 1e9)
        self.value.setValue(value if value is not None else price)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItem("Only once", True)
        self.mode.addItem("Every time (1 min cooldown)", False)
        self.expiry = QtWidgets.QCheckBox("Expires on")
        self.when = QtWidgets.QDateTimeEdit(QtCore.QDateTime.currentDateTime().addDays(30))
        self.when.setCalendarPopup(True)
        self.when.setEnabled(False)
        self.expiry.toggled.connect(self.when.setEnabled)
        self.message = QtWidgets.QLineEdit()
        self.message.setPlaceholderText("Optional message")
        f.addRow("Condition", self.cond)
        f.addRow("Value", self.value)
        f.addRow("Trigger", self.mode)
        f.addRow(self.expiry, self.when)
        f.addRow("Message", self.message)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        ok = bb.addButton("Create", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setObjectName("primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def result_values(self) -> dict:
        return {"cond": self.cond.currentData(), "value": self.value.value(), "once": self.mode.currentData(),
                "message": self.message.text().strip(),
                "expires": self.when.dateTime().toString("yyyy-MM-ddTHH:mm:ss") if self.expiry.isChecked() else ""}


class AlertsPanel(QtWidgets.QWidget):
    newRequested = QtCore.Signal()

    def __init__(self, mgr: AlertManager, parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.mgr = mgr
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        row = QtWidgets.QHBoxLayout()
        new = QtWidgets.QPushButton("+  Create alert")
        new.setObjectName("primary")
        new.clicked.connect(self.newRequested)
        clr = QtWidgets.QPushButton("Clear done")
        clr.clicked.connect(mgr.clear_done)
        row.addWidget(new, 1)
        row.addWidget(clr)
        v.addLayout(row)
        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Alert", "Status", "Last"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setShowGrid(False)
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        v.addWidget(self.table, 2)
        self.empty = QtWidgets.QLabel(
            "<div style='text-align:center'><span style='font-size:15px;color:#dbdbdb'>No alerts yet</span><br><br>"
            f"<span style='color:{theme.MUTED}'>Press <b>Alt+A</b>, click the <b>+</b> next to the price scale,<br>"
            "or right-click the chart to add one at a price.</span></div>")
        self.empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        v.addWidget(self.empty, 2)
        v.addWidget(QtWidgets.QLabel(f"<span style='color:{theme.MUTED}'>Log</span>"))
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        v.addWidget(self.log, 1)
        mgr.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        al = self.mgr.alerts
        self.table.setVisible(bool(al))
        self.empty.setVisible(not al)
        self.table.setRowCount(len(al))
        colors = {"active": theme.UP, "triggered": "#ff9800", "paused": theme.MUTED, "expired": theme.MUTED}
        for r, a in enumerate(al):
            it = QtWidgets.QTableWidgetItem(describe(a))
            it.setData(QtCore.Qt.ItemDataRole.UserRole, a["id"])
            st = QtWidgets.QTableWidgetItem(a["status"].capitalize())
            st.setForeground(QtGui.QColor(colors.get(a["status"], theme.TEXT)))
            last = QtWidgets.QTableWidgetItem(a["last_fired"][5:16].replace("T", " ") if a["last_fired"] else "—")
            for c, w in enumerate((it, st, last)):
                self.table.setItem(r, c, w)
        self.log.setPlainText("\n".join(
            f"{h['time'][5:16].replace('T', ' ')}  {h['text']}  @ {h['price']:,.2f}"
            + (f"  - {h['message']}" if h.get("message") else "") for h in reversed(self.mgr.history[-50:])))

    def _menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        aid = self.table.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
        a = next((x for x in self.mgr.alerts if x["id"] == aid), None)
        m = QtWidgets.QMenu(self)
        if a and a["status"] == "active":
            m.addAction("Pause", lambda: self.mgr.set_status(aid, "paused"))
        elif a:
            m.addAction("Activate", lambda: self.mgr.set_status(aid, "active"))
        m.addAction("Delete", lambda: self.mgr.remove(aid))
        m.exec(self.table.viewport().mapToGlobal(pos))


class Toast(QtWidgets.QFrame):
    """Slide-in notification pill, top-right of the window."""

    def __init__(self, parent: QtWidgets.QWidget, title: str, text: str, color: str = "#ff9800"):
        super().__init__(parent)
        self.setObjectName("toast")
        self.setStyleSheet(f"QFrame#toast {{ background: rgba(24,26,33,245); border: 1px solid {color}; "
                           f"border-radius: 12px; }} QLabel {{ background: transparent; }}")
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(14, 10, 16, 10)
        ic = QtWidgets.QLabel()
        ic.setPixmap(icons.get("bell", color, 22).pixmap(22, 22))
        h.addWidget(ic)
        lab = QtWidgets.QLabel(f"<b style='font-size:13px;color:white'>{title}</b><br>"
                               f"<span style='color:{theme.TEXT}'>{text}</span>")
        h.addWidget(lab)
        self.adjustSize()
        shadow = QtWidgets.QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QtGui.QColor(0, 0, 0, 210))
        self.setGraphicsEffect(shadow)
        end = QtCore.QPoint(parent.width() - self.width() - 24, 64)
        self.move(end.x(), end.y() - 30)
        self.show()
        self.raise_()
        self._anim = QtCore.QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(320)
        self._anim.setStartValue(self.pos())
        self._anim.setEndValue(end)
        self._anim.setEasingCurve(QtCore.QEasingCurve.Type.OutBack)
        self._anim.start()
        QtCore.QTimer.singleShot(6500, self._leave)

    def _leave(self):
        a = QtCore.QPropertyAnimation(self, b"pos", self)
        a.setDuration(260)
        a.setStartValue(self.pos())
        a.setEndValue(QtCore.QPoint(self.x() + 40, self.y() - 10))
        a.finished.connect(self.deleteLater)
        a.start()
        self._leave_anim = a
