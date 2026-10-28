"""Dark, TradingView-like colours and Qt stylesheet."""
import pyqtgraph as pg

BG = "#131722"
PANEL = "#1e222d"
GRID = "#2a2e39"
BORDER = "#363a45"
TEXT = "#d1d4dc"
MUTED = "#868993"
UP = "#26a69a"
DOWN = "#ef5350"
ACCENT = "#2962ff"
LINE_COLORS = ["#f5a623", "#2196f3", "#e91e63", "#9c27b0", "#00bcd4", "#8bc34a", "#ffeb3b"]


def apply_pg_config():
    pg.setConfigOptions(background=BG, foreground=TEXT, antialias=False)


STYLESHEET = f"""
* {{ font-size: 13px; }}
QMainWindow, QWidget {{ background: {BG}; color: {TEXT}; }}
QToolBar {{ background: {PANEL}; border: 0; border-bottom: 1px solid {GRID}; spacing: 8px; padding: 6px; }}
QLabel#chartHeader {{ background: {BG}; padding: 8px 12px; font-family: Menlo, Consolas, monospace; font-size: 12px; }}
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 4px; padding: 4px 8px; selection-background-color: {ACCENT}; }}
QComboBox::drop-down {{ border: 0; width: 18px; }}
QComboBox QAbstractItemView {{ background: {PANEL}; border: 1px solid {BORDER}; selection-background-color: {ACCENT}; }}
QPushButton, QToolButton {{ background: {GRID}; border: 1px solid {BORDER}; border-radius: 4px; padding: 5px 12px; }}
QPushButton:hover, QToolButton:hover {{ background: {BORDER}; }}
QPushButton#primary {{ background: {ACCENT}; border-color: {ACCENT}; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: #4a7bff; }}
QPushButton:disabled {{ color: {MUTED}; }}
QPlainTextEdit {{ background: #0f121b; border: 1px solid {GRID}; selection-background-color: {ACCENT}; }}
QTableWidget {{ background: {BG}; gridline-color: {GRID}; border: 1px solid {GRID}; alternate-background-color: #171b27; selection-background-color: #2a3150; }}
QHeaderView::section {{ background: {PANEL}; color: {MUTED}; border: 0; border-bottom: 1px solid {GRID}; padding: 5px; }}
QDockWidget::title {{ background: {PANEL}; padding: 6px 10px; border-bottom: 1px solid {GRID}; }}
QTabWidget::pane {{ border: 1px solid {GRID}; top: -1px; }}
QTabBar::tab {{ background: {PANEL}; padding: 6px 14px; border: 1px solid {GRID}; border-bottom: 0; }}
QTabBar::tab:selected {{ background: {BG}; color: white; }}
QMenuBar, QMenu {{ background: {PANEL}; }}
QMenu::item:selected {{ background: {ACCENT}; }}
QStatusBar {{ background: {PANEL}; color: {MUTED}; }}

QToolButton::menu-indicator {{ image: none; width: 0px; }}
"""
