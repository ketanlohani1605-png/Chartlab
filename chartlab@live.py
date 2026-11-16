"""Real-time prices from the connected Dhan account.

One WebSocket (Dhan live market feed v2) carries every instrument that is on screen: the charts, the option chain and
the watchlist. Ticks update the newest bar in place. If the socket cannot connect, prices are polled once a second
over the plain HTTPS API instead, so the charts keep moving either way.

Nothing here places orders; it only reads prices."""
from __future__ import annotations

import datetime as dt
import json
import struct
import time

from PySide6 import QtCore, QtNetwork, QtWidgets

from . import dhan_core

try:                                                     # QtWebSockets is an add-on module: polling works without it
    from PySide6 import QtWebSockets
except Exception:  # noqa: BLE001
    QtWebSockets = None

SEG_NUM = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY", 4: "BSE_EQ", 5: "MCX_COMM", 7: "BSE_CURRENCY",
           8: "BSE_FNO"}
_SIZE = {2: 16, 4: 50, 5: 12, 6: 16, 7: 8, 8: 162, 50: 10}
IST = 19800
ERRORS = {805: "too many live connections on this Dhan account (5 allowed)",
          806: "this Dhan account has no Data API subscription (needed for live prices and candles)",
          807: "the Dhan access token has expired: paste a fresh one in Accounts",
          808: "Dhan did not accept the client id / access token",
          809: "the Dhan access token is not valid", 810: "the Dhan client id is not valid"}


def parse_packets(data: bytes) -> list[dict]:
    """Dhan's little-endian binary feed -> [{'kind': 'tick'|'prev'|'oi'|'bye', 'seg', 'sid', ...}]."""
    out, p, n = [], 0, len(data)
    while p + 8 <= n:
        code = data[p]
        mlen = struct.unpack_from("<H", data, p + 1)[0]
        size = _SIZE.get(code) or (mlen if 8 <= mlen <= n - p else n - p)
        if p + size > n:
            break
        seg = SEG_NUM.get(data[p + 3], str(data[p + 3]))
        sid = str(struct.unpack_from("<I", data, p + 4)[0])
        b = p + 8
        try:
            if code in (1, 2) and size >= 16:              # index / ticker: price + trade time
                ltp, ltt = struct.unpack_from("<fI", data, b)
                out.append({"kind": "tick", "seg": seg, "sid": sid, "ltp": ltp, "ltt": ltt, "vol": -1.0})
            elif code == 4:                                # quote: + day volume and day open/high/low
                ltp, _q, ltt, _atp, vol, _ts, _tb, o, c, h, lo = struct.unpack_from("<fHIfIIIffff", data, b)
                out.append({"kind": "tick", "seg": seg, "sid": sid, "ltp": ltp, "ltt": ltt, "vol": float(vol),
                            "open": o, "high": h, "low": lo, "close": c})
            elif code == 8:                                # full packet
                ltp, _q, ltt, _atp, vol, _ts, _tb, oi, _oh, _ol, o, c, h, lo = struct.unpack_from(
                    "<fHIfIIIIIIffff", data, b)
                out.append({"kind": "tick", "seg": seg, "sid": sid, "ltp": ltp, "ltt": ltt, "vol": float(vol),
                            "open": o, "high": h, "low": lo, "close": c, "oi": float(oi)})
            elif code == 6:
                prev, _oi = struct.unpack_from("<fI", data, b)
                out.append({"kind": "prev", "seg": seg, "sid": sid, "prev": prev})
            elif code == 5:
                out.append({"kind": "oi", "seg": seg, "sid": sid, "oi": float(struct.unpack_from("<I", data, b)[0])})
            elif code == 50:
                out.append({"kind": "bye", "code": struct.unpack_from("<H", data, b)[0]})
        except struct.error:
            break
        p += size
    return out


def market_open(now: float | None = None) -> bool:
    """NSE / BSE cash and F&O hours, Monday to Friday 09:00-15:40 India time (holidays are not known here)."""
    t = dt.datetime.fromtimestamp((time.time() if now is None else now) + IST, dt.timezone.utc)
    return t.weekday() < 5 and dt.time(9, 0) <= t.time() <= dt.time(15, 40)


class _Call(QtCore.QThread):
    """Run one blocking function off the UI thread."""
    done = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class LiveFeed(QtCore.QObject):
    tick = QtCore.Signal(str, str, float, float, float)       # segment, security id, price, trade time, day volume
    prev = QtCore.Signal(str, str, float)                     # yesterday's close
    state = QtCore.Signal(str, str)                           # 'off' | 'connecting' | 'live' | 'polling' | 'error', text

    URL = "wss://api-feed.dhan.co"
    POLL_MS = 1500

    def __init__(self, parent=None):
        super().__init__(parent)
        self.acct = None
        self.wanted: set = set()
        self._subbed: set = set()
        self._ws = None
        self._up = False                 # socket is open
        self._last_packet = 0.0
        self._fails = 0
        self._blocked_until = 0.0        # no socket attempts before this time (auth / subscription errors)
        self._poll_job = None
        self._poll_fail = 0
        self._poll_pause = 0.0
        self._last_poll: dict = {}
        self.mode, self.detail = "off", ""
        self._retry = QtCore.QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._open)
        self._poll = QtCore.QTimer(self)
        self._poll.setInterval(self.POLL_MS)
        self._poll.timeout.connect(self._poll_tick)
        self._watch = QtCore.QTimer(self)
        self._watch.setInterval(20000)
        self._watch.timeout.connect(self._watchdog)

    # ------------------------------------------------------------------ public
    def set_account(self, acct: dict | None):
        key = (acct or {}).get("client_id"), (acct or {}).get("access_token")
        if key == ((self.acct or {}).get("client_id"), (self.acct or {}).get("access_token")):
            return
        self.acct = acct if acct and acct.get("access_token") else None
        self._fails, self._blocked_until, self._poll_fail, self._poll_pause = 0, 0.0, 0, 0.0
        self._close()
        self._kick()

    def watch(self, instruments):
        new = {(str(s), str(i)) for s, i in instruments}
        if new == self.wanted:
            return
        self.wanted = new
        if self._up:
            self._sync()
        else:
            self._kick()

    def stop(self):
        self.acct, self.wanted = None, set()
        self._close()
        for t in (self._retry, self._poll, self._watch):
            t.stop()
        if self._poll_job is not None:
            self._poll_job.wait(3000)

    # ------------------------------------------------------------------ socket
    def _set(self, mode, detail=""):
        if (mode, detail) != (self.mode, self.detail):
            self.mode, self.detail = mode, detail
            self.state.emit(mode, detail)

    def _kick(self):
        if not self.acct or not self.wanted:
            self._close()
            self._poll.stop()
            self._watch.stop()
            self._retry.stop()
            self._set("off", "" if self.acct else "no Dhan account connected")
            return
        if not self._watch.isActive():
            self._watch.start()
        if not self._poll.isActive():
            self._poll.start()
        if self._ws is None and not self._retry.isActive():
            self._open()

    def _open(self):
        if self._ws is not None or not self.acct or not self.wanted:
            return
        if QtWebSockets is None or time.time() < self._blocked_until:
            return
        self._set("connecting" if self.mode != "polling" else "polling", self.detail if self.mode == "polling" else "")
        ws = QtWebSockets.QWebSocket("", QtWebSockets.QWebSocketProtocol.Version.VersionLatest, self)
        self._ws = ws
        ws.connected.connect(self._connected)
        ws.disconnected.connect(self._gone)
        ws.binaryMessageReceived.connect(self._binary)
        url = QtCore.QUrl(self.URL)
        q = QtCore.QUrlQuery()
        q.addQueryItem("version", "2")
        q.addQueryItem("token", str(self.acct["access_token"]))
        q.addQueryItem("clientId", str(self.acct["client_id"]))
        q.addQueryItem("authType", "2")
        url.setQuery(q)
        ws.open(url)
        QtCore.QTimer.singleShot(8000, lambda w=ws: self._give_up(w))

    def _give_up(self, ws):
        if ws is self._ws and not self._up:                  # never connected: drop it and try again later
            self._drop()

    def _connected(self):
        if self.sender() is not self._ws:
            return
        self._up, self._subbed = True, set()
        self._last_packet = time.time()
        self._sync()
        self._set("live", "")

    def _sync(self):
        if not self._up or self._ws is None:
            return
        add, gone = self.wanted - self._subbed, self._subbed - self.wanted
        for items, tick_code, quote_code in ((gone, 16, 18), (add, 15, 17)):
            # an index has no volume / depth: Dhan sends it as a ticker; everything else as a quote (with volume)
            for code, part in ((tick_code, [x for x in items if x[0] == "IDX_I"]),
                               (quote_code, [x for x in items if x[0] != "IDX_I"])):
                part = sorted(part)
                for i in range(0, len(part), 100):
                    chunk = part[i:i + 100]
                    self._ws.sendTextMessage(json.dumps({
                        "RequestCode": code, "InstrumentCount": len(chunk),
                        "InstrumentList": [{"ExchangeSegment": s, "SecurityId": str(k)} for s, k in chunk]}))
        self._subbed = set(self.wanted)

    def _binary(self, data):
        if self.sender() is not self._ws:
            return
        self._last_packet = time.time()
        self._fails = 0
        for m in parse_packets(bytes(data)):
            k = m["kind"]
            if k == "tick":
                if m["ltp"] > 0:
                    self.tick.emit(m["seg"], m["sid"], float(m["ltp"]), float(m["ltt"]), float(m["vol"]))
            elif k == "prev":
                self.prev.emit(m["seg"], m["sid"], float(m["prev"]))
            elif k == "bye":
                code = int(m["code"])
                why = ERRORS.get(code, f"Dhan closed the live feed (code {code})")
                if code in (806, 807, 808, 809, 810):
                    self._blocked_until = time.time() + 600      # nothing changes until the account does
                    self._drop(schedule=False)
                    self._set("error", why)
                else:
                    self._drop()
                return

    def _gone(self):
        if self.sender() is self._ws:
            self._drop()

    def _drop(self, schedule=True):
        self._close()
        self._fails += 1
        if schedule and self.acct and self.wanted:
            self._retry.start(min(60000, 2000 * 2 ** min(self._fails, 5)))

    def _close(self):
        ws, self._ws, self._up, self._subbed = self._ws, None, False, set()
        if ws is not None:
            try:
                ws.abort()
            except RuntimeError:
                pass
            ws.deleteLater()

    def _watchdog(self):
        if self._up and self.wanted and market_open() and time.time() - self._last_packet > 45:
            self._drop()                                     # connected but silent in market hours: start again
        elif self._ws is None and self.acct and self.wanted and not self._retry.isActive() \
                and time.time() >= self._blocked_until:
            self._open()

    # ------------------------------------------------------------------ polling fallback
    def _poll_tick(self):
        if self._up or not self.acct or not self.wanted or self._poll_job is not None:
            return
        if time.time() < self._poll_pause or QtWidgets.QApplication.instance() is None:
            return
        acct, items = dict(self.acct), sorted(self.wanted)[:1000]
        job = _Call(lambda: dhan_core.ltp(acct, items), self)
        self._poll_job = job
        job.done.connect(self._polled)
        job.failed.connect(self._poll_failed)
        job.finished.connect(self._poll_done)
        job.start()

    def _poll_done(self):
        if self.sender() is self._poll_job:
            self._poll_job = None

    def _polled(self, prices):
        self._poll_fail = 0
        if self._up:
            return
        self._set("polling", "live socket not connected yet: prices are fetched every 1.5 s")
        now = time.time()
        for (seg, sid), px in prices.items():
            moved = self._last_poll.get((seg, sid)) != px
            self._last_poll[(seg, sid)] = px
            # no trade time over HTTPS: a price that moved in market hours happened now
            self.tick.emit(seg, sid, float(px), -(now if moved and market_open(now) else 0.0), -1.0)

    def _poll_failed(self, msg):
        self._poll_fail += 1
        self._poll_pause = time.time() + min(120, 5 * 2 ** min(self._poll_fail, 5))
        if self._up:
            return
        low = msg.lower()
        if any(k in low for k in ("806", "dh-902", "not subscribed", "data api")):
            self._poll_pause = time.time() + 600
            self._set("error", ERRORS[806])
        elif any(k in low for k in ("401", "807", "808", "809", "dh-901", "expired", "invalid")):
            self._poll_pause = time.time() + 600
            self._set("error", ERRORS[807])
        elif self._poll_fail >= 3:
            self._set("error", msg[:140])


class Clock:
    """Dhan stamps trades with a number of seconds. Whether that number is counted in UTC or in India time is learnt
    from the first fresh trade (it must be 'now'), so bars never land five and a half hours away."""

    def __init__(self):
        self.shift = None                # seconds to add to a trade time to get chart (India wall-clock) time

    def chart_time(self, ltt: float, now: float | None = None):
        now = time.time() if now is None else now
        if ltt < 0:                                          # polled price: the feed passes -now (or -0: unknown)
            return (-ltt + IST) if ltt < -1 else None
        if ltt <= 0:
            return None
        if self.shift is None:
            for shift in (0, IST):
                if abs((ltt + shift) - (now + IST)) < 120:   # a trade that just happened
                    self.shift = shift
                    break
        if self.shift is not None:
            t = ltt + self.shift
            return t if t <= now + IST + 300 else None
        # not known yet (market closed, nothing fresh): take the reading that falls inside trading hours. When both
        # do, the earlier one: at worst a stale trade looks older than it is and is ignored, never a bar in the future.
        ok = []
        for shift in (0, IST):
            t = ltt + shift
            tod = t % 86400
            if t <= now + IST + 300 and 9 * 3600 <= tod <= 16 * 3600:
                ok.append(t)
        return min(ok) if ok else None


class LiveController(QtCore.QObject):
    """Glue between the feed and the window: which instruments are on screen, and where each tick goes."""
    status = QtCore.Signal(str, str)

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.feed = LiveFeed(self)
        self.clock = Clock()
        self._ids: dict = {}             # SYMBOL -> (segment, security id) or None when Dhan has no such symbol
        self._queue: list = []           # symbols waiting to be looked up (one background job at a time)
        self._later: dict = {}           # SYMBOL -> time: the lookup could not reach Dhan, try again after this
        self._job = None
        self._routes: dict = {}          # (seg, sid) -> [panels]
        self._wl: dict = {}              # (seg, sid) -> [watchlist symbols]
        self._prev: dict = {}            # (seg, sid) -> yesterday's close, as the feed reports it
        self._pending: dict = {}
        self.extra = None                # callable -> {(seg, sid): callback(ltp)} for the option chain
        self._flush = QtCore.QTimer(self)
        self._flush.setSingleShot(True)
        self._flush.setInterval(40)      # many ticks in one frame are drawn once
        self._flush.timeout.connect(self._deliver)
        self.feed.tick.connect(self._on_tick)
        self.feed.prev.connect(lambda seg, sid, px: self._prev.__setitem__((seg, sid), px))
        self.feed.state.connect(self.status)
        self.enabled = True

    # ------------------------------------------------------------------ what to listen to
    def account(self):
        from . import accounts
        accts = [a for a in accounts.load_accounts() if a.get("broker") == "dhan" and a.get("access_token")]
        if not accts:
            return None
        src = str(self.win.active.params.get("source", "")) if getattr(self.win, "active", None) else ""
        if src.startswith("api:"):
            for a in accts:
                if a["name"] == src.split(":", 1)[1]:
                    return a
        return accts[0]

    @staticmethod
    def _norm(symbol) -> str:
        s = str(symbol).strip().upper()
        for suf, ex in ((".NS", "NSE:"), (".BO", "BSE:")):
            if s.endswith(suf):
                s = ex + s[:-len(suf)]
        return s

    @classmethod
    def _wants(cls, panel) -> str:
        """The symbol to follow live for this pane, or '' (own files and non-Indian symbols have no Dhan feed)."""
        prm = panel.params
        src = str(prm.get("source", ""))
        if panel.df is None or src == "file" or src.startswith("provider:"):
            return ""
        if str(panel.label).endswith("my data"):
            return ""
        return cls._norm(prm.get("symbol", ""))

    def sync(self):
        acct = self.account() if self.enabled else None
        if acct is None:
            self._routes, self._wl = {}, {}
            self.feed.set_account(None)
            self.feed.watch(set())
            return
        routes: dict = {}
        need = []
        for p in self.win.panels:
            if not p.isVisible():
                continue
            sym = self._wants(p)
            if not sym:
                continue
            if sym not in self._ids:
                need.append(sym)
                continue
            if self._ids[sym]:
                routes.setdefault(self._ids[sym], []).append(p)
        self._routes = routes
        wl: dict = {}
        wlw = getattr(self.win, "watchlist", None)
        for raw in (wlw.symbols() if wlw is not None else []):
            sym = self._norm(raw)
            if sym not in self._ids:
                need.append(sym)
            elif self._ids[sym]:
                wl.setdefault(self._ids[sym], []).append(raw)
        self._wl = wl
        keys = set(routes) | set(wl)
        if self.extra is not None:
            try:
                keys |= set(self.extra())
            except Exception:  # noqa: BLE001
                pass
        self.feed.set_account(acct)
        self.feed.watch(keys)
        now = time.time()
        self._queue.extend(x for x in dict.fromkeys(need) if x not in self._queue and self._later.get(x, 0) <= now)
        self._resolve()

    def _resolve(self):
        """Look the waiting symbols up in Dhan's instrument list, off the UI thread, one batch at a time."""
        if self._job is not None or not self._queue:
            return
        batch, self._queue = self._queue, []

        def work():
            out = {}
            for sym in batch:
                try:
                    ins = dhan_core.resolve_any(sym)
                    out[sym] = (ins["segment"], str(ins["security_id"]))
                except Exception as e:  # noqa: BLE001
                    msg = str(e)
                    if "Could not reach" in msg or "API error" in msg or "unexpected format" in msg:
                        out[sym] = "later"                   # offline: not an answer, ask again in a minute
                    else:
                        out[sym] = None                      # not on Dhan (crypto, US stock, typo): no live feed
            return out
        job = _Call(work, self)
        self._job = job
        job.done.connect(self._resolved)
        job.finished.connect(self._job_done)
        job.start()

    def _job_done(self):
        self._job = None
        if self._queue:
            self._resolve()

    def _resolved(self, res):
        later = [k for k, v in res.items() if v == "later"]
        self._ids.update({k: v for k, v in res.items() if v != "later"})
        for k in later:
            self._later[k] = time.time() + 60
        if later:
            QtCore.QTimer.singleShot(61000, self.sync)
        self._queue = [x for x in self._queue if x not in self._ids]
        QtCore.QTimer.singleShot(0, self.sync)

    # ------------------------------------------------------------------ ticks
    def _on_tick(self, seg, sid, ltp, ltt, vol):
        self._pending[(seg, sid)] = (ltp, ltt, vol)
        if not self._flush.isActive():
            self._flush.start()

    def _deliver(self):
        pend, self._pending = self._pending, {}
        extra = {}
        if self.extra is not None:
            try:
                extra = self.extra() or {}
            except Exception:  # noqa: BLE001
                extra = {}
        modal = QtWidgets.QApplication.activeModalWidget() is not None
        for key, (ltp, ltt, vol) in pend.items():
            cb = extra.get(key)
            if cb is not None:
                cb(ltp)
            t = self.clock.chart_time(ltt)
            names = self._wl.get(key)
            if names:
                wlw = self.win.watchlist
                for name in names:
                    wlw.live_price(name, ltp, self._prev.get(key))
            for p in self._routes.get(key, ()):
                try:
                    r = p.on_tick(ltp, t, vol if vol >= 0 else None)
                except RuntimeError:                         # pane deleted while the tick was in flight
                    continue
                if r and not modal:
                    self.win.on_live_price(p, ltp, r == "new")

    def stop(self):
        self.enabled = False
        self.feed.stop()
        if self._job is not None:
            self._job.wait(2000)


def _unused():  # pragma: no cover - keeps QtNetwork imported so PyInstaller bundles the TLS backend for the socket
    return QtNetwork.QSslSocket.supportsSsl()
