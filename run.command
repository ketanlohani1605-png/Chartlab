#!/bin/bash
# Double-click this file on macOS (or run ./run.command) - sets up a virtualenv on first run, then starts ChartLab.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi
exec .venv/bin/python main.py
