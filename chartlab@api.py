"""Local control API: lets Claude (through the MCP server in mcp_server.py) drive the running app.

Security: listens on 127.0.0.1 only, every call needs a random per-run token that is stored in
~/ChartLab/api.json (readable only by you), and requests with a foreign Host header are refused.
There is deliberately no "run arbitrary code" call - Claude can edit/run the *strategy* in the editor.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import threading
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pandas as pd
from PySide6 import QtCore

from . import __version__
from . import data as datamod
from .chart import INDICATOR_KEYS, STYLES
from .drawings import TOOLS

API_FILE = datamod.USER_DIR / "api.json"
EPOCH = pd.Timestamp("1970-01-01")


# --------------------------------------------------------------------------- helpers
def _clean(o):
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if (np.isnan(f) or np.isinf(f)) else round(f, 6)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, pd.Timestamp):
        return o.isoformat()
    return o


def _to_epoch(t) -> float:
    if isinstance(t, (int, float)):
        return float(t)
    ts = pd.Timestamp(str(t))
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return (ts - EPOCH).total_seconds()


def _from_epoch(sec: float) -> str:
    return pd.Timestamp(sec, unit="s").isoformat()


class _Bridge(QtCore.QObject):
    """Runs callables on the Qt main thread on behalf of HTTP worker threads."""
    request = QtCore.Signal(object)

    def __init__(self):
        super().__init__()
        self.request.connect(self._run)

    @QtCore.Slot(object)
    def _run(self, job):
        fn, fut = job
        try:
            fut.set_result(fn())
        except BaseException as e:  # noqa: BLE001
            fut.set_exception(e)


# --------------------------------------------------------------------------- method implementations (main thread)
def _panel(win, p):
    n = p.get("panel")
    if n is None:
        return win.active
    n = int(n)
    visible = [pn for pn in win.panels if pn.isVisible()]
    if not 1 <= n <= len(win.panels) or win.panels[n - 1] not in visible:
        raise ValueError(f"panel {n} is not visible (layout '{win.layout_name}' shows panels 1-{len(visible)})")
    return win.panels[n - 1]


def _panel_info(win, pn) -> dict:
    info = {"panel": win.panels.index(pn) + 1, "active": pn is win.active, "loading": pn.loading,
            "source": pn.params["source"], "symbol": pn.params["symbol"], "interval": pn.params["interval"],
            "range": pn.params["range"], "label": pn.label, "error": pn.last_error or None,
            "style": pn.chart.style, "indicators": sorted(pn.chart._enabled),
            "drawings": len(pn.chart.layer.drawings), "has_backtest": pn.result is not None, "bars": 0}
    if pn.df is not None:
        df = pn.df
        r = df.iloc[-1]
        info.update(bars=len(df), first=df.index[0].isoformat(), last=df.index[-1].isoformat(),
                    last_bar={"open": r.Open, "high": r.High, "low": r.Low, "close": r.Close, "volume": r.Volume})
    return info


def m_get_state(win, p):
    return {"app": "ChartLab", "version": __version__, "layout": win.layout_name,
            "active_panel": win.panels.index(win.active) + 1,
            "panels": [_panel_info(win, pn) for pn in win.panels if pn.isVisible()],
            "watchlist": win.watchlist.symbols(), "strategy_length": len(win.strategy_code()),
            "capital": win.capital.value(), "fee_pct": win.fee.value(),
            "indicators": INDICATOR_KEYS, "chart_styles": STYLES, "drawing_tools": list(TOOLS),
            "data_sources": ["yahoo", "file", *[f"provider:{n}" for n in datamod.list_providers()]]}


def _start_load(win, p):
    pn = _panel(win, p)
    cur = pn.params
    source = p.get("source") or (cur["source"] if cur["source"] != "file" else "yahoo")
    if source in datamod.list_providers():
        source = f"provider:{source}"
    symbol = p.get("symbol") or cur["symbol"]
    interval = p.get("interval") or cur["interval"]
    rng = p.get("range") or cur["range"]
    path = p.get("path") or ""
    if source == "file":
        if not path:
            raise ValueError("source 'file' needs a 'path'")
        symbol = symbol if p.get("symbol") else os.path.splitext(os.path.basename(path))[0]
    if interval not in ("1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo"):
        raise ValueError(f"unsupported interval '{interval}'")
    win.load_into(pn, source, symbol, interval, rng, path)
    return pn


def m_get_ohlcv(win, p):
    pn = _panel(win, p)
    if pn.df is None:
        raise ValueError("no data loaded in this chart")
    n = max(1, min(int(p.get("last_n", 100)), 1000))
    df = pn.df.tail(n)
    rows = [[t.isoformat(), r.Open, r.High, r.Low, r.Close, r.Volume] for t, r in zip(df.index, df.itertuples())]
    return {"panel": win.panels.index(pn) + 1, "symbol": pn.label, "columns": ["time", "open", "high", "low", "close", "volume"],
            "rows": rows, "total_bars": len(pn.df)}


def m_set_indicator(win, p):
    key = p.get("key")
    if key not in INDICATOR_KEYS:
        raise ValueError(f"key must be one of {INDICATOR_KEYS}")
    pn = _panel(win, p)
    pn.chart.set_indicator(key, bool(p.get("on", True)))
    if pn is win.active:
        win._sync_toolbar()
    return _panel_info(win, pn)


def m_set_chart_style(win, p):
    style = p.get("style")
    if style not in STYLES:
        raise ValueError(f"style must be one of {STYLES}")
    pn = _panel(win, p)
    pn.chart.set_style(style)
    if pn is win.active:
        win._sync_toolbar()
    return _panel_info(win, pn)


def m_set_layout(win, p):
    layout = p.get("layout")
    if layout not in ("1", "2h", "2v", "4"):
        raise ValueError("layout must be one of '1', '2h', '2v', '4'")
    win.set_layout(layout)
    return m_get_state(win, {})


def m_set_active_panel(win, p):
    win.set_active(_panel(win, {"panel": p.get("panel", 1)}))
    return m_get_state(win, {})


def m_get_strategy(win, p):
    return {"code": win.strategy_code()}


def m_set_strategy(win, p):
    code = p.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("'code' must be a non-empty string")
    if len(code) > 200_000:
        raise ValueError("strategy too long")
    win.set_strategy_code(code)
    return {"ok": True, "length": len(code)}


def m_list_strategy_templates(win, p):
    d = datamod.ROOT / "strategies"
    return {"templates": sorted(f.stem for f in d.glob("*.py"))} if d.exists() else {"templates": []}


def m_load_strategy_template(win, p):
    f = datamod.ROOT / "strategies" / f"{os.path.basename(str(p.get('name', '')))}.py"
    if not f.exists():
        raise ValueError("unknown template; call list_strategy_templates")
    code = f.read_text()
    win.set_strategy_code(code)
    return {"code": code}


def m_run_backtest(win, p):
    pn = _panel(win, p)
    cap = float(p["capital"]) if p.get("capital") is not None else None
    fee = float(p["fee_pct"]) if p.get("fee_pct") is not None else None
    if cap is not None:
        win.capital.setValue(cap)
    if fee is not None:
        win.fee.setValue(fee)
    out = win.run_backtest_on(pn, cap, fee)
    if not out["ok"]:
        return {"ok": False, "error": out["error"][-1800:], "log": out.get("log", "")[-1500:]}
    res = out["result"]
    mx = max(1, min(int(p.get("max_trades", 50)), 500))
    t = res.trades
    shown = t.tail(mx)
    trades = [{"entry_time": r.entry_time.isoformat(), "exit_time": r.exit_time.isoformat(),
               "entry_price": r.entry_price, "exit_price": r.exit_price, "pnl": r.pnl,
               "pnl_pct": round(r.pnl_pct * 100, 3), "bars": r.bars, "open": bool(r.open)}
              for r in shown.itertuples()]
    return {"ok": True, "panel": win.panels.index(pn) + 1, "symbol": pn.label, "metrics": res.metrics,
            "trades_total": len(t), "trades_shown": len(trades), "trades": trades,
            "plots": list(pn.signals.overlays) if pn.signals else [], "log": out.get("log", "")[-1500:]}


def m_get_log(win, p):
    n = max(1, min(int(p.get("last_n", 40)), 500))
    return {"log": "\n".join(win.log_view.toPlainText().splitlines()[-n:])}


def m_add_drawing(win, p):
    pn = _panel(win, p)
    dtype = p.get("type")
    if dtype not in TOOLS:
        raise ValueError(f"type must be one of {list(TOOLS)}")
    pts = [[_to_epoch(q["time"]) if "time" in q else pn.chart.x_to_time(float(q["bar_index"])), float(q["price"])]
           for q in p.get("points", [])]
    did = pn.chart.layer.add(dtype, pts, p.get("color") or "#2962ff")
    return {"ok": True, "id": did, "panel": win.panels.index(pn) + 1}


def m_list_drawings(win, p):
    pn = _panel(win, p)
    return {"drawings": [{"id": d["id"], "type": d["type"], "color": d["color"],
                          "points": [{"time": _from_epoch(t), "price": pr} for t, pr in d["pts"]]}
                         for d in pn.chart.layer.drawings]}


def m_delete_drawing(win, p):
    return {"ok": _panel(win, p).chart.layer.remove(int(p["id"]))}


def m_clear_drawings(win, p):
    _panel(win, p).chart.layer.clear()
    return {"ok": True}


def m_watchlist_get(win, p):
    return {"symbols": win.watchlist.symbols(),
            "quotes": {k: {"last": v[0], "change_pct": round(v[1] * 100, 3)} for k, v in win.watchlist.quotes.items()}}


def m_watchlist_add(win, p):
    return {"added": win.watchlist.add(str(p.get("symbol", ""))), "symbols": win.watchlist.symbols()}


def m_watchlist_remove(win, p):
    return {"removed": win.watchlist.remove(str(p.get("symbol", ""))), "symbols": win.watchlist.symbols()}


def m_screenshot(win, p):
    from PySide6 import QtGui  # noqa: F401
    target = p.get("target", "window")
    widget = win if target == "window" else _panel(win, p)
    pix = widget.grab()
    if pix.width() > 1600:
        pix = pix.scaledToWidth(1600, QtCore.Qt.TransformationMode.SmoothTransformation)
    ba = QtCore.QByteArray()
    buf = QtCore.QBuffer(ba)
    buf.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    pix.save(buf, "PNG")
    return {"png_base64": base64.b64encode(bytes(ba)).decode(), "width": pix.width(), "height": pix.height()}


METHODS = {
    "get_state": m_get_state, "get_ohlcv": m_get_ohlcv, "set_indicator": m_set_indicator,
    "set_chart_style": m_set_chart_style, "set_layout": m_set_layout, "set_active_panel": m_set_active_panel,
    "get_strategy": m_get_strategy, "set_strategy": m_set_strategy,
    "list_strategy_templates": m_list_strategy_templates, "load_strategy_template": m_load_strategy_template,
    "run_backtest": m_run_backtest, "get_log": m_get_log,
    "add_drawing": m_add_drawing, "list_drawings": m_list_drawings, "delete_drawing": m_delete_drawing,
    "clear_drawings": m_clear_drawings, "watchlist_get": m_watchlist_get, "watchlist_add": m_watchlist_add,
    "watchlist_remove": m_watchlist_remove, "screenshot": m_screenshot,
}


# --------------------------------------------------------------------------- server
class ApiServer:
    def __init__(self, win):
        self.win = win
        self.token = secrets.token_urlsafe(24)
        self.port = 0
        self.bridge = _Bridge()          # created on the main thread -> lives there
        self._httpd = None
        self._thread = None

    def on_main(self, fn, timeout: float = 90.0):
        fut: Future = Future()
        self.bridge.request.emit((fn, fut))
        return fut.result(timeout)

    def call(self, method: str, params: dict):
        if method == "load_data":
            pn = self.on_main(lambda: _start_load(self.win, params))
            pn.load_event.wait(75)
            return self.on_main(lambda: {"ok": not pn.last_error and not pn.loading,
                                         "error": pn.last_error or None, **_panel_info(self.win, pn)})
        fn = METHODS.get(method)
        if fn is None:
            raise ValueError(f"unknown method '{method}'")
        return self.on_main(lambda: fn(self.win, params))

    def start(self):
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _host_ok(self):
                return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

            def do_GET(self):
                if self.path == "/ping" and self._host_ok():
                    self._send(200, {"app": "ChartLab", "version": __version__})
                else:
                    self._send(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                if self.path != "/rpc" or not self._host_ok():
                    return self._send(404, {"ok": False, "error": "not found"})
                if not secrets.compare_digest(self.headers.get("X-ChartLab-Token", ""), api.token):
                    return self._send(401, {"ok": False, "error": "bad token"})
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    req = json.loads(self.rfile.read(n) or b"{}")
                    res = api.call(str(req.get("method", "")), req.get("params") or {})
                    self._send(200, {"ok": True, "result": _clean(res)})
                except Exception as e:  # noqa: BLE001 - report every failure to the caller
                    self._send(200, {"ok": False, "error": f"{type(e).__name__}: {e}"})

        for port in range(8765, 8790):
            try:
                self._httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
                self.port = port
                break
            except OSError:
                continue
        if self._httpd is None:
            raise OSError("no free port in 8765-8789")
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        API_FILE.parent.mkdir(parents=True, exist_ok=True)
        API_FILE.write_text(json.dumps({"port": self.port, "token": self.token, "pid": os.getpid()}))
        try:
            os.chmod(API_FILE, 0o600)
        except OSError:
            pass

    def stop(self):
        try:
            if self._httpd:
                self._httpd.shutdown()
            if API_FILE.exists() and json.loads(API_FILE.read_text()).get("pid") == os.getpid():
                API_FILE.unlink()
        except Exception:  # noqa: BLE001
            pass
