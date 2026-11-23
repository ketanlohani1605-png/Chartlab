"""One look for everything the app opens on top of the chart - the look of its symbol search, taken further:

  * every dialog is a card with round corners and no system title bar: its own header (title, close button, drag
    it by the header), a hairline edge, the system's soft shadow;
  * the card is dark glass: what lies behind it (the chart) shimmers through, blurred;
  * while a dialog is open the window behind it dims a little, and the dialog fades in instead of popping up;
  * message boxes (questions, notices, About) are the same kind of card instead of the system's grey box.

Nothing here changes what a dialog does - only how it is framed. install() switches it on for the whole app; a dialog
can opt out with  dlg.setProperty("chrome", "off")."""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

# Every number below is TradingView's own, read from its live chart page (computed styles of its dialogs):
#   dialog   background rgba(19,19,19,0.85) . backdrop-filter blur(15px) saturate(0.75) . border-radius 20px
#            box-shadow inset 0 0 0 1px rgba(140,140,140,0.1), 0 0 2px #000, 0 3px 9px #000
#            enters in 0.2 s, cubic-bezier(0.23, 1, 0.32, 1), from opacity 0
#   menu     background rgba(19,19,19,0.75) . blur(11px) saturate(0.25) . border-radius 6px . the same shadows
#   backdrop (only behind the symbol search) rgba(26,26,26,0.4)
#   title    20px, weight 600, #b8b8b8, 20 px from the left, in a 60 px header; close button 33 x 33
RADIUS = 20.0                      # corner radius of a dialog
TINT = (19, 19, 19, 217)           # the glass: rgba(19,19,19,0.85)
SOLID = (19, 19, 19, 255)          # the card when there is nothing behind it to show through
EDGE = (140, 140, 140, 26)         # hairline inside the card's edge: rgba(140,140,140,0.1)
BLUR = 15.0                        # backdrop blur (a Gaussian of this many pixels)
SATURATE = 0.75                    # ... and how much of its colour stays
MENU_TINT = (19, 19, 19, 191)      # rgba(19,19,19,0.75)
MENU_BLUR = 11.0
MENU_SATURATE = 0.25
MENU_RADIUS = 6.0
SCRIM = (26, 26, 26, 102)          # rgba(26,26,26,0.4): the window behind the symbol search
CATCH = (0, 0, 0, 3)               # an overlay without dimming: not seen, but it takes the click that closes it
HEADER = 60                        # height of a card's header
TITLE_PX = 20
FADE_MS = 200
_on = {"v": False}
_orig: dict = {}                   # the methods install() wrapped, to call them (and to put them back)
SKIP = ("QFileDialog", "QColorDialog", "QFontDialog", "QProgressDialog", "QErrorMessage", "QWizard",
        "QPrintDialog", "QPageSetupDialog")
QSS = (" QDialog{background:transparent;border:0;border-radius:0;}"
       " QWidget#chromeBody{background:transparent;}"
       " QSizeGrip{background:transparent;image:none;width:16px;height:16px;}")


# ------------------------------------------------------------------ the glass a card is made of
def frosted(pm: QtGui.QPixmap, sigma: float = BLUR, saturation: float = SATURATE) -> QtGui.QImage:
    """What is behind a card, as CSS draws it under `backdrop-filter: blur(sigma) saturate(saturation)`."""
    import numpy as np
    img = pm.toImage().convertToFormat(QtGui.QImage.Format.Format_RGB32)
    dpr = pm.devicePixelRatio() or 1.0
    w, h = img.width(), img.height()
    if w < 4 or h < 4:
        return img
    sm = QtCore.Qt.TransformationMode.SmoothTransformation
    ig = QtCore.Qt.AspectRatioMode.IgnoreAspectRatio
    k = max(1, int(round(sigma * dpr / 3.0)))               # work on a picture k times smaller: fast, same result
    sw, sh = max(2, w // k), max(2, h // k)
    small = img.scaled(sw, sh, ig, sm)
    stride = small.bytesPerLine()
    arr = np.frombuffer(small.constBits(), np.uint8, stride * sh).reshape(sh, stride // 4, 4)[:, :sw, :]
    arr = arr.astype(np.float32)
    sg = max(0.5, sigma * dpr / k)
    r = max(1, int(3 * sg + 0.5))
    x = np.arange(-r, r + 1, dtype=np.float32)
    ker = np.exp(-(x * x) / (2 * sg * sg))
    ker /= ker.sum()
    pad = np.pad(arr, ((0, 0), (r, r), (0, 0)), mode="edge")
    arr = sum(ker[i] * pad[:, i:i + sw, :] for i in range(2 * r + 1))
    pad = np.pad(arr, ((r, r), (0, 0), (0, 0)), mode="edge")
    arr = sum(ker[i] * pad[i:i + sh, :, :] for i in range(2 * r + 1))
    if saturation != 1.0:                                    # (memory order is blue, green, red)
        lum = 0.0722 * arr[..., 0] + 0.7152 * arr[..., 1] + 0.2126 * arr[..., 2]
        arr[..., :3] = lum[..., None] + saturation * (arr[..., :3] - lum[..., None])
    arr[..., 3] = 255.0
    buf = np.ascontiguousarray(np.clip(arr, 0, 255).astype(np.uint8))
    out = QtGui.QImage(buf.data, sw, sh, sw * 4, QtGui.QImage.Format.Format_RGB32).copy()
    out = out.scaled(w, h, ig, sm)
    out.setDevicePixelRatio(dpr)
    return out


def card_image(size: QtCore.QSize, dpr: float, backdrop: QtGui.QImage | None, at: QtCore.QPoint | None = None,
               radius: float = RADIUS, tint: tuple = TINT) -> QtGui.QImage:
    """A card of this size, ready to be drawn: the glass over the blurred backdrop (or solid when there is nothing
    behind), round corners, TradingView's hairline just inside the edge."""
    img = QtGui.QImage(max(1, int(size.width() * dpr)), max(1, int(size.height() * dpr)),
                       QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(dpr)
    img.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(img)
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    r = QtCore.QRectF(0, 0, size.width(), size.height())
    radius = min(radius, size.width() / 2.0, size.height() / 2.0)
    path = QtGui.QPainterPath()
    path.addRoundedRect(r, radius, radius)
    p.fillPath(path, QtGui.QColor(*SOLID))
    if backdrop is not None and not backdrop.isNull():
        p.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceAtop)   # stay inside the card
        p.drawImage(at or QtCore.QPoint(0, 0), backdrop)
        p.fillRect(r, QtGui.QColor(*tint))
        p.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)
    edge = QtGui.QPainterPath()
    edge.addRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), max(0.0, radius - 0.5), max(0.0, radius - 0.5))
    p.setPen(QtGui.QPen(QtGui.QColor(*EDGE), 1.0))
    p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
    p.drawPath(edge)
    p.end()
    return img


def paint_shadow(p: QtGui.QPainter, rect: QtCore.QRectF, radius: float):
    """TradingView's card shadow, `0 0 2px #000, 0 3px 9px #000`, as rings that fade out (an overlay dialog has
    no system shadow: it would trace the whole window)."""
    import math
    p.save()
    p.setPen(QtCore.Qt.PenStyle.NoPen)
    for dy, sigma, strength in ((3.0, 4.5, 0.55), (0.0, 1.0, 0.7)):
        n = max(2, int(math.ceil(sigma * 2.6)))
        prof = [strength * math.exp(-(d * d) / (2.0 * sigma * sigma)) for d in range(n + 1)]
        for i in range(n, 0, -1):
            a = max(0.0, prof[i - 1] - prof[i])
            if a <= 0.002:
                continue
            r = rect.translated(0, dy).adjusted(-i, -i, i, i)
            path = QtGui.QPainterPath()
            path.addRoundedRect(r, radius + i, radius + i)
            p.fillPath(path, QtGui.QColor(0, 0, 0, int(round(255 * a))))
    p.restore()


class _HostWatch(QtCore.QObject):
    """Keeps an overlay dialog over its window when the window moves or changes size."""

    def __init__(self, glass_):
        super().__init__(glass_)
        self.glass = glass_

    def eventFilter(self, host, ev):
        if ev.type() in (QtCore.QEvent.Type.Move, QtCore.QEvent.Type.Resize):
            for w in list(self.glass._over_dialogs(host)):
                try:
                    self.glass._place_overlay(w, host)
                    w.update()
                except RuntimeError:
                    pass
        return False


# ------------------------------------------------------------------ the dimmed window behind a dialog
class Scrim(QtWidgets.QWidget):
    def __init__(self, host: QtWidgets.QWidget):
        super().__init__(host)
        self.setObjectName("chromeScrim")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.users = 0
        self._a = 0.0
        self._anim = QtCore.QVariantAnimation(self)
        self._anim.setDuration(FADE_MS)
        self._anim.setEasingCurve(ease())
        self._anim.valueChanged.connect(self._set)
        self._anim.finished.connect(self._done)
        host.installEventFilter(self)
        self.hide()

    def _set(self, v):
        self._a = float(v)
        self.update()

    def _done(self):
        if self._a <= 0.5 and self.users <= 0:
            self.hide()

    def eventFilter(self, host, ev):
        if ev.type() == QtCore.QEvent.Type.Resize and self.isVisible():
            self.setGeometry(host.rect())
        return False

    def come(self):
        self.users += 1
        if self.users == 1:
            self.setGeometry(self.parentWidget().rect())
            self.show()
            self.raise_()
            self._go(float(SCRIM[3]))

    def go(self):
        self.users = max(0, self.users - 1)
        if self.users == 0:
            self._go(0.0)

    def _go(self, to):
        self._anim.stop()
        self._anim.setStartValue(self._a)
        self._anim.setEndValue(to)
        self._anim.start()

    def paintEvent(self, _ev):
        if self._a > 0.5:
            p = QtGui.QPainter(self)
            p.fillRect(self.rect(), QtGui.QColor(SCRIM[0], SCRIM[1], SCRIM[2], int(self._a)))
            p.end()


def scrim_of(host: QtWidgets.QWidget) -> Scrim:
    s = host.findChild(Scrim, "chromeScrim", QtCore.Qt.FindChildOption.FindDirectChildrenOnly)
    return s if s is not None else Scrim(host)


# ------------------------------------------------------------------ a card's header
class Header(QtWidgets.QWidget):
    """Title on the left (20 px, semi-bold, as TradingView writes it), a close button on the right; the card is
    dragged by it."""

    def __init__(self, dlg: QtWidgets.QDialog):
        super().__init__(dlg)
        self.setObjectName("chromeHeader")
        self.setFixedHeight(HEADER)
        self._press = None
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(20, 0, 17, 0)
        lay.setSpacing(8)
        self.title = QtWidgets.QLabel(dlg.windowTitle())
        self.title.setObjectName("chromeTitle")
        self.title.setStyleSheet(f"QLabel#chromeTitle{{color:#b8b8b8;font-size:{TITLE_PX}px;font-weight:600;"
                                 "background:transparent;}")
        self.title.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.title, 1)
        self.close_btn = QtWidgets.QToolButton()
        self.close_btn.setObjectName("chromeClose")
        self.close_btn.setToolTip("Close")
        self.close_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.close_btn.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.close_btn.setFixedSize(33, 33)
        self.close_btn.setIconSize(QtCore.QSize(22, 22))
        try:
            from . import icons
            self.close_btn.setIcon(icons.get("close", "#b8b8b8", 22))
        except Exception:  # noqa: BLE001
            self.close_btn.setText("\u00d7")
        self.close_btn.setStyleSheet("QToolButton#chromeClose{background:transparent;border:0;border-radius:8px;"
                                     "padding:0;color:#b8b8b8;font-size:18px;}"
                                     "QToolButton#chromeClose:hover{background:#222222;}"
                                     "QToolButton#chromeClose:pressed{background:#2e2e2e;}")
        self.close_btn.clicked.connect(dlg.reject)
        lay.addWidget(self.close_btn)
        dlg.windowTitleChanged.connect(self.title.setText)

    def mousePressEvent(self, ev):
        if ev.button() == QtCore.Qt.MouseButton.LeftButton:
            win = self.window()
            h = win.windowHandle()
            if h is not None and h.startSystemMove():       # the system moves the window: smooth, snaps as usual
                self._press = None
                return
            self._press = ev.globalPosition().toPoint() - win.frameGeometry().topLeft()

    def mouseMoveEvent(self, ev):
        if self._press is not None and ev.buttons() & QtCore.Qt.MouseButton.LeftButton:
            self.window().move(ev.globalPosition().toPoint() - self._press)

    def mouseReleaseEvent(self, _ev):
        self._press = None


def ease() -> QtCore.QEasingCurve:
    """TradingView's cubic-bezier(0.23, 1, 0.32, 1)."""
    c = QtCore.QEasingCurve(QtCore.QEasingCurve.Type.BezierSpline)
    c.addCubicBezierSegment(QtCore.QPointF(0.23, 1.0), QtCore.QPointF(0.32, 1.0), QtCore.QPointF(1.0, 1.0))
    return c


# ------------------------------------------------------------------ what turns a dialog into a card
class Glass(QtCore.QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._card: dict = {}        # id(dialog) -> (size, QImage)
        self._timers: dict = {}
        self._fades: dict = {}
        self._dimmed: dict = {}      # id(dialog) -> Scrim it switched on
        self._over: dict = {}        # id(overlay dialog) -> (dialog, host, the card's rect in it)
        self._watch = _HostWatch(self)

    def _over_dialogs(self, host):
        return [d for d, h, _r in self._over.values() if h is host]

    def card_rect(self, w) -> QtCore.QRect | None:
        """Where an overlay dialog's card is (in the dialog); None for an ordinary card."""
        hit = self._over.get(id(w))
        return QtCore.QRect(hit[2]) if hit else None

    def eventFilter(self, w, ev):
        t = ev.type()
        E = QtCore.QEvent.Type
        try:
            hit = self._over.get(id(w)) if t in (E.MouseButtonPress, E.MouseButtonDblClick, E.Wheel) else None
            if hit is not None and not hit[2].contains(ev.position().toPoint()):
                if t == E.MouseButtonPress and ev.button() == QtCore.Qt.MouseButton.LeftButton:
                    w.reject()                              # a click beside the card closes it (TradingView)
                return True                                 # (and nothing behind it scrolls)
            if t == E.Paint:
                self._paint(w)
            elif t == E.Show:
                self._shown(w)
            elif t == E.Hide:
                self._hidden(w)
            elif t in (E.Move, E.Resize):
                self._changed(w)
            elif t == E.Destroy or t == E.DeferredDelete:
                self._forget(w)
        except RuntimeError:         # the dialog is being deleted under our feet
            pass
        return False

    # -- painting
    def _host(self, w):
        p = w.parentWidget()
        h = p.window() if p is not None else None
        return h if (h is not None and h is not w and h.isVisible()) else None

    def _make(self, w):
        host = self._host(w)
        back, at = None, None
        if host is not None:
            top = host.mapFromGlobal(w.mapToGlobal(QtCore.QPoint(0, 0)))
            want = QtCore.QRect(top, w.size())
            got = want.intersected(host.rect())
            if got.width() > 8 and got.height() > 8:
                sc = host.findChild(Scrim, "chromeScrim", QtCore.Qt.FindChildOption.FindDirectChildrenOnly)
                keep = sc._a if sc is not None else 0.0
                try:
                    if sc is not None:
                        sc._a = 0.0                         # (the dimming is drawn over the page, not into the glass)
                    back = frosted(host.grab(got))
                    at = got.topLeft() - want.topLeft()
                except Exception:  # noqa: BLE001 - no backdrop then: a solid card
                    back = None
                finally:
                    if sc is not None:
                        sc._a = keep
        dpr = w.devicePixelRatioF()
        rad = w.property("chromeRadius")
        img = card_image(w.size(), dpr, back, at, float(rad) if rad else RADIUS)
        self._card[id(w)] = (w.size(), img)
        return img

    def _paint(self, w):
        hit = self._card.get(id(w))
        img = hit[1] if hit and hit[0] == w.size() else (self._make_overlay(w) if id(w) in self._over
                                                          else self._make(w))
        p = QtGui.QPainter(w)
        p.drawImage(0, 0, img)
        p.end()

    # -- an overlay dialog: as big as its window, the card in the middle, the rest dims (and closes it when clicked)
    def _place_overlay(self, w, host):
        cs = w.property("_chromeCardSize")
        if not isinstance(cs, QtCore.QSize) or cs.isEmpty():
            cs = QtCore.QSize(w.size())
            w.setProperty("_chromeCardSize", cs)
        W, H = host.width(), host.height()
        cw, ch = min(cs.width(), max(200, W - 16)), min(cs.height(), max(160, H - 16))
        cx = max(0, (W - cw) // 2)
        cy = max(8, (H - ch) // 2) if H - ch >= 16 else max(0, (H - ch) // 2)
        top = w.property("chromeTop")                       # a dialog that sits near the top (quick search)
        if isinstance(top, int) and top >= 0:
            cy = max(0, min(top, H - ch))
        card = QtCore.QRect(cx, cy, cw, ch)
        w.setMinimumSize(0, 0)
        w.setMaximumSize(16777215, 16777215)
        w.setGeometry(QtCore.QRect(host.mapToGlobal(QtCore.QPoint(0, 0)), QtCore.QSize(W, H)))
        lay = w.layout()
        if lay is not None:
            own = w.property("_chromeMargins")              # the dialog's own padding stays inside the card
            if not isinstance(own, QtCore.QMargins):
                own = QtCore.QMargins(lay.contentsMargins())
                w.setProperty("_chromeMargins", own)
            lay.setContentsMargins(cx + own.left(), cy + own.top(), W - cx - cw + own.right(),
                                   H - cy - ch + own.bottom())
        self._over[id(w)] = (w, host, card)
        self._card.pop(id(w), None)

    def _make_overlay(self, w):
        _d, host, card = self._over[id(w)]
        dpr = w.devicePixelRatioF()
        back, at = None, None
        try:
            got = card.intersected(host.rect())
            if got.width() > 8 and got.height() > 8:
                back = frosted(host.grab(got))
                at = got.topLeft() - card.topLeft()
        except Exception:  # noqa: BLE001 - no backdrop then: a solid card
            back = None
        rad = w.property("chromeRadius")
        rad = float(rad) if rad else RADIUS
        cimg = card_image(card.size(), dpr, back, at, rad)
        img = QtGui.QImage(max(1, int(w.width() * dpr)), max(1, int(w.height() * dpr)),
                           QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(QtGui.QColor(*(SCRIM if w.property("chromeScrim") else CATCH)))
        p = QtGui.QPainter(img)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        paint_shadow(p, QtCore.QRectF(card), rad)
        p.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Source)   # the glass, not glass+dim
        clip = QtGui.QPainterPath()
        clip.addRoundedRect(QtCore.QRectF(card), rad, rad)
        p.setClipPath(clip)
        p.drawImage(card.topLeft(), cimg)
        p.end()
        self._card[id(w)] = (w.size(), img)
        return img

    # -- life
    def _shown(self, w):
        self._card.pop(id(w), None)
        host = self._host(w)
        if host is not None and w.property("chromeOverlay"):
            self._place_overlay(w, host)
            host.installEventFilter(self._watch)
            w.setProperty("_chromePlaced", True)
            w.setProperty("_chromeSized", True)
        if not w.property("_chromeLaid"):
            w.setProperty("_chromeLaid", True)
            # lists that laid their rows out while the content was being moved under the header did so at a
            # passing size: lay them out again now that the card has its real one
            QtCore.QTimer.singleShot(0, lambda ww=w: self._relayout(ww))
        lay = w.layout()
        if lay is not None and not w.property("_chromeSized"):
            w.setProperty("_chromeSized", True)
            if lay.hasHeightForWidth():                     # wrapped text needs more lines at this width than Qt
                need = lay.totalHeightForWidth(w.width())   # guessed: nothing may be squeezed or cut off
                room = (host.height() - 40) if host is not None else 2000
                if w.height() < need <= min(w.maximumHeight(), room):
                    w.resize(w.width(), need)
        if host is not None and not w.property("_chromePlaced"):
            w.setProperty("_chromePlaced", True)
            if not w.property("_chromeOwnPlace"):           # the dialog did not choose a place: over the window's
                g = host.frameGeometry()                    # middle, a little above centre
                x = g.x() + (g.width() - w.width()) // 2
                y = g.y() + max(24, int((g.height() - w.height()) * 0.42))
                w.move(x, y)
        if host is not None and w.property("chromeScrim") and id(w) not in self._dimmed and id(w) not in self._over:
            self._make(w)                                   # look through the glass before the window dims
            s = scrim_of(host)                              # (TradingView dims the page only behind its symbol
            s.come()                                        # search; its other dialogs float on the chart as it is)
            self._dimmed[id(w)] = s
        if FADE_MS > 0 and id(w) not in self._fades:
            w.setWindowOpacity(0.0)
            a = QtCore.QVariantAnimation(w)
            a.setDuration(FADE_MS)
            a.setStartValue(0.0)
            a.setEndValue(1.0)
            a.setEasingCurve(ease())
            a.valueChanged.connect(lambda v, ww=w: self._opacity(ww, v))
            a.finished.connect(lambda ww=w: (self._opacity(ww, 1.0), self._fades.pop(id(ww), None)))
            self._fades[id(w)] = a
            a.start()

    @staticmethod
    def _relayout(w):
        try:
            for v in w.findChildren(QtWidgets.QAbstractItemView):
                v.scheduleDelayedItemsLayout()
        except RuntimeError:
            pass

    @staticmethod
    def _opacity(w, v):
        try:
            w.setWindowOpacity(float(v))
        except RuntimeError:
            pass

    def _hidden(self, w):
        hit = self._over.pop(id(w), None)
        if hit is not None and not self._over_dialogs(hit[1]):
            try:
                hit[1].removeEventFilter(self._watch)
            except RuntimeError:
                pass
        s = self._dimmed.pop(id(w), None)
        if s is not None:
            try:
                s.go()
            except RuntimeError:
                pass
        a = self._fades.pop(id(w), None)
        if a is not None:
            a.stop()
            self._opacity(w, 1.0)
        self._card.pop(id(w), None)

    def _changed(self, w):
        if not w.isVisible():
            return
        t = self._timers.get(id(w))
        if t is None:
            t = self._timers[id(w)] = QtCore.QTimer(w)
            t.setSingleShot(True)
            t.setInterval(70)
            t.timeout.connect(lambda ww=w: self._refresh(ww))
        t.start()
        hit = self._card.get(id(w))
        if hit and hit[0] != w.size():                      # resized: a card of the new size at once
            self._card.pop(id(w), None)

    def _refresh(self, w):
        try:
            if w.isVisible():
                self._card.pop(id(w), None)
                w.update()
        except RuntimeError:
            pass

    def _forget(self, w):
        for d in (self._card, self._timers, self._fades):
            d.pop(id(w), None)
        self._hidden(w)


_menus: dict = {}                  # id(menu) -> (size, image) while it is open


def menu_image(m, radius: float = MENU_RADIUS) -> QtGui.QImage:
    """The card a popup menu is painted on (made once each time the menu opens)."""
    hit = _menus.get(id(m))
    if hit is not None and hit[0] == m.size():
        return hit[1]
    back, at = None, None
    p = m.parentWidget()
    host = p.window() if p is not None else QtWidgets.QApplication.activeWindow()
    while isinstance(host, QtWidgets.QMenu):                # a sub-menu: look through to the window below
        pp = host.parentWidget()
        host = pp.window() if pp is not None else None
    if host is not None and host is not m and host.isVisible():
        top = host.mapFromGlobal(m.mapToGlobal(QtCore.QPoint(0, 0)))
        want = QtCore.QRect(top, m.size())
        got = want.intersected(host.rect())
        if got.width() > 8 and got.height() > 8:
            try:
                back = frosted(host.grab(got), MENU_BLUR, MENU_SATURATE)
                at = got.topLeft() - want.topLeft()
            except Exception:  # noqa: BLE001
                back = None
    img = card_image(m.size(), m.devicePixelRatioF(), back, at, radius, MENU_TINT)
    _menus[id(m)] = (m.size(), img)
    return img


def menu_closed(m):
    _menus.pop(id(m), None)


_glass = None


def glass() -> Glass:
    global _glass
    if _glass is None:
        _glass = Glass(QtWidgets.QApplication.instance())
    return _glass


def skipped(dlg) -> bool:
    if str(dlg.property("chrome") or "") == "off":
        return True
    mo = dlg.metaObject()
    while mo is not None:
        if mo.className() in SKIP:
            return True
        mo = mo.superClass()
    return False


def _plain(text: str) -> str:
    import re
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(text or ""))).strip()


def own_heading(dlg, layout):
    """The dialog's own first line when it says what the window title says ('Create alert on NIFTY' under a title
    'Create alert'): -> (label, its text), so the header can carry it and the line is not there twice."""
    title = _plain(dlg.windowTitle()).lower()
    if not title:
        return None
    found: list = []

    def look(lay, depth):
        for i in range(min(lay.count(), 2)):
            it = lay.itemAt(i)
            w = it.widget()
            if isinstance(w, QtWidgets.QLabel):
                found.append(w)
            elif w is None and it.layout() is not None and depth < 2:
                look(it.layout(), depth + 1)
    look(layout, 0)
    import re
    for lab in found[:2]:
        raw = lab.text()
        parts = re.split(r"<br\s*/?>", raw, maxsplit=1, flags=re.I)      # a second line ("Last price ...") stays
        text = _plain(parts[0])
        low = text.lower()
        if low and len(text) <= 80 and (low == title or low.startswith(title)):
            return lab, text, (parts[1] if len(parts) > 1 and _plain(parts[1]) else "")
    return None


def _add_header(dlg):
    """Put the dialog's content under a header: the old layout moves, untouched, into a body widget."""
    old = dlg.layout()
    if old is None:
        return None
    twin = own_heading(dlg, old)
    body = QtWidgets.QWidget(dlg)
    body.setObjectName("chromeBody")
    body.setLayout(old)                                     # Qt hands the layout (and its widgets) over
    outer = QtWidgets.QVBoxLayout(dlg)
    outer.setContentsMargins(0, 0, 0, 0)
    outer.setSpacing(0)
    head = Header(dlg)
    if twin is not None:
        if twin[2]:
            twin[0].setText(twin[2])                        # its second line stays where it was
        else:
            twin[0].hide()
        head.title.setText(twin[1])
        try:
            dlg.windowTitleChanged.disconnect(head.title.setText)
        except (RuntimeError, TypeError):
            pass
    outer.addWidget(head)
    outer.addWidget(body, 1)
    lo, hi = dlg.minimumHeight(), dlg.maximumHeight()
    if hi < 16777215:
        dlg.setMaximumHeight(hi + HEADER)
    if lo > 0:
        dlg.setMinimumHeight(lo + HEADER)
    if dlg.testAttribute(QtCore.Qt.WidgetAttribute.WA_Resized):
        dlg.resize(dlg.width(), dlg.height() + HEADER)      # the content keeps the room it had
    if dlg.minimumSize() != dlg.maximumSize():              # it could be resized by its frame: now by its corner
        grip = QtWidgets.QSizeGrip(dlg)
        grip.setObjectName("chromeGrip")
        grip.resize(16, 16)
        dlg.installEventFilter(_GripKeeper(grip))
    return head


class _GripKeeper(QtCore.QObject):
    """Keeps the (invisible) resize grip in the card's bottom-right corner."""

    def __init__(self, grip):
        super().__init__(grip)
        self.grip = grip

    def eventFilter(self, dlg, ev):
        if ev.type() in (QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.Show):
            try:
                self.grip.move(dlg.width() - self.grip.width(), dlg.height() - self.grip.height())
                self.grip.raise_()
            except RuntimeError:
                pass
        return False


def dress(dlg) -> bool:
    """Turn a dialog into a card. Safe to call again; call it before the dialog is shown."""
    if not _on["v"] or not isinstance(dlg, QtWidgets.QDialog) or dlg.property("_chrome") or skipped(dlg):
        return False
    if dlg.isVisible():
        return False
    dlg.setProperty("_chrome", True)
    dlg.setProperty("_chromeOwnPlace", bool(dlg.testAttribute(QtCore.Qt.WidgetAttribute.WA_Moved)))
    own_frame = not bool(dlg.windowFlags() & QtCore.Qt.WindowType.FramelessWindowHint)
    plain = isinstance(dlg, QtWidgets.QMessageBox)          # it rebuilds its own layout: no header, just the card
    dlg.setWindowFlag(QtCore.Qt.WindowType.FramelessWindowHint, True)
    if dlg.property("chromeOverlay"):                       # (its card draws its own: the system's would trace the
        dlg.setWindowFlag(QtCore.Qt.WindowType.NoDropShadowWindowHint, True)          # whole window)
    dlg.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
    if plain:
        try:
            dlg.setOption(QtWidgets.QMessageBox.Option.DontUseNativeDialog, True)
        except Exception:  # noqa: BLE001 - an older Qt
            pass
    elif own_frame and not dlg.property("chromeNoHeader"):
        _add_header(dlg)
    for b in dlg.findChildren(QtWidgets.QPushButton):
        if not b.objectName() and _plain(b.text()).lower() in ("cancel", "close", "no"):
            b.setObjectName("tvSecondary")                  # TradingView's white-outline button
    dlg.setStyleSheet((dlg.styleSheet() or "") + QSS)
    dlg.installEventFilter(glass())
    return True


# ------------------------------------------------------------------ message boxes as cards
_SB = QtWidgets.QMessageBox.StandardButton
_NAMES = [(_SB.Ok, "OK"), (_SB.Open, "Open"), (_SB.Save, "Save"), (_SB.Cancel, "Cancel"), (_SB.Close, "Close"),
          (_SB.Discard, "Discard"), (_SB.Apply, "Apply"), (_SB.Reset, "Reset"), (_SB.Yes, "Yes"), (_SB.No, "No"),
          (_SB.Abort, "Abort"), (_SB.Retry, "Retry"), (_SB.Ignore, "Ignore")]
_QUIET = (_SB.Cancel, _SB.No, _SB.Close, _SB.Abort)        # what Esc means, in this order


class MessageCard(QtWidgets.QDialog):
    """A notice or a question: a bold title, the text, the buttons on the right (the main one white)."""

    def __init__(self, parent, title: str, text: str, buttons=_SB.Ok, default=_SB.NoButton):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setProperty("chromeNoHeader", True)
        self.setWindowFlag(QtCore.Qt.WindowType.FramelessWindowHint, True)
        self.setModal(True)
        self.clicked = None
        self.setStyleSheet(
            "QDialog{background:#131313;border:0;border-radius:20px;}"
            "QLabel{background:transparent;color:#b8b8b8;font-size:14px;}"
            "QLabel#cardTitle{color:#b8b8b8;font-size:20px;font-weight:600;}"
            "QPushButton{background:transparent;border:1px solid #ffffff;border-radius:8px;padding:0 11px;"
            "min-height:31px;color:#ffffff;font-size:14px;min-width:20px;}"
            "QPushButton:hover{background:#222222;}"
            "QPushButton#main{background:#f2f2f2;color:#000000;border-color:#f2f2f2;}"
            "QPushButton#main:hover{background:#dbdbdb;border-color:#dbdbdb;}")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(10)
        self.title_lbl = QtWidgets.QLabel(title)
        self.title_lbl.setObjectName("cardTitle")
        self.title_lbl.setWordWrap(True)
        self.title_lbl.setVisible(bool(title))
        lay.addWidget(self.title_lbl)
        self.text_lbl = QtWidgets.QLabel(text)
        self.text_lbl.setWordWrap(True)
        self.text_lbl.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextBrowserInteraction)
        self.text_lbl.setOpenExternalLinks(True)
        self.text_lbl.setMinimumWidth(330)
        self.text_lbl.setMaximumWidth(480)
        lay.addWidget(self.text_lbl)
        lay.addSpacing(8)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self._buttons: dict = {}
        have = [(sb, name) for sb, name in _NAMES if int(buttons.value if hasattr(buttons, "value") else buttons)
                & int(sb.value)]
        if not have:
            have = [(_SB.Ok, "OK")]
        loud = [x for x in have if x[0] not in _QUIET]
        quiet = [x for x in have if x[0] in _QUIET]
        main = default if default != _SB.NoButton else (loud[0][0] if loud else have[-1][0])
        for sb, name in quiet + loud:                       # the quiet answers first, the main one at the right edge
            b = QtWidgets.QPushButton(name)
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            b.setAutoDefault(False)
            if sb == main:
                b.setObjectName("main")
                b.setDefault(True)
            b.clicked.connect(lambda _=False, s=sb: self._pick(s))
            row.addWidget(b)
            self._buttons[sb] = b
        lay.addLayout(row)
        self._esc = next((sb for sb in _QUIET if sb in self._buttons), have[-1][0] if len(have) == 1 else _SB.NoButton)
        self._main = main

    # (the few QMessageBox calls other code and tests use)
    def button(self, sb):
        return self._buttons.get(sb)

    def buttons(self):
        return list(self._buttons.values())

    def text(self):
        return self.text_lbl.text()

    def _pick(self, sb):
        self.clicked = sb
        self.accept()

    def reject(self):
        if self.clicked is None:
            self.clicked = self._esc
        super().reject()

    def run(self):
        self.exec()
        return self.clicked if self.clicked is not None else self._esc


def _message(parent, title, text, buttons=_SB.Ok, default=_SB.NoButton):
    return MessageCard(parent, str(title or ""), str(text or ""), buttons, default).run()


def _question(parent, title, text, buttons=_SB.Yes | _SB.No, default=_SB.NoButton):
    return MessageCard(parent, str(title or ""), str(text or ""), buttons, default).run()


def _about(parent, title, text):
    MessageCard(parent, "", str(text or ""), _SB.Close).run()


class TextCard(QtWidgets.QDialog):
    """One line of text is asked for: title, the field, Cancel / OK."""

    def __init__(self, parent, title: str, label: str, text: str = "",
                 echo=QtWidgets.QLineEdit.EchoMode.Normal):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setProperty("chromeNoHeader", True)
        self.setWindowFlag(QtCore.Qt.WindowType.FramelessWindowHint, True)
        self.setModal(True)
        self.setStyleSheet(
            "QDialog{background:#131313;border:0;border-radius:20px;}"
            "QLabel{background:transparent;color:#b8b8b8;font-size:14px;}"
            "QLabel#cardTitle{color:#b8b8b8;font-size:20px;font-weight:600;}"
            "QLineEdit{background:transparent;border:1px solid #464646;border-radius:8px;padding:6px 10px;"
            "color:#dbdbdb;font-size:14px;selection-background-color:#2962ff;}"
            "QLineEdit:hover{border-color:#636363;} QLineEdit:focus{border-color:#2962ff;}"
            "QPushButton{background:transparent;border:1px solid #ffffff;border-radius:8px;padding:0 11px;"
            "min-height:31px;color:#ffffff;font-size:14px;min-width:20px;}"
            "QPushButton:hover{background:#222222;}"
            "QPushButton#main{background:#f2f2f2;color:#000000;border-color:#f2f2f2;}"
            "QPushButton#main:hover{background:#dbdbdb;border-color:#dbdbdb;}")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(10)
        t = QtWidgets.QLabel(title)
        t.setObjectName("cardTitle")
        t.setVisible(bool(title))
        lay.addWidget(t)
        shown = _plain(label).rstrip(":")
        if shown and shown.lower() != _plain(title).lower() and shown.lower() != "text":
            lay.addWidget(QtWidgets.QLabel(label))
        self.edit = QtWidgets.QLineEdit(text)
        self.edit.setEchoMode(echo)
        self.edit.setMinimumWidth(340)
        self.edit.selectAll()
        lay.addWidget(self.edit)
        lay.addSpacing(8)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        ok = QtWidgets.QPushButton("OK")
        ok.setObjectName("main")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        for b in (cancel, ok):
            b.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            row.addWidget(b)
        lay.addLayout(row)
        self.edit.setFocus()

    def textValue(self) -> str:
        return self.edit.text()


def _get_text(parent, title, label, echo=QtWidgets.QLineEdit.EchoMode.Normal, text="", *a, **k):
    d = TextCard(parent, str(title or ""), str(label or ""), str(text or ""), echo)
    ok = bool(d.exec())
    return d.textValue(), ok


# ------------------------------------------------------------------ switching it on
def _wrap(cls, name):
    orig = getattr(cls, name, None)
    if orig is None or (cls, name) in _orig:
        return
    _orig[(cls, name)] = orig

    def run(self, *a, **k):
        try:
            dress(self)
        except Exception:  # noqa: BLE001 - a dialog that cannot be dressed is still a dialog
            import logging
            logging.exception("chrome: dress %s", type(self).__name__)
        return orig(self, *a, **k)
    run.__name__ = name
    setattr(cls, name, run)


def install():
    """Dialogs opened from here on are cards. (Called once, when the app's style is made.)"""
    if _on["v"]:
        return
    _on["v"] = True
    W = QtWidgets
    for cls in (W.QDialog, W.QMessageBox, W.QInputDialog):
        for name in ("exec", "exec_", "open", "show"):
            if name in ("exec", "exec_", "open") or cls is W.QDialog:
                _wrap(cls, name)
    for name, fn in (("information", _message), ("warning", _message), ("critical", _message),
                     ("question", _question), ("about", _about)):
        if (W.QMessageBox, name) not in _orig:
            _orig[(W.QMessageBox, name)] = getattr(W.QMessageBox, name)
            setattr(W.QMessageBox, name, staticmethod(fn))
    if (W.QInputDialog, "getText") not in _orig:
        _orig[(W.QInputDialog, "getText")] = W.QInputDialog.getText
        W.QInputDialog.getText = staticmethod(_get_text)


def uninstall():
    """Put the toolkit's own dialogs back (tests)."""
    for (cls, name), orig in list(_orig.items()):
        setattr(cls, name, orig)
    _orig.clear()
    _on["v"] = False
