"""Dark, TradingView-like colours and Qt stylesheet."""
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
LINE_COLORS = ["#f5a623", "#2196f3", "#e91e63", "#9c27b0", "#00bcd4", "#8bc34a", "#ffeb3b"]


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
QToolButton::menu-indicator {{ image: none; width: 0px; }}
QToolBar#leftrail QToolButton, QToolBar#rightrail QToolButton {{ padding: 7px; border-radius: 6px; }}
QToolBar#leftrail QToolButton:checked {{ background: {HOVER}; }}
QLabel#chartHeader {{ background: transparent; padding: 0; font-size: 12px; }}
QFrame#stripbar {{ border-top: 4px solid {GUTTER}; }}
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px 8px; selection-background-color: {ACCENT}; }}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QLineEdit#symbolbox {{ background: #1a1a1a; border: 0; border-radius: 16px; padding: 5px 14px; font-weight: 700; font-size: 14px; color: white; }}
QComboBox {{ background: transparent; border: 0; padding: 4px 6px; }}
QComboBox:hover {{ background: {HOVER}; }}
QComboBox::drop-down {{ border: 0; width: 14px; }}
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
QMenu {{ background: #1a1a1a; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {HOVER}; }}
QStatusBar {{ background: {BG}; color: {MUTED}; border-top: 1px solid {BORDER}; }}
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
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QDialog {{ background: #1a1a1a; }}
QDialog QLineEdit, QDialog QComboBox, QDialog QDoubleSpinBox, QDialog QDateTimeEdit {{ background: {BG}; border: 1px solid {BORDER}; }}
QListWidget {{ outline: 0; }}
QPushButton#primary:pressed {{ background: #1e53e5; }}
QToolTip {{ background: #1a1a1a; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px 6px; }}
"""
