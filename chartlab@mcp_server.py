"""MCP (Model Context Protocol) server for ChartLab - stdlib only, speaks JSON-RPC over stdin/stdout.

Claude Desktop starts it as:   <ChartLab binary> --mcp
It forwards every tool call to the running ChartLab app (local API, see api.py) and starts the app first
if it is not running.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_FILE = Path.home() / "ChartLab" / "api.json"
PANEL = {"type": "integer", "description": "Chart panel number, 1-4 (default: the active chart)"}


def _obj(props: dict | None = None, required: list | None = None) -> dict:
    return {"type": "object", "properties": props or {}, "required": required or []}


# (name, description, input schema)
TOOLS = [
    ("get_state", "Overview of the running ChartLab app: layout, every visible chart (symbol, timeframe, bars, last "
                  "price, indicators, drawings), watchlist and the available options. Call this first.", _obj()),
    ("load_data", "Load price data into a chart. Yahoo symbols: RELIANCE (NSE), BSE:TCS, NIFTY, BANKNIFTY, SENSEX, "
                 "US:AAPL, BTC-USD. source: 'yahoo', 'file' (give path to a CSV/Parquet) or a provider name.",
     _obj({"panel": PANEL, "symbol": {"type": "string"}, "interval": {"type": "string", "description": "1m 5m 15m 30m 1h 1d 1wk 1mo"},
           "range": {"type": "string", "description": "5d 1mo 3mo 6mo 1y 2y 5y 10y max"},
           "source": {"type": "string"}, "path": {"type": "string", "description": "file path when source='file'"}})),
    ("get_ohlcv", "Read the most recent candles (time, open, high, low, close, volume) of a chart.",
     _obj({"panel": PANEL, "last_n": {"type": "integer", "description": "1-1000, default 100"}})),
    ("set_indicator", "Turn a chart indicator on/off. Keys: sma20 sma50 sma200 ema20 ema50 bb rsi.",
     _obj({"panel": PANEL, "key": {"type": "string"}, "on": {"type": "boolean"}}, ["key"])),
    ("set_chart_style", "Chart style: candles, hollow, line or area.",
     _obj({"panel": PANEL, "style": {"type": "string"}}, ["style"])),
    ("set_layout", "Chart layout: '1' single, '2h' two side by side, '2v' two stacked, '4' grid of four.",
     _obj({"layout": {"type": "string"}}, ["layout"])),
    ("set_active_panel", "Select which chart the toolbar, strategy runs and drawing tools act on.",
     _obj({"panel": PANEL}, ["panel"])),
    ("get_strategy", "Read the Python strategy currently in the editor.", _obj()),
    ("set_strategy", "Replace the strategy code in the editor. It must define strategy(df) returning a dict with boolean "
                     "Series 'buy' and 'sell' (extra keys 'plot_<name>' are drawn on the chart). `ta`, `np`, `pd` are "
                     "available; ta has sma ema rsi macd bollinger atr highest lowest crossover crossunder.",
     _obj({"code": {"type": "string"}}, ["code"])),
    ("list_strategy_templates", "List the built-in strategy templates.", _obj()),
    ("load_strategy_template", "Put a built-in strategy template into the editor and return its code.",
     _obj({"name": {"type": "string"}}, ["name"])),
    ("run_backtest", "Run the editor strategy on a chart's data. Returns metrics (net profit, drawdown, Sharpe, win "
                     "rate, ...), the latest trades and any error. Signals fill at the next bar's open, long-only.",
     _obj({"panel": PANEL, "capital": {"type": "number"}, "fee_pct": {"type": "number", "description": "fee per side in %"},
           "max_trades": {"type": "integer"}})),
    ("get_log", "Read the app's log tab (strategy print() output and errors).",
     _obj({"last_n": {"type": "integer"}})),
    ("add_drawing", "Draw on a chart. type: trend, ray, hline, vline, rect, fib. points: [{time:'2026-03-02', price:2450}] - "
                    "hline needs 1 point (price), vline 1 point (time), others 2.",
     _obj({"panel": PANEL, "type": {"type": "string"},
           "points": {"type": "array", "items": _obj({"time": {"type": "string"}, "price": {"type": "number"}})},
           "color": {"type": "string", "description": "#rrggbb"}}, ["type", "points"])),
    ("list_drawings", "List drawings on a chart.", _obj({"panel": PANEL})),
    ("delete_drawing", "Delete one drawing by id.", _obj({"panel": PANEL, "id": {"type": "integer"}}, ["id"])),
    ("clear_drawings", "Remove all drawings from a chart.", _obj({"panel": PANEL})),
    ("watchlist_get", "Read the watchlist (and the last fetched quotes).", _obj()),
    ("watchlist_add", "Add a symbol to the watchlist.", _obj({"symbol": {"type": "string"}}, ["symbol"])),
    ("watchlist_remove", "Remove a symbol from the watchlist.", _obj({"symbol": {"type": "string"}}, ["symbol"])),
    ("screenshot", "Take a screenshot of the whole ChartLab window (target='window') or of one chart (target='chart').",
     _obj({"target": {"type": "string"}, "panel": PANEL})),
]
TOOL_NAMES = {t[0] for t in TOOLS}


# --------------------------------------------------------------------------- talking to the app
def _read_api():
    try:
        return json.loads(API_FILE.read_text())
    except Exception:  # noqa: BLE001
        return None


def _ping(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2) as r:
            return json.loads(r.read()).get("app") == "ChartLab"
    except Exception:  # noqa: BLE001
        return False


def _app_alive():
    info = _read_api()
    return info if info and _ping(info["port"]) else None


def _launch_app():
    exe = Path(sys.executable)
    if getattr(sys, "frozen", False) and sys.platform == "darwin" and "Contents/MacOS" in str(exe):
        bundle = exe.parents[2]
        subprocess.Popen(["open", str(bundle)])
    elif getattr(sys, "frozen", False):
        subprocess.Popen([str(exe)], start_new_session=True)
    else:
        main_py = Path(__file__).resolve().parent.parent / "main.py"
        subprocess.Popen([sys.executable, str(main_py)], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_app(wait: float = 40.0):
    info = _app_alive()
    if info:
        return info
    _launch_app()
    end = time.time() + wait
    while time.time() < end:
        time.sleep(0.7)
        info = _app_alive()
        if info:
            return info
    raise RuntimeError("ChartLab is not running and could not be started. Open the ChartLab app and try again.")


def rpc(method: str, params: dict, timeout: float = 120.0):
    info = ensure_app()
    req = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}/rpc",
        data=json.dumps({"method": method, "params": params}).encode(),
        headers={"Content-Type": "application/json", "X-ChartLab-Token": info["token"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach ChartLab: {e}") from e
    if not out.get("ok"):
        raise RuntimeError(out.get("error", "unknown error"))
    return out["result"]


# --------------------------------------------------------------------------- MCP protocol
def _tool_result(name: str, result) -> dict:
    if name == "screenshot":
        return {"content": [{"type": "image", "data": result["png_base64"], "mimeType": "image/png"},
                            {"type": "text", "text": f"Screenshot {result['width']}x{result['height']}"}]}
    return {"content": [{"type": "text", "text": json.dumps(result, indent=1, ensure_ascii=False)}]}


def handle(msg: dict):
    """Return a response dict for a request, or None for notifications."""
    mid, method = msg.get("id"), msg.get("method")
    params = msg.get("params") or {}
    if method is None:
        return None
    if mid is None:  # notification
        return None
    try:
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "chartlab", "version": "1.0.0"},
                      "instructions": "Controls the ChartLab desktop charting/backtesting app. Start with get_state."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s in TOOLS]}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in TOOL_NAMES:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
            try:
                result = _tool_result(name, rpc(name, args))
            except Exception as e:  # noqa: BLE001 - tool errors go back as isError results
                result = {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    except Exception as e:  # noqa: BLE001
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve():
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            out.write(json.dumps(resp) + "\n")
            out.flush()


if __name__ == "__main__":
    serve()
