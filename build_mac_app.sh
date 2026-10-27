#!/bin/bash
# Builds a double-clickable ChartLab.app into ./dist  (run on your Mac, after run.command has created .venv)
cd "$(dirname "$0")"
[ -d .venv ] || { echo "Run ./run.command once first to create the virtualenv."; exit 1; }
.venv/bin/pip install pyinstaller
.venv/bin/pyinstaller --noconfirm --windowed --name ChartLab \
  --add-data "strategies:strategies" \
  --add-data "providers:providers" \
  --collect-submodules yfinance \
  main.py
echo "Done -> dist/ChartLab.app   (drag it to /Applications)"
