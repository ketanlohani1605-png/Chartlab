"""TradingView-style chart settings: store (QSettings JSON) + the Settings dialog (Symbol / Status line / Scales / Canvas)."""
from __future__ import annotations

import copy
import json

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from . import theme

DEFAULTS = {
    # Symbol
    "up": "#089981", "down": "#f23645",
    "body": True, "border": True, "wick": True,
    "border_up": "#089981", "border_down": "#f23645",
    "wick_up": "#089981", "wick_down": "#f23645",
    "prev_close": False, "precision": "Default",
    # Status line
    "sl_title": True, "sl_values": True, "sl_change": True, "sl_volume": True, "sl_trade": True,
    "sl_ind_titles": True, "sl_ind_values": True,
    # Scales and lines
    "settings_rev": 30, "countdown": True, "last_label": True, "sym_label": False, "last_line": True, "plus_button": True, "scale_right_bars": 12,
    "date_format": "%d %b '%y", "h24": True, "dow": True,
    # Canvas
    "bg": "#000000", "grid_v": True, "grid_h": True, "grid_color": "#303030",
    "crosshair": "#9c9c9c", "crosshair_style": "dashed", "watermark": "Hidden", "wm_color": "#787b86",
    "text_color": "#b8b8b8", "text_size": 12, "margin_top": 10, "margin_bottom": 8,
}

_cur: dict = copy.deepcopy(DEFAULTS)
KEY = "chartlab/chart_settings"


def get() -> dict:
    return _cur


def load(qs: QtCore.QSettings):
    d = {}
    try:
        d = json.loads(qs.value(KEY, "{}") or "{}")
        _cur.update({k: v for k, v in d.items() if k in DEFAULTS})
    except Exception:  # noqa: BLE001
        pass
    if int(d.get("settings_rev", 0) or 0) < SETTINGS_REV:   # once per new version: an old default that was never
        for key, (olds, new) in _MOVED_DEFAULTS.items():    # changed by hand becomes the new default
            if str(_cur.get(key, "")).lower() in olds:
                _cur[key] = new
    _cur["settings_rev"] = SETTINGS_REV                     # (a choice made after this is never touched again)
    sync_theme()


SETTINGS_REV = 30


# defaults that changed between versions: key -> (earlier defaults, new default). v0.30 matches TradingView's
# measured colours: crosshair #9c9c9c, grid #303030 (dotted), scale text #b8b8b8.
_MOVED_DEFAULTS = {"crosshair": (("#868993", "#9598a1"), "#9c9c9c"),
                   "grid_color": (("#262626", "#2a2e39", "#1e222d", "#1a1a1a"), "#303030"),
                   "text_color": (("#d1d4dc", "#b2b5be"), "#b8b8b8"),
                   "sym_label": (("true",), False)}      # TradingView shows no symbol name next to the price


def save(qs: QtCore.QSettings):
    qs.setValue(KEY, json.dumps(_cur))


def sync_theme():
    theme.UP, theme.DOWN = _cur["up"], _cur["down"]


def _default_store() -> Path:
    from . import optstore
    return optstore.default_root()


def datamod_dir() -> str:
    from . import data as datamod
    return str(datamod.DATA_DIR / "downloads")


class ColorBtn(QtWidgets.QPushButton):
    changed = QtCore.Signal(str)

    def __init__(self, color: str):
        super().__init__()
        self.setFixedSize(34, 30)
        self.set(color)
        self.clicked.connect(self._pick)

    def set(self, c: str):
        self.c = c
        self.setStyleSheet(f"QPushButton{{background:{c};border:4px solid #1e1e1e;border-radius:7px;"
                           f"outline:1px solid #4a4a4a;}} QPushButton{{border:1px solid #4a4a4a;}}")

    def _pick(self):
        col = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.c), self, "Colour")
        if col.isValid():
            self.set(col.name())
            self.changed.emit(col.name())


PRECISIONS = ["Default", "Integer"] + [f"{i} decimals" if i > 1 else "1 decimal" for i in range(1, 16)]
WATERMARKS = ["Hidden", "Ticker", "Interval", "Ticker and description", "Logo"]
STATUS_TITLES = ["Name", "Ticker"]


class SettingsDialog(QtWidgets.QDialog):
    applied = QtCore.Signal(dict)

    def __init__(self, parent=None, start_tab: int = 0):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(760, 560)
        self.v = copy.deepcopy(_cur)
        self._ctl: dict[str, QtWidgets.QWidget] = {}
        self.setStyleSheet(
            "QDialog{background:#1e1e1e;} QDialog QWidget{background:transparent;} "
            "QLabel{color:#dbdbdb;font-size:14px;} QLabel#sec{color:#9598a1;font-size:11px;font-weight:600;} "
            "QCheckBox{color:#dbdbdb;spacing:10px;font-size:14px;} "
            "QCheckBox::indicator{width:16px;height:16px;border:1px solid #6a6d78;border-radius:4px;background:#1e1e1e;} "
            "QCheckBox::indicator:checked{background:#ffffff;border-color:#ffffff;image:url(%s);} "
            "QListWidget{border:0;outline:0;} "
            "QListWidget::item{padding:9px 12px;color:#dbdbdb;border-radius:6px;margin:1px 0;} "
            "QListWidget::item:selected{background:#2e2e2e;color:#fff;} QListWidget::item:hover{background:#262626;} "
            "QComboBox,QSpinBox{background:#1e1e1e;border:1px solid #4a4a4a;border-radius:6px;padding:5px 10px;"
            "min-height:22px;color:#dbdbdb;font-size:14px;} QComboBox:hover,QSpinBox:hover{border-color:#6a6d78;} "
            "QComboBox QAbstractItemView{background:#1e1e1e;border:1px solid #2e2e2e;selection-background-color:#2e2e2e;} "
            "QPushButton{background:transparent;border:1px solid #6a6d78;border-radius:8px;padding:7px 18px;color:#dbdbdb;"
            "font-size:14px;} QPushButton:hover{background:#2e2e2e;} "
            "QPushButton[here=\"true\"]{background:#2e2e2e;border-color:#dbdbdb;color:#ffffff;} "
            "QPushButton#ok{background:#ffffff;color:#000;font-weight:600;border-color:#ffffff;} "
            "QPushButton#ok:hover{background:#e0e0e0;} QFrame#footline{background:#2e2e2e;}" % theme.check_icon_path())
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(16, 16, 0, 0)
        self.tabs = QtWidgets.QListWidget()
        self.tabs.setFixedWidth(210)
        self.tabs.setIconSize(QtCore.QSize(20, 20))
        self.stack = QtWidgets.QStackedWidget()
        from . import icons
        for name, ico, builder in (("Symbol", "candles_icon", self._symbol), ("Status line", "menu", self._status),
                                   ("Scales and lines", "measure", self._scales), ("Canvas", "rect", self._canvas),
                                   ("Updates", "update_icon", self._updates), ("Data", "cloud", self._data)):
            self.tabs.addItem(QtWidgets.QListWidgetItem(icons.get(ico, "#dbdbdb"), "  " + name))
            page = QtWidgets.QWidget()
            lay = QtWidgets.QVBoxLayout(page)
            lay.setContentsMargins(26, 4, 26, 18)
            lay.setSpacing(10)
            builder(lay)
            lay.addStretch(1)
            sc = QtWidgets.QScrollArea()
            sc.setWidgetResizable(True)
            sc.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)   # pages fit the width
            sc.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            sc.setWidget(page)
            self.stack.addWidget(sc)
        self.tabs.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.tabs.setCurrentRow(start_tab)
        body.addWidget(self.tabs)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)
        fl = QtWidgets.QFrame()
        fl.setObjectName("footline")
        fl.setFixedHeight(1)
        root.addWidget(fl)
        foot = QtWidgets.QHBoxLayout()
        foot.setContentsMargins(20, 12, 20, 14)
        tpl = QtWidgets.QPushButton("Template ▾")
        m = QtWidgets.QMenu(tpl)
        m.addAction("Reset settings", self._reset)
        tpl.setMenu(m)
        foot.addWidget(tpl)
        foot.addStretch(1)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QtWidgets.QPushButton("Ok")
        ok.setObjectName("ok")
        ok.setDefault(True)
        ok.clicked.connect(self._ok)
        foot.addWidget(cancel)
        foot.addWidget(ok)
        root.addLayout(foot)

    # -- helpers
    def _head(self, lay, text):
        h = QtWidgets.QLabel(text.upper())
        h.setObjectName("sec")
        lay.addSpacing(12)
        lay.addWidget(h)

    def _check(self, lay, key, text):
        c = QtWidgets.QCheckBox(text)
        c.setChecked(bool(self.v[key]))
        c.toggled.connect(lambda x, k=key: self.v.__setitem__(k, x))
        lay.addWidget(c)
        self._ctl[key] = c
        return c

    def _row(self, lay, text, widgets):
        """TradingView two-column row: label (or a checkbox) on the left, controls from a fixed column."""
        r = QtWidgets.QHBoxLayout()
        r.setSpacing(8)
        first = text if isinstance(text, QtWidgets.QWidget) else QtWidgets.QLabel(text)
        first.setFixedWidth(190)
        r.addWidget(first)
        for w in widgets:
            r.addWidget(w)
        r.addStretch(1)
        lay.addLayout(r)

    def _cb(self, key, text):
        c = QtWidgets.QCheckBox(text)
        c.setChecked(bool(self.v[key]))
        c.toggled.connect(lambda x, k=key: self.v.__setitem__(k, x))
        self._ctl[key] = c
        return c

    def _color(self, key):
        b = ColorBtn(self.v[key])
        b.changed.connect(lambda c, k=key: self.v.__setitem__(k, c))
        self._ctl[key] = b
        return b

    def _combo(self, key, items):
        """items: plain strings, or (shown text, stored value) pairs."""
        c = QtWidgets.QComboBox()
        vals = []
        for it in items:
            lab, val = it if isinstance(it, tuple) else (it, it)
            c.addItem(lab, val)
            vals.append(val)
        i = c.findData(self.v[key])
        c.setCurrentIndex(i if i >= 0 else max(0, c.findText(str(self.v[key]))))

        def picked(i, k=key, vals=vals):          # (never touches the combo: it may be mid-destruction)
            if 0 <= i < len(vals):
                self.v[k] = vals[i]
        c.currentIndexChanged.connect(picked)
        c.setMinimumWidth(180)
        self._ctl[key] = c
        return c

    def _spin(self, key, lo, hi):
        s = QtWidgets.QSpinBox()
        s.setRange(lo, hi)
        s.setValue(int(self.v[key]))
        s.setMinimumWidth(80)
        s.valueChanged.connect(lambda x, k=key: self.v.__setitem__(k, x))
        self._ctl[key] = s
        return s

    # -- tabs
    def _symbol(self, lay):
        self._head(lay, "Candles")
        self._check(lay, "prev_close", "Color bars based on previous close")
        for k, label in (("body", "Body"), ("border", "Borders"), ("wick", "Wick")):
            ks = {"body": ("up", "down"), "border": ("border_up", "border_down"), "wick": ("wick_up", "wick_down")}[k]
            self._row(lay, self._cb(k, label), [self._color(ks[0]), self._color(ks[1])])
        self._head(lay, "Precision")
        self._row(lay, "Precision", [self._combo("precision", PRECISIONS)])

    def _status(self, lay):
        self._head(lay, "Symbol")
        self._check(lay, "sl_title", "Title")
        self._check(lay, "sl_values", "Chart values (OHLC)")
        self._check(lay, "sl_change", "Bar change values")
        self._check(lay, "sl_volume", "Volume")
        self._check(lay, "sl_trade", "Buy/sell buttons")
        self._head(lay, "Indicators")
        self._check(lay, "sl_ind_titles", "Titles")
        self._check(lay, "sl_ind_values", "Values")

    def _scales(self, lay):
        self._head(lay, "Price scale")
        self._check(lay, "sym_label", "Symbol name label")
        self._check(lay, "last_label", "Symbol last value label")
        self._check(lay, "last_line", "Symbol last price line")
        self._check(lay, "countdown", "Countdown to bar close")
        self._check(lay, "plus_button", "Plus button")
        self._head(lay, "Time scale")
        self._check(lay, "dow", "Day of week on labels")
        self._check(lay, "h24", "24-hour format")
        import datetime as _dt
        today = _dt.date.today()                           # show what each format looks like, not "%d %b"
        fmts = ["%d %b '%y", "%b %d, %Y", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d"]
        self._row(lay, "Date format", [self._combo("date_format", [(today.strftime(f), f) for f in fmts])])

    def _canvas(self, lay):
        self._head(lay, "Chart basic styles")
        self._row(lay, "Background", [self._color("bg")])
        self._row(lay, self._cb("grid_v", "Vertical grid lines"), [self._color("grid_color")])
        self._row(lay, self._cb("grid_h", "Horizontal grid lines"), [])
        self._row(lay, "Crosshair", [self._color("crosshair"),
                                     self._combo("crosshair_style", [("Dashed", "dashed"), ("Dotted", "dotted"),
                                                                     ("Solid", "solid")])])
        self._row(lay, "Watermark", [self._combo("watermark", WATERMARKS), self._color("wm_color")])
        self._head(lay, "Scales")
        self._row(lay, "Text", [self._color("text_color"), self._spin("text_size", 9, 20)])
        self._head(lay, "Margins")
        for key, lab, lo, hi, unit in (("margin_top", "Top", 0, 40, "%"), ("margin_bottom", "Bottom", 0, 40, "%"),
                                       ("scale_right_bars", "Right", 0, 100, "bars")):
            u = QtWidgets.QLabel(unit)
            u.setStyleSheet("color:#9598a1;")
            self._row(lay, lab, [self._spin(key, lo, hi), u])

    UPDATES_TAB = 4

    def _updates(self, lay):
        """The app's version and its updates, in Settings (TradingView Desktop keeps them with the app's settings)."""
        from . import __version__
        win = self.parent()
        self._head(lay, "ChartLab")
        ver = QtWidgets.QLabel(f"Version {getattr(win, 'version', __version__)}")
        ver.setStyleSheet("font-size:20px;font-weight:600;color:#ffffff;")
        lay.addWidget(ver)
        self.upd_state = QtWidgets.QLabel("")
        self.upd_state.setWordWrap(True)
        self.upd_state.setStyleSheet("color:#9598a1;")
        lay.addWidget(self.upd_state)
        row = QtWidgets.QHBoxLayout()
        self.upd_check = QtWidgets.QPushButton("Check for updates")
        self.upd_go = QtWidgets.QPushButton("Restart to update")
        self.upd_go.setObjectName("ok")
        self.upd_go.hide()
        row.addWidget(self.upd_check)
        row.addWidget(self.upd_go)
        row.addStretch(1)
        lay.addLayout(row)
        self._head(lay, "Automatic updates")
        auto = QtWidgets.QCheckBox("Check for a new version when ChartLab starts, and every few hours")
        qs = getattr(win, "settings", None)
        auto.setChecked(qs is None or qs.value("auto_update", "true") != "false")
        if qs is not None:
            auto.toggled.connect(lambda on: qs.setValue("auto_update", "true" if on else "false"))
        lay.addWidget(auto)
        self.upd_auto = auto
        note = QtWidgets.QLabel("A new version is downloaded in the background and checked; nothing changes until you "
                                "click Restart to update. Your charts, drawings and settings stay.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#787b86;font-size:12px;")
        lay.addWidget(note)
        if hasattr(win, "check_updates"):
            self.upd_check.clicked.connect(lambda: win.check_updates(True))
            self.upd_go.clicked.connect(lambda: (self.reject(), win.start_update()))
            self._upd_timer = QtCore.QTimer(self)
            self._upd_timer.timeout.connect(self._upd_tick)
            self._upd_timer.start(250)
            self._upd_tick()
        else:
            self.upd_check.setEnabled(False)

    DATA_TAB = 5

    def _data(self, lay):
        """What ChartLab does by itself: fresh broker logins, and the data store - where it lives (a cloud drive
        when there is one) and what goes into it. (App settings: saved at once, not part of the chart template.)"""
        win = self.parent()
        qs = getattr(win, "settings", None)
        if qs is None or not hasattr(win, "option_archive_settings"):
            lay.addWidget(QtWidgets.QLabel("Open Settings from the main window to change these."))
            return
        from . import optstore, store

        def flag(key, default, text, after=None):
            c = QtWidgets.QCheckBox(text)
            c.setChecked(qs.value(key, default) == "true")

            def put(on):
                qs.setValue(key, "true" if on else "false")
                if after:
                    after(on)
            c.toggled.connect(put)
            lay.addWidget(c)
            return c

        def small(text):
            n = QtWidgets.QLabel(text)
            n.setWordWrap(True)
            n.setStyleSheet("color:#787b86;font-size:12px;")
            lay.addWidget(n)
            return n

        cfg = win.option_archive_settings()
        # ---- where the data lives
        self._head(lay, "Data store")
        self.d_where = QtWidgets.QLabel("")
        self.d_where.setWordWrap(True)
        lay.addWidget(self.d_where)
        self.d_folder = QtWidgets.QLabel(cfg["folder"], self)      # (the plain path: for "Choose folder" and tests)
        self.d_folder.hide()
        self.d_size = small("")
        r = QtWidgets.QHBoxLayout()
        r.setSpacing(8)
        self.d_cloud_btns = {}
        for name, base in store.cloud_folders():
            b = QtWidgets.QPushButton(name)
            b.setToolTip(f"Keep the data in {name} \u203a ChartLab: {name} copies it to the cloud by itself")
            b.clicked.connect(lambda _=False, t=store.cloud_target(base): self._set_data_folder(str(t)))
            r.addWidget(b)
            self.d_cloud_btns[name] = b
        self.d_local = QtWidgets.QPushButton("This computer")
        self.d_local.setToolTip("Keep the data only on this computer (~/ChartLab)")
        self.d_local.clicked.connect(lambda: self._set_data_folder(""))
        choose = QtWidgets.QPushButton("Choose folder\u2026")
        choose.clicked.connect(lambda: self._set_data_folder(QtWidgets.QFileDialog.getExistingDirectory(
            self, "Folder for the data store", str(Path(self.d_folder.text()).parent)), ask=True))
        show = QtWidgets.QPushButton("Show folder")
        show.setToolTip("Open the folder the data is in")
        show.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(Path(win.option_archive_settings()["folder"]).parent))))
        r.addWidget(self.d_local)
        r.addStretch(1)
        lay.addLayout(r)
        r1 = QtWidgets.QHBoxLayout()                          # (a second row: the page never gets wider than it is)
        r1.setSpacing(8)
        for b in (choose, show):
            r1.addWidget(b)
        r1.addStretch(1)
        lay.addLayout(r1)
        if not self.d_cloud_btns:
            small("No cloud drive was found on this computer. Turn on iCloud Drive (System Settings \u203a your name "
                  "\u203a iCloud \u203a iCloud Drive) or install Google Drive for desktop: the next time ChartLab "
                  "starts it finds the drive and moves the data there by itself.")
        # ---- logins
        self._head(lay, "Broker login")
        self.d_renew = flag("auto_renew", "true", "Keep broker logins fresh automatically", win.set_auto_renew)
        small("Dhan tokens are renewed before their 24 hours end; with PIN + TOTP key saved in the account, ChartLab "
              "also logs in again by itself. Angel One and Fyers the same way; Zerodha and Upstox want a browser "
              "login every morning (their rule). Chart data comes from every logged-in account in turn: when one "
              "stops answering, the next one takes over.")
        # ---- chart data
        self._head(lay, "Chart data")
        self.d_daily = flag("daily_download", "true", "Save the whole watchlist every day (daily and 1-minute bars)",
                            lambda on: win._daily_download() if on else None)
        self.d_daily_last = small("")
        small("Every chart you open is kept too, so intraday history keeps growing instead of ending at the "
              "source's last few days. In a script: chartlab.history('RELIANCE', '5m').")
        # ---- options
        self._head(lay, "Option archive")
        self.d_opt = flag("opt_archive", "true", "Save every day the 1-minute history of options, with open interest")
        self.d_unds = QtWidgets.QLineEdit(", ".join(cfg["underlyings"]))
        self.d_unds.setStyleSheet("QLineEdit{background:#1e1e1e;border:1px solid #4a4a4a;border-radius:6px;"
                                  "padding:5px 10px;color:#dbdbdb;font-size:14px;}")
        self.d_unds.setMinimumWidth(240)
        self.d_unds.setCursorPosition(0)                      # (show the start of the list, not its end)
        self.d_unds.setToolTip("Index names whose options are saved, separated by commas")
        self.d_unds.editingFinished.connect(lambda: qs.setValue("opt_underlyings", self.d_unds.text().upper()))
        rn = QtWidgets.QHBoxLayout()
        rn.setSpacing(8)
        ln = QtWidgets.QLabel("Names")
        ln.setFixedWidth(190)
        rn.addWidget(ln)
        rn.addWidget(self.d_unds, 1)                          # as wide as the page allows
        lay.addLayout(rn)
        self.d_stocks = flag("opt_stocks", "true", "Also every stock of my watchlist that has options")
        self.d_width = QtWidgets.QSpinBox()
        self.d_width.setFixedWidth(72)
        self.d_width.setRange(1, 60)
        self.d_width.setValue(cfg["width"])
        self.d_width.valueChanged.connect(lambda v: qs.setValue("opt_width", int(v)))
        self.d_all_strikes = QtWidgets.QCheckBox("All strikes")
        self.d_all_strikes.setToolTip("Every strike of every followed expiry: thousands of contracts, "
                                      "the first pass takes a while")
        self.d_all_strikes.setChecked(cfg["all_strikes"])
        self.d_all_strikes.toggled.connect(lambda on: (qs.setValue("opt_all_strikes", "true" if on else "false"),
                                                       self.d_width.setEnabled(not on)))
        self.d_width.setEnabled(not cfg["all_strikes"])
        self._row(lay, "Strikes each side", [self.d_width, self.d_all_strikes])
        self.d_exp = QtWidgets.QSpinBox()
        self.d_exp.setFixedWidth(72)
        self.d_exp.setRange(1, 6)
        self.d_exp.setValue(cfg["expiries"])
        self.d_exp.valueChanged.connect(lambda v: qs.setValue("opt_expiries", int(v)))
        self.d_all_exp = QtWidgets.QCheckBox("All listed expiries")
        self.d_all_exp.setToolTip("Every weekly, monthly and quarterly expiry the exchange lists")
        self.d_all_exp.setChecked(cfg["all_expiries"])
        self.d_all_exp.toggled.connect(lambda on: (qs.setValue("opt_all_expiries", "true" if on else "false"),
                                                   self.d_exp.setEnabled(not on)))
        self.d_exp.setEnabled(not cfg["all_expiries"])
        self._row(lay, "Nearest expiries", [self.d_exp, self.d_all_exp])
        now = QtWidgets.QPushButton("Save now")
        now.clicked.connect(lambda: (win._options_archive(force=True), win._daily_download(force=True)))
        r2 = QtWidgets.QHBoxLayout()
        r2.addWidget(now)
        r2.addStretch(1)
        lay.addLayout(r2)
        self.d_last = small("")
        small("Weekly and monthly expiries alike. One file per contract, from its listing day to its expiry; expired "
              "contracts stay and still open as charts. Several Dhan accounts share the work. Needs a connected "
              "Dhan account. In a script: chartlab.contracts('NIFTY').")
        self._data_timer = QtCore.QTimer(self)
        self._data_timer.timeout.connect(self._data_tick)
        self._data_timer.start(500)
        self._size_at = 0.0
        self._data_tick()

    def _set_data_folder(self, path, ask: bool = False):
        win = self.parent()
        if ask and not path:
            return                                           # the folder dialog was cancelled
        if path and ask:
            path = str(Path(path) / "options") if Path(path).name.lower() != "options" else path
        win.set_data_folder(path)
        self._size_at = 0.0
        self._data_tick()

    def _data_tick(self):
        import time as _t
        from . import store
        win = self.parent()
        cfg = win.option_archive_settings()
        home = Path(cfg["folder"]).parent
        cloud = cfg.get("cloud") or ""
        moving = getattr(win, "_store_busy", False)
        if moving:
            self.d_where.setText("Moving the data store\u2026")
        elif cloud:
            self.d_where.setText(f"In {store.nice_path(home)}  \u2014  {cloud} keeps a copy in the cloud")
        else:
            self.d_where.setText(f"On this computer only  ({store.nice_path(home)})")
        self.d_folder.setText(cfg["folder"])
        local = not cloud and Path(cfg["folder"]) == _default_store()
        for name, b in list(self.d_cloud_btns.items()) + [("", self.d_local)]:
            here = (name == cloud) if name else local
            b.setEnabled(not moving and not here)
            if b.property("here") != here:                   # the place the data is in now: shown as the chosen one
                b.setProperty("here", here)
                b.style().unpolish(b)
                b.style().polish(b)
        if _t.time() - self._size_at > 20 and not getattr(self, "_size_job", None):
            self._size_at = _t.time()
            from .panel import LoadWorker

            def job():
                a, b = store.folder_size(Path(cfg["folder"]))
                c, d2 = store.folder_size(store.candles_root(cfg["folder"]))
                return (a + c, b + d2), ""
            w = self._size_job = LoadWorker(job, self)

            def done(res, _s):
                self._size_job = None
                size, files = res
                self.d_size.setText(f"{store.nice_size(size)} in {files:,} files" if files else "Nothing saved yet.")
            w.done.connect(done)
            w.failed.connect(lambda *_: setattr(self, "_size_job", None))
            w.start()
        busy = getattr(win, "_opt_busy", False)
        last = str(win.settings.value("opt_last", "") or "")
        if busy:
            k, n = getattr(win, "_opt_progress", (0, 0))
            self.d_last.setText(f"Saving now\u2026  {k:,} of {n:,}" if n else "Saving now\u2026")
        else:
            self.d_last.setText("Last run: " + last if last else "Not run yet.")
        dl = str(win.settings.value("daily_last", "") or "")
        self.d_daily_last.setText("Saving now\u2026" if getattr(win, "_daily_busy", False)
                                  else ("Last run: " + dl if dl else "Not run yet."))

    def done(self, r):
        job = getattr(self, "_size_job", None)
        if job is not None and job.isRunning():
            job.wait(3000)
        super().done(r)

    def _upd_tick(self):
        win = self.parent()
        text, ready = win.update_state()
        self.upd_state.setText(text)
        self.upd_go.setVisible(ready)
        self.upd_check.setEnabled(not getattr(win, "_upd_busy", False))

    def _reset(self):
        self.v = copy.deepcopy(DEFAULTS)
        for k, w in self._ctl.items():
            val = self.v[k]
            if isinstance(w, QtWidgets.QCheckBox):
                w.setChecked(bool(val))
            elif isinstance(w, ColorBtn):
                w.set(val)
            elif isinstance(w, QtWidgets.QComboBox):
                i = w.findData(val)
                w.setCurrentIndex(i if i >= 0 else max(0, w.findText(str(val))))
            elif isinstance(w, QtWidgets.QSpinBox):
                w.setValue(int(val))

    def _ok(self):
        _cur.clear()
        _cur.update(self.v)
        sync_theme()
        self.applied.emit(_cur)
        self.accept()
