"""Self-update for the Mac app, like TradingView's: it finds the new version by itself, fetches it in the background
and one click on 'Restart to update' puts it in place - no download / unzip / drag / "Open Anyway".

Every build is published as a GitHub release of this repository. The app asks for the latest one, downloads the zip
for this Mac (Apple Silicon or Intel), checks it against the SHA-256 GitHub lists for the file, checks that the new
app really starts and reports the expected version, swaps itself and opens again."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

from . import __version__

REPO = "ketanlohani1605-png/Chartlab"                  # where the code lives and is built
RELEASES = "ketanlohani1605-png/ChartLab-app"          # a public repository with nothing in it but the builds
SOURCES = (RELEASES, REPO)                              # both are asked; the newest build of either one wins.
# (So the code repository can be made private: the app then simply stops seeing it and updates from RELEASES.)
API = f"https://api.github.com/repos/{REPO}/releases/latest"
_found = {"repo": REPO}                                 # where the last check found a build
UPDATES = Path.home() / "ChartLab" / "updates"
PENDING = "pending.json"                 # written just before the swap, read by the app that starts afterwards
FAILED = "swap_failed.txt"               # the helper writes macOS's reason here when it could not swap


def parse(v: str) -> tuple:
    out = []
    for part in str(v).lstrip("vV").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out)


def app_bundle() -> Path | None:
    """…/ChartLab.app when running as the built Mac app, else None (running from source)."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    for p in Path(sys.executable).resolve().parents:
        if p.suffix == ".app":
            return p
    return None


def why_not() -> str:
    """'' when this copy can replace itself, else the reason in plain words."""
    b = app_bundle()
    if b is None:
        if sys.platform == "win32" and getattr(sys, "frozen", False):
            return (f"On Windows: download ChartLab-<version>-windows.zip from github.com/{_found['repo']}/releases, "
                    "unzip it and run ChartLab.exe (your charts and settings stay).")
        return "Updates work in the Mac app (this copy runs from source code)."
    if "/AppTranslocation/" in str(b):
        return ("macOS is running ChartLab from a temporary place. Move ChartLab into the Applications folder, "
                "open it from there, then update.")
    if not os.access(b.parent, os.W_OK):
        return f"ChartLab cannot write to {b.parent}. Move the app to your Applications folder."
    return ""


def _latest(repo: str, timeout: float):
    """The newest release of one repository, or None when it has none / does not exist / cannot be seen."""
    import requests
    r = requests.get(f"https://api.github.com/repos/{repo}/releases/latest", timeout=timeout,
                     headers={"Accept": "application/vnd.github+json"})
    if getattr(r, "status_code", 200) == 404:
        return None
    r.raise_for_status()
    return r.json()


def check(current: str = __version__, timeout: float = 12.0) -> dict | None:
    """-> {"version", "url", "size", "name"} when a newer build is published for this computer, else None."""
    if sys.platform == "win32":
        want = "windows"
    elif platform.machine().lower() in ("arm64", "aarch64"):
        want = "mac-m1"
    else:                                 # Intel Macs: there has been no Intel build since 0.34.2
        want = "mac-intel"
    best, errors, answered = None, [], 0
    for repo in SOURCES:
        try:
            rel = _latest(repo, timeout)
        except Exception as e:  # noqa: BLE001 - the other repository may still answer
            errors.append(e)
            continue
        if rel is None:
            continue
        answered += 1
        ver = str(rel.get("tag_name", "")).lstrip("vV")
        if not ver or parse(ver) <= parse(current) or (best is not None and parse(ver) <= parse(best["version"])):
            continue
        for a in rel.get("assets", []):
            if want in a.get("name", "") and a.get("name", "").endswith(".zip"):
                best = {"version": ver, "url": a["browser_download_url"], "size": int(a.get("size", 0)),
                        "name": a["name"], "digest": str(a.get("digest") or ""), "notes": str(rel.get("body") or ""),
                        "repo": repo}
                break
    if best is None and not answered and errors:
        raise errors[-1]                  # nobody could be asked (no internet): say so instead of "up to date"
    if best is not None:
        _found["repo"] = best["repo"]
    return best


def download(info: dict, progress=None, cancelled=lambda: False) -> Path:
    import requests
    UPDATES.mkdir(parents=True, exist_ok=True)
    keep = {info["name"], PENDING, FAILED}
    for old in UPDATES.iterdir():                      # keep the folder small: one update at a time
        if old.name not in keep:
            subprocess.run(["rm", "-rf", str(old)], check=False)
    out = UPDATES / info["name"]
    part = out.with_suffix(".part")
    got = 0
    sha = hashlib.sha256()
    with requests.get(info["url"], stream=True, timeout=30) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or info.get("size") or 0)
        with open(part, "wb") as f:
            for chunk in r.iter_content(1 << 18):
                if cancelled():
                    raise RuntimeError("cancelled")
                f.write(chunk)
                sha.update(chunk)
                got += len(chunk)
                if progress:
                    progress(got, total)
    if info.get("size") and got != info["size"]:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"download incomplete ({got:,} of {info['size']:,} bytes)")
    want = str(info.get("digest") or "")
    if want.startswith("sha256:") and sha.hexdigest() != want.split(":", 1)[1].strip().lower():
        part.unlink(missing_ok=True)                   # GitHub lists each file's SHA-256: this is not that file
        raise RuntimeError("the downloaded file does not match the release (SHA-256 differs)")
    os.replace(part, out)
    return out


def unpack(zip_path: Path, version: str) -> Path:
    """Unzip next to the download and prove the new app starts and is the expected version."""
    dest = zip_path.parent / f"new-{version}"
    subprocess.run(["rm", "-rf", str(dest)], check=False)
    subprocess.run(["ditto", "-x", "-k", str(zip_path), str(dest)], check=True, timeout=300)
    app = dest / "ChartLab.app"
    exe = app / "Contents" / "MacOS" / "ChartLab"
    if not exe.exists():
        raise RuntimeError("the downloaded zip has no ChartLab.app in it")
    got = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=120).stdout.strip()
    if parse(got) != parse(version):
        raise RuntimeError(f"the downloaded app reports version '{got}', expected {version}")
    return app


SWAP = """#!/bin/sh
# waits for the running ChartLab to quit, puts the new app in its place, opens it. When macOS does not allow the
# swap the old app is put back, the reason is left in $FAIL and the old app opens again (it tells the user).
PID="$1"; APP="$2"; NEW="$3"; FAIL="$4"
i=0
while kill -0 "$PID" 2>/dev/null && [ $i -lt 150 ]; do sleep 0.2; i=$((i+1)); done
rm -rf "$APP.old"
if mv "$APP" "$APP.old" 2>"$FAIL.err" && mv "$NEW" "$APP" 2>>"$FAIL.err"; then
  rm -rf "$APP.old" "$FAIL.err"
else
  [ -d "$APP" ] || mv "$APP.old" "$APP"
  mv "$FAIL.err" "$FAIL" 2>/dev/null || echo "could not replace $APP" > "$FAIL"
fi
open "$APP"
"""


def mark_pending(old: str, new: str, new_app: Path) -> None:
    """Remember what is being installed, so the app that opens next can say 'Updated to …' (or why not)."""
    UPDATES.mkdir(parents=True, exist_ok=True)
    (UPDATES / PENDING).write_text(json.dumps({"from": old, "to": new, "new_app": str(new_app),
                                               "time": time.time()}))


def take_pending(current: str = __version__) -> dict | None:
    """After a restart: {"from", "to", "ok", "reason", "new_app"} for the update just tried (once), else None."""
    p = UPDATES / PENDING
    if not p.exists():
        return None
    try:
        info = json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        info = {}
    p.unlink(missing_ok=True)
    f = UPDATES / FAILED
    reason = f.read_text(errors="replace").strip() if f.exists() else ""
    f.unlink(missing_ok=True)
    if time.time() - float(info.get("time", 0)) > 3 * 86400:     # an old leftover, not this start
        return None
    info["ok"] = parse(current) == parse(info.get("to", "0")) and not reason
    info["reason"] = reason
    return info


def swap_and_restart(new_app: Path, current: str = __version__, version: str = "") -> None:
    """Start the tiny helper that replaces this app once it has quit. Call, then quit the app."""
    bundle = app_bundle()
    if bundle is None:
        raise RuntimeError(why_not())
    script = UPDATES / "swap.sh"
    script.write_text(SWAP)
    os.chmod(script, 0o755)
    (UPDATES / FAILED).unlink(missing_ok=True)
    mark_pending(current, version, new_app)
    subprocess.Popen(["/bin/sh", str(script), str(os.getpid()), str(bundle), str(new_app), str(UPDATES / FAILED)],
                     start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)
