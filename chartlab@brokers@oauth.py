"""Browser login: ChartLab opens the broker's login page and listens on this computer (127.0.0.1 only) for the redirect
that carries the login result. Nothing is sent anywhere; the page the browser lands on just says it is done.

The same redirect address must be saved in the broker's API app settings (ChartLab shows it with a Copy button):
    http://127.0.0.1:8767/callback
If the redirect cannot be caught (another program uses the port, a different redirect is registered), the address the
browser ended up on can be pasted instead: parse_pasted() reads the result out of it."""
from __future__ import annotations

import http.server
import threading
import urllib.parse

DEFAULT_REDIRECT = "http://127.0.0.1:8767/callback"

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>ChartLab</title>
<style>body{background:#131722;color:#d1d4dc;font:16px -apple-system,Helvetica,sans-serif;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0}div{text-align:center}b{color:#fff;font-size:22px}
p{color:#787b86}</style></head><body><div><b>%s</b><p>%s</p></div></body></html>"""


def parse_pasted(text: str) -> dict:
    """The redirected address (or just its ?query part) -> its parameters."""
    t = (text or "").strip()
    if not t:
        return {}
    if "?" in t:
        t = t.split("?", 1)[1]
    t = t.split("#", 1)[0]
    return {k: v for k, v in urllib.parse.parse_qsl(t, keep_blank_values=True)}


def check_redirect(url: str) -> str:
    """'' when ChartLab can listen on this redirect address itself, else why not."""
    u = urllib.parse.urlsplit(url or "")
    if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost"):
        return ("ChartLab can only catch a redirect to http://127.0.0.1:<port>/... on this computer. With another address, "
                "paste the page address you land on after logging in.")
    if not u.port:
        return "The redirect address needs a port, like http://127.0.0.1:8767/callback"
    return ""


class Catcher:
    """One login: listen, keep the first redirect's parameters, stop."""

    def __init__(self, redirect: str = DEFAULT_REDIRECT, state: str = ""):
        u = urllib.parse.urlsplit(redirect)
        self.host = "127.0.0.1"
        self.port = u.port or 8767
        self.path = u.path or "/"
        self.state = state
        self.params: dict | None = None
        self.done = threading.Event()
        self._srv = None

    def start(self):
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):                       # noqa: N802
                u = urllib.parse.urlsplit(self.path)
                if u.path.rstrip("/") != owner.path.rstrip("/") and owner.path not in ("", "/"):
                    self.send_response(404)
                    self.end_headers()
                    return
                params = {k: v for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True)}
                if owner.state and params.get("state") not in (None, "", owner.state):
                    body = _PAGE % ("Login not accepted", "This answer does not belong to the login ChartLab started.")
                    self.send_response(400)
                else:
                    owner.params = params
                    owner.done.set()
                    body = _PAGE % ("You are logged in", "You can close this tab and go back to ChartLab.")
                    self.send_response(200)
                data = body.encode()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self._srv = http.server.ThreadingHTTPServer((self.host, self.port), Handler)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def wait(self, timeout: float) -> dict | None:
        return self.params if self.done.wait(timeout) else None

    def stop(self):
        srv, self._srv = self._srv, None
        if srv is not None:
            threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()), daemon=True).start()
