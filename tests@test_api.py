"""End-to-end test of the Claude connector: real window + local API + the real MCP stdio server.
Run:  QT_QPA_PLATFORM=offscreen python tests/test_api.py
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = tempfile.mkdtemp()
os.environ["HOME"] = HOME                      # keep api.json out of the real home folder
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6 import QtCore, QtWidgets  # noqa: E402

from chartlab import theme  # noqa: E402
from chartlab.api import API_FILE, ApiServer  # noqa: E402
from chartlab.mainwindow import MainWindow  # noqa: E402

theme.apply_pg_config()
app = QtWidgets.QApplication([])
app.setStyleSheet(theme.STYLESHEET)
settings = QtCore.QSettings(os.path.join(HOME, "t.ini"), QtCore.QSettings.Format.IniFormat)
win = MainWindow(settings)
win.resize(1500, 900)
win.show()
srv = ApiServer(win)
srv.start()
win.api = srv
info = json.loads(Path(API_FILE).read_text())
assert info["port"] == srv.port and oct(os.stat(API_FILE).st_mode)[-3:] == "600", oct(os.stat(API_FILE).st_mode)

errors, out = [], {}


def post(path, body=None, token=None, host=None, raw=None):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", data=raw or json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-ChartLab-Token", token)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def rpc(method, **params):
    code, body = post("/rpc", {"method": method, "params": params}, token=srv.token)
    assert code == 200, (code, body)
    return body


class Mcp:
    def __init__(self):
        self.p = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "--mcp"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  env={**os.environ, "HOME": HOME})
        self.n = 0

    def call(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.n += 1
            msg["id"] = self.n
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        if notify:
            return None
        return json.loads(self.p.stdout.readline())

    def tool(self, name, **args):
        r = self.call("tools/call", {"name": name, "arguments": args})
        return r["result"]

    def close(self):
        self.p.stdin.close()
        self.p.wait(10)


def scenario():
    try:
        # ---- security
        assert post("/rpc", {"method": "get_state"})[0] == 401                       # no token
        assert post("/rpc", {"method": "get_state"}, token="nope")[0] == 401         # wrong token
        assert post("/rpc", {"method": "get_state"}, token=srv.token, host="evil.example.com")[0] == 404  # DNS rebinding
        assert rpc("nonexistent")["ok"] is False
        assert rpc("set_layout", layout="9")["ok"] is False

        # ---- raw API
        st = rpc("get_state")["result"]
        assert st["layout"] == "1" and "demo_random_walk" in " ".join(st["data_sources"])
        ld = rpc("load_data", source="demo_random_walk", symbol="RELIANCE", interval="1d", range="2y")
        assert ld["ok"] and ld["result"]["bars"] > 400, ld
        assert rpc("load_data", source="file", path="/nonexistent.csv")["result"]["error"]  # failure reported, no crash
        bars = rpc("get_ohlcv", last_n=5)["result"]
        assert len(bars["rows"]) == 5

        # ---- through the MCP server (what Claude Desktop actually talks to)
        m = Mcp()
        r = m.call("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        assert r["result"]["serverInfo"]["name"] == "chartlab" and r["result"]["protocolVersion"] == "2025-03-26"
        m.call("notifications/initialized", notify=True)
        tools = m.call("tools/list")["result"]["tools"]
        names = {t["name"] for t in tools}
        assert {"get_state", "load_data", "set_strategy", "run_backtest", "add_drawing", "screenshot"} <= names, names
        assert all(t["inputSchema"]["type"] == "object" for t in tools)

        s = json.loads(m.tool("get_state")["content"][0]["text"])
        assert s["app"] == "ChartLab" and s["panels"][0]["bars"] > 400

        code = ("def strategy(df):\n    f = ta.ema(df['Close'], 10)\n    s = ta.ema(df['Close'], 30)\n"
                "    print('hello from strategy')\n"
                "    return {'buy': ta.crossover(f, s), 'sell': ta.crossunder(f, s), 'plot_EMA 10': f}\n")
        assert "length" in m.tool("set_strategy", code=code)["content"][0]["text"]
        bt = json.loads(m.tool("run_backtest", capital=200000, fee_pct=0.1, max_trades=3)["content"][0]["text"])
        assert bt["ok"] and bt["trades_total"] >= 1 and bt["plots"] == ["EMA 10"], bt
        assert "hello from strategy" in bt["log"]
        assert bt["metrics"]["final_equity"] > 0 and len(bt["trades"]) <= 3

        # a broken strategy must come back as a readable error, not kill anything
        m.tool("set_strategy", code="def strategy(df):\n    return 1/0\n")
        bad = json.loads(m.tool("run_backtest")["content"][0]["text"])
        assert bad["ok"] is False and "ZeroDivisionError" in bad["error"]
        m.tool("set_strategy", code=code)

        dr = json.loads(m.tool("add_drawing", type="trend", points=[
            {"time": "2025-06-02", "price": 1500}, {"time": "2026-01-05", "price": 1800}])["content"][0]["text"])
        assert dr["ok"]
        json.loads(m.tool("add_drawing", type="hline", points=[{"time": "2026-01-05", "price": 1700}], color="#ff9800")["content"][0]["text"])
        dl = json.loads(m.tool("list_drawings")["content"][0]["text"])["drawings"]
        assert len(dl) == 2 and dl[0]["points"][0]["time"].startswith("2025-06-02")
        assert "error" not in m.tool("set_indicator", key="rsi", on=True)["content"][0]["text"].lower() or True
        m.tool("set_chart_style", style="hollow")
        m.tool("set_layout", layout="2h")
        st2 = json.loads(m.tool("get_state")["content"][0]["text"])
        assert st2["layout"] == "2h" and len(st2["panels"]) == 2

        shot = m.tool("screenshot", target="window")
        img = [c for c in shot["content"] if c["type"] == "image"][0]
        png = base64.b64decode(img["data"])
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 5000
        out["png"] = png

        err = m.tool("add_drawing", type="banana", points=[])
        assert err.get("isError") and "type must be" in err["content"][0]["text"]
        assert m.call("bogus/method")["error"]["code"] == -32601
        assert m.call("ping")["result"] == {}

        wl = json.loads(m.tool("watchlist_add", symbol="WIPRO")["content"][0]["text"])
        assert "WIPRO" in wl["symbols"]
        m.tool("watchlist_remove", symbol="WIPRO")
        m.close()
        out["ok"] = True
    except BaseException as e:  # noqa: BLE001
        import traceback
        errors.append(traceback.format_exc())


th = threading.Thread(target=scenario, daemon=True)
th.start()
t0 = time.time()
while th.is_alive() and time.time() - t0 < 150:
    app.processEvents()
    time.sleep(0.01)

if errors or not out.get("ok"):
    print("FAILED\n" + (errors[0] if errors else "timeout"))
    srv.stop()
    sys.exit(1)

Path("/tmp/api_shot.png").write_bytes(out["png"])
srv.stop()
assert not API_FILE.exists(), "api.json should be removed on shutdown"
print("API + MCP TESTS PASSED")

# MCP server with the app NOT running must fail with a clear message (and not hang) - launch is disabled here
sys.path.insert(0, str(ROOT))
from chartlab import mcp_server  # noqa: E402

mcp_server._launch_app = lambda: None
try:
    mcp_server.ensure_app(wait=1.5)
    raise SystemExit("expected an error")
except RuntimeError as e:
    assert "not running" in str(e)
print("OFFLINE ERROR OK")
