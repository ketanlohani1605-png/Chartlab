"""Self-update for the Mac app: no more download / unzip / drag / "Open Anyway".

Every build is published as a GitHub release. The app asks for the latest one, downloads the zip for this Mac
(Apple Silicon or Intel), checks that the new app really starts, swaps itself and opens again."""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

from . import __version__

REPO = "ketanlohani1605-png/Chartlab"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
UPDATES = Path.home() / "ChartLab" / "updates"


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
        return "Updates work in the Mac app (this copy runs from source code)."
    if "/AppTranslocation/" in str(b):
        return ("macOS is running ChartLab from a temporary place. Move ChartLab into the Applications folder, "
                "open it from there, then update.")
    if not os.access(b.parent, os.W_OK):
        return f"ChartLab cannot write to {b.parent}. Move the app to your Applications folder."
    return ""


def check(current: str = __version__, timeout: float = 12.0) -> dict | None:
    """-> {"version", "url", "size", "name"} when a newer build is published for this Mac, else None."""
    import requests
    r = requests.get(API, timeout=timeout, headers={"Accept": "application/vnd.github+json"})
    r.raise_for_status()
    rel = r.json()
    ver = str(rel.get("tag_name", "")).lstrip("vV")
    if not ver or parse(ver) <= parse(current):
        return None
    want = "mac-m1" if platform.machine().lower() in ("arm64", "aarch64") else "mac-intel"
    for a in rel.get("assets", []):
        if want in a.get("name", "") and a.get("name", "").endswith(".zip"):
            return {"version": ver, "url": a["browser_download_url"], "size": int(a.get("size", 0)), "name": a["name"]}
    return None


def download(info: dict, progress=None, cancelled=lambda: False) -> Path:
    import requests
    UPDATES.mkdir(parents=True, exist_ok=True)
    for old in UPDATES.iterdir():                      # keep the folder small: one update at a time
        if old.name != info["name"]:
            subprocess.run(["rm", "-rf", str(old)], check=False)
    out = UPDATES / info["name"]
    part = out.with_suffix(".part")
    got = 0
    with requests.get(info["url"], stream=True, timeout=30) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or info.get("size") or 0)
        with open(part, "wb") as f:
            for chunk in r.iter_content(1 << 18):
                if cancelled():
                    raise RuntimeError("cancelled")
                f.write(chunk)
                got += len(chunk)
                if progress:
                    progress(got, total)
    if info.get("size") and got != info["size"]:
        raise RuntimeError(f"download incomplete ({got:,} of {info['size']:,} bytes)")
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
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(app)], check=False)
    got = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=120).stdout.strip()
    if parse(got) != parse(version):
        raise RuntimeError(f"the downloaded app reports version '{got}', expected {version}")
    return app


SWAP = """#!/bin/sh
# waits for the running ChartLab to quit, puts the new app in its place, opens it
PID="$1"; APP="$2"; NEW="$3"
i=0
while kill -0 "$PID" 2>/dev/null && [ $i -lt 150 ]; do sleep 0.2; i=$((i+1)); done
rm -rf "$APP.old"
if mv "$APP" "$APP.old" && mv "$NEW" "$APP"; then
  xattr -dr com.apple.quarantine "$APP" 2>/dev/null
  rm -rf "$APP.old"
else
  [ -d "$APP" ] || mv "$APP.old" "$APP"
fi
open "$APP"
"""


def swap_and_restart(new_app: Path) -> None:
    """Start the tiny helper that replaces this app once it has quit. Call, then quit the app."""
    bundle = app_bundle()
    if bundle is None:
        raise RuntimeError(why_not())
    script = UPDATES / "swap.sh"
    script.write_text(SWAP)
    os.chmod(script, 0o755)
    subprocess.Popen(["/bin/sh", str(script), str(os.getpid()), str(bundle), str(new_app)],
                     start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)
