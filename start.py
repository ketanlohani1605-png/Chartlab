#!/usr/bin/env python3
"""ChartLab one-file launcher.

Just run:   python3 start.py        (or right-click -> Open With -> Python Launcher)

First run only: it creates a private environment (.venv next to this file) and installs
everything needed (needs internet, ~2-5 minutes). Every later run opens the app straight away.
Nothing is installed system-wide.
"""
import os
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
REQ = HERE / "requirements.txt"
STAMP = VENV / ".chartlab_installed"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def setup_and_relaunch():
    if not venv_python().exists():
        print("First run: creating a private environment (one time only)...", flush=True)
        venv.EnvBuilder(with_pip=True).create(VENV)
    if not STAMP.exists() or STAMP.stat().st_mtime < REQ.stat().st_mtime:
        print("Installing required packages (one time only, 2-5 minutes, needs internet)...", flush=True)
        py = str(venv_python())
        subprocess.check_call([py, "-m", "pip", "install", "--upgrade", "pip", "-q"])
        subprocess.check_call([py, "-m", "pip", "install", "-r", str(REQ)])
        STAMP.write_text("ok")
    cmd = [str(venv_python()), str(Path(__file__).resolve())] + sys.argv[1:]
    if os.name == "nt":
        sys.exit(subprocess.call(cmd))
    os.execv(cmd[0], cmd)


def main():
    if sys.version_info < (3, 9):
        sys.exit("ChartLab needs Python 3.9 or newer. Get it from https://www.python.org/downloads/")
    if Path(sys.prefix).resolve() != VENV.resolve():
        try:
            setup_and_relaunch()
        except subprocess.CalledProcessError:
            sys.exit("\nPackage install failed. Check your internet connection and run the same command again.")
        return

    sys.path.insert(0, str(HERE))
    if "--check" in sys.argv:  # headless self-test: imports everything, then exits
        import chartlab.mainwindow  # noqa: F401
        print("ChartLab environment OK")
        return
    from chartlab.mainwindow import main as run_app
    run_app()


if __name__ == "__main__":
    main()
