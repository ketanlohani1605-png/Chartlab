"""'Connect to Claude' dialog: shows / installs the MCP config for Claude Desktop."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6 import QtGui, QtWidgets

from . import theme

CONFIG = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def server_entry() -> dict:
    if getattr(sys, "frozen", False):
        return {"command": sys.executable, "args": ["--mcp"]}
    main_py = Path(__file__).resolve().parent.parent / "main.py"
    return {"command": sys.executable, "args": [str(main_py), "--mcp"]}


def merged_config(existing: dict | None) -> dict:
    cfg = dict(existing or {})
    servers = dict(cfg.get("mcpServers") or {})
    servers["chartlab"] = server_entry()
    cfg["mcpServers"] = servers
    return cfg


class ConnectDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect ChartLab to Claude")
        self.resize(720, 460)
        v = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "<b>Let Claude control this app</b> (load symbols, write &amp; run strategies, draw on charts, take "
            "screenshots).<br><br>"
            "1. Click <b>Add to Claude Desktop</b> (it edits Claude's config file, a backup is kept).<br>"
            "2. Quit and reopen <b>Claude Desktop</b>.<br>"
            "3. ChartLab starts automatically whenever Claude needs it.<br><br>"
            "<span style='color:#868993'>If macOS blocks it, run once in Terminal: "
            "<code>xattr -dr com.apple.quarantine /Applications/ChartLab.app</code></span><br><br>"
            "<span style='color:#868993'>Only programs on this computer can reach the link, and each run uses a "
            "private token. Claude can edit and run the strategy in the editor, nothing else on your Mac.</span>")
        intro.setWordWrap(True)
        v.addWidget(intro)
        self.text = QtWidgets.QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlainText(json.dumps({"mcpServers": {"chartlab": server_entry()}}, indent=2))
        v.addWidget(self.text, 1)
        self.note = QtWidgets.QLabel("")
        self.note.setWordWrap(True)
        v.addWidget(self.note)
        row = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton("Add to Claude Desktop")
        add.setObjectName("primary")
        add.clicked.connect(self.install)
        copy = QtWidgets.QPushButton("Copy config")
        copy.clicked.connect(lambda: QtGui.QGuiApplication.clipboard().setText(self.text.toPlainText()))
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(add)
        row.addWidget(copy)
        row.addStretch(1)
        row.addWidget(close)
        v.addLayout(row)

    def install(self):
        try:
            existing = {}
            if CONFIG.exists():
                raw = CONFIG.read_text().strip()
                existing = json.loads(raw) if raw else {}
                CONFIG.with_suffix(".json.chartlab-backup").write_text(raw)
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps(merged_config(existing), indent=2))
        except Exception as e:  # noqa: BLE001
            self.note.setText(f"<span style='color:{theme.DOWN}'>Could not update the config: {e}. "
                              f"Use <b>Copy config</b> and paste it into Claude Desktop's settings (Developer → Edit config).</span>")
            return
        self.note.setText(f"<span style='color:{theme.UP}'>Done. Now quit and reopen Claude Desktop.</span>")
