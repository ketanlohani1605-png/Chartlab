"""Finding your way around:

- 'Keyboard shortcuts' (main menu, or Cmd/Ctrl+/) lists every shortcut, grouped (Chart, Price scale, Drawing
  tool, Trading, ... App), each key on its own cap (⌥ T on a Mac), searchable; a click beside the card closes it
- the ☰ main menu starts with ChartLab and its version, then Settings, Keyboard shortcuts, Quick search, Check
  for updates, then File / Strategy / View / Claude, then Quit
- View has the Market panel; Cmd/Ctrl+K knows the Market panel and the shortcuts sheet
- Cmd/Ctrl+K's quick search sits near the top, and a click beside it closes it
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["CHARTLAB_OFFLINE"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["HOME"] = tempfile.mkdtemp()

from PySide6 import QtCore, QtGui, QtTest, QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from chartlab import chrome, theme, tvui  # noqa: E402

theme.apply_pg_config()
app.setStyle(theme.make_style())
app.setStyleSheet(theme.STYLESHEET)
from chartlab.mainwindow import MainWindow  # noqa: E402


def pump(ms=40):
    QtTest.QTest.qWait(ms)


errors = []


def during(fn, wait=400):
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


# ---------------------------------------------------------------- keys on caps
assert tvui.keycaps("Alt+Shift+B") == ["Alt", "Shift", "B"] or sys.platform == "darwin"
assert tvui.keycaps("type a letter") == ["type a letter"] and tvui.keycaps("Esc") == ["Esc"]
assert tvui.keycaps("") == []
real_sc = theme.sc
real_platform = sys.platform
try:                                                          # a Mac writes them as symbols: one cap each
    sys.platform = "darwin"
    theme.sc = lambda k: {"Alt+T": "⌥T", "Ctrl+Shift+Z": "⇧⌘Z", "Ctrl+,": "⌘,",
                          "Ctrl+Return": "⌘↩"}.get(k, k)
    assert tvui.keycaps("Alt+T") == ["⌥", "T"]
    assert tvui.keycaps("Ctrl+Shift+Z") == ["⇧", "⌘", "Z"]
    assert tvui.keycaps("Ctrl+,") == ["⌘", ","]
    assert tvui.keycaps("Cmd+Enter") == ["⌘", "↩"], "Cmd+Enter is Ctrl+Return to Qt"
finally:
    sys.platform = real_platform
    theme.sc = real_sc

win = MainWindow(QtCore.QSettings(tempfile.mktemp(suffix=".ini"), QtCore.QSettings.Format.IniFormat))
win.resize(1411, 786)
win.show()
pump(200)

# ---------------------------------------------------------------- what Cmd/Ctrl+K and the sheet know
acts = win.quick_actions()
titles = [a[0] for a in acts]
assert "Market: NIFTY 50 movers & heatmap" in titles and "Keyboard shortcuts" in titles
assert not [a for a in acts if a[2].startswith("Cmd+")], "written so the Mac shows ⌘"
rows = tvui.ShortcutsDialog.collect(acts)
cats = list(dict.fromkeys(r[0] for r in rows))
assert cats[0] == "Chart" and cats[-1] == "App", cats
assert [r[0] for r in rows] == sorted((r[0] for r in rows), key=cats.index), "each group once"
names = [r[1] for r in rows]
assert len(names) == len(set(names)), "each shortcut once"
by = {r[1]: r[2] for r in rows}
assert by["Trend line"] == "Alt+T" and by["Undo"] == "Ctrl+Z" and by["Keyboard shortcuts"] == "Ctrl+/"
assert by["Buy at the cursor's price (1 share / 1 lot)"] == "Alt+Shift+B"
assert not [r for r in rows if not r[2]], "only things with a shortcut"
assert any(isinstance(s, QtGui.QShortcut) and s.key() == QtGui.QKeySequence("Ctrl+/")
           for s in win.findChildren(QtGui.QShortcut)), "Ctrl+/ opens it"


# ---------------------------------------------------------------- the sheet
def sheet(m):
    assert isinstance(m, tvui.ShortcutsDialog)
    card = chrome.glass().card_rect(m)
    assert card is not None and card.width() == 640 and m.size() == win.size()
    n = m.count
    assert n == len(rows) and m.lst.count() == n + len(cats), "a heading per group"
    head0 = m.lst.item(0)
    assert head0.data(tvui.ROLE + 2) == "Chart" and head0.flags() == QtCore.Qt.ItemFlag.NoItemFlags
    QtTest.QTest.keyClicks(m.edit, "fib")
    assert m.count == 1 and m.lst.item(1).data(tvui.ROLE) == "Fib retracement"
    assert m.lst.item(1).data(tvui.ROLE + 1) == tvui.keycaps("Alt+F")
    m.edit.setText("zzzz")
    assert m.count == 0 and m.empty.isVisible() and not m.lst.isVisible()
    m.edit.setText("")
    assert m.count == n and m.lst.isVisible()
    m.lst.viewport().grab()                                    # (the caps paint)
    # a click in the card keeps it, one beside it closes it
    QtTest.QTest.mouseClick(m, QtCore.Qt.MouseButton.LeftButton, pos=card.center())
    assert m.isVisible()
    QtTest.QTest.mouseClick(m, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(card.left() - 30, card.top() + 40))
    assert not m.isVisible(), "a click beside the card closes it"


during(sheet)
win.show_shortcuts()
passed("shortcuts sheet")


def sheet_x(m):
    QtTest.QTest.mouseClick(m.close_btn, QtCore.Qt.MouseButton.LeftButton)
    assert not m.isVisible()


during(sheet_x)
win.show_shortcuts()
passed("shortcuts x")

# ---------------------------------------------------------------- the main menu
hm = win.main_menu
assert win.menu_btn.menu() is hm
acts = hm.actions()
assert isinstance(acts[0], QtWidgets.QWidgetAction) and not acts[0].isEnabled()
head = acts[0].defaultWidget()
assert any(win.version in lab.text() and "ChartLab" in lab.text() for lab in head.findChildren(QtWidgets.QLabel))
texts = [a.text().split("\t")[0] for a in acts if not a.isSeparator() and not isinstance(a, QtWidgets.QWidgetAction)]
assert texts == ["Settings…", "Keyboard shortcuts", "Quick search", "Check for updates…", "What’s new", "File",
                 "Strategy", "View", "Claude", "Quit ChartLab"], texts
assert acts[1].isSeparator() and acts[-2].isSeparator()
opened = []
real = win.show_shortcuts
win.show_shortcuts = lambda: opened.append(1)
next(a for a in acts if a.text().startswith("Keyboard shortcuts")).trigger()
win.show_shortcuts = real
assert opened == [1], "the menu opens the sheet"

# View -> Market
view = next(a.menu() for a in acts if a.menu() is not None and a.text() == "View")
mk = next(a for a in view.actions() if a.text().startswith("Market"))
mk.trigger()
pump(200)
assert win.market_dock.isVisible()
win.show_right_panel(None)
pump(100)


# ---------------------------------------------------------------- quick search: near the top, closes on a click beside
def quick(m):
    assert isinstance(m, tvui.QuickSearch)
    card = chrome.glass().card_rect(m)
    assert card is not None and card.top() == 90 and abs(card.center().x() - win.width() / 2) <= 2, card
    QtTest.QTest.mouseClick(m, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(card.center().x(), card.bottom() + 60))
    assert not m.isVisible(), "a click under the card closes it"


during(quick)
win.quick_search()
passed("quick search")

# ---------------------------------------------------------------- what's new: once after an update, never on a
# first install, never twice
from chartlab import whatsnew  # noqa: E402

assert whatsnew.notes_for(win.version), "this version says what it brings"
from chartlab import icons as _icons  # noqa: E402
for ico, _h, _t in whatsnew.notes_for(win.version):
    assert ico in _icons._DRAWERS, ico
shown = []
real_show = win.show_whats_new
win.show_whats_new = lambda: shown.append(1)
st = win.settings
for k in ("seen_version", "geometry", "watchlist"):
    st.remove(k)
assert win.maybe_whats_new() is False and shown == [] and st.value("seen_version") == win.version, "first install"
st.setValue("seen_version", "0.36.0")
assert win.maybe_whats_new() is True and shown == [1], "after an update"
assert win.maybe_whats_new() is False and shown == [1], "only once"
st.remove("seen_version")
st.setValue("geometry", QtCore.QByteArray(b"x"))
assert win.maybe_whats_new() is True and shown == [1, 1], "an update from a version that did not remember"
win.show_whats_new = real_show


def card(m):
    assert isinstance(m, whatsnew.WhatsNew)
    c = chrome.glass().card_rect(m)
    assert c is not None and c.width() == 540 and m.property("chromeScrim")
    assert m.tag.text() == f"NEW IN {win.version}" and len(m.items) == len(whatsnew.notes_for(win.version))
    assert all(t.wordWrap() for _h, t in m.items)
    assert theme.sc("Ctrl+/") in m.items[-1][1].text(), "the keys as this computer writes them"
    QtTest.QTest.mouseClick(m.ok, QtCore.Qt.MouseButton.LeftButton)
    assert not m.isVisible()


during(card)
win.show_whats_new()
passed("what's new")
assert "What’s new in this version" in [a[0] for a in win.quick_actions()]

print("SHORTCUTS OK")
os._exit(0)
