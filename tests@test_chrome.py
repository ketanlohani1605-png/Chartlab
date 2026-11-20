"""Every dialog is a glass card with TradingView's own measures (read from its live chart page): dark glass
rgba(19,19,19,0.85) over the blurred chart, 20 px corners, a hairline edge, a 60 px header with a 20 px title and a
close button; only the symbol search dims the window behind it; menus are glass too; message boxes are cards."""
import os
import sys
import tempfile

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["CHARTLAB_OFFLINE"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from PySide6 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

app = QtWidgets.QApplication([])
from chartlab import accounts, chrome, theme  # noqa: E402

app.setStyle(theme.make_style())
app.setStyleSheet(theme.STYLESHEET)
accounts.load_accounts = lambda path=None: []
from chartlab.mainwindow import MainWindow  # noqa: E402

# ---- the numbers are TradingView's
assert chrome.TINT == (19, 19, 19, 217) and chrome.RADIUS == 20 and chrome.BLUR == 15 and chrome.SATURATE == 0.75
assert chrome.EDGE == (140, 140, 140, 26) and chrome.SCRIM == (26, 26, 26, 102) and chrome.FADE_MS == 200
assert chrome.MENU_TINT == (19, 19, 19, 191) and chrome.MENU_BLUR == 11 and chrome.MENU_SATURATE == 0.25
assert chrome.MENU_RADIUS == 6 and chrome.HEADER == 60 and chrome.TITLE_PX == 20

win = MainWindow(QtCore.QSettings(os.path.join(tempfile.mkdtemp(), "t.ini"), QtCore.QSettings.Format.IniFormat))
win.resize(1440, 860)
win.show()
r = np.random.default_rng(3)
c = 24000 * np.exp(np.cumsum(r.normal(0.0004, 0.011, 300)))
df = pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99, "Close": c * 1.002, "Volume": 1e5},
                  index=pd.bdate_range(end="2026-10-30", periods=300))
win.active.set_dataframe(df, "NIFTY")
win.active.params.update(symbol="NIFTY", source="yahoo", interval="1d")
QtTest.QTest.qWait(150)
seen = {}


errors = []


def during(fn, wait=450):
    """Run fn(the modal dialog) while it is open, then close it. (A failed check inside is kept: Qt swallows
    exceptions raised in a timer.)"""
    def go():
        import traceback
        m = app.activeModalWidget()
        try:
            fn(m)
        except BaseException:  # noqa: BLE001
            errors.append(traceback.format_exc())
        finally:
            if m is not None and m.isVisible():
                m.reject()
    QtCore.QTimer.singleShot(wait, go)


def passed(what):
    assert not errors, what + " failed:\n" + "\n".join(errors)


def alpha(img, x, y):
    return QtGui.qAlpha(img.pixel(int(x * img.devicePixelRatio()), int(y * img.devicePixelRatio())))


# ---- Settings: had the system's title bar; now a card with its own header
def settings(m):
    seen["settings"] = True
    F = QtCore.Qt.WindowType.FramelessWindowHint
    assert m.windowFlags() & F and m.testAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
    head = m.findChild(chrome.Header)
    assert head is not None and head.height() == 60 and head.title.text() == "Settings"
    assert head.close_btn.size() == QtCore.QSize(33, 33)
    f = head.title.font()
    assert head.title.fontInfo().pixelSize() == 20 and f.weight() >= QtGui.QFont.Weight.DemiBold
    img = m.grab().toImage()
    w, h = m.width(), m.height()
    assert alpha(img, 1, 1) == 0 and alpha(img, w - 2, 1) == 0 and alpha(img, 1, h - 2) == 0, "corners are not round"
    assert alpha(img, w // 2, 2) == 255 and alpha(img, 30, 30) == 255                 # the card itself is solid
    diag = [alpha(img, k, k) for k in range(12)]
    assert diag[4] == 0 and diag[5] < 60 and diag[7] > 200, diag                      # a 20 px corner (its arc
    #                                                                                   crosses the diagonal at 5.9)
    px = QtGui.QColor(img.pixel(int(w * 0.75 * img.devicePixelRatio()), int(h * 0.6 * img.devicePixelRatio())))
    assert 12 <= px.red() <= 60 and 12 <= px.green() <= 60, px.name()                 # dark glass, not a grey box
    g = win.geometry()
    assert abs((m.x() + w / 2) - (g.x() + g.width() / 2)) <= 2                        # centred over the window
    assert win.findChild(chrome.Scrim, "chromeScrim") is None or not win.findChild(chrome.Scrim, "chromeScrim").isVisible()
    assert m.property("_chrome") and m.findChild(QtWidgets.QWidget, "chromeBody") is not None
    assert m.minimumHeight() == 560 + 60
    ok = [b for b in m.findChildren(QtWidgets.QPushButton) if b.text() == "Ok"][0]
    cancel = [b for b in m.findChildren(QtWidgets.QPushButton) if b.text() == "Cancel"][0]
    assert ok.mapTo(m, QtCore.QPoint(0, 0)).x() > cancel.mapTo(m, QtCore.QPoint(0, 0)).x()   # the main button right
    QtTest.QTest.mouseClick(head.close_btn, QtCore.Qt.MouseButton.LeftButton)        # the header's x closes it
    assert not m.isVisible()


during(settings)
win.open_settings(0)
assert seen.get("settings")
passed("settings")


# ---- the symbol search: TradingView's card (840 wide, 20 px corners), and the only dialog with a dimmed backdrop
def search(m):
    seen["search"] = type(m).__name__
    assert m.width() == 840 and m.property("chromeScrim") and m.findChild(chrome.Header) is None
    s = win.findChild(chrome.Scrim, "chromeScrim")
    assert s is not None and s.isVisible() and s.geometry() == win.rect()
    QtTest.QTest.qWait(260)
    assert abs(s._a - 102) < 1, s._a
    assert m.lst.itemDelegate().sizeHint(QtWidgets.QStyleOptionViewItem(), m.lst.model().index(0, 0)).height() == 58
    chips = m.chips
    assert chips[0].isChecked() and chips[0].height() == 28 and chips[1].x() - (chips[0].x() + chips[0].width()) == 4
    assert chips[0].mapTo(m, QtCore.QPoint(0, 0)) == QtCore.QPoint(20, 68)
    assert m.lst.mapTo(m, QtCore.QPoint(0, 0)).y() == 113 and m.edit.mapTo(m, QtCore.QPoint(0, 0)).x() == 52
    # it opened on the letter typed on the chart: that letter is the start of the name, the next keys follow it
    assert m.edit.text() == "N" and not m.edit.hasSelectedText() and m.edit.cursorPosition() == 1
    QtTest.QTest.keyClicks(m.edit, "if")
    assert m.edit.text() == "NIF", m.edit.text()
    m.edit.setText("")
    QtTest.QTest.keyClicks(m.edit, "rel")
    assert m.edit.text() == "REL" and m.clear_btn.isVisible() and not m.hint.isVisible()
    img = m.grab().toImage()
    assert alpha(img, 1, 1) == 0 and alpha(img, 4, 4) == 0 and alpha(img, 420, 3) == 255


during(search, wait=900)
win.open_symbol_search("N")
assert seen.get("search") == "SymbolSearch"
passed("symbol search")
QtTest.QTest.qWait(300)
s = win.findChild(chrome.Scrim, "chromeScrim")
assert s is not None and not s.isVisible() and s.users == 0, "the window stayed dimmed"


# ---- a dialog whose own first line repeats its title: the header carries it, once
def alert(m):
    seen["alert"] = True
    head = m.findChild(chrome.Header)
    assert head.title.text() == "Create alert on NIFTY", head.title.text()
    labels = [x.text() for x in m.findChildren(QtWidgets.QLabel) if x.isVisible() and "Create alert" in x.text()]
    assert labels == ["Create alert on NIFTY"], labels
    assert any(b.objectName() == "tvSecondary" and b.text() == "Cancel" for b in m.findChildren(QtWidgets.QPushButton))


from chartlab import alerts as alertmod  # noqa: E402

during(alert)
alertmod.AlertDialog(win, "NIFTY", float(c[-1]), float(c[-1])).exec()
assert seen.get("alert")
passed("alert")


# ---- wrapped text gets the height it needs: nothing in the auto-trade dialog is squeezed
def auto(m):
    seen["auto"] = True
    assert m.qty.height() >= 26 and m.product.height() >= 26, (m.qty.height(), m.product.height())
    assert m.height() >= m.layout().totalHeightForWidth(m.width())


from chartlab.autotrade import AutoTradeDialog  # noqa: E402

during(auto)
AutoTradeDialog(win, [{"name": "Paper", "broker": "paper"}], "NIFTY", "5m").exec()
assert seen.get("auto")
passed("auto-trade")

# ---- questions and notices are cards; they answer like the toolkit's own boxes
SB = QtWidgets.QMessageBox.StandardButton


def press(which):
    def go(m):
        seen["box"] = type(m).__name__
        assert isinstance(m, chrome.MessageCard) and m.property("_chrome")
        assert m.testAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        if which == "esc":
            QtTest.QTest.keyClick(m, QtCore.Qt.Key.Key_Escape)
        else:
            m.button(which).click()
    return go


during(press(SB.Yes))
assert QtWidgets.QMessageBox.question(win, "Reset", "Start again?") == SB.Yes and seen["box"] == "MessageCard"
during(press(SB.No))
assert QtWidgets.QMessageBox.question(win, "Reset", "Start again?") == SB.No
during(press("esc"))
assert QtWidgets.QMessageBox.question(win, "Reset", "Start again?") == SB.No          # Esc is the quiet answer
during(press(SB.Ok))
assert QtWidgets.QMessageBox.information(win, "Update", "Done.") == SB.Ok
during(press("esc"))
assert QtWidgets.QMessageBox.information(win, "Update", "Done.") == SB.Ok             # one button: Esc presses it


def typed(m):
    assert isinstance(m, chrome.TextCard)
    m.edit.setText("Breakout")
    m.accept()


during(typed)
assert QtWidgets.QInputDialog.getText(win, "Text note", "Text:") == ("Breakout", True)
during(lambda m: m.reject())
assert QtWidgets.QInputDialog.getText(win, "Text note", "Text:")[1] is False
passed("message cards")

# ---- menus are glass with 6 px corners; the frosted backdrop is a real blur with the colour taken down
m = QtWidgets.QMenu(win)
m.addAction("Copy price")
m.addAction("Paste")
m.popup(win.mapToGlobal(QtCore.QPoint(300, 300)))
QtTest.QTest.qWait(150)
assert m.testAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
img = m.grab().toImage()
assert alpha(img, 0.4, 0.4) < 140 and alpha(img, m.width() // 2, m.height() // 2) == 255, "menu corners are square"
m.close()
src = QtGui.QImage(240, 160, QtGui.QImage.Format.Format_RGB32)
src.fill(QtGui.QColor("#000000"))
p = QtGui.QPainter(src)
p.fillRect(100, 0, 40, 160, QtGui.QColor("#ff0000"))                              # one red bar on black
p.end()
out = chrome.frosted(QtGui.QPixmap.fromImage(src), 15.0, 0.75)
assert out.size() == src.size()
mid, far, near = QtGui.QColor(out.pixel(120, 80)), QtGui.QColor(out.pixel(10, 80)), QtGui.QColor(out.pixel(92, 80))
assert 60 < mid.red() < 200 and far.red() < 8 and 8 < near.red() < mid.red(), (mid.red(), near.red(), far.red())
assert mid.green() > 5 and mid.red() > mid.green() * 2          # saturate(0.75): the red is greyer, still red
total_in, total_out = 255 * 40 * 160, sum(QtGui.QColor(out.pixel(x, 80)).red() for x in range(240)) * 160
assert abs(total_out - total_in * (0.2126 + 0.75 * (1 - 0.2126))) / total_in < 0.12      # a blur keeps the light
card = chrome.card_image(QtCore.QSize(200, 120), 1.0, None)
assert QtGui.qAlpha(card.pixel(0, 0)) == 0 and QtGui.qAlpha(card.pixel(100, 60)) == 255
assert QtGui.QColor(card.pixel(100, 60)).name() == "#131313"
print("CHROME OK")
os._exit(0)
