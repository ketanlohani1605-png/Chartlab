"""Python code editor for strategies: syntax highlighting, line numbers, current-line and error-line highlight,
smart indent (Enter / Tab / Shift+Tab / Backspace), Cmd+/ to toggle comments, Cmd+Enter to run."""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

_KEYWORDS = ("and as assert break class continue def del elif else except finally for from global if "
             "import in is lambda not or pass raise return try while with yield True False None nonlocal async "
             "await").split()
_BUILTINS = ("print len range enumerate zip min max abs sum round int float str bool list dict set tuple isinstance "
             "sorted reversed any all map filter open getattr setattr hasattr").split()

BG = "#0b0b0b"
GUTTER_BG = "#0b0b0b"
GUTTER_FG = "#4a4d55"
GUTTER_FG_CUR = "#b2b5be"
CURLINE = "#151515"
ERRLINE = "#3a1416"


def _fmt(color: str, bold=False, italic=False) -> QtGui.QTextCharFormat:
    f = QtGui.QTextCharFormat()
    f.setForeground(QtGui.QColor(color))
    if bold:
        f.setFontWeight(QtGui.QFont.Weight.Bold)
    f.setFontItalic(italic)
    return f


class PyHighlighter(QtGui.QSyntaxHighlighter):
    def __init__(self, doc):
        super().__init__(doc)
        R = QtCore.QRegularExpression
        self.rules = [
            (R(r"\b(" + "|".join(_KEYWORDS) + r")\b"), _fmt("#c678dd")),
            (R(r"\b(" + "|".join(_BUILTINS) + r")\b(?=\()"), _fmt("#61afef")),
            (R(r"\b(df|ta|np|pd|chartlab|self)\b"), _fmt("#56b6c2")),
            (R(r"(?<=\bdef )\w+"), _fmt("#61afef", bold=True)),
            (R(r"\b\d+(\.\d+)?\b"), _fmt("#d19a66")),
            (R(r"\"[^\"\n]*\"|'[^'\n]*'"), _fmt("#98c379")),
            (R(r"#[^\n]*"), _fmt("#5c6370", italic=True)),
        ]

    def highlightBlock(self, text: str):
        for rx, fmt in self.rules:
            it = rx.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), fmt)


class _Gutter(QtWidgets.QWidget):
    def __init__(self, ed):
        super().__init__(ed)
        self.ed = ed

    def sizeHint(self):
        return QtCore.QSize(self.ed.gutter_width(), 0)

    def paintEvent(self, ev):
        self.ed.paint_gutter(ev)


class CodeEditor(QtWidgets.QPlainTextEdit):
    runRequested = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
        for fam in ("Menlo", "SF Mono", "JetBrains Mono", "Consolas"):
            if fam in QtGui.QFontDatabase.families():
                font = QtGui.QFont(fam)
                break
        font.setPointSize(12)
        self.setFont(font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.setStyleSheet(f"QPlainTextEdit {{ background:{BG}; color:#d7dae0; border:0; "
                           f"selection-background-color:#264f78; }}")
        self.highlighter = PyHighlighter(self.document())
        self.error_line: int | None = None
        self._gutter = _Gutter(self)
        self.blockCountChanged.connect(lambda *_: self._update_margins())
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._highlight)
        self.textChanged.connect(self._text_changed)
        self._update_margins()
        self._highlight()

    # ------------------------------------------------------------------ gutter
    def gutter_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 14 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_margins(self):
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _update_gutter(self, rect, dy):
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        cr = self.contentsRect()
        self._gutter.setGeometry(QtCore.QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def paint_gutter(self, ev):
        p = QtGui.QPainter(self._gutter)
        p.fillRect(ev.rect(), QtGui.QColor(GUTTER_BG))
        block = self.firstVisibleBlock()
        n = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        cur = self.textCursor().blockNumber()
        w = self._gutter.width() - 8
        h = self.fontMetrics().height()
        while block.isValid() and top <= ev.rect().bottom():
            if block.isVisible() and bottom >= ev.rect().top():
                if self.error_line is not None and n + 1 == self.error_line:
                    p.setPen(QtGui.QColor("#f23645"))
                else:
                    p.setPen(QtGui.QColor(GUTTER_FG_CUR if n == cur else GUTTER_FG))
                p.drawText(0, top, w, h, QtCore.Qt.AlignmentFlag.AlignRight, str(n + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            n += 1

    # ------------------------------------------------------------------ highlights
    def _highlight(self):
        sels = []
        cur = QtWidgets.QTextEdit.ExtraSelection()
        cur.format.setBackground(QtGui.QColor(CURLINE))
        cur.format.setProperty(QtGui.QTextFormat.Property.FullWidthSelection, True)
        cur.cursor = self.textCursor()
        cur.cursor.clearSelection()
        sels.append(cur)
        if self.error_line is not None:
            blk = self.document().findBlockByNumber(self.error_line - 1)
            if blk.isValid():
                e = QtWidgets.QTextEdit.ExtraSelection()
                e.format.setBackground(QtGui.QColor(ERRLINE))
                e.format.setProperty(QtGui.QTextFormat.Property.FullWidthSelection, True)
                e.cursor = QtGui.QTextCursor(blk)
                sels.append(e)
        self.setExtraSelections(sels)
        self._gutter.update()

    def set_error_line(self, line: int | None):
        """Mark the line of the last error (1-based) in red; None clears it. Scrolls it into view."""
        self.error_line = line
        if line is not None:
            blk = self.document().findBlockByNumber(line - 1)
            if blk.isValid():
                c = QtGui.QTextCursor(blk)
                self.setTextCursor(c)
                self.centerCursor()
        self._highlight()

    def _text_changed(self):
        if self.error_line is not None:      # editing clears the old error mark
            self.error_line = None
            self._highlight()

    # ------------------------------------------------------------------ editing
    def _selected_blocks(self):
        c = self.textCursor()
        doc = self.document()
        a = doc.findBlock(c.selectionStart()).blockNumber()
        b = doc.findBlock(max(c.selectionEnd() - (1 if c.hasSelection() else 0), c.selectionStart())).blockNumber()
        return a, b

    def _edit_lines(self, fn):
        a, b = self._selected_blocks()
        c = self.textCursor()
        c.beginEditBlock()
        for n in range(a, b + 1):
            blk = self.document().findBlockByNumber(n)
            new = fn(blk.text())
            if new != blk.text():
                lc = QtGui.QTextCursor(blk)
                lc.movePosition(QtGui.QTextCursor.MoveOperation.EndOfBlock, QtGui.QTextCursor.MoveMode.KeepAnchor)
                lc.insertText(new)
        c.endEditBlock()

    def toggle_comment(self):
        a, b = self._selected_blocks()
        lines = [self.document().findBlockByNumber(n).text() for n in range(a, b + 1)]
        code = [ln for ln in lines if ln.strip()]
        uncomment = bool(code) and all(ln.lstrip().startswith("#") for ln in code)

        def fn(t):
            if not t.strip():
                return t
            ind = t[: len(t) - len(t.lstrip())]
            body = t.lstrip()
            if uncomment:
                return ind + (body[2:] if body.startswith("# ") else body[1:])
            return ind + "# " + body
        self._edit_lines(fn)

    def keyPressEvent(self, e: QtGui.QKeyEvent):
        K, M = QtCore.Qt.Key, QtCore.Qt.KeyboardModifier
        key, mods = e.key(), e.modifiers()
        ctrl = bool(mods & M.ControlModifier)
        if ctrl and key in (K.Key_Return, K.Key_Enter):
            self.runRequested.emit()
            return
        if ctrl and key == K.Key_Slash:
            self.toggle_comment()
            return
        c = self.textCursor()
        if key == K.Key_Backtab or (key == K.Key_Tab and mods & M.ShiftModifier):
            self._edit_lines(lambda t: t[min(4, len(t) - len(t.lstrip(" "))):])
            return
        if key == K.Key_Tab and not (mods & ~M.KeypadModifier):
            if c.hasSelection() and " " in c.selectedText():
                self._edit_lines(lambda t: "    " + t if t.strip() else t)
            else:
                col = c.positionInBlock()
                self.insertPlainText(" " * (4 - col % 4))
            return
        if key == K.Key_Backspace and not c.hasSelection() and not mods:
            before = c.block().text()[: c.positionInBlock()]
            if before and not before.strip(" ") and len(before) % 4 == 0:
                for _ in range(4):
                    c.deletePreviousChar()
                return
        if key in (K.Key_Return, K.Key_Enter) and not (mods & ~M.KeypadModifier):
            line = c.block().text()[: c.positionInBlock()]
            indent = line[: len(line) - len(line.lstrip())]
            stripped = line.strip()
            if stripped.endswith(":"):
                indent += "    "
            elif stripped in ("return", "pass", "break", "continue") or stripped.startswith(("return ", "raise ")):
                indent = indent[:-4] if len(indent) >= 4 else ""
            super().keyPressEvent(e)
            self.insertPlainText(indent)
            return
        super().keyPressEvent(e)
