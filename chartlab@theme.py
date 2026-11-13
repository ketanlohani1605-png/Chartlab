"""Dark, TradingView-like colours and Qt stylesheet."""
import os
import tempfile

import pyqtgraph as pg

BG = "#000000"
PANEL = "#000000"
GRID = "#1a1a1a"
GUTTER = "#1a1a1a"
BORDER = "#2b2b2b"
HOVER = "#262626"
TEXT = "#d1d4dc"
MUTED = "#787b86"
UP = "#089981"
DOWN = "#f23645"
ACCENT = "#2962ff"
CHECK_PNG = os.path.join(tempfile.gettempdir(), "chartlab_check_v1.png").replace("\\", "/")
CHEVRON_PNG = os.path.join(tempfile.gettempdir(), "chartlab_chevron_v1.png").replace("\\", "/")
LINE_COLORS = ["#f5a623", "#2196f3", "#e91e63", "#9c27b0", "#00bcd4", "#8bc34a", "#ffeb3b"]


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
    return path


def apply_pg_config():
    pg.setConfigOptions(background=BG, foreground=TEXT, antialias=False)


STYLESHEET = f"""
* {{ font-size: 13px; }}
QMainWindow, QWidget {{ background: {BG}; color: {TEXT}; }}
QMainWindow {{ background: {GUTTER}; }}
QToolBar {{ background: {BG}; border: 0; spacing: 2px; padding: 3px 6px; }}
QToolBar#topbar {{ border-bottom: 4px solid {GUTTER}; }}
QToolBar#leftrail {{ border-right: 4px solid {GUTTER}; padding: 6px 4px; spacing: 4px; }}
QToolBar#rightrail {{ border-left: 4px solid {GUTTER}; padding: 6px 4px; spacing: 4px; }}
QToolBar::separator {{ background: {BORDER}; width: 1px; height: 1px; margin: 6px 6px; }}
QToolButton {{ background: transparent; border: 0; border-radius: 6px; padding: 5px 8px; color: {TEXT}; font-weight: 500; }}
QToolButton:hover {{ background: {HOVER}; }}
QToolButton:checked {{ background: {HOVER}; color: white; }}
QToolButton:pressed {{ background: #3a3e4a; }}
QToolBar#topbar QToolButton {{ font-size: 14px; font-weight: 400; color: #d1d4dc; padding: 6px 10px; }}
QToolBar#topbar QToolButton:checked {{ background: #262626; color: #fff; }}
QToolBar#topbar QComboBox {{ font-size: 14px; color: #d1d4dc; }}
QFrame#stripbar QToolButton {{ font-size: 13px; font-weight: 400; color: #d1d4dc; padding: 5px 8px; }}
QToolButton::menu-indicator {{ image: none; width: 0px; }}
QToolButton#ivbtn {{ padding: 5px 7px; border-radius: 6px; color: #dbdbdb; font-size: 14px; }}
QToolButton#ivbtn:hover {{ background: #2a2a2a; }}
QToolButton#ivbtn:checked {{ background: #2e2e2e; color: #ffffff; font-weight: 600; }}
QPushButton::menu-indicator {{ image: none; width: 0px; }}
QToolBar#leftrail QToolButton, QToolBar#rightrail QToolButton {{ padding: 7px; border-radius: 6px; }}
QToolBar#leftrail QToolButton:checked {{ background: {HOVER}; }}
QLabel#chartHeader {{ background: transparent; padding: 0; font-size: 12px; }}
QFrame#stripbar {{ border-top: 4px solid {GUTTER}; }}
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px 8px; selection-background-color: {ACCENT}; }}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QFrame#symbolpill {{ background: #1a1a1a; border-radius: 17px; min-height: 34px; max-height: 34px; min-width: 120px; }}
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
QPushButton#pill {{ background: white; color: black; font-weight: 700; border-radius: 16px; padding: 6px 18px; }}
QPushButton#pill:hover {{ background: #e0e3eb; }}
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
QToolButton#symbtn {{ background: transparent; color: white; font-size: 14px; font-weight: 600; padding: 4px 14px 4px 10px; border-radius: 17px; }}
QToolButton#symbtn:hover {{ background: #2a2a2a; }}
QToolButton#srcbtn {{ color: #b2b5be; font-size: 13px; padding: 5px 10px; }}
QMenu {{ background: #1e1e1e; border: 1px solid #2e2e2e; border-radius: 8px; padding: 6px 0; }}
QMenu::item {{ padding: 7px 28px 7px 16px; border-radius: 0; color: #dbdbdb; }}
QMenu::item:selected {{ background: #2e2e2e; color: white; }}
QMenu::item:disabled {{ color: #6a6d78; }}
QMenu::item:checked {{ color: #ffffff; background: #262626; }}
QMenu::item:checked:selected {{ background: #2e2e2e; }}
QMenu::separator {{ height: 1px; background: #2e2e2e; margin: 5px 0; }}
QMenu::icon {{ padding-left: 10px; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}
QStatusBar {{ background: {BG}; color: {MUTED}; border-top: 4px solid {GUTTER}; min-height: 20px; font-size: 12px; }}
QToolBar#rightrail QToolButton:checked {{ background: #2a2a2a; }}
QToolTip {{ background: #1a1a1a; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px 6px; }}
"""
