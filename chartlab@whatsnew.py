"""'What's new': once after an update, a card that says what this version brings (and where to find it). Also in
the main menu."""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from . import icons, theme

# version -> [(icon, title, one line: what it is and where to find it)]
NOTES = {
    "0.37.1": [
        ("zoom", "Smoother chart",
         "Dragging, zooming and the crosshair are about twice as quick with indicators on: each move is drawn once, "
         "lines are drawn the quick way while the chart moves and in full as soon as it rests."),
        ("cursor", "Trackpad and wheel, your way",
         "A sideways swipe only scrolls, an up / down swipe only zooms, and the zoom stops when the fingers lift. "
         "Settings → Navigation: what the wheel does, the zoom point, speed and direction, pinch, gliding."),
        ("lock", "Broker accounts, redone",
         "Pick the broker with one click, follow its three steps, and Save / Test connection stay in sight under the "
         "form. Each broker has its own colour in the list."),
        ("layout_icon", "Smaller things",
         "Dhan asks only for the Client ID and token (browser login is under More ways to log in); a practice "
         "account shows only what it uses."),
    ],
    "0.37.0": [
        ("market", "Market panel",
         "NIFTY 50 gainers, losers, a heatmap of all fifty and every sector (IT, Bank, Auto, Pharma, Metal ...), "
         "live while the market is open. It is in the bar on the right; click any row to open its chart."),
        ("chain", "Every index, with the right numbers",
         "NIFTY IT, Auto, Pharma, FMCG, Metal, Realty, Private Bank, Midcap, Smallcap, Bankex and 30 more now open "
         "with their real data (they used to fail), and every index's change matches TradingView."),
        ("search", "Company names and sectors from NSE",
         "Names as NSE lists them (ABB India Limited, not ABB Limited), the company's sector under its name, the "
         "NIFTY 50 as it is today, and an old symbol opens the new one (ZOMATO → ETERNAL)."),
        ("watchlist", "Sort your watchlist",
         "Click Last, Chg or Chg% at the top of the list: biggest first, click again for smallest, a third time "
         "for your own order."),
        ("datawin", "The right numbers after the close",
         "Yahoo leaves the day\u2019s last candle unfinished for hours after the market closes. ChartLab now "
         "completes it, so daily charts, the watchlist and the Performance tiles match TradingView all evening "
         "and all weekend."),
        ("refresh", "A live watchlist",
         "Every row updates every few seconds in market hours, and a price glows green or red as it moves."),
        ("candles_icon", "Today\u2019s change in the legend",
         "With the mouse off the chart, the legend shows the price and today\u2019s change, like TradingView."),
        ("calendar", "More history, by itself",
         "Drag the chart to the left and older bars load on their own. 1-minute charts now go back a month."),
        ("layout_icon", "Cleaner panels",
         "Option chain, Strategy Tester, Trading Panel and Alerts now say what goes in them and how to start, on "
         "one dark surface; after hours the bottom bar says Market closed instead of Live."),
        ("menu", "Shortcuts, search, menu",
         "Every shortcut on one sheet (main menu → Keyboard shortcuts, or {keys}); symbol search and quick "
         "search close with a click outside them."),
    ],
}


def _key(v: str) -> tuple:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return ()


def notes_for(version: str) -> list:
    """This version's notes, then the earlier ones of the same series (0.37.1 also tells what 0.37.0 brought: an
    update often skips a small release)."""
    k = _key(version)
    if str(version) not in NOTES or len(k) < 2:
        return list(NOTES.get(str(version), []))
    out = []
    for v in sorted(NOTES, key=_key, reverse=True):
        if _key(v)[:2] == k[:2] and _key(v) <= k:
            out += NOTES[v]
    return out


class WhatsNew(QtWidgets.QDialog):
    """The card: a 'New in 0.37.0' tag, the title, one row per change, 'Got it'."""

    def __init__(self, parent, version: str):
        super().__init__(parent)
        self.setWindowFlag(QtCore.Qt.WindowType.FramelessWindowHint)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setProperty("chromeRadius", 16)
        self.setProperty("chromeOverlay", True)                  # a click beside it closes it ...
        self.setProperty("chromeScrim", True)                    # ... on a dimmed window
        self.setStyleSheet(
            "QDialog{border:0;border-radius:16px;background:#131313;}"
            "QLabel{background:transparent;}"
            "QToolButton#wnClose{background:transparent;border:0;border-radius:8px;padding:4px;}"
            "QToolButton#wnClose:hover{background:#222222;}"
            "QPushButton#wnOk{background:#f2f2f2;color:#000;border:0;border-radius:8px;padding:0 22px;"
            "font-size:14px;font-weight:600;min-height:36px;}"
            "QPushButton#wnOk:hover{background:#ffffff;}"
            "QScrollArea{background:transparent;border:0;}"
            "QScrollArea > QWidget > QWidget{background:transparent;}")
        self.version = str(version)
        self.rows = notes_for(self.version)
        ph = parent.height() if parent is not None else 900
        self.resize(540, max(420, min(660, ph - 120)))
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(26, 18, 18, 18)
        lay.setSpacing(0)

        top = QtWidgets.QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        tag = QtWidgets.QLabel(f"NEW IN {self.version}")
        acc = QtGui.QColor(theme.ACCENT)
        tag.setStyleSheet(f"color:{theme.ACCENT}; font-size:11px; font-weight:700; letter-spacing:0.6px;"
                          f"background:rgba({acc.red()},{acc.green()},{acc.blue()},0.16); border-radius:9px;"
                          "padding:3px 9px;")
        tag.setSizePolicy(QtWidgets.QSizePolicy.Policy.Maximum, QtWidgets.QSizePolicy.Policy.Fixed)
        self.tag = tag
        top.addWidget(tag, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
        top.addStretch(1)
        self.close_btn = QtWidgets.QToolButton()
        self.close_btn.setObjectName("wnClose")
        self.close_btn.setFixedSize(33, 33)
        self.close_btn.setIconSize(QtCore.QSize(22, 22))
        self.close_btn.setIcon(icons.get("close", "#b8b8b8", 22))
        self.close_btn.setToolTip("Close  (Esc)")
        self.close_btn.clicked.connect(self.reject)
        top.addWidget(self.close_btn)
        lay.addLayout(top)
        lay.addSpacing(10)
        title = QtWidgets.QLabel("What’s new in ChartLab")
        title.setStyleSheet("color:#ffffff; font-size:22px; font-weight:600;")
        lay.addWidget(title)
        lay.addSpacing(4)
        sub = QtWidgets.QLabel("A few things that changed since your last version.")
        sub.setStyleSheet("color:#8c8c8c; font-size:13px;")
        lay.addWidget(sub)
        lay.addSpacing(16)

        body = QtWidgets.QWidget()
        bl = QtWidgets.QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 8, 0)
        bl.setSpacing(14)
        keys = theme.sc("Ctrl+/")
        self.items = []
        for ico, head, text in self.rows:
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(14)
            badge = QtWidgets.QLabel()
            badge.setFixedSize(38, 38)
            badge.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            badge.setStyleSheet("background:#1f1f1f; border:1px solid #2a2a2a; border-radius:10px;")
            try:
                badge.setPixmap(icons.get(ico, "#e0e0e0", 20).pixmap(20, 20))
            except Exception:  # noqa: BLE001 - an unknown icon: an empty tile
                pass
            row.addWidget(badge, 0, QtCore.Qt.AlignmentFlag.AlignTop)
            col = QtWidgets.QVBoxLayout()
            col.setSpacing(2)
            h = QtWidgets.QLabel(head)
            h.setStyleSheet("color:#ffffff; font-size:14px; font-weight:600;")
            t = QtWidgets.QLabel(text.replace("{keys}", keys))
            t.setWordWrap(True)
            t.setStyleSheet("color:#9a9a9a; font-size:13px;")
            col.addWidget(h)
            col.addWidget(t)
            row.addLayout(col, 1)
            bl.addLayout(row)
            self.items.append((h, t))
        bl.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)
        lay.addSpacing(14)

        foot = QtWidgets.QHBoxLayout()
        foot.addStretch(1)
        self.ok = QtWidgets.QPushButton("Got it")
        self.ok.setObjectName("wnOk")
        self.ok.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.ok.setDefault(True)
        self.ok.clicked.connect(self.accept)
        foot.addWidget(self.ok)
        lay.addLayout(foot)
