"""The small toolbar that appears when a drawing is selected (TradingView's floating drawing toolbar): colour, line
width, line style, lock, clone, settings, delete - and the settings dialog a double-click on a drawing opens."""
from __future__ import annotations

import pandas as pd
from PySide6 import QtCore, QtGui, QtWidgets

from . import icons, theme
from .drawings import DEFAULT_COLOR, DEFAULT_WIDTH, TOOL_LABELS

COLORS = [("Blue", "#2962ff"), ("Red", "#f23645"), ("Green", "#089981"), ("Orange", "#ff9800"), ("Yellow", "#ffeb3b"),
          ("Purple", "#9c27b0"), ("Teal", "#00bcd4"), ("Pink", "#e91e63"), ("Grey", "#787b86"), ("White", "#ffffff")]
WIDTHS = [1.0, 1.6, 2.5, 4.0]
STYLE_NAMES = [("solid", "Solid"), ("dash", "Dashed"), ("dot", "Dotted")]
NO_LINE = ("text", "measure", "pricerange", "daterange", "long", "short")      # width / style do not apply


def swatch(color: str, size: int = 16) -> QtGui.QIcon:
    pm = QtGui.QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2.0)
    pm.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    p.setPen(QtGui.QPen(QtGui.QColor("#5d606b"), 1))
    p.setBrush(QtGui.QColor(color))
    p.drawRoundedRect(QtCore.QRectF(1.5, 1.5, size - 3, size - 3), 3, 3)
    p.end()
    return QtGui.QIcon(pm)


def line_icon(width: float, style: str = "solid", color: str = "#d1d4dc") -> QtGui.QIcon:
    pm = QtGui.QPixmap(56, 32)
    pm.setDevicePixelRatio(2.0)
    pm.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    pen = QtGui.QPen(QtGui.QColor(color), width)
    pen.setStyle({"dash": QtCore.Qt.PenStyle.DashLine, "dot": QtCore.Qt.PenStyle.DotLine}.get(style,
                                                                                             QtCore.Qt.PenStyle.SolidLine))
    p.setPen(pen)
    p.drawLine(QtCore.QPointF(3, 8), QtCore.QPointF(25, 8))
    p.end()
    return QtGui.QIcon(pm)


class DrawBar(QtWidgets.QFrame):
    def __init__(self, chart):
        super().__init__(chart.glw)
        self.chart = chart
        self.setObjectName("drawbar")
        self.setStyleSheet(
            "QFrame#drawbar{background:#1e222d;border:1px solid #363a45;border-radius:8px;}"
            "QToolButton{background:transparent;border:0;border-radius:5px;padding:4px;color:#d1d4dc;font-size:12px;}"
            "QToolButton:hover{background:#2a2e39;} QToolButton:checked{background:#2962ff;}"
            "QToolButton::menu-indicator{image:none;width:0;}")
        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(6, 4, 6, 4)
        h.setSpacing(2)
        self.name = QtWidgets.QLabel("")
        self.name.setStyleSheet(f"color:{theme.MUTED};font-size:12px;padding:0 6px 0 2px;background:transparent;border:0;")
        h.addWidget(self.name)

        def btn(tip, icon=None, text=""):
            b = QtWidgets.QToolButton()
            b.setToolTip(tip)
            b.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
            b.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            b.setIconSize(QtCore.QSize(18, 18))
            if icon is not None:
                b.setIcon(icon)
            if text:
                b.setText(text)
            h.addWidget(b)
            return b
        self.color_btn = btn("Colour")
        m = QtWidgets.QMenu(self.color_btn)
        for name, c in COLORS:
            m.addAction(swatch(c), name, lambda c=c: self._set(color=c))
        self.color_btn.setMenu(m)
        self.color_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.width_btn = btn("Line width")
        self.width_btn.setIconSize(QtCore.QSize(28, 16))
        m = QtWidgets.QMenu(self.width_btn)
        for w in WIDTHS:
            m.addAction(line_icon(w), f"{w:g} px", lambda w=w: self._set(w=w))
        self.width_btn.setMenu(m)
        self.width_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.style_btn = btn("Line style")
        self.style_btn.setIconSize(QtCore.QSize(28, 16))
        m = QtWidgets.QMenu(self.style_btn)
        for key, lab in STYLE_NAMES:
            m.addAction(line_icon(2, key), lab, lambda k=key: self._set(ls=k))
        self.style_btn.setMenu(m)
        self.style_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self.lock_btn = btn("Lock: the drawing cannot be moved by mistake", icons.get("lock"))
        self.lock_btn.setCheckable(True)
        self.lock_btn.clicked.connect(lambda on: self._set(lock=True if on else None))
        self.clone_btn = btn("Clone  (or hold Option and drag the drawing)", icons.get("plus"))
        self.clone_btn.clicked.connect(lambda: self.chart.layer.duplicate_selected())
        self.gear_btn = btn("Settings…  (double-click the drawing)", icons.get("gear"))
        self.gear_btn.clicked.connect(lambda: self.chart.edit_drawing(self.chart.layer.selected))
        self.del_btn = btn("Remove  (Delete)", icons.get("trash"))
        self.del_btn.clicked.connect(lambda: self.chart.layer.delete_selected())
        self.hide()

    def _set(self, **props):
        lay = self.chart.layer
        if lay.selected is not None:
            lay.set_props(lay.selected, **props)

    def sync(self):
        lay = self.chart.layer
        d = lay.get(lay.selected) if lay.selected is not None else None
        if d is None or lay.tool or lay.hidden:
            self.hide()
            return
        self.name.setText(TOOL_LABELS.get(d["type"], d["type"]).split(" (")[0])
        col = d.get("color", DEFAULT_COLOR)
        self.color_btn.setIcon(swatch(col))
        line = d["type"] not in NO_LINE
        self.width_btn.setVisible(line)
        self.style_btn.setVisible(line)
        self.width_btn.setIcon(line_icon(float(d.get("w", DEFAULT_WIDTH))))
        self.style_btn.setIcon(line_icon(2, d.get("ls", "solid")))
        self.lock_btn.setChecked(bool(d.get("lock")))
        self.adjustSize()
        self.place()
        self.show()
        self.raise_()

    def place(self):
        r = self.chart.price.vb.sceneBoundingRect()
        tl = self.chart.glw.mapFromScene(r.topLeft())
        x = int(tl.x() + (r.width() - self.width()) / 2)
        hd = self.chart.header
        y = hd.y() + hd.height() + 6 if not hd.isHidden() else int(tl.y()) + 8      # under the OHLC line, not on it
        self.move(max(4, x), int(y))


class DrawingSettings(QtWidgets.QDialog):
    """Everything about one drawing: colour, width, style, text, lock and the exact price / time of each point."""

    def __init__(self, chart, d: dict):
        super().__init__(chart)
        self.chart, self.d = chart, d
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(TOOL_LABELS.get(d["type"], d["type"]).split(" (")[0])
        self.setMinimumWidth(380)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 14)
        v.setSpacing(10)
        form = QtWidgets.QFormLayout()
        form.setSpacing(8)
        self.color = d.get("color", DEFAULT_COLOR)
        self.color_btn = QtWidgets.QPushButton()
        m = QtWidgets.QMenu(self.color_btn)
        for name, c in COLORS:
            m.addAction(swatch(c), name, lambda c=c: self._pick(c))
        m.addSeparator()
        m.addAction("Other colour…", self._custom)
        self.color_btn.setMenu(m)
        self._pick(self.color)
        form.addRow("Colour", self.color_btn)
        self.width = self.style = None
        if d["type"] not in NO_LINE:
            self.width = QtWidgets.QComboBox()
            for w in WIDTHS:
                self.width.addItem(line_icon(w), f"{w:g} px", w)
            cur = float(d.get("w", DEFAULT_WIDTH))
            self.width.setCurrentIndex(min(range(len(WIDTHS)), key=lambda i: abs(WIDTHS[i] - cur)))
            self.width.setIconSize(QtCore.QSize(28, 16))
            form.addRow("Line width", self.width)
            self.style = QtWidgets.QComboBox()
            for key, lab in STYLE_NAMES:
                self.style.addItem(line_icon(2, key), lab, key)
            self.style.setCurrentIndex(max(0, self.style.findData(d.get("ls", "solid"))))
            self.style.setIconSize(QtCore.QSize(28, 16))
            form.addRow("Line style", self.style)
        self.text = None
        if d["type"] == "text":
            self.text = QtWidgets.QLineEdit(d.get("text", ""))
            form.addRow("Text", self.text)
        self.pts = []
        if d["type"] != "brush":
            names = {"long": ["Entry", "Target", "Stop"], "short": ["Entry", "Target", "Stop"]}.get(d["type"])
            for i, (t, price) in enumerate(d["pts"]):
                sp = QtWidgets.QDoubleSpinBox()
                sp.setDecimals(2)
                sp.setRange(-1e9, 1e9)
                sp.setValue(float(price))
                sp.setGroupSeparatorShown(True)
                when = pd.Timestamp(t, unit="s")
                lab = (names[i] if names and i < len(names) else f"Point {i + 1}") if d["type"] != "vline" else "Time"
                if d["type"] == "vline":
                    form.addRow(lab, QtWidgets.QLabel(f"{when:%d %b %Y %H:%M}"))
                    continue
                sp.setToolTip(f"{when:%d %b %Y %H:%M}")
                form.addRow(f"{lab} price", sp)
                self.pts.append((i, sp))
        self.lock = QtWidgets.QCheckBox("Locked (cannot be moved)")
        self.lock.setChecked(bool(d.get("lock")))
        form.addRow("", self.lock)
        v.addLayout(form)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _pick(self, c):
        self.color = c
        self.color_btn.setIcon(swatch(c))
        self.color_btn.setText("  " + next((n for n, x in COLORS if x == c), c))

    def _custom(self):
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.color), self, "Colour")
        if c.isValid():
            self._pick(c.name())

    def _ok(self):
        props = {"color": self.color, "lock": True if self.lock.isChecked() else None}
        if self.width is not None:
            props["w"] = float(self.width.currentData())
            props["ls"] = self.style.currentData()
        if self.text is not None and self.text.text().strip():
            props["text"] = self.text.text().strip()
        pts = [list(q) for q in self.d["pts"]]
        for i, sp in self.pts:
            pts[i][1] = float(sp.value())
        if self.d["type"] in ("long", "short") and len(pts) == 3:
            pts[2][0] = pts[1][0]
        props["pts"] = pts
        self.chart.layer.set_props(self.d["id"], **props)
        self.accept()
