"""In-app update like TradingView's: found by itself, fetched + checked (size, SHA-256, version) in the background,
then one click on 'Restart to update'; after the restart the app says 'Updated to …' or why macOS kept the old one."""
import hashlib
import http.server
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from PySide6 import QtCore, QtTest, QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from chartlab import theme, updater  # noqa: E402

theme.apply_pg_config()
app.setStyle("Fusion")
app.setStyleSheet(theme.STYLESHEET)
tmp = Path(tempfile.mkdtemp())
updater.UPDATES = tmp / "updates"

# ---- a local web server standing in for GitHub's release file
payload = os.urandom(300_000)
srv_dir = tmp / "srv"
srv_dir.mkdir()
(srv_dir / "ChartLab-9.9.9-mac-m1.zip").write_bytes(payload)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(srv_dir), **k)

    def log_message(self, *a):
        pass


httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{httpd.server_address[1]}/ChartLab-9.9.9-mac-m1.zip"

# ---- check(): the newest release, the zip for this Mac, its SHA-256 from GitHub
import requests  # noqa: E402

real_get = requests.get


class FakeResp:
    def __init__(self, js):
        self._js = js

    def raise_for_status(self):
        pass

    def json(self):
        return self._js


digest = "sha256:" + hashlib.sha256(payload).hexdigest()
release = {"tag_name": "v9.9.9", "body": "notes", "assets": [
    {"name": "ChartLab-9.9.9-mac-intel.zip", "browser_download_url": url, "size": 1, "digest": "sha256:00"},
    {"name": "ChartLab-9.9.9-mac-m1.zip", "browser_download_url": url, "size": len(payload), "digest": digest}]}
requests.get = lambda u, **k: FakeResp(release) if "api.github.com" in u else real_get(u, **k)
import platform  # noqa: E402

platform.machine = lambda: "arm64"
info = updater.check("0.32.0")
assert info and info["version"] == "9.9.9" and info["name"].endswith("mac-m1.zip") and info["digest"] == digest
assert updater.check("9.9.9") is None and updater.check("10.0") is None
# the code repository can be private: the app then updates from the public builds-only repository
asked = []


class Missing(FakeResp):
    status_code = 404


def two(u, **k):
    asked.append(u)
    if updater.REPO in u:
        return Missing({})                                    # private: GitHub says "not found"
    if updater.RELEASES in u:
        return FakeResp(release)
    return real_get(u, **k)


requests.get = two
info = updater.check("0.32.0")
assert info and info["version"] == "9.9.9" and info["repo"] == updater.RELEASES and len(asked) == 2, (info, asked)
assert updater.RELEASES in updater.why_not() or sys.platform != "win32"
older = dict(release, tag_name="v9.9.8")
requests.get = lambda u, **k: FakeResp(older if updater.RELEASES in u else release) if "api.github.com" in u else real_get(u, **k)
assert updater.check("0.32.0")["repo"] == updater.REPO        # the newest build of either one wins
requests.get = lambda u, **k: Missing({}) if "api.github.com" in u else real_get(u, **k)
assert updater.check("0.32.0") is None                        # neither has a release: nothing to do, no error


def down(u, **k):
    raise ConnectionError("no internet")


requests.get = down
try:
    updater.check("0.32.0")
    raise SystemExit("no internet was reported as 'up to date'")
except ConnectionError:
    pass
requests.get = real_get

# ---- download(): size + SHA-256 must match, a wrong file is thrown away
seen = []
z = updater.download(info, progress=lambda g, t: seen.append((g, t)))
assert z.read_bytes() == payload and seen[-1][0] == len(payload)
bad = dict(info, digest="sha256:" + "0" * 64, name="ChartLab-9.9.9-mac-m1-x.zip")
try:
    updater.download(bad)
    raise AssertionError("a file with the wrong SHA-256 was accepted")
except RuntimeError as e:
    assert "SHA-256" in str(e)
assert not (updater.UPDATES / "ChartLab-9.9.9-mac-m1-x.zip").exists()
assert not (updater.UPDATES / "ChartLab-9.9.9-mac-m1-x.part").exists()

# ---- after a restart: 'Updated to …' once, or the reason the swap failed
updater.mark_pending("0.31.0", "0.32.0", tmp / "new.app")
r = updater.take_pending("0.32.0")
assert r and r["ok"] and updater.take_pending("0.32.0") is None
updater.mark_pending("0.31.0", "0.32.0", tmp / "new.app")
(updater.UPDATES / updater.FAILED).write_text("mv: rename: Operation not permitted")
r = updater.take_pending("0.31.0")
assert r and not r["ok"] and "not permitted" in r["reason"] and not (updater.UPDATES / updater.FAILED).exists()

# ---- the window: found -> fetched quietly -> 'Restart to update' -> restart
from chartlab.mainwindow import MainWindow, quit_cleanly  # noqa: E402

w = MainWindow(QtCore.QSettings(str(tmp / "t.ini"), QtCore.QSettings.Format.IniFormat))
w.resize(1600, 878)
w.show()
QtTest.QTest.qWait(150)
assert not w._upd_action.isVisible()
fetched = []
w._fetch_update = lambda inf, quiet: fetched.append((inf["version"], quiet))
w._update_checked(info, None)
assert fetched == [("9.9.9", True)] and not w._upd_action.isVisible()      # quiet: nothing shows while fetching
w._upd_info = info
w._update_downloaded(tmp / "new.app", None)
assert w._upd_action.isVisible() and w.upd_btn.text() == "Restart to update" and w.upd_btn.isEnabled()
(tmp / "new.app").mkdir()
calls = []
updater.why_not = lambda: ""
updater.swap_and_restart = lambda p, cur, ver: calls.append((str(p), cur, ver))
QtCore.QTimer.singleShot = lambda *a, **k: None                              # (do not really quit the test)
w.start_update()
assert calls == [(str(tmp / "new.app"), w.version, "9.9.9")] and w.upd_btn.text() == "Restarting…"
# a failed background fetch leaves a normal 'Update to …' button that fetches again with progress
w._update_downloaded(None, "network down")
assert w.upd_btn.text() == "Update to 9.9.9" and w.upd_btn.isEnabled()
updater.mark_pending("0.31.0", w.version, tmp / "new.app")
w.report_last_update()
assert w.statusBar().currentMessage().startswith("Updated to ChartLab")
httpd.shutdown()
quit_cleanly(w, 0, exit_process=False)
print("UPDATE OK")
os._exit(0)
