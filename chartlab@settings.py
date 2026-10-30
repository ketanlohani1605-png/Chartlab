"""TradingView-style chart settings: store (QSettings JSON) + the Settings dialog (Symbol / Status line / Scales / Canvas)."""
from __future__ import annotations

import copy
import json

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
    "countdown": True, "last_label": True, "last_line": True, "plus_button": True, "scale_right_bars": 12,
    "date_format": "%d %b '%y", "h24": True, "dow": True,
    # Canvas
    "bg": "#000000", "grid_v": True, "grid_h": True, "grid_color": "#262626",
    "crosshair": "#868993", "watermark": "Hidden", "wm_color": "#787b86",
    "text_color": "#d1d4dc", "text_size": 12, "margin_top": 10, "margin_bottom": 8,
}

_cur: dict = copy.deepcopy(DEFAULTS)
KEY = "chartlab/chart_settings"


def get() -> dict:
    return _cur


def load(qs: QtCore.QSettings):
    try:
        d = json.loads(qs.value(KEY, "{}") or "{}")
        _cur.update({k: v for k, v in d.items() if k in DEFAULTS})
    except Exception:  # noqa: BLE001
        pass
    sync_theme()


def save(qs: QtCore.QSettings):
    qs.setValue(KEY, json.dumps(_cur))


def sync_theme():
    theme.UP, theme.DOWN = _cur["up"], _cur["down"]


class ColorBtn(QtWidgets.QPushButton):
    changed = QtCore.Signal(str)

    def __init__(self, color: str):
        super().__init__()
        self.setFixedSize(28, 22)
        self.set(color)
        self.clicked.connect(self._pick)

    def set(self, c: str):
        self.c = c
        self.setStyleSheet(f"QPushButton{{background:{c};border:1px solid #434651;border-radius:4px;}}")

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
            "QDialog,QScrollArea,QScrollArea>QWidget>QWidget{background:#1e222d;border:0;} QLabel{color:#d1d4dc;background:transparent;} "
            "QCheckBox{color:#d1d4dc;spacing:8px;} QListWidget{background:#1e222d;border:0;border-right:1px solid #2a2e39;outline:0;} "
            "QListWidget::item{padding:10px 14px;color:#d1d4dc;} QListWidget::item:selected{background:#2a2e39;color:#fff;} "
            "QComboBox,QSpinBox{background:#131722;border:1px solid #434651;border-radius:4px;padding:4px 8px;min-height:22px;color:#d1d4dc;} "
            "QPushButton{background:#2a2e39;border:1px solid #434651;border-radius:6px;padding:7px 18px;color:#d1d4dc;} "
            "QPushButton:hover{background:#363a45;} QPushButton#ok{background:#d1d4dc;color:#131722;font-weight:600;}")
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        body = QtWidgets.QHBoxLayout()
        self.tabs = QtWidgets.QListWidget()
        self.tabs.setFixedWidth(200)
        self.stack = QtWidgets.QStackedWidget()
        for name, builder in (("Symbol", self._symbol), ("Status line", self._status),
                              ("Scales and lines", self._scales), ("Canvas", self._canvas)):
            self.tabs.addItem(name)
            page = QtWidgets.QWidget()
            lay = QtWidgets.QVBoxLayout(page)
            lay.setContentsMargins(22, 18, 22, 18)
            builder(lay)
            lay.addStretch(1)
            sc = QtWidgets.QScrollArea()
            sc.setWidgetResizable(True)
            sc.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            sc.setWidget(page)
            self.stack.addWidget(sc)
        self.tabs.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.tabs.setCurrentRow(start_tab)
        body.addWidget(self.tabs)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)
        foot = QtWidgets.QHBoxLayout()
        foot.setContentsMargins(16, 10, 16, 12)
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
        h.setStyleSheet("color:#787b86;font-size:11px;font-weight:600;margin-top:10px;")
        lay.addWidget(h)

    def _check(self, lay, key, text):
        c = QtWidgets.QCheckBox(text)
        c.setChecked(bool(self.v[key]))
        c.toggled.connect(lambda x, k=key: self.v.__setitem__(k, x))
        lay.addWidget(c)
        self._ctl[key] = c
        return c

    def _row(self, lay, text, widgets):
        r = QtWidgets.QHBoxLayout()
        r.addWidget(QtWidgets.QLabel(text), 1)
        for w in widgets:
            r.addWidget(w)
        lay.addLayout(r)

    def _color(self, key):
        b = ColorBtn(self.v[key])
        b.changed.connect(lambda c, k=key: self.v.__setitem__(k, c))
        self._ctl[key] = b
        return b

    def _combo(self, key, items):
        c = QtWidgets.QComboBox()
        c.addItems(items)
        c.setCurrentText(str(self.v[key]))
        c.currentTextChanged.connect(lambda t, k=key: self.v.__setitem__(k, t))
        c.setMinimumWidth(150)
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
            c = QtWidgets.QCheckBox(label)
            c.setChecked(bool(self.v[k]))
            c.toggled.connect(lambda x, kk=k: self.v.__setitem__(kk, x))
            self._row(lay, "", [c, self._color(ks[0]), self._color(ks[1])])
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
        self._check(lay, "last_label", "Symbol last value label")
        self._check(lay, "last_line", "Symbol last price line")
        self._check(lay, "countdown", "Countdown to bar close")
        self._check(lay, "plus_button", "Plus button")
        self._head(lay, "Time scale")
        self._check(lay, "dow", "Day of week on labels")
        self._check(lay, "h24", "24-hour format")
        self._row(lay, "Date format", [self._combo("date_format", ["%d %b '%y", "%b %d, %Y", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d"])])

    def _canvas(self, lay):
        self._head(lay, "Chart basic styles")
        self._row(lay, "Background", [self._color("bg")])
        c = QtWidgets.QCheckBox("Vertical grid lines")
        c.setChecked(self.v["grid_v"])
        c.toggled.connect(lambda x: self.v.__setitem__("grid_v", x))
        self._row(lay, "", [c, self._color("grid_color")])
        c = QtWidgets.QCheckBox("Horizontal grid lines")
        c.setChecked(self.v["grid_h"])
        c.toggled.connect(lambda x: self.v.__setitem__("grid_h", x))
        self._row(lay, "", [c])
        self._row(lay, "Crosshair colour", [self._color("crosshair")])
        self._row(lay, "Watermark", [self._combo("watermark", WATERMARKS), self._color("wm_color")])
        self._head(lay, "Scales")
        self._row(lay, "Text colour", [self._color("text_color")])
        self._row(lay, "Text size", [self._spin("text_size", 9, 20)])
        self._head(lay, "Margins")
        self._row(lay, "Top %", [self._spin("margin_top", 0, 40)])
        self._row(lay, "Bottom %", [self._spin("margin_bottom", 0, 40)])
        self._row(lay, "Right (bars)", [self._spin("scale_right_bars", 0, 100)])

    def _reset(self):
        self.v = copy.deepcopy(DEFAULTS)
        for k, w in self._ctl.items():
            val = self.v[k]
            if isinstance(w, QtWidgets.QCheckBox):
                w.setChecked(bool(val))
            elif isinstance(w, ColorBtn):
                w.set(val)
            elif isinstance(w, QtWidgets.QComboBox):
                w.setCurrentText(str(val))
            elif isinstance(w, QtWidgets.QSpinBox):
                w.setValue(int(val))

    def _ok(self):
        _cur.clear()
        _cur.update(self.v)
        sync_theme()
        self.applied.emit(_cur)
        self.accept()
