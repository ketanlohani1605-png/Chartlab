"""Real-time prices from the connected broker account (Dhan, Zerodha, Upstox, Angel One or Fyers).

One WebSocket carries every instrument that is on screen: the charts, the option chain and the watchlist. Ticks update
the newest bar in place. If the socket cannot connect, prices are polled over the broker's plain HTTPS API instead
(about once a second), so the charts keep moving either way. The socket protocols themselves live in
brokers/feeds.py; this file only drives them.

Nothing here places orders; it only reads prices."""
from __future__ import annotations

import datetime as dt
import json
import time

from PySide6 import QtCore, QtNetwork, QtWidgets

from . import dhan_core
from .brokers import feeds as _feeds

try:                                                     # QtWebSockets is an add-on module: polling works without it
    from PySide6 import QtWebSockets
except Exception:  # noqa: BLE001
    QtWebSockets = None

SEG_NUM = _feeds.DHAN_SEG
IST = 19800
ERRORS = _feeds.DHAN_ERRORS
parse_packets = _feeds.dhan_packets                      # kept for callers / tests of the Dhan feed
LIVE_BROKERS = ("dhan", "zerodha", "upstox", "angel", "fyers")


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


def _broker_of(acct):
    from . import brokers
    return brokers.get(acct)


class LiveFeed(QtCore.QObject):
    tick = QtCore.Signal(str, str, float, float, float)       # key part 1, key part 2, price, trade time, day volume
    prev = QtCore.Signal(str, str, float)                     # yesterday's close
    state = QtCore.Signal(str, str)                           # 'off' | 'connecting' | 'live' | 'polling' | 'error', text
    order = QtCore.Signal(object)                             # an order update pushed by the broker

    URL = "wss://api-feed.dhan.co"                            # Dhan's feed (tests point this at a local server)
    POLL_MS = 1500

    def __init__(self, parent=None):
        super().__init__(parent)
        self.acct = None
        self.broker = None
        self.proto = None
        self.wanted: set = set()
        self.insts: dict = {}            # key -> Instrument (needed to poll prices of non-Dhan brokers)
        self._subbed: set = set()
        self._ws = None
        self._up = False                 # socket is open
        self._prep = None                # background pre-step (Upstox: authorise the socket address)
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
        self._ping = QtCore.QTimer(self)
        self._ping.timeout.connect(self._send_ping)

    # ------------------------------------------------------------------ public
    @property
    def broker_name(self) -> str:
        return getattr(self.broker, "name", "the broker")

    def set_account(self, acct: dict | None):
        def sig(a):
            a = a or {}
            return (a.get("name"), a.get("broker"), a.get("client_id"), a.get("access_token"), a.get("feed_token"),
                    a.get("api_key"), a.get("app_id"))
        if sig(acct) == sig(self.acct):
            return
        self.acct = acct if acct and acct.get("access_token") and acct.get("broker") in LIVE_BROKERS else None
        self.broker = self.proto = None
        if self.acct is not None:
            try:
                self.broker = _broker_of(self.acct)
                self.proto = _feeds.proto_for(self.broker)
            except Exception:  # noqa: BLE001 - unknown broker: no live prices, nothing breaks
                self.acct = None
        self._fails, self._blocked_until, self._poll_fail, self._poll_pause = 0, 0.0, 0, 0.0
        self._close()
        self._kick()

    def watch(self, instruments, insts: dict | None = None):
        new = {(str(s), str(i)) for s, i in instruments}
        if insts:
            self.insts.update(insts)
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
        for t in (self._retry, self._poll, self._watch, self._ping):
            t.stop()
        for j in (self._poll_job, self._prep):
            if j is not None:
                j.wait(3000)

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
            self._set("off", "" if self.acct else "no broker account connected")
            return
        if not self._watch.isActive():
            self._watch.start()
        if not self._poll.isActive():
            self._poll.start()
        if self._ws is None and not self._retry.isActive():
            self._open()

    def _open(self):
        if self._ws is not None or self._prep is not None or not self.acct or not self.wanted or self.proto is None:
            return
        if QtWebSockets is None or time.time() < self._blocked_until:
            return
        self._set("connecting" if self.mode != "polling" else "polling", self.detail if self.mode == "polling" else "")
        if isinstance(self.proto, _feeds.DhanProto):
            self.proto.URL = self.URL
        if type(self.proto).prepare is not _feeds.Proto.prepare:
            job = _Call(self.proto.prepare, self)            # e.g. Upstox: ask for the authorised address first
            self._prep = job
            job.done.connect(lambda _r, p=self.proto: self._connect(p))
            job.failed.connect(self._prep_failed)
            job.finished.connect(lambda: setattr(self, "_prep", None))
            job.start()
            return
        self._connect(self.proto)

    def _prep_failed(self, msg):
        self._prep = None
        if any(w in msg.lower() for w in ("token", "log in", "auth")):
            self._blocked_until = time.time() + 600
            self._set("error", msg[:160])
            self._drop(schedule=False)
            return
        self._drop()

    def _connect(self, proto):
        if proto is not self.proto or self._ws is not None or not self.acct:
            return
        ws = QtWebSockets.QWebSocket("", QtWebSockets.QWebSocketProtocol.Version.VersionLatest, self)
        self._ws = ws
        ws.connected.connect(self._connected)
        ws.disconnected.connect(self._gone)
        ws.binaryMessageReceived.connect(self._binary)
        ws.textMessageReceived.connect(self._text)
        req = QtNetwork.QNetworkRequest(QtCore.QUrl(proto.url()))
        for k, v in (proto.headers() or {}).items():
            if v:
                req.setRawHeader(k.encode(), str(v).encode())
        ws.open(req)
        QtCore.QTimer.singleShot(8000, lambda w=ws: self._give_up(w))

    def _give_up(self, ws):
        if ws is self._ws and not self._up:                  # never connected: drop it and try again later
            self._drop()

    def _send(self, msg):
        if self._ws is None:
            return
        if isinstance(msg, (bytes, bytearray)):
            self._ws.sendBinaryMessage(QtCore.QByteArray(bytes(msg)))
        else:
            self._ws.sendTextMessage(msg)

    def _connected(self):
        if self.sender() is not self._ws:
            return
        self._up, self._subbed = True, set()
        self._last_packet = time.time()
        for m in self.proto.on_open():
            self._send(m)
        self._sync()
        if self.proto.ping_every:
            self._ping.start(int(self.proto.ping_every * 1000))
        self._set("live", "")

    def _send_ping(self):
        if self._up and self.proto is not None:
            m = self.proto.ping()
            if m is not None:
                self._send(m)

    def _sync(self):
        if not self._up or self._ws is None or self.proto is None:
            return
        add, gone = sorted(self.wanted - self._subbed), sorted(self._subbed - self.wanted)
        if gone:
            for m in self.proto.unsubscribe(gone):
                self._send(m)
        if add:
            for m in self.proto.subscribe(add[:self.proto.max_keys]):
                self._send(m)
        self._subbed = set(self.wanted)

    def _binary(self, data):
        if self.sender() is not self._ws:
            return
        self._handle(bytes(data))

    def _text(self, text):
        if self.sender() is not self._ws:
            return
        self._handle(str(text))

    def _handle(self, data):
        self._last_packet = time.time()
        self._fails = 0
        for e in self.proto.parse(data):
            k = e["kind"]
            if k == "tick":
                s, i = e["key"]
                self.tick.emit(str(s), str(i), float(e["ltp"]), float(e.get("ltt") or 0), float(e.get("vol", -1)))
            elif k == "prev":
                s, i = e["key"]
                self.prev.emit(str(s), str(i), float(e["prev"]))
            elif k == "order":
                self.order.emit(e.get("data") or {})
            elif k == "send":
                self._send(e["data"])
            elif k == "error":
                if e.get("fatal"):
                    self._blocked_until = time.time() + 600      # nothing changes until the account does
                    self._drop(schedule=False)
                    self._set("error", e["msg"])
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
        self._ping.stop()
        if ws is not None:
            try:
                ws.abort()
            except RuntimeError:
                pass
            ws.deleteLater()

    def _watchdog(self):
        if self._up and self.wanted and market_open() and time.time() - self._last_packet > 45:
            self._drop()                                     # connected but silent in market hours: start again
        elif self._ws is None and self._prep is None and self.acct and self.wanted and not self._retry.isActive() \
                and time.time() >= self._blocked_until:
            self._open()

    # ------------------------------------------------------------------ polling fallback
    def _poll_tick(self):
        if self._up or not self.acct or not self.wanted or self._poll_job is not None:
            return
        if time.time() < self._poll_pause or QtWidgets.QApplication.instance() is None:
            return
        acct, items = dict(self.acct), sorted(self.wanted)[:1000]
        if acct.get("broker") == "dhan":
            fn = lambda: dhan_core.ltp(acct, items)  # noqa: E731
        else:
            b, insts = self.broker, {k: self.insts[k] for k in items if k in self.insts}
            by_tok = {str(v.token): k for k, v in insts.items()}

            def fn():
                got = b.ltp(list(insts.values()))
                return {by_tok[t]: px for t, px in got.items() if t in by_tok}
        job = _Call(fn, self)
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
        if any(k in low for k in ("806", "dh-902", "not subscribed", "data api", "subscription", "permission")):
            self._poll_pause = time.time() + 600
            self._set("error", ERRORS[806] if self.acct and self.acct.get("broker") == "dhan" else msg[:160])
        elif any(k in low for k in ("401", "807", "808", "809", "dh-901", "expired", "invalid", "log in again")):
            self._poll_pause = time.time() + 600
            self._set("error", ERRORS[807] if self.acct and self.acct.get("broker") == "dhan"
                      else f"{self.broker_name}: the login has expired - log in again in Accounts")
        elif self._poll_fail >= 3:
            self._set("error", msg[:140])


YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1m&range=1d"
YAHOO_EVERY = 3.0            # seconds between price checks per symbol (Yahoo allows roughly 2000 requests an hour)


def yahoo_quote(sym: str, timeout: float = 8.0) -> dict:
    """The newest price Yahoo has for one symbol: {'price', 'time' (Unix), 'offset' (exchange's seconds from UTC),
    'volume' (today's), 'prev' (yesterday's close)}."""
    import urllib.parse
    import urllib.request
    url = YAHOO_URL.format(sym=urllib.parse.quote(sym, safe=""))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) ChartLab", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    res = (d.get("chart") or {}).get("result") or []
    if not res:
        err = ((d.get("chart") or {}).get("error") or {}).get("description") or "no data"
        raise RuntimeError(f"Yahoo: {err}")
    m = res[0].get("meta") or {}
    price = m.get("regularMarketPrice")
    if not price:
        raise RuntimeError("Yahoo: no price")
    return {"price": float(price), "time": float(m.get("regularMarketTime") or 0), "offset": float(m.get("gmtoffset") or 0),
            "volume": float(m.get("regularMarketVolume") or -1),
            "prev": float(m.get("chartPreviousClose") or m.get("previousClose") or 0)}


class YahooLive(QtCore.QObject):
    """Moving candles without a broker account: Yahoo's newest price, asked for every few seconds, for the symbols
    on the charts. (Not tick by tick, and for some exchanges Yahoo's prices are delayed; a connected broker
    account always wins.)"""
    quote = QtCore.Signal(str, object)                   # yahoo symbol, quote dict
    state = QtCore.Signal(str, str)

    def __init__(self, parent=None, fetch=None):
        super().__init__(parent)
        self.fetch = fetch or yahoo_quote
        self.symbols: tuple = ()
        self._last: dict = {}            # symbol -> (price, time) seen last
        self._quiet = 0                  # polls in a row in which nothing moved (market closed): slow down
        self._fails = 0
        self._job = None
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._poll)
        self.active = False

    def watch(self, symbols):
        syms = tuple(sorted(set(symbols)))
        if syms == self.symbols:
            return
        self.symbols = syms
        self._last = {k: v for k, v in self._last.items() if k in syms}
        self._quiet = 0
        if syms and not self._timer.isActive() and self._job is None:
            self._timer.start(50)
        if not syms:
            self._timer.stop()
            if self.active:
                self.active = False
                self.state.emit("off", "")

    def stop(self):
        self.symbols = ()
        self._timer.stop()
        if self._job is not None:
            self._job.wait(2000)

    def _poll(self):
        if not self.symbols or self._job is not None:
            return
        syms, fetch = self.symbols, self.fetch

        def work():
            out, err = {}, ""
            for s in syms:
                try:
                    out[s] = fetch(s)
                except Exception as e:  # noqa: BLE001
                    err = str(e)
            return out, err
        job = _Call(work, self)
        self._job = job
        job.done.connect(self._done)
        job.failed.connect(lambda msg: self._done(({}, msg)))
        job.finished.connect(self._finished)
        job.start()

    def _finished(self):
        self._job = None
        if not self.symbols:
            return
        every = max(YAHOO_EVERY, YAHOO_EVERY * len(self.symbols))
        if self._fails:
            every = min(120.0, 15.0 * self._fails)           # refused / offline: ask less often
        elif self._quiet > 20:
            every = max(every, 20.0)                         # nothing has traded for a minute: the market is closed
        self._timer.start(int(every * 1000))

    def _done(self, res):
        out, err = res
        if not out:
            self._fails += 1
            if self._fails == 3:
                self.active = False
                self.state.emit("error", "Yahoo does not answer (" + (err or "no connection") + ")")
            return
        self._fails = 0
        moved = False
        now = time.time()
        for sym, q in out.items():
            sig = (q["price"], q["time"])
            first = sym not in self._last
            changed = (not first) and self._last[sym] != sig
            self._last[sym] = sig
            if changed or (first and now - q["time"] < 120):  # a stale price (closed market) never opens a bar
                moved = True
                self.quote.emit(sym, q)
        self._quiet = 0 if moved else self._quiet + 1
        if not self.active:
            self.active = True
            self.state.emit("yahoo", "")


class Clock:
    """Dhan stamps trades with a number of seconds. Whether that number is counted in UTC or in India time is learnt
    from the first fresh trade (it must be 'now'), so bars never land five and a half hours away. The other brokers
    send true Unix times, which this learns the same way (as UTC)."""

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
    tickPrice = QtCore.Signal(str, float)            # SYMBOL, price - for the paper broker and order lines

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.feed = LiveFeed(self)
        self.clock = Clock()
        self._acct_sig = None
        self._ids: dict = {}             # SYMBOL -> key (ns, id) or None when the broker has no such symbol
        self._insts: dict = {}           # key -> Instrument
        self._queue: list = []           # symbols waiting to be looked up (one background job at a time)
        self._later: dict = {}           # SYMBOL -> time: the lookup could not reach the broker, try again after this
        self._job = None
        self._routes: dict = {}          # key -> [panels]
        self._wl: dict = {}              # key -> [watchlist symbols]
        self._syms: dict = {}            # key -> SYMBOL
        self._prev: dict = {}            # key -> yesterday's close, as the feed reports it
        self._pending: dict = {}
        self.extra = None                # callable -> {(seg, sid): callback(ltp)} for the option chain (Dhan)
        self.wanted_symbols = None       # callable -> extra symbols to follow (open positions / orders)
        self._flush = QtCore.QTimer(self)
        self._flush.setSingleShot(True)
        self._flush.setInterval(40)      # many ticks in one frame are drawn once
        self._flush.timeout.connect(self._deliver)
        self.yahoo = YahooLive(self)
        self._yroutes: dict = {}         # yahoo symbol -> [panels] followed through Yahoo (no broker feed for them)
        self.yahoo.quote.connect(self._on_yahoo)
        self.yahoo.state.connect(self._yahoo_state)
        self.feed.tick.connect(self._on_tick)
        self.feed.prev.connect(lambda seg, sid, px: self._prev.__setitem__((seg, sid), px))
        self.feed.state.connect(self._feed_state)
        self.enabled = True

    # ------------------------------------------------------------------ what to listen to
    def account(self):
        """The account whose live feed is used: the chart's API source if it is one, else the chosen live-data
        account (Settings), else the first connected Dhan account, else any connected broker."""
        from . import accounts
        accts = [a for a in accounts.load_accounts() if a.get("broker") in LIVE_BROKERS and a.get("access_token")]
        if not accts:
            return None
        src = str(self.win.active.params.get("source", "")) if getattr(self.win, "active", None) else ""
        if src.startswith("api:"):
            for a in accts:
                if a["name"] == src.split(":", 1)[1]:
                    return a
        pref = ""
        try:
            pref = str(self.win.settings.value("live_account", "") or "")
        except Exception:  # noqa: BLE001
            pref = ""
        for a in accts:
            if a["name"] == pref:
                return a
        for a in accts:
            if a.get("broker") == "dhan":
                return a
        return accts[0]

    def dhan_account(self):
        """The option chain reads Dhan's option-chain API: it needs a Dhan account whatever feeds the prices."""
        a = self.account()
        if a is not None and a.get("broker") == "dhan":
            return a
        from . import accounts
        return next((x for x in accounts.load_accounts() if x.get("broker") == "dhan" and x.get("access_token")), None)

    @staticmethod
    def _norm(symbol) -> str:
        s = str(symbol).strip().upper()
        for suf, ex in ((".NS", "NSE:"), (".BO", "BSE:")):
            if s.endswith(suf):
                s = ex + s[:-len(suf)]
        return s

    @classmethod
    def _wants(cls, panel) -> str:
        """The symbol to follow live for this pane, or '' (own files and non-Indian symbols have no broker feed)."""
        prm = panel.params
        src = str(prm.get("source", ""))
        if panel.df is None or src == "file" or src.startswith("provider:"):
            return ""
        if str(panel.label).endswith("my data"):
            return ""
        return cls._norm(prm.get("symbol", ""))

    def sync(self):
        acct = self.account() if self.enabled else None
        sig = (acct or {}).get("name"), (acct or {}).get("broker"), (acct or {}).get("access_token")
        if sig != self._acct_sig:                    # another broker: its ids and its clock are different
            self._acct_sig = sig
            self._ids, self._insts, self._later, self._queue = {}, {}, {}, []
            self.clock = Clock()
        self._sync_yahoo(acct)
        if acct is None:
            self._routes, self._wl = {}, {}
            self.feed.set_account(None)
            self.feed.watch(set())
            return
        routes: dict = {}
        need = []
        syms: dict = {}
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
                syms[self._ids[sym]] = sym
        self._routes = routes
        wl: dict = {}
        wlw = getattr(self.win, "watchlist", None)
        for raw in (wlw.symbols() if wlw is not None else []):
            sym = self._norm(raw)
            if sym not in self._ids:
                need.append(sym)
            elif self._ids[sym]:
                wl.setdefault(self._ids[sym], []).append(raw)
                syms.setdefault(self._ids[sym], sym)
        self._wl = wl
        keys = set(routes) | set(wl)
        if self.wanted_symbols is not None:
            try:
                for raw in self.wanted_symbols() or []:
                    sym = self._norm(raw)
                    if sym not in self._ids:
                        need.append(sym)
                    elif self._ids[sym]:
                        keys.add(self._ids[sym])
                        syms.setdefault(self._ids[sym], sym)
            except Exception:  # noqa: BLE001
                pass
        self._syms = syms
        if self.extra is not None and acct.get("broker") == "dhan":
            try:
                keys |= set(self.extra())
            except Exception:  # noqa: BLE001
                pass
        self.feed.set_account(acct)
        self.feed.watch(keys, {k: self._insts[k] for k in keys if k in self._insts})
        now = time.time()
        self._queue.extend(x for x in dict.fromkeys(need) if x not in self._queue and self._later.get(x, 0) <= now)
        self._resolve()

    def _resolve(self):
        """Look the waiting symbols up in the broker's instrument list, off the UI thread, one batch at a time."""
        if self._job is not None or not self._queue:
            return
        batch, self._queue = self._queue, []
        acct = self.account()
        if acct is None:
            return
        dhan = acct.get("broker") == "dhan"
        proto = self.feed.proto if self.feed.acct is not None and self.feed.acct.get("name") == acct.get("name") \
            else None

        def work():
            out = {}
            b = proto.b if proto is not None else _broker_of(acct)
            pr = proto or _feeds.proto_for(b)
            for sym in batch:
                try:
                    if dhan:
                        ins = dhan_core.resolve_any(sym)
                        out[sym] = ((ins["segment"], str(ins["security_id"])), None)
                    else:
                        inst = b.resolve(sym)
                        out[sym] = (pr.key(inst), inst)
                except Exception as e:  # noqa: BLE001
                    msg = str(e)
                    if any(w in msg for w in ("Could not reach", "API error", "unexpected format", "did not answer",
                                              "Could not download")):
                        out[sym] = "later"                   # offline: not an answer, ask again in a minute
                    else:
                        out[sym] = None                      # not on this broker (crypto, US stock, typo)
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
        for k, v in res.items():
            if v == "later":
                continue
            if v is None:
                self._ids[k] = None
            else:
                key, inst = v
                self._ids[k] = key
                if inst is not None:
                    self._insts[key] = inst
        for k in later:
            self._later[k] = time.time() + 60
        if later:
            QtCore.QTimer.singleShot(61000, self.sync)
        self._queue = [x for x in self._queue if x not in self._ids]
        QtCore.QTimer.singleShot(0, self.sync)

    # ------------------------------------------------------------------ no broker feed: Yahoo's newest price
    def _sync_yahoo(self, acct):
        from . import data as datamod
        from .dhan_core import is_option
        routes: dict = {}
        if self.enabled:
            for p in self.win.panels:
                if not p.isVisible():
                    continue
                sym = self._wants(p)
                src = str(p.params.get("source", ""))
                if not sym or src not in ("yahoo", "auto") or is_option(p.params.get("symbol", "")):
                    continue
                if acct is not None and self._ids.get(sym, "?") is not None:
                    continue                                 # the broker has it (or is still being asked)
                try:
                    routes.setdefault(datamod.normalize_symbol(p.params.get("symbol", "")), []).append(p)
                except Exception:  # noqa: BLE001
                    continue
        self._yroutes = routes
        self.yahoo.watch(routes.keys())

    def _feed_state(self, mode, detail):
        if mode in ("off", "") and self.yahoo.active:
            mode, detail = "yahoo", ""
        self.status.emit(mode, detail)

    def _yahoo_state(self, mode, detail):
        if self.feed.acct is None or mode == "yahoo":        # the broker's own state wins when there is one
            if self.feed.acct is None or self.feed.mode in ("off", ""):
                self.status.emit(mode, detail)

    def _on_yahoo(self, ysym, q):
        t = q["time"] + q["offset"] if q.get("time") else None
        vol = q["volume"] if q.get("volume", -1) >= 0 else None
        modal = QtWidgets.QApplication.activeModalWidget() is not None
        for p in self._yroutes.get(ysym, ()):
            try:
                r = p.on_tick(q["price"], t, vol)
                sym = self._wants(p)
            except RuntimeError:
                continue
            if sym:
                self.tickPrice.emit(sym, q["price"])
            if r and not modal:
                self.win.on_live_price(p, q["price"], r == "new")

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
            sym = self._syms.get(key)
            if sym:
                self.tickPrice.emit(sym, ltp)
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
        self.yahoo.stop()
        self.feed.stop()
        if self._job is not None:
            self._job.wait(2000)


def _unused():  # pragma: no cover - keeps QtNetwork imported so PyInstaller bundles the TLS backend for the socket
    return QtNetwork.QSslSocket.supportsSsl()
