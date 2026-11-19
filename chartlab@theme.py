"""Dark, TradingView-like colours and Qt stylesheet."""
import os
import tempfile

import pyqtgraph as pg

BG = "#000000"
PANEL = "#000000"
GRID = "#1a1a1a"
GUTTER = "#222222"                 # TradingView: 4 px gaps of #222 between the panels
BORDER = "#2b2b2b"
HOVER = "#262626"
TEXT = "#dbdbdb"
MUTED = "#787b86"
UP = "#089981"
DOWN = "#f23645"
ACCENT = "#2962ff"
CHECK_PNG = os.path.join(tempfile.gettempdir(), "chartlab_check_v1.png").replace("\\", "/")
CHEVRON_PNG = os.path.join(tempfile.gettempdir(), "chartlab_chevron_v1.png").replace("\\", "/")
CHEVRON_R_PNG = os.path.join(tempfile.gettempdir(), "chartlab_chevron_r_v1.png").replace("\\", "/")
LINE_COLORS = ["#f5a623", "#2196f3", "#e91e63", "#9c27b0", "#00bcd4", "#8bc34a", "#ffeb3b"]


def fmt_vol(v: float) -> str:
    """Volume the way TradingView shows it: 342.55K, 3.42M, 30.03B. (Its text has a narrow space before the unit that
    is about 2 px wide on screen; in this app's font that character came out as wide as a full space, so the unit
    simply follows the number.)"""
    v = float(v)
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:.2f}M"
    if a >= 1e3:
        return f"{v / 1e3:.2f}K"
    return f"{v:,.0f}"


def sc(keys: str) -> str:
    """A shortcut the way the platform writes it: on macOS 'Alt+A' -> '⌥A', 'Ctrl+Shift+Z' -> '⇧⌘Z',
    'Del' -> '⌫' (TradingView for Mac shows these symbols); unchanged elsewhere."""
    import sys
    if not keys or sys.platform != "darwin":
        return keys
    if keys in ("Del", "Delete"):
        return "\u232b"
    from PySide6 import QtGui
    out = QtGui.QKeySequence(keys, QtGui.QKeySequence.SequenceFormat.PortableText).toString(
        QtGui.QKeySequence.SequenceFormat.NativeText)
    return out or keys


def menu_section(menu, title: str):
    """TradingView-style small grey heading inside a menu (Qt's addSection is invisible with style sheets)."""
    from PySide6 import QtWidgets
    lab = QtWidgets.QLabel(title.upper())
    lab.setStyleSheet("color:#787b86; font-size:11px; font-weight:600; padding:8px 16px 4px 16px; background:transparent;")
    act = QtWidgets.QWidgetAction(menu)
    act.setDefaultWidget(lab)
    act.setEnabled(False)
    menu.addAction(act)
    return act


def check_icon_path() -> str:
    """A small black tick on transparent, used for checked checkboxes (Qt style sheets need a file)."""
    import os
    import tempfile
    from PySide6 import QtCore, QtGui
    path = CHECK_PNG
    if not os.path.exists(path):
        img = QtGui.QImage(32, 32, QtGui.QImage.Format.Format_ARGB32)
        img.fill(QtCore.Qt.GlobalColor.transparent)
        p = QtGui.QPainter(img)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        pen = QtGui.QPen(QtGui.QColor("#000000"), 4)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawPolyline([QtCore.QPointF(8, 16.5), QtCore.QPointF(13.5, 22), QtCore.QPointF(24, 10)])
        p.end()
        img.save(path)
    if not os.path.exists(CHEVRON_PNG):                 # the little "v" on every drop-down (TradingView style)
        img = QtGui.QImage(24, 24, QtGui.QImage.Format.Format_ARGB32)
        img.fill(QtCore.Qt.GlobalColor.transparent)
        p = QtGui.QPainter(img)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        pen = QtGui.QPen(QtGui.QColor("#b2b5be"), 2.6)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawPolyline([QtCore.QPointF(5, 9), QtCore.QPointF(12, 16), QtCore.QPointF(19, 9)])
        p.end()
        img.save(CHEVRON_PNG)
    if not os.path.exists(CHEVRON_R_PNG):               # the small ">" of a tool group in the left toolbar
        img = QtGui.QImage(22, 68, QtGui.QImage.Format.Format_ARGB32)
        img.fill(QtCore.Qt.GlobalColor.transparent)
        p = QtGui.QPainter(img)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        pen = QtGui.QPen(QtGui.QColor("#8c8c8c"), 2.0)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawPolyline([QtCore.QPointF(8, 28), QtCore.QPointF(14, 34), QtCore.QPointF(8, 40)])
        p.end()
        img.save(CHEVRON_R_PNG)
    return path


def apply_pg_config():
    pg.setConfigOptions(background=BG, foreground=TEXT, antialias=False)


# TradingView's menus, measured on tradingview.com: 32 px rows; a 28 px icon box 8 px in from the edge; the label
# 40 px in, 14 px #b8b8b8 (#dbdbdb under the mouse, row #222222); the shortcut 12 px #636363 ending 20 px from the
# right edge; 13 px separators with a 1 px #303030 line (the separators stay in the style sheet).
MENU_ROW, MENU_LEFT, MENU_RIGHT, MENU_GAP, MENU_ICON_X, MENU_ICON = 32, 40, 20, 24, 8, 28
MENU_TEXT, MENU_HOT, MENU_OFF, MENU_SC, MENU_SEL = "#b8b8b8", "#dbdbdb", "#5d5d5d", "#636363", "#222222"


def make_style():
    """The app's style: Fusion, with TradingView's menu rows (Qt style sheets cannot give a shortcut its own colour
    and size, or put the icon in TradingView's box)."""
    from PySide6 import QtCore, QtGui, QtWidgets

    S = QtWidgets.QStyle

    class TVStyle(QtWidgets.QProxyStyle):
        def _ours(self, opt, w) -> bool:
            return (isinstance(w, QtWidgets.QMenu) and isinstance(opt, QtWidgets.QStyleOptionMenuItem)
                    and opt.menuItemType in (QtWidgets.QStyleOptionMenuItem.MenuItemType.Normal,
                                             QtWidgets.QStyleOptionMenuItem.MenuItemType.SubMenu,
                                             QtWidgets.QStyleOptionMenuItem.MenuItemType.DefaultItem))

        def sizeFromContents(self, ct, opt, size, w=None):
            if ct == S.ContentsType.CT_MenuItem and self._ours(opt, w):
                # size = the label's width in the menu font; QMenu adds the widest shortcut itself
                has_sc = "\t" in (opt.text or "")
                sub = opt.menuItemType == QtWidgets.QStyleOptionMenuItem.MenuItemType.SubMenu
                return QtCore.QSize(MENU_LEFT + size.width() + (MENU_GAP if has_sc else 0) + MENU_RIGHT
                                    + (14 if sub else 0), MENU_ROW)
            return super().sizeFromContents(ct, opt, size, w)

        def polish(self, w):
            if isinstance(w, QtWidgets.QMenu):
                w.installEventFilter(painter)
                return
            super().polish(w)

    class MenuPaint(QtCore.QObject):
        """Paints a whole menu the TradingView way. (With a style sheet on the app Qt draws menu rows itself and
        never asks the style, so the menu's paint event is taken over; sizes still come from TVStyle.)"""

        def eventFilter(self, m, ev):
            if ev.type() != QtCore.QEvent.Type.Paint or not isinstance(m, QtWidgets.QMenu):
                return False
            p = QtGui.QPainter(m)
            p.fillRect(m.rect(), QtGui.QColor("#161616"))
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            p.setPen(QtGui.QPen(QtGui.QColor("#262626"), 1))
            p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QtCore.QRectF(m.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
            act = m.activeAction()
            for a in m.actions():
                r = m.actionGeometry(a)
                if not a.isVisible() or r.isEmpty() or isinstance(a, QtWidgets.QWidgetAction):
                    continue
                if a.isSeparator():
                    p.fillRect(QtCore.QRect(r.left(), r.center().y(), r.width(), 1), QtGui.QColor("#303030"))
                    continue
                self._row(m, a, r, p, a is act)
            p.end()
            return True

        def _row(self, m, a, r, p, active):
            on = a.isEnabled()
            hot = on and active
            p.save()
            if hot:
                p.fillRect(r, QtGui.QColor(MENU_SEL))
            col = QtGui.QColor(MENU_HOT if hot else (MENU_TEXT if on else MENU_OFF))
            box = QtCore.QRect(r.left() + MENU_ICON_X - 1, r.top() + (r.height() - MENU_ICON) // 2, MENU_ICON, MENU_ICON)
            if a.isCheckable() and a.isChecked():
                p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
                pen = QtGui.QPen(QtGui.QColor(MENU_HOT if on else MENU_OFF), 1.6)
                pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
                p.setPen(pen)
                c = QtCore.QPointF(box.center()) + QtCore.QPointF(0.5, 0.5)
                p.drawPolyline([c + QtCore.QPointF(-5, 0), c + QtCore.QPointF(-1.5, 3.5), c + QtCore.QPointF(5, -3.5)])
            elif not a.icon().isNull():
                mode = (QtGui.QIcon.Mode.Active if hot else QtGui.QIcon.Mode.Normal) if on else QtGui.QIcon.Mode.Disabled
                a.icon().paint(p, box, QtCore.Qt.AlignmentFlag.AlignCenter, mode, QtGui.QIcon.State.Off)
            label, _tab, short = (a.text() or "").partition("\t")
            if not short and not a.shortcut().isEmpty():
                short = a.shortcut().toString(QtGui.QKeySequence.SequenceFormat.NativeText)
            flags = (QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.TextFlag.TextHideMnemonic
                     | QtCore.Qt.TextFlag.TextSingleLine)
            f = QtGui.QFont(a.font() if a.font() != QtWidgets.QApplication.font() else m.font())
            p.setFont(f)
            p.setPen(col)
            p.drawText(QtCore.QRect(r.left() + MENU_LEFT - 1, r.top(), r.width() - MENU_LEFT, r.height()),
                       int(flags | QtCore.Qt.AlignmentFlag.AlignLeft), label)
            if short:
                fs = QtGui.QFont(f)
                fs.setPixelSize(12)
                p.setFont(fs)
                p.setPen(QtGui.QColor(MENU_SC if on else MENU_OFF))
                p.drawText(QtCore.QRect(r.left(), r.top(), r.width() - MENU_RIGHT + 1, r.height()),
                           int(flags | QtCore.Qt.AlignmentFlag.AlignRight), short)
            if a.menu() is not None:
                p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
                pen = QtGui.QPen(col, 1.4)
                pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
                p.setPen(pen)
                x, y = r.right() - MENU_RIGHT + 0.5, r.center().y() + 0.5
                p.drawPolyline([QtCore.QPointF(x - 3, y - 4.5), QtCore.QPointF(x + 1.5, y), QtCore.QPointF(x - 3, y + 4.5)])
            p.restore()

    painter = MenuPaint(QtWidgets.QApplication.instance())
    return TVStyle("Fusion")


STYLESHEET = f"""
* {{ font-size: 13px; }}
QMainWindow, QWidget {{ background: {BG}; color: {TEXT}; }}
QMainWindow {{ background: {GUTTER}; }}
QToolBar {{ background: {BG}; border: 0; spacing: 2px; padding: 3px 6px; }}
QToolBar#topbar {{ border-bottom: 4px solid {GUTTER}; padding: 0px; }}
QToolBar#leftrail {{ border-right: 4px solid {GUTTER}; padding: 4px 0px; spacing: 0px; }}
QToolBar#rightrail {{ border-left: 4px solid {GUTTER}; padding: 4px 0px; spacing: 0px; }}
QToolBar::separator {{ background: {BORDER}; width: 1px; height: 1px; margin: 6px 6px; }}
QToolButton {{ background: transparent; border: 0; border-radius: 6px; padding: 5px 8px; color: {TEXT}; font-weight: 500; }}
QToolButton:hover {{ background: {HOVER}; }}
QToolButton:checked {{ background: {HOVER}; color: white; }}
QToolButton:pressed {{ background: #3a3e4a; }}
QToolBar#topbar QToolButton {{ font-size: 14px; font-weight: 400; color: #b8b8b8; padding: 0px 5px; border-radius: 4px; }}
QToolBar#topbar QToolButton[tvkind="both"] {{ padding: 0px 10px 0px 5px; }}
QToolBar#topbar QToolButton[tvkind="text"] {{ padding: 0px; }}
QToolBar#topbar QToolButton[tvkind="menu"] {{ padding: 0px 12px; }}
QToolBar#topbar QToolButton[tvkind="chev"] {{ padding: 0px; }}
QToolBar#topbar QToolButton::menu-indicator {{ image: none; width: 0px; }}
QToolBar#topbar {{ spacing: 0px; }}
QToolBar#topbar QToolButton:hover {{ background: #2e2e2e; color: #dbdbdb; }}
QToolBar#topbar QToolButton:checked {{ background: #2e2e2e; color: #fff; }}
QToolBar#topbar QComboBox {{ font-size: 14px; color: #b8b8b8; }}
QFrame#stripbar QToolButton {{ font-size: 13px; font-weight: 400; color: #d1d4dc; padding: 5px 8px; }}
QToolButton::menu-indicator {{ image: none; width: 0px; }}
QToolBar#topbar QToolButton#ivbtn {{ padding: 8px 3px; border-radius: 4px; color: #b8b8b8; font-size: 14px; }}
QToolBar#topbar QToolButton#ivbtn:hover {{ background: #2e2e2e; color: #dbdbdb; }}
QToolBar#topbar QToolButton#ivbtn:checked {{ background: #2e2e2e; color: #ffffff; font-weight: 600; }}
QPushButton::menu-indicator {{ image: none; width: 0px; }}
QToolBar#leftrail QToolButton {{ padding: 3px 10px; margin: 1px 2px; border-radius: 4px; }}
QToolBar#rightrail QToolButton {{ padding: 3px 6px; margin: 1px 2px; border-radius: 4px; }}
QToolBar#leftrail QToolButton:hover, QToolBar#rightrail QToolButton:hover {{ background: #2e2e2e; }}
QToolBar#leftrail QToolButton:checked, QToolBar#rightrail QToolButton:checked {{ background: #2e2e2e; }}
QToolBar#leftrail QToolButton[popupMode="1"] {{ padding-right: 10px; }}
QToolBar#leftrail QToolButton::menu-button {{ border: 0; width: 11px; background: transparent; }}
QToolBar#leftrail QToolButton::menu-arrow {{ image: none; width: 11px; height: 34px; }}
QToolBar#leftrail QToolButton::menu-arrow:hover {{ image: url({CHEVRON_R_PNG}); }}
QLabel#chartHeader {{ background: transparent; padding: 0; font-size: 12px; }}
QFrame#stripbar {{ border-top: 4px solid {GUTTER}; }}
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px 8px; selection-background-color: {ACCENT}; }}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QFrame#symbolpill {{ background: #303030; border-radius: 14px; min-height: 28px; max-height: 28px; }}
QFrame#symbolpill QPushButton#symbtn {{ background: transparent; border: 0; color: #b8b8b8; font-size: 14px;
    font-weight: 600; text-align: left; padding: 0px 0px 0px 12px; margin: 0px; min-height: 28px; max-height: 28px;
    border-top-left-radius: 14px; border-bottom-left-radius: 14px; border-top-right-radius: 0px;
    border-bottom-right-radius: 0px; }}
QFrame#symbolpill QPushButton#symbtn:hover {{ background: #3a3a3a; color: #dbdbdb; }}
QFrame#symbolpill QToolButton#symact {{ background: transparent; border: 0; padding: 0px; margin: 0px;
    border-radius: 14px; }}
QFrame#symbolpill QToolButton#symact:hover {{ background: #3a3a3a; }}
QFrame#symbolpill QToolButton#symact::menu-indicator {{ image: none; width: 0px; }}
QFrame#symbolpill QLabel {{ background: transparent; }}
QLineEdit#symbolbox {{ background: transparent; border: 0; border-radius: 0; padding: 5px 14px; font-weight: 700; font-size: 14px; color: white; }}
QComboBox {{ background: transparent; border: 0; padding: 4px 24px 4px 8px; }}
QComboBox:hover {{ background: {HOVER}; }}
QComboBox::drop-down {{ border: 0; width: 22px; subcontrol-origin: padding; subcontrol-position: center right; }}
QComboBox::down-arrow {{ image: url({CHEVRON_PNG}); width: 11px; height: 11px; }}
QComboBox::down-arrow:on {{ top: 1px; }}
QComboBox QAbstractItemView {{ background: #1a1a1a; border: 1px solid {BORDER}; selection-background-color: {HOVER}; outline: 0; }}
QPushButton {{ background: {HOVER}; border: 0; border-radius: 6px; padding: 6px 14px; }}
QPushButton:hover {{ background: #3a3e4a; }}
QPushButton#primary {{ background: {ACCENT}; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: #4a7bff; }}
QPushButton#pill {{ background: white; color: black; font-weight: 600; font-size: 14px; border-radius: 14px;
    padding: 0px 16px; min-height: 28px; max-height: 28px; }}
QPushButton#pill:hover {{ background: #e0e0e0; }}
QToolBar#topbar QToolButton#tradepill {{ background: transparent; color: #dbdbdb; font-size: 14px;
    border: 1px solid #4a4a4a; border-radius: 14px; padding: 0px 12px; }}
QToolBar#topbar QToolButton#tradepill:hover {{ background: #2e2e2e; }}
QToolBar#topbar QToolButton#tradepill::menu-indicator {{ image: none; width: 0px; }}
QToolBar#topbar QToolButton#tradepill[folded="true"] {{ border: 0; padding: 0px 14px; }}
QPushButton#updpill {{ background: #2962ff; color: white; font-weight: 600; border-radius: 16px; padding: 6px 14px; }}
QPushButton#updpill:hover {{ background: #1e53e5; }}
QPushButton#updpill:disabled {{ background: #1c2f6b; color: #c9d4ff; }}
QPushButton:disabled {{ color: {MUTED}; }}
QPlainTextEdit {{ background: #0a0a0a; border: 1px solid {GRID}; selection-background-color: {ACCENT}; }}
QTableWidget {{ background: {BG}; gridline-color: {GRID}; border: 0; alternate-background-color: #0d0d0d; selection-background-color: #1e2a55; }}
QHeaderView::section {{ background: {BG}; color: {MUTED}; border: 0; border-bottom: 1px solid {BORDER}; padding: 6px; font-weight: 500; }}
QDockWidget::title {{ background: {BG}; padding: 8px 12px; border-bottom: 1px solid {BORDER}; font-weight: 600; }}
QTabWidget::pane {{ border: 0; border-top: 1px solid {BORDER}; }}
QTabBar::tab {{ background: transparent; padding: 7px 14px; border: 0; border-bottom: 2px solid transparent; color: {MUTED}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{ color: white; border-bottom: 2px solid white; }}
QMenuBar {{ background: {BG}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QSplitter::handle {{ background: {BORDER}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #363a45; border-radius: 4px; min-width: 24px; margin: 1px; }}
QScrollBar::handle:horizontal:hover, QScrollBar::handle:vertical:hover {{ background: #4c525e; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ margin: 1px; background: #363a45; border-radius: 4px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 0; border: 0; }}
QDockWidget {{ border: 0; }}
QMainWindow::separator {{ background: {GUTTER}; width: 4px; height: 4px; }}
QMainWindow::separator:hover {{ background: {ACCENT}; }}
QStatusBar::item {{ border: 0; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px; }}
QCheckBox::indicator {{ border: 1px solid #50535e; border-radius: 3px; background: {BG}; }}
QCheckBox::indicator:checked {{ background: #ffffff; border-color: #ffffff; image: url({CHECK_PNG}); }}
QRadioButton::indicator {{ border: 1px solid #6a6d78; border-radius: 7px; background: #121212; }}
QRadioButton::indicator:checked {{ border: 4px solid #ffffff; background: #121212; width: 8px; height: 8px; }}
QListWidget::indicator, QTableWidget::indicator {{ width: 15px; height: 15px; border: 1px solid #6a6d78; border-radius: 4px; background: #121212; }}
QListWidget::indicator:checked, QTableWidget::indicator:checked {{ background: #ffffff; border-color: #ffffff; image: url({CHECK_PNG}); }}
QDialog {{ background: #1e1e1e; }}
QDialog QWidget {{ background: transparent; }}
QDialog QLineEdit, QDialog QComboBox, QDialog QDoubleSpinBox, QDialog QSpinBox, QDialog QDateTimeEdit,
QDialog QPlainTextEdit, QDialog QTextEdit {{ background: #121212; border: 1px solid #3a3a3a; border-radius: 6px;
    padding: 5px 8px; color: #dbdbdb; }}
QDialog QComboBox {{ padding-right: 26px; }}
QDialog QComboBox:hover, QDialog QDateTimeEdit:hover, QDialog QLineEdit:hover, QDialog QAbstractSpinBox:hover {{ border-color: #50535e; }}
QDialog QComboBox:on {{ border-color: #6a6d78; }}
QDialog QLineEdit:focus, QDialog QPlainTextEdit:focus {{ border-color: #6a6d78; }}
QDialog QAbstractSpinBox QLineEdit, QDialog QComboBox QLineEdit, QDialog QDateTimeEdit QLineEdit {{ padding: 0; border: 0; background: transparent; }}
QDialog QListWidget, QDialog QTableWidget {{ background: #121212; border: 1px solid #2e2e2e; border-radius: 6px; }}
QDialog QComboBox QAbstractItemView {{ background: #1e1e1e; border: 1px solid #2e2e2e; selection-background-color: #2e2e2e; }}
QDialog QPushButton {{ background: transparent; border: 1px solid #5a5a5a; border-radius: 8px; padding: 7px 16px; color: #dbdbdb; }}
QDialog QPushButton:hover {{ background: #2e2e2e; }}
QDialog QPushButton#primary, QDialog QPushButton:default {{ background: #ffffff; color: #000000; border-color: #ffffff; font-weight: 600; }}
QDialog QPushButton#primary:hover, QDialog QPushButton:default:hover {{ background: #e3e3e3; }}
QDialog QCheckBox, QDialog QRadioButton, QDialog QLabel {{ color: #dbdbdb; }}
QListWidget {{ outline: 0; }}
QPushButton#primary:pressed {{ background: #1e53e5; }}
QFrame#vsep {{ background: #2e2e2e; }}
QLabel#panelTitle {{ background: {BG}; color: white; font-size: 15px; font-weight: 600; padding: 10px 14px 8px 14px; }}
QToolButton#srcbtn {{ color: #b8b8b8; font-size: 14px; padding: 5px 8px; }}
QMenu {{ background: #161616; border: 1px solid #262626; border-radius: 6px; padding: 6px 0; font-size: 14px; }}
QMenu::separator {{ height: 1px; background: #303030; margin: 6px 0; }}
QStatusBar {{ background: {BG}; color: {MUTED}; border-top: 4px solid {GUTTER}; min-height: 20px; font-size: 12px; }}
QToolTip {{ background: #1a1a1a; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px 6px; }}
"""
