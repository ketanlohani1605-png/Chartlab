"""Small Python code editor with syntax highlighting and auto-indent."""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

_KEYWORDS = ("and as assert break class continue def del elif else except finally for from global if "
             "import in is lambda not or pass raise return try while with yield True False None").split()


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
            (R(r"\b(" + "|".join(_KEYWORDS) + r")\b"), _fmt("#c678dd", bold=True)),
            (R(r"\b(df|ta|np|pd|self)\b"), _fmt("#56b6c2")),
            (R(r"\b\d+(\.\d+)?\b"), _fmt("#d19a66")),
            (R(r"\"[^\"\n]*\"|'[^'\n]*'"), _fmt("#98c379")),
            (R(r"#[^\n]*"), _fmt("#7f848e", italic=True)),
        ]

    def highlightBlock(self, text: str):
        for rx, fmt in self.rules:
            it = rx.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), fmt)


class CodeEditor(QtWidgets.QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(12)
        self.setFont(font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        self.highlighter = PyHighlighter(self.document())

    def keyPressEvent(self, e: QtGui.QKeyEvent):
        key, mods = e.key(), e.modifiers()
        if key == QtCore.Qt.Key.Key_Tab and mods == QtCore.Qt.KeyboardModifier.NoModifier:
            self.insertPlainText("    ")
            return
        if key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter) and mods == QtCore.Qt.KeyboardModifier.NoModifier:
            line = self.textCursor().block().text()
            indent = line[: len(line) - len(line.lstrip())]
            if line.rstrip().endswith(":"):
                indent += "    "
            super().keyPressEvent(e)
            self.insertPlainText(indent)
            return
        super().keyPressEvent(e)
