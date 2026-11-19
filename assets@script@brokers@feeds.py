"""Live-price socket protocols of every broker, without Qt: each class says where to connect, what to send and how to
read what comes back. The app's LiveFeed (live.py) drives one of them over a QWebSocket.

Events returned by parse():
    {"kind": "tick", "key": (ns, id), "ltp": float, "ltt": unix seconds or 0, "vol": day volume or -1}
    {"kind": "prev", "key": (ns, id), "prev": yesterday's close}
    {"kind": "order", "data": {...}}                  order update pushed by the broker (Kite)
    {"kind": "error", "msg": str, "fatal": bool}      fatal = do not reconnect until the account changes
"""
from __future__ import annotations

import base64
import json
import struct
import time
import uuid

from .base import Instrument

IST = 19800


# ---------------------------------------------------------------------------------------------------- protobuf (tiny)
def _varint(b: bytes, p: int):
    shift = result = 0
    while True:
        c = b[p]
        p += 1
        result |= (c & 0x7F) << shift
        if not c & 0x80:
            return result, p
        shift += 7


def pb_fields(b: bytes):
    """Protocol-buffer wire format -> [(field number, wire type, value)]. Value: int, bytes, or 8/4 raw bytes."""
    out, p, n = [], 0, len(b)
    while p < n:
        key, p = _varint(b, p)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, p = _varint(b, p)
        elif wt == 1:
            v, p = b[p:p + 8], p + 8
        elif wt == 2:
            ln, p = _varint(b, p)
            v, p = b[p:p + ln], p + ln
        elif wt == 5:
            v, p = b[p:p + 4], p + 4
        else:
            break
        out.append((f, wt, v))
    return out


def _d(raw) -> float:
    return struct.unpack("<d", raw)[0] if isinstance(raw, (bytes, bytearray)) and len(raw) == 8 else float(raw or 0)


def _signed(v: int) -> int:
    return v - (1 << 64) if v >= 1 << 63 else v


# ---------------------------------------------------------------------------------------------------- base
class Proto:
    broker = ""
    ns = ""
    ping_every = 0.0                  # seconds between keep-alive messages (0 = none needed)
    max_keys = 1000

    def __init__(self, broker):
        self.b = broker
        self.acct = broker.acct

    def key(self, inst: Instrument) -> tuple:
        return (self.ns, str(inst.token))

    def prepare(self) -> None:        # blocking work before connecting (runs off the UI thread)
        pass

    def url(self) -> str:
        raise NotImplementedError

    def headers(self) -> dict:
        return {}

    def on_open(self) -> list:
        return []

    def subscribe(self, keys: list) -> list:
        return []

    def unsubscribe(self, keys: list) -> list:
        return []

    def parse(self, data) -> list:
        return []

    def ping(self):
        return None


# ---------------------------------------------------------------------------------------------------- Dhan
DHAN_SEG = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY", 4: "BSE_EQ", 5: "MCX_COMM", 7: "BSE_CURRENCY",
            8: "BSE_FNO"}
_DHAN_SIZE = {2: 16, 4: 50, 5: 12, 6: 16, 7: 8, 8: 162, 50: 10}
DHAN_ERRORS = {805: "too many live connections on this Dhan account (5 allowed)",
               806: "this Dhan account has no Data API subscription (needed for live prices and candles)",
               807: "the Dhan access token has expired: log in again in Accounts",
               808: "Dhan did not accept the client id / access token",
               809: "the Dhan access token is not valid", 810: "the Dhan client id is not valid"}


def dhan_packets(data: bytes) -> list[dict]:
    """Dhan's little-endian binary feed -> [{'kind': 'tick'|'prev'|'oi'|'bye', 'seg', 'sid', ...}]."""
    out, p, n = [], 0, len(data)
    while p + 8 <= n:
        code = data[p]
        mlen = struct.unpack_from("<H", data, p + 1)[0]
        size = _DHAN_SIZE.get(code) or (mlen if 8 <= mlen <= n - p else n - p)
        if p + size > n:
            break
        seg = DHAN_SEG.get(data[p + 3], str(data[p + 3]))
        sid = str(struct.unpack_from("<I", data, p + 4)[0])
        b = p + 8
        try:
            if code in (1, 2) and size >= 16:
                ltp, ltt = struct.unpack_from("<fI", data, b)
                out.append({"kind": "tick", "seg": seg, "sid": sid, "ltp": ltp, "ltt": ltt, "vol": -1.0})
            elif code == 4:
                ltp, _q, ltt, _atp, vol, _ts, _tb, o, c, h, lo = struct.unpack_from("<fHIfIIIffff", data, b)
                out.append({"kind": "tick", "seg": seg, "sid": sid, "ltp": ltp, "ltt": ltt, "vol": float(vol),
                            "open": o, "high": h, "low": lo, "close": c})
            elif code == 8:
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


class DhanProto(Proto):
    broker = "dhan"
    URL = "wss://api-feed.dhan.co"

    def key(self, inst):
        return (inst.seg, str(inst.token))

    def url(self):
        return (f"{self.URL}?version=2&token={self.acct.get('access_token', '')}"
                f"&clientId={self.acct.get('client_id', '')}&authType=2")

    def _msgs(self, keys, tick_code, quote_code):
        out = []
        for code, part in ((tick_code, sorted(k for k in keys if k[0] == "IDX_I")),
                           (quote_code, sorted(k for k in keys if k[0] != "IDX_I"))):
            for i in range(0, len(part), 100):
                chunk = part[i:i + 100]
                out.append(json.dumps({"RequestCode": code, "InstrumentCount": len(chunk),
                                       "InstrumentList": [{"ExchangeSegment": s, "SecurityId": str(k)}
                                                          for s, k in chunk]}))
        return out

    def subscribe(self, keys):
        return self._msgs(keys, 15, 17)

    def unsubscribe(self, keys):
        return self._msgs(keys, 16, 18)

    def parse(self, data):
        if isinstance(data, str):
            return []
        out = []
        for m in dhan_packets(bytes(data)):
            k = m["kind"]
            if k == "tick" and m["ltp"] > 0:
                # Dhan's trade time is ambiguous (UTC or India time): the app's Clock works it out, so pass it raw
                out.append({"kind": "tick", "key": (m["seg"], m["sid"]), "ltp": float(m["ltp"]),
                            "ltt": float(m["ltt"]), "vol": float(m["vol"]), "raw_time": True})
            elif k == "prev":
                out.append({"kind": "prev", "key": (m["seg"], m["sid"]), "prev": float(m["prev"])})
            elif k == "bye":
                code = int(m["code"])
                out.append({"kind": "error", "msg": DHAN_ERRORS.get(code, f"Dhan closed the live feed (code {code})"),
                            "fatal": code in (806, 807, 808, 809, 810)})
        return out


# ---------------------------------------------------------------------------------------------------- Zerodha
KITE_DIV = {3: 10000000.0, 6: 10000.0, 12: 10000.0}


def kite_packets(data: bytes) -> list[dict]:
    if len(data) < 2:
        return []                                   # 1-byte heartbeat
    n = struct.unpack_from(">H", data, 0)[0]
    out, j = [], 2
    for _ in range(n):
        if j + 2 > len(data):
            break
        ln = struct.unpack_from(">H", data, j)[0]
        pk = data[j + 2:j + 2 + ln]
        j += 2 + ln
        if len(pk) < 8:
            continue
        tok = struct.unpack_from(">I", pk, 0)[0]
        div = KITE_DIV.get(tok & 0xFF, 100.0)
        u = lambda a: struct.unpack_from(">I", pk, a)[0]  # noqa: E731
        d = {"token": tok, "ltp": u(4) / div, "vol": -1.0, "ltt": 0.0}
        if len(pk) in (28, 32):                     # index: ltp high low open close [exchange time]
            d.update(high=u(8) / div, low=u(12) / div, open=u(16) / div, close=u(20) / div)
            if len(pk) == 32:
                d["ltt"] = float(u(28))
        elif len(pk) in (44, 184):                  # quote / full
            d.update(vol=float(u(16)), open=u(28) / div, high=u(32) / div, low=u(36) / div, close=u(40) / div)
            if len(pk) == 184:
                d["ltt"] = float(u(44))
                d["oi"] = float(u(48))
        out.append(d)
    return out


class KiteProto(Proto):
    broker = "zerodha"
    ns = "KITE"
    max_keys = 3000

    def url(self):
        return f"wss://ws.kite.trade?api_key={self.acct.get('api_key', '')}&access_token={self.acct.get('access_token', '')}"

    def subscribe(self, keys):
        toks = [int(k[1]) for k in keys]
        if not toks:
            return []
        return [json.dumps({"a": "subscribe", "v": toks}), json.dumps({"a": "mode", "v": ["full", toks]})]

    def unsubscribe(self, keys):
        toks = [int(k[1]) for k in keys]
        return [json.dumps({"a": "unsubscribe", "v": toks})] if toks else []

    def parse(self, data):
        if isinstance(data, str):
            try:
                js = json.loads(data)
            except ValueError:
                return []
            if js.get("type") == "order":
                return [{"kind": "order", "data": js.get("data") or {}}]
            if js.get("type") == "error":
                return [{"kind": "error", "msg": f"Zerodha: {js.get('data')}", "fatal": False}]
            return []
        out = []
        for d in kite_packets(bytes(data)):
            key = (self.ns, str(d["token"]))
            if d["ltp"] > 0:
                out.append({"kind": "tick", "key": key, "ltp": d["ltp"], "ltt": d["ltt"], "vol": d["vol"]})
            if d.get("close"):
                out.append({"kind": "prev", "key": key, "prev": d["close"]})
        return out


# ---------------------------------------------------------------------------------------------------- Upstox
def upstox_feed(data: bytes) -> list[dict]:
    """FeedResponse (MarketDataFeedV3.proto) -> [{'key', 'ltp', 'ltt', 'cp', 'vol'}]."""
    out = []
    for f, wt, v in pb_fields(data):
        if f != 2 or wt != 2:                       # feeds map entries
            continue
        key, feed = "", b""
        for f2, _w2, v2 in pb_fields(v):
            if f2 == 1:
                key = v2.decode("utf8", "replace")
            elif f2 == 2:
                feed = v2
        ltpc, vol = None, -1.0
        for f3, _w3, v3 in pb_fields(feed):
            if f3 == 1:                             # ltpc
                ltpc = v3
            elif f3 == 2:                           # fullFeed
                for f4, _w4, v4 in pb_fields(v3):   # marketFF (1) / indexFF (2)
                    for f5, w5, v5 in pb_fields(v4):
                        if f5 == 1:
                            ltpc = v5
                        elif f4 == 1 and f5 == 6 and w5 == 0:
                            vol = float(v5)         # vtt: volume traded today
            elif f3 == 3:                           # firstLevelWithGreeks
                for f4, w4, v4 in pb_fields(v3):
                    if f4 == 1:
                        ltpc = v4
                    elif f4 == 4 and w4 == 0:
                        vol = float(v4)
        if ltpc is None or not key:
            continue
        d = {"key": key, "ltp": 0.0, "ltt": 0.0, "cp": 0.0, "vol": vol}
        for f6, _w6, v6 in pb_fields(ltpc):
            if f6 == 1:
                d["ltp"] = _d(v6)
            elif f6 == 2:
                d["ltt"] = _signed(v6) / 1000.0
            elif f6 == 4:
                d["cp"] = _d(v6)
        out.append(d)
    return out


class UpstoxProto(Proto):
    broker = "upstox"
    ns = "UPX"
    max_keys = 2000

    def __init__(self, broker):
        super().__init__(broker)
        self._url = ""

    def prepare(self):
        self._url = self.b.feed_url()               # one-time authorised wss address

    def url(self):
        return self._url

    def _req(self, method, keys, mode=None):
        if not keys:
            return []
        body = {"guid": uuid.uuid4().hex[:20], "method": method, "data": {"instrumentKeys": [k[1] for k in keys]}}
        if mode:
            body["data"]["mode"] = mode
        return [json.dumps(body).encode()]          # Upstox wants the JSON as a binary frame

    def subscribe(self, keys):
        return self._req("sub", keys, "full")

    def unsubscribe(self, keys):
        return self._req("unsub", keys)

    def parse(self, data):
        if isinstance(data, str):
            return []
        out = []
        try:
            rows = upstox_feed(bytes(data))
        except Exception:  # noqa: BLE001 - a frame we cannot read is skipped, never fatal
            return []
        for d in rows:
            key = (self.ns, d["key"])
            if d["ltp"] > 0:
                out.append({"kind": "tick", "key": key, "ltp": d["ltp"], "ltt": d["ltt"], "vol": d["vol"]})
            if d["cp"] > 0:
                out.append({"kind": "prev", "key": key, "prev": d["cp"]})
        return out


# ---------------------------------------------------------------------------------------------------- Angel One
ANGEL_EXCH = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5, "NCO": 7, "CDS": 13}


def angel_packet(b: bytes) -> dict | None:
    if len(b) < 51:
        return None
    mode, ex = b[0], b[1]
    tok = b[2:27].split(b"\x00", 1)[0].decode("ascii", "replace")
    ts, ltp = struct.unpack_from("<qq", b, 35)
    div = 10000000.0 if ex == 13 else 100.0
    d = {"mode": mode, "ex": ex, "token": tok, "ltt": ts / 1000.0, "ltp": ltp / div, "vol": -1.0, "close": 0.0}
    if mode in (2, 3) and len(b) >= 123:
        vol = struct.unpack_from("<q", b, 67)[0]
        d["vol"] = float(vol)
        d["close"] = struct.unpack_from("<q", b, 115)[0] / div
    return d


class AngelProto(Proto):
    broker = "angel"
    ns = "ANG"
    ping_every = 25.0
    max_keys = 1000

    def key(self, inst):
        return (str(ANGEL_EXCH.get(inst.exchange, 1)), str(inst.token))

    def url(self):
        import urllib.parse
        q = {"clientCode": self.acct.get("client_code", ""), "feedToken": self.acct.get("feed_token", ""),
             "apiKey": self.acct.get("api_key", "")}
        return "wss://smartapisocket.angelone.in/smart-stream?" + urllib.parse.urlencode(q)

    def headers(self):
        return {"Authorization": self.acct.get("access_token", ""), "x-api-key": self.acct.get("api_key", ""),
                "x-client-code": self.acct.get("client_code", ""), "x-feed-token": self.acct.get("feed_token", "")}

    def _req(self, action, keys):
        by: dict = {}
        for ex, tok in keys:
            by.setdefault(int(ex), []).append(tok)
        if not by:
            return []
        return [json.dumps({"correlationID": "chartlab01", "action": action,
                            "params": {"mode": 2, "tokenList": [{"exchangeType": e, "tokens": t}
                                                                for e, t in sorted(by.items())]}})]

    def subscribe(self, keys):
        return self._req(1, keys)

    def unsubscribe(self, keys):
        return self._req(0, keys)

    def ping(self):
        return "ping"

    def parse(self, data):
        if isinstance(data, str):
            if data.strip().lower() == "pong":
                return []
            try:
                js = json.loads(data)
            except ValueError:
                return []
            if js.get("errorCode") or js.get("errorMessage"):
                msg = f"Angel One live feed: {js.get('errorMessage') or js.get('errorCode')}"
                return [{"kind": "error", "msg": msg, "fatal": "token" in msg.lower() or "auth" in msg.lower()}]
            return []
        d = angel_packet(bytes(data))
        if not d:
            return []
        key = (str(d["ex"]), d["token"])
        out = []
        if d["ltp"] > 0:
            out.append({"kind": "tick", "key": key, "ltp": d["ltp"], "ltt": d["ltt"], "vol": d["vol"]})
        if d["close"] > 0:
            out.append({"kind": "prev", "key": key, "prev": d["close"]})
        return out


# ---------------------------------------------------------------------------------------------------- Fyers (HSM)
FY_SCRIP = ["ltp", "vol_traded_today", "last_traded_time", "exch_feed_time", "bid_size", "ask_size", "bid_price",
            "ask_price", "last_traded_qty", "tot_buy_qty", "tot_sell_qty", "avg_trade_price", "OI", "low_price",
            "high_price", "Yhigh", "Ylow", "lower_ckt", "upper_ckt", "open_price", "prev_close_price", "type",
            "symbol"]
FY_INDEX = ["ltp", "prev_close_price", "exch_feed_time", "high_price", "low_price", "open_price", "type", "symbol"]
_MISSING = -2147483648


def fyers_auth_msg(token: str, source: str = "PythonSDK-3.3.0", mode: str = "P") -> bytes:
    b = bytearray()
    size = 18 + len(token) + len(source)
    b += struct.pack("!H", size - 2) + bytes([1, 4])
    b += bytes([1]) + struct.pack("!H", len(token)) + token.encode()
    b += bytes([2]) + struct.pack("!H", 1) + mode.encode()
    b += bytes([3]) + struct.pack("!H", 1) + bytes([1])
    b += bytes([4]) + struct.pack("!H", len(source)) + source.encode()
    return bytes(b)


def fyers_mode_msg(channel: int = 11, full: bool = True) -> bytes:
    b = bytearray(struct.pack(">H", 0) + struct.pack("B", 12) + struct.pack("B", 2))
    b += struct.pack("B", 1) + struct.pack(">H", 8) + struct.pack(">Q", 1 << channel)
    b += struct.pack("B", 2) + struct.pack(">H", 1) + struct.pack("B", 70 if full else 76)
    return bytes(b)


def fyers_sub_msg(topics: list, token: str, source: str = "PythonSDK-3.3.0", channel: int = 11,
                  unsubscribe: bool = False) -> bytes:
    data = bytearray([len(topics) >> 8 & 0xFF, len(topics) & 0xFF])
    for t in topics:
        tb = str(t).encode("ascii")
        data.append(len(tb))
        data += tb
    b = bytearray(struct.pack(">H", 18 + len(data) + len(token) + len(source)))
    b += bytes([5 if unsubscribe else 4, 2, 1]) + struct.pack(">H", len(data)) + data
    b += bytes([2]) + struct.pack(">H", 1) + bytes([channel])
    return bytes(b)


def fyers_ack_msg(n: int) -> bytes:
    return struct.pack(">H", 9) + bytes([3, 1, 1]) + struct.pack(">H", 4) + struct.pack(">I", n)


class FyersProto(Proto):
    broker = "fyers"
    ns = "FY"
    ping_every = 10.0
    max_keys = 5000

    def __init__(self, broker):
        super().__init__(broker)
        self.topics: dict = {}       # hsm topic name -> key
        self.by_id: dict = {}        # topic id -> [topic name, kind, state dict]
        self.ack_every = 0
        self._count = 0

    @property
    def token(self) -> str:
        return f"{self.acct.get('app_id', '')}:{self.acct.get('access_token', '')}"

    def key(self, inst):
        topic = self.b.hsm_topic(inst)
        k = (self.ns, inst.token)
        self.topics[topic] = k
        return k

    def url(self):
        return "wss://socket.fyers.in/hsm/v1-5/prod"

    def on_open(self):
        self.by_id, self._count = {}, 0
        return [fyers_auth_msg(self.token), fyers_mode_msg()]

    def _names(self, keys):
        want = set(keys)
        return [t for t, k in self.topics.items() if k in want]

    def subscribe(self, keys):
        names = self._names(keys)
        return [fyers_sub_msg(names[i:i + 1500], self.token) for i in range(0, len(names), 1500)]

    def unsubscribe(self, keys):
        names = self._names(keys)
        return [fyers_sub_msg(names[i:i + 1500], self.token, unsubscribe=True) for i in range(0, len(names), 1500)]

    def ping(self):
        return bytes([0, 1, 11])

    def parse(self, data):
        if isinstance(data, str):
            return []
        b = bytes(data)
        if len(b) < 3:
            return []
        typ = b[2]
        try:
            if typ == 1:
                return self._auth(b)
            if typ == 6:
                return self._feed(b)
        except (struct.error, IndexError, UnicodeDecodeError):
            return []
        return []

    def _auth(self, b):
        off = 5
        ln = struct.unpack_from("!H", b, off)[0]
        off += 2
        ok = b[off:off + ln].decode("utf8", "replace") == "K"
        off += ln + 1
        if off + 6 <= len(b):
            off += 2
            self.ack_every = struct.unpack_from(">I", b, off)[0]
        return [] if ok else [{"kind": "error", "msg": "Fyers did not accept the login for live prices - log in again",
                               "fatal": True}]

    def _emit(self, st: dict, names: list, topic: str) -> list:
        k = self.topics.get(topic)
        if k is None:
            return []
        div = (10 ** st.get("precision", 2)) * (st.get("multiplier") or 1)
        out = []
        ltp = st.get("ltp")
        if ltp is not None and ltp != _MISSING and ltp > 0:
            t = st.get("last_traded_time") or st.get("exch_feed_time") or 0
            out.append({"kind": "tick", "key": k, "ltp": ltp / div, "ltt": float(t),
                        "vol": float(st.get("vol_traded_today", -1))})
        pc = st.get("prev_close_price")
        if pc and pc > 0:
            out.append({"kind": "prev", "key": k, "prev": pc / div})
        return out

    def _feed(self, b):
        out = []
        if self.ack_every:
            self._count += 1
            if self._count >= self.ack_every:
                self._count = 0
                out.append({"kind": "send", "data": fyers_ack_msg(struct.unpack_from(">I", b, 3)[0])})
        n = struct.unpack_from("!H", b, 7)[0]
        off = 9
        for _ in range(n):
            dt_ = b[off]
            off += 1
            if dt_ == 83:                                    # snapshot
                tid = struct.unpack_from("<H", b, off)[0]
                off += 2
                tl = b[off]
                off += 1
                topic = b[off:off + tl].decode("utf8", "replace")
                off += tl
                fc = b[off]
                off += 1
                names = FY_INDEX if topic.startswith("if") else FY_SCRIP
                st: dict = {}
                for i in range(fc):
                    v = struct.unpack_from(">i", b, off)[0]
                    off += 4
                    if v != _MISSING and i < len(names):
                        st[names[i]] = v
                off += 2
                st["multiplier"] = struct.unpack_from(">H", b, off)[0]
                off += 2
                st["precision"] = b[off]
                off += 1
                for _s in range(3):
                    sl = b[off]
                    off += 1 + sl
                self.by_id[tid] = [topic, names, st]
                out += self._emit(st, names, topic)
            elif dt_ == 85:                                  # update
                tid = struct.unpack_from("<H", b, off)[0]
                off += 2
                fc = b[off]
                off += 1
                ent = self.by_id.get(tid)
                for i in range(fc):
                    v = struct.unpack_from(">i", b, off)[0]
                    off += 4
                    if ent is not None and v != _MISSING and i < len(ent[1]):
                        ent[2][ent[1][i]] = v
                if ent is not None:
                    out += self._emit(ent[2], ent[1], ent[0])
            elif dt_ == 76:                                  # lite update: ltp only
                tid = struct.unpack_from("<H", b, off)[0]
                off += 2
                v = struct.unpack_from(">i", b, off)[0]
                off += 4
                ent = self.by_id.get(tid)
                if ent is not None and v != _MISSING:
                    ent[2]["ltp"] = v
                    out += self._emit(ent[2], ent[1], ent[0])
            else:
                break
        return out


PROTOS = {"dhan": DhanProto, "zerodha": KiteProto, "upstox": UpstoxProto, "angel": AngelProto, "fyers": FyersProto}


def proto_for(broker) -> Proto | None:
    c = PROTOS.get(getattr(broker, "id", ""))
    return c(broker) if c else None
