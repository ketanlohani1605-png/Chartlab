"""Market: how the NIFTY 50 is moving - top gainers, top losers and a heatmap of all fifty, live while the market is
open (one request to Yahoo for every price, every few seconds). Click a stock to open its chart."""
from __future__ import annotations

import time

from PySide6 import QtCore, QtGui, QtWidgets

from . import theme

def _nifty50() -> list:
    """The NIFTY 50's members from NSE's own list (assets/nse_symbols.json: BSE came in and Wipro went out at the
    last rebalance); the built-in copy below only if that file is missing."""
    try:
        from .symbols import nifty50
        got = nifty50()
        if len(got) >= 45:
            return got
    except Exception:  # noqa: BLE001
        pass
    return ["ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV",
            "BEL", "BHARTIARTL", "BSE", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH",
            "HDFCBANK", "HDFCLIFE", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDIGO", "INFY", "ITC", "JIOFIN",
            "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC", "POWERGRID",
            "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TMPV", "TATASTEEL", "TCS",
            "TECHM", "TITAN", "TRENT", "ULTRACEMCO"]


NIFTY50 = _nifty50()
MODES = ("Gainers", "Losers", "Heatmap", "Sectors")
ROLE_SYM = QtCore.Qt.ItemDataRole.UserRole
ROLE_ROW = QtCore.Qt.ItemDataRole.UserRole + 1
ROLE_HEAD = QtCore.Qt.ItemDataRole.UserRole + 2


def _sector_syms() -> tuple[list, list]:
    """(NSE's sectoral indices, the themes) from the index table."""
    try:
        from . import indices as _ix
        sec = [i.sym for i in _ix.INDICES if i.kind == "sector" and i.exchange == "NSE"]
        th = [i.sym for i in _ix.INDICES if i.kind == "theme"]
        return sec, th
    except Exception:  # noqa: BLE001
        return [], []


SECTORS, THEMES = _sector_syms()


def short_name(sym: str) -> str:
    """'Nifty Financial Services Index' -> 'Financial Services', 'Nifty India Defence Index' -> 'Defence'."""
    try:
        from . import indices as _ix
        n = _ix.name_of(sym) or sym
    except Exception:  # noqa: BLE001
        n = sym
    n = n.replace(" Index", "")
    for pre in ("Nifty India ", "Nifty ", "BSE "):
        if n.startswith(pre):
            return n[len(pre):]
    return n


def heat_color(pct: float) -> QtGui.QColor:
    """A tile's colour: grey at 0 %, deeper green / red up to +-3 %."""
    base = QtGui.QColor("#262626")
    to = QtGui.QColor(theme.UP if pct >= 0 else theme.DOWN)
    k = min(1.0, abs(pct) / 3.0) ** 0.75
    if abs(pct) < 0.05:
        k = 0.0
    dark = 0.62                                          # the deepest tile is a deep, not a neon, colour
    return QtGui.QColor(int(base.red() + (to.red() * dark - base.red()) * k),
                        int(base.green() + (to.green() * dark - base.green()) * k),
                        int(base.blue() + (to.blue() * dark - base.blue()) * k))


def _name(sym: str) -> str:
    try:
        from .symbols import name_of
        return name_of(sym) or ""
    except Exception:  # noqa: BLE001
        return ""


class _RowDelegate(QtWidgets.QStyledItemDelegate):
    """Logo · symbol over company name | last price | change % in a coloured pill."""

    def sizeHint(self, option, index):
        return QtCore.QSize(option.rect.width(), 46)

    def paint(self, p, option, index):
        from .watchlist import draw_logo, price_decimals_of
        p.save()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = option.rect
        al = QtCore.Qt.AlignmentFlag
        if option.state & QtWidgets.QStyle.StateFlag.State_MouseOver:
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor("#1b1b1b"))
            p.drawRoundedRect(QtCore.QRectF(r).adjusted(4, 1, -4, -1), 6, 6)
        sym = index.data(ROLE_SYM)
        row = index.data(ROLE_ROW)                        # (price, change %) or None
        f = QtGui.QFont(option.font)
        if not sym:                                       # 'Loading prices...'
            f.setPixelSize(13)
            p.setFont(f)
            p.setPen(QtGui.QColor(theme.MUTED))
            p.drawText(r.adjusted(16, 0, 0, 0), al.AlignLeft | al.AlignVCenter, str(index.data() or "").strip())
            p.restore()
            return
        draw_logo(p, QtCore.QRectF(r.x() + 12, r.center().y() - 12, 24, 24), sym, f)
        f.setPixelSize(14)
        p.setFont(f)
        p.setPen(QtGui.QColor("#dbdbdb"))
        pill_w = 70
        right = r.right() - 12
        txt_right = right - pill_w - 10
        p.drawText(QtCore.QRect(r.x() + 46, r.y() + 5, 140, 20), al.AlignLeft | al.AlignVCenter, sym)
        f2 = QtGui.QFont(f)
        f2.setPixelSize(11)
        p.setFont(f2)
        p.setPen(QtGui.QColor("#8c8c8c"))
        nm = _name(sym)
        p.drawText(QtCore.QRect(r.x() + 46, r.y() + 24, max(40, txt_right - 90 - (r.x() + 46)), 16),
                   al.AlignLeft | al.AlignVCenter,
                   p.fontMetrics().elidedText(nm, QtCore.Qt.TextElideMode.ElideRight,
                                              max(40, txt_right - 90 - (r.x() + 46))))
        if row:
            price, pct = row
            col = QtGui.QColor(theme.UP if pct >= 0 else theme.DOWN)
            f3 = QtGui.QFont(f)
            f3.setPixelSize(13)
            p.setFont(f3)
            p.setPen(QtGui.QColor("#dbdbdb"))
            p.drawText(QtCore.QRect(txt_right - 90, r.y(), 90, r.height()), al.AlignRight | al.AlignVCenter,
                       f"{price:,.{price_decimals_of(sym, price)}f}")
            pill = QtCore.QRectF(right - pill_w, r.center().y() - 12, pill_w, 24)
            bg = QtGui.QColor(col)
            bg.setAlphaF(0.16)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(pill, 6, 6)
            p.setPen(col)
            f4 = QtGui.QFont(f3)
            f4.setWeight(QtGui.QFont.Weight.DemiBold)
            p.setFont(f4)
            p.drawText(pill, al.AlignCenter, f"{pct:+.2f}%".replace("-", "−"))
        p.restore()


class _SectorDelegate(QtWidgets.QStyledItemDelegate):
    """A heading ('SECTORS' / 'THEMES'), or: sector name over its index | a bar for the day's move | change %."""

    def sizeHint(self, option, index):
        return QtCore.QSize(option.rect.width(), 30 if index.data(ROLE_HEAD) else 44)

    def paint(self, p, option, index):
        p.save()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        r = option.rect
        al = QtCore.Qt.AlignmentFlag
        f = QtGui.QFont(option.font)
        head = index.data(ROLE_HEAD)
        if head:
            f.setPixelSize(11)
            f.setWeight(QtGui.QFont.Weight.DemiBold)
            p.setFont(f)
            p.setPen(QtGui.QColor(theme.MUTED))
            p.drawText(r.adjusted(16, 8, 0, 0), al.AlignLeft | al.AlignVCenter, str(head))
            p.restore()
            return
        if option.state & QtWidgets.QStyle.StateFlag.State_MouseOver:
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor("#1b1b1b"))
            p.drawRoundedRect(QtCore.QRectF(r).adjusted(4, 1, -4, -1), 6, 6)
        sym = index.data(ROLE_SYM)
        row = index.data(ROLE_ROW)                       # (price, change %, biggest |change %| in the list)
        if not sym:
            f.setPixelSize(13)
            p.setFont(f)
            p.setPen(QtGui.QColor(theme.MUTED))
            p.drawText(r.adjusted(16, 0, 0, 0), al.AlignLeft | al.AlignVCenter, str(index.data() or "").strip())
            p.restore()
            return
        pill_w, right = 70, r.right() - 12
        name_w = int(r.width() * 0.42)
        f.setPixelSize(14)
        p.setFont(f)
        p.setPen(QtGui.QColor("#dbdbdb"))
        nm = short_name(sym)
        p.drawText(QtCore.QRect(r.x() + 16, r.y() + 5, name_w, 20), al.AlignLeft | al.AlignVCenter,
                   p.fontMetrics().elidedText(nm, QtCore.Qt.TextElideMode.ElideRight, name_w))
        f2 = QtGui.QFont(f)
        f2.setPixelSize(11)
        p.setFont(f2)
        p.setPen(QtGui.QColor("#8c8c8c"))
        sub = sym if not row else f"{sym}  {row[0]:,.2f}"
        p.drawText(QtCore.QRect(r.x() + 16, r.y() + 24, name_w + 40, 16), al.AlignLeft | al.AlignVCenter,
                   p.fontMetrics().elidedText(sub, QtCore.Qt.TextElideMode.ElideRight, name_w + 40))
        if row:
            price, pct, top = row
            col = QtGui.QColor(theme.UP if pct >= 0 else theme.DOWN)
            # the bar: centred on 0, as long as this move is next to the biggest one in the list
            x0, x1 = r.x() + 16 + name_w + 48, right - pill_w - 12
            if x1 - x0 > 30:
                mid, half = (x0 + x1) / 2.0, (x1 - x0) / 2.0
                cy = r.center().y()
                p.setPen(QtCore.Qt.PenStyle.NoPen)
                p.setBrush(QtGui.QColor("#1f1f1f"))
                p.drawRoundedRect(QtCore.QRectF(x0, cy - 2, x1 - x0, 4), 2, 2)
                ln = half * min(1.0, abs(pct) / top) if top else 0.0
                p.setBrush(col)
                p.drawRoundedRect(QtCore.QRectF(mid if pct >= 0 else mid - ln, cy - 2, max(ln, 1.0), 4), 2, 2)
            pill = QtCore.QRectF(right - pill_w, r.center().y() - 12, pill_w, 24)
            bg = QtGui.QColor(col)
            bg.setAlphaF(0.16)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(pill, 6, 6)
            p.setPen(col)
            f4 = QtGui.QFont(f)
            f4.setPixelSize(13)
            f4.setWeight(QtGui.QFont.Weight.DemiBold)
            p.setFont(f4)
            p.drawText(pill, al.AlignCenter, f"{pct:+.2f}%".replace("-", "\u2212"))
        p.restore()


class Heatmap(QtWidgets.QWidget):
    """All fifty as tiles, biggest gain first, coloured by the day's change."""
    chosen = QtCore.Signal(str)
    TILE_H = 48
    GAP = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list = []                              # [(sym, price, pct)]
        self._hover = -1
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)

    def set_rows(self, rows):
        self.rows = list(rows)
        self.updateGeometry()
        self.update()

    def _cols(self) -> int:
        return max(3, (self.width() - 16) // 100)            # (ULTRACEMCO, APOLLOHOSP fit whole)

    def _tiles(self):
        cols = self._cols()
        w = (self.width() - 16 - self.GAP * (cols - 1)) / cols
        out = []
        for k in range(len(self.rows)):
            r, c = divmod(k, cols)
            out.append(QtCore.QRectF(8 + c * (w + self.GAP), 4 + r * (self.TILE_H + self.GAP), w, self.TILE_H))
        return out

    def sizeHint(self):
        cols = self._cols()
        n = (len(self.rows) + cols - 1) // cols
        return QtCore.QSize(300, 8 + n * (self.TILE_H + self.GAP))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        cols = max(3, (w - 16) // 100)
        n = (len(self.rows) + cols - 1) // cols
        return 8 + n * (self.TILE_H + self.GAP)

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        al = QtCore.Qt.AlignmentFlag
        f = QtGui.QFont(self.font())
        for k, (rect, (sym, _price, pct)) in enumerate(zip(self._tiles(), self.rows)):
            col = heat_color(pct)
            if k == self._hover:
                col = col.lighter(125)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(rect, 5, 5)
            f.setPixelSize(12)
            f.setWeight(QtGui.QFont.Weight.DemiBold)
            p.setFont(f)
            p.setPen(QtGui.QColor("#ffffff"))
            top = QtCore.QRectF(rect.x() + 4, rect.y() + 6, rect.width() - 8, 18)
            p.drawText(top, al.AlignCenter, p.fontMetrics().elidedText(sym, QtCore.Qt.TextElideMode.ElideRight,
                                                                        int(top.width())))
            f.setPixelSize(11)
            f.setWeight(QtGui.QFont.Weight.Normal)
            p.setFont(f)
            p.setPen(QtGui.QColor(255, 255, 255, 215))
            p.drawText(QtCore.QRectF(rect.x(), rect.y() + 24, rect.width(), 18), al.AlignCenter,
                       f"{pct:+.2f}%".replace("-", "−"))
        p.end()

    def _at(self, pos) -> int:
        for k, rect in enumerate(self._tiles()[:len(self.rows)]):
            if rect.contains(QtCore.QPointF(pos)):
                return k
        return -1

    def mouseMoveEvent(self, ev):
        k = self._at(ev.position())
        if k != self._hover:
            self._hover = k
            if k >= 0:
                sym, price, pct = self.rows[k]
                try:
                    from .symbols import sector_of
                    sec = sector_of(sym)
                except Exception:  # noqa: BLE001
                    sec = ""
                self.setToolTip(f"{sym}  {_name(sym)}" + (f"\n{sec}" if sec else "") + f"\n{price:,.2f}   {pct:+.2f}%")
            self.update()

    def leaveEvent(self, _ev):
        self._hover = -1
        self.update()

    def mousePressEvent(self, ev):
        k = self._at(ev.position())
        if k >= 0 and ev.button() == QtCore.Qt.MouseButton.LeftButton:
            self.chosen.emit(self.rows[k][0])


class Breadth(QtWidgets.QWidget):
    """'34 up  ▕████████▏  16 down': how many of the fifty are up today."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.up = self.down = 0
        self.setFixedHeight(34)

    def set_counts(self, up: int, down: int):
        self.up, self.down = int(up), int(down)
        self.update()

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        f = QtGui.QFont(self.font())
        f.setPixelSize(12)
        p.setFont(f)
        al = QtCore.Qt.AlignmentFlag
        w, h = self.width(), self.height()
        p.setPen(QtGui.QColor(theme.UP))
        p.drawText(QtCore.QRect(14, 0, 90, h), al.AlignLeft | al.AlignVCenter, f"▲ {self.up} up")
        p.setPen(QtGui.QColor(theme.DOWN))
        p.drawText(QtCore.QRect(w - 104, 0, 90, h), al.AlignRight | al.AlignVCenter, f"{self.down} down ▼")
        bar = QtCore.QRectF(96, h / 2 - 3, max(20, w - 192), 6)
        tot = self.up + self.down
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor("#262626"))
        p.drawRoundedRect(bar, 3, 3)
        if tot:
            g = QtCore.QRectF(bar.x(), bar.y(), bar.width() * self.up / tot, bar.height())
            path = QtGui.QPainterPath()
            path.addRoundedRect(bar, 3, 3)
            p.setClipPath(path)
            p.setBrush(QtGui.QColor(theme.DOWN))
            p.drawRect(bar)
            p.setBrush(QtGui.QColor(theme.UP))
            p.drawRect(g)
        p.end()


class MarketPanel(QtWidgets.QWidget):
    """The right-side panel: NIFTY 50 gainers, losers and heatmap."""
    chosen = QtCore.Signal(str)

    def __init__(self, parent=None, symbols=None):
        super().__init__(parent)
        self.setObjectName("marketPanel")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.symbols = list(symbols or NIFTY50)
        self.sectors, self.themes = list(SECTORS), list(THEMES)
        self.data: dict = {}                             # sym -> (price, change %)
        self.sector_data: dict = {}                      # index sym -> (price, change %)
        self.mode = "Gainers"
        self._job = None
        self.updated = None
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(0)
        head = QtWidgets.QHBoxLayout()
        head.setContentsMargins(14, 2, 12, 6)
        title = QtWidgets.QLabel("NIFTY 50")
        title.setStyleSheet("color:#ffffff; font-size:16px; font-weight:600; background:transparent;")
        head.addWidget(title)
        head.addStretch(1)
        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet(f"color:{theme.MUTED}; font-size:11px; background:transparent;")
        head.addWidget(self.status)
        v.addLayout(head)
        chips = QtWidgets.QHBoxLayout()
        chips.setContentsMargins(12, 2, 12, 4)
        chips.setSpacing(4)
        self.chips = {}
        for m in MODES:
            b = QtWidgets.QPushButton(m)
            b.setObjectName("chip")
            b.setCheckable(True)
            b.setChecked(m == self.mode)
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _=False, m=m: self.set_mode(m))
            chips.addWidget(b)
            self.chips[m] = b
        chips.addStretch(1)
        v.addLayout(chips)
        self.setStyleSheet(
            "QWidget#marketPanel{background:#000000;}"
            "QPushButton#chip{background:#222222;border:1px solid #222222;border-radius:13px;padding:0 11px;"
            "color:#b8b8b8;font-size:13px;min-height:24px;max-height:24px;}"
            "QPushButton#chip:hover{background:#303030;border-color:#303030;}"
            "QPushButton#chip:checked{background:#f2f2f2;border-color:#f2f2f2;color:#000000;}"
            "QListWidget{background:transparent;border:0;outline:0;}"
            "QScrollArea{background:transparent;border:0;}"
            "QScrollArea > QWidget > QWidget{background:transparent;}")
        self.breadth = Breadth()
        v.addWidget(self.breadth)
        self.stack = QtWidgets.QStackedWidget()
        self.list = QtWidgets.QListWidget()
        self.list.setItemDelegate(_RowDelegate(self.list))
        self.list.setMouseTracking(True)
        self.list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.list.itemClicked.connect(lambda it: it.data(ROLE_SYM) and self.chosen.emit(it.data(ROLE_SYM)))
        self.stack.addWidget(self.list)
        self.sec_list = QtWidgets.QListWidget()
        self.sec_list.setItemDelegate(_SectorDelegate(self.sec_list))
        self.sec_list.setMouseTracking(True)
        self.sec_list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.sec_list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sec_list.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.sec_list.itemClicked.connect(lambda it: it.data(ROLE_SYM) and self.chosen.emit(it.data(ROLE_SYM)))
        self.heat = Heatmap()
        self.heat.chosen.connect(self.chosen.emit)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self.heat)
        self.stack.addWidget(scroll)
        self.stack.addWidget(self.sec_list)
        v.addWidget(self.stack, 1)
        self._fill()

    # -- data
    def refresh(self) -> bool:
        """Every price in one round (False while one is still running)."""
        if self._job is not None and self._job.isRunning():
            return False
        from .watchlist import SparkWorker
        self._job = SparkWorker(list(dict.fromkeys(self.symbols + self.sectors + self.themes)), self)
        self._job.done.connect(self._got)
        self._job.start()
        return True

    def _got(self, got: dict):
        idx = set(self.sectors) | set(self.themes)
        for sym, r in (got or {}).items():
            pv = r.get("prev")
            if pv:
                row = (float(r["price"]), (float(r["price"]) / float(pv) - 1.0) * 100.0)
                if sym in idx:
                    self.sector_data[sym] = row
                if sym in self.symbols:
                    self.data[sym] = row
        if got:
            self.updated = time.time()
            self.stamp()
        self._fill()

    def stamp(self, now=None):
        """'Live · 10:42:07' in market hours; after the close the numbers are the last session's, and it says so."""
        from .watchlist import market_state
        st = market_state(self.symbols[0] if self.symbols else "RELIANCE", now)[0]
        if st == "open":
            self.status.setText("Live · " + time.strftime("%H:%M:%S"))
        elif st == "pre":
            self.status.setText("Pre-open · " + time.strftime("%H:%M"))
        else:
            self.status.setText("Market closed · last close")

    def set_data(self, data: dict, sectors: dict | None = None):
        """{sym: (price, change %)} for the stocks, and for the sector indices (tests, a broker's quotes)."""
        self.data = dict(data)
        if sectors is not None:
            self.sector_data = dict(sectors)
        self._fill()

    # -- view
    def set_mode(self, m: str):
        self.mode = m if m in MODES else "Gainers"
        for k, b in self.chips.items():
            b.setChecked(k == self.mode)
        self.stack.setCurrentIndex({"Heatmap": 1, "Sectors": 2}.get(self.mode, 0))
        self._fill()

    def ranked(self) -> list:
        rows = [(s, *self.data[s]) for s in self.symbols if s in self.data]
        rows.sort(key=lambda r: r[2], reverse=self.mode != "Losers")
        return rows

    def ranked_sectors(self, syms: list) -> list:
        """[(sym, price, change %)] biggest gain first."""
        rows = [(s, *self.sector_data[s]) for s in syms if s in self.sector_data]
        rows.sort(key=lambda r: r[2], reverse=True)
        return rows

    def _fill_sectors(self):
        sec, th = self.ranked_sectors(self.sectors), self.ranked_sectors(self.themes)
        allr = sec + th
        self.breadth.set_counts(sum(1 for r in allr if r[2] > 0), sum(1 for r in allr if r[2] < 0))
        top = max((abs(r[2]) for r in allr), default=0.0)
        keep = self.sec_list.verticalScrollBar().value()
        self.sec_list.clear()
        for head, rows in (("SECTORS", sec), ("THEMES", th)):
            if not rows:
                continue
            h = QtWidgets.QListWidgetItem()
            h.setData(ROLE_HEAD, head)
            h.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
            self.sec_list.addItem(h)
            for sym, price, pct in rows:
                it = QtWidgets.QListWidgetItem()
                it.setData(ROLE_SYM, sym)
                it.setData(ROLE_ROW, (price, pct, top))
                it.setToolTip(f"{short_name(sym)} \u2014 {sym}  {price:,.2f}  {pct:+.2f}%")
                self.sec_list.addItem(it)
        if not allr:
            it = QtWidgets.QListWidgetItem("  Loading sectors\u2026")
            it.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
            self.sec_list.addItem(it)
        self.sec_list.verticalScrollBar().setValue(keep)

    def _fill(self):
        if self.mode == "Sectors":
            self._fill_sectors()
            return
        rows = self.ranked()
        up = sum(1 for r in rows if r[2] > 0)
        down = sum(1 for r in rows if r[2] < 0)
        self.breadth.set_counts(up, down)
        if self.mode == "Heatmap":
            self.heat.set_rows(rows)
            return
        keep = self.list.verticalScrollBar().value()
        self.list.clear()
        for sym, price, pct in rows:
            it = QtWidgets.QListWidgetItem()
            it.setData(ROLE_SYM, sym)
            it.setData(ROLE_ROW, (price, pct))
            it.setToolTip(f"{sym} — {_name(sym)}")
            self.list.addItem(it)
        if not rows:
            it = QtWidgets.QListWidgetItem("  Loading prices…")
            it.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
            self.list.addItem(it)
        self.list.verticalScrollBar().setValue(keep)
