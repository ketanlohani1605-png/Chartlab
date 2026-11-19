"""Paper trading: a pretend broker that fills orders against the prices ChartLab is already showing.

Market orders fill at the last price; limit, stop (SL-M) and stop-limit (SL) orders wait until the price gets there.
An order with a stop loss and / or take profit becomes a bracket: when the entry fills, both exits are placed and the
first one that fills cancels the other. Positions, cash and the order history are kept per paper account in
~/ChartLab/paper/<account>.json, so they survive restarts."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
from pathlib import Path

from .base import (USER_DIR, Broker, BrokerError, Funds, Instrument, Order, OrderRequest, Position, Rejected,
                   parse_symbol)

DIR = USER_DIR / "paper"
START_CASH = 1_000_000.0


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).strip().upper())


class Paper(Broker):
    id = "paper"
    name = "Paper trading"
    caps = frozenset({"orders", "modify", "cancel", "positions", "funds", "bracket"})
    fields = [("capital", "Starting cash", "text", False, "Default 10,00,000"),
              ("commission", "Charges per order", "text", False, "Flat amount per filled order, default 0")]
    products = ("INTRADAY", "DELIVERY", "MARGIN", "MTF")
    exchanges = ("NSE", "BSE", "NFO", "BFO", "MCX")

    _locks: dict = {}
    _prices: dict = {}                     # SYMBOL -> last price, shared by every paper account

    def __init__(self, acct: dict, http=None, base_url=None, folder: Path | None = None):
        super().__init__(acct, http, base_url)
        self.dir = folder or DIR
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", self.acct.get("name") or "paper")
        self.path = self.dir / f"{safe}.json"
        self.lock = Paper._locks.setdefault(str(self.path), threading.RLock())
        self.listeners: list = []          # callables(event: str, payload) - the trading panel listens

    # ------------------------------------------------------------------ store
    def _blank(self) -> dict:
        cap = _num(self.acct.get("capital"), START_CASH)
        return {"start": cap, "realised": 0.0, "charges": 0.0, "seq": 0, "orders": [], "positions": {},
                "fills": [], "created": time.time()}

    _mem: dict = {}                        # path -> (mtime, state): prices arrive many times a second

    def _load(self) -> dict:
        key = str(self.path)
        try:
            mt = self.path.stat().st_mtime_ns
        except OSError:
            mt = None
        hit = Paper._mem.get(key)
        if hit is not None and hit[0] == mt and mt is not None:
            return json.loads(hit[1])
        try:
            raw = self.path.read_text()
            st = json.loads(raw)
            if isinstance(st, dict) and "orders" in st:
                Paper._mem[key] = (mt, raw, _working(st))
                return st
        except Exception:  # noqa: BLE001
            pass
        return self._blank()

    def _save(self, st: dict):
        self.dir.mkdir(parents=True, exist_ok=True)
        st["orders"] = st["orders"][-500:]
        st["fills"] = st["fills"][-1000:]
        raw = json.dumps(st, indent=1)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(raw)
        os.replace(tmp, self.path)
        try:
            Paper._mem[str(self.path)] = (self.path.stat().st_mtime_ns, raw, _working(st))
        except OSError:
            Paper._mem.pop(str(self.path), None)

    def working_keys(self) -> set:
        """Symbols with an order waiting for a price (cheap: no file read or parse when nothing changed)."""
        key = str(self.path)
        try:
            mt = self.path.stat().st_mtime_ns
        except OSError:
            return set()
        hit = Paper._mem.get(key)
        if hit is None or hit[0] != mt:
            self._load()
            hit = Paper._mem.get(key)
        return hit[2] if hit else set()

    def _emit(self, event: str, payload=None):
        for cb in list(self.listeners):
            try:
                cb(event, payload)
            except Exception:  # noqa: BLE001
                pass

    def reset(self, capital: float | None = None):
        with self.lock:
            st = self._blank()
            if capital:
                st["start"] = float(capital)
            self._save(st)
        self._emit("reset")

    # ------------------------------------------------------------------ prices
    @classmethod
    def set_price(cls, symbol: str, price: float):
        if price and price > 0:
            cls._prices[_key(symbol)] = float(price)

    def on_price(self, symbol: str, price: float) -> list[str]:
        """A new price for a symbol: fill whatever it reaches. -> ids of orders that filled."""
        Paper.set_price(symbol, price)
        k = _key(symbol)
        filled = []
        if k not in self.working_keys():
            return filled
        with self.lock:
            st = self._load()
            changed = False
            for o in st["orders"]:
                if o["status"] not in ("open", "trigger_pending") or o["key"] != k:
                    continue
                if o["status"] == "trigger_pending":
                    buy = o["side"] == "BUY"
                    if not (price >= o["trigger"] if buy else price <= o["trigger"]):
                        continue
                    if o["order_type"] == "SL-M":            # stop reached: fill at the market
                        self._fill(st, o, price)
                        filled.append(o["id"])
                        changed = True
                        continue
                    o["status"] = "open"                      # stop-limit reached: a limit order from now on
                    changed = True
                px = self._reach(o, price)
                if px is None:
                    continue
                self._fill(st, o, px)
                filled.append(o["id"])
                changed = True
            if changed:
                self._save(st)
        for oid in filled:
            self._emit("fill", oid)
        return filled

    @staticmethod
    def _reach(o: dict, px: float):
        """Fill price of a working (not stop-waiting) order at market price px, or None."""
        if o["order_type"] in ("LIMIT", "SL"):
            lim = o["price"]
            if o["side"] == "BUY":
                return px if px <= lim else None
            return px if px >= lim else None
        return px

    # ------------------------------------------------------------------ engine
    def _fill(self, st: dict, o: dict, px: float):
        o.update(status="complete", filled=o["qty"], pending=0, avg_price=round(px, 4),
                 time=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        q = o["qty"] if o["side"] == "BUY" else -o["qty"]
        pk = f"{o['key']}|{o['product']}"
        p = st["positions"].get(pk) or {"key": o["key"], "symbol": o["symbol"], "exchange": o.get("exchange", ""),
                                        "product": o["product"], "qty": 0, "avg": 0.0, "realised": 0.0,
                                        "buy_qty": 0, "sell_qty": 0, "buy_val": 0.0, "sell_val": 0.0,
                                        "lot": o.get("lot", 1)}
        old = p["qty"]
        if o["side"] == "BUY":
            p["buy_qty"] += o["qty"]
            p["buy_val"] += o["qty"] * px
        else:
            p["sell_qty"] += o["qty"]
            p["sell_val"] += o["qty"] * px
        if old == 0 or (old > 0) == (q > 0):                       # opening or adding
            p["avg"] = (abs(old) * p["avg"] + abs(q) * px) / (abs(old) + abs(q))
            p["qty"] = old + q
        else:                                                      # reducing, closing or flipping
            close = min(abs(old), abs(q))
            gain = (px - p["avg"]) * close * (1 if old > 0 else -1)
            p["realised"] += gain
            st["realised"] += gain
            p["qty"] = old + q
            if p["qty"] == 0:
                p["avg"] = 0.0
            elif (p["qty"] > 0) != (old > 0):                      # flipped: the rest opens at this price
                p["avg"] = px
        comm = _num(self.acct.get("commission"), 0.0)
        if comm:
            st["charges"] += comm
        st["positions"][pk] = p
        st["fills"].append({"id": o["id"], "symbol": o["symbol"], "side": o["side"], "qty": o["qty"], "price": px,
                            "time": o["time"]})
        # bracket: the entry filled -> place both exits, linked so one cancels the other
        if o.get("sl") or o.get("tp"):
            grp = o["id"]
            exit_side = "SELL" if o["side"] == "BUY" else "BUY"
            sgn = 1 if o["side"] == "BUY" else -1
            kids = []
            if o.get("tp"):
                kids.append(self._new(st, o["symbol"], o["key"], exit_side, o["qty"], "LIMIT", o["product"],
                                      price=px + sgn * o["tp"], parent=grp, lot=o.get("lot", 1)))
            if o.get("sl"):
                kids.append(self._new(st, o["symbol"], o["key"], exit_side, o["qty"], "SL-M", o["product"],
                                      trigger=px - sgn * o["sl"], parent=grp, lot=o.get("lot", 1)))
            for k in kids:
                k["oco"] = [x["id"] for x in kids if x is not k]
        for other in o.get("oco") or []:
            for x in st["orders"]:
                if x["id"] == other and x["status"] in ("open", "trigger_pending"):
                    x["status"], x["message"] = "cancelled", "other side of the bracket filled"

    def _new(self, st, symbol, key, side, qty, order_type, product, price=0.0, trigger=0.0, parent="", lot=1,
             sl=0.0, tp=0.0, tag="chartlab", exchange="") -> dict:
        st["seq"] += 1
        o = {"id": f"P{int(time.time()) % 100000:05d}{st['seq']:04d}", "symbol": symbol, "key": key, "side": side,
             "qty": int(qty), "filled": 0, "pending": int(qty), "order_type": order_type, "product": product,
             "price": float(price or 0), "trigger": float(trigger or 0), "avg_price": 0.0,
             "status": "trigger_pending" if order_type in ("SL", "SL-M") else "open", "message": "",
             "time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "parent": parent, "lot": lot, "sl": float(sl or 0),
             "tp": float(tp or 0), "tag": tag, "exchange": exchange}
        st["orders"].append(o)
        return o

    # ------------------------------------------------------------------ Broker interface
    def has_token(self) -> bool:
        return True

    def status(self) -> dict:
        f = self.funds()
        pos = self.positions()
        return {"state": "ok", "msg": "Paper trading - no real orders", "client": "paper", "funds": f.available,
                "expires": None, "open": sum(1 for p in pos if p.qty), "pnl": sum(p.pnl for p in pos),
                "broker": self.name, "ts": dt.datetime.now().strftime("%H:%M:%S")}

    def profile(self) -> dict:
        return {"client": "paper", "name": self.acct.get("name", "Paper")}

    def resolve(self, symbol) -> Instrument:
        if isinstance(symbol, Instrument):
            return symbol
        try:
            sp = parse_symbol(str(symbol))
        except ValueError as e:
            raise BrokerError(str(e)) from None
        return Instrument(broker="paper", token=_key(symbol), exchange=sp.exchange or "NSE", tsym=_key(symbol),
                          name=sp.name, kind="EQ" if sp.kind == "IDX" else sp.kind, lot=1, tick=0.05)

    def place(self, req: OrderRequest) -> str:
        if req.instrument is not None and req.instrument.kind == "IDX":
            req = OrderRequest(**{**req.__dict__, "instrument": None})     # paper may trade an index at its price
        r = self.prepare(req)
        key = _key(req.symbol)
        last = Paper._prices.get(key)
        if r.order_type == "MARKET" and not last:
            raise Rejected(f"Paper: no price yet for {req.symbol} - open its chart first")
        with self.lock:
            st = self._load()
            o = self._new(st, req.symbol.strip().upper(), key, r.side, r.qty, r.order_type, r.product, r.price,
                          r.trigger, lot=r.instrument.lot if r.instrument else 1, sl=r.stop_loss, tp=r.take_profit,
                          tag=r.tag, exchange=r.instrument.exchange if r.instrument else "")
            if last and r.order_type == "MARKET":
                self._fill(st, o, last)
            self._save(st)
        self._emit("order", o["id"])
        if last and r.order_type != "MARKET":
            self.on_price(key, last)            # a limit already through the market fills at once
        return o["id"]

    def modify(self, order: Order, qty=None, price=None, trigger=None, order_type=None) -> str:
        with self.lock:
            st = self._load()
            for o in st["orders"]:
                if o["id"] == order.id:
                    if o["status"] not in ("open", "trigger_pending"):
                        raise Rejected(f"Paper: order {order.id} is {o['status']} and cannot be changed")
                    if qty:
                        o["qty"] = o["pending"] = int(qty)
                    if price is not None:
                        o["price"] = float(price)
                    if trigger is not None:
                        o["trigger"] = float(trigger)
                    if order_type:
                        o["order_type"] = order_type
                        o["status"] = "trigger_pending" if order_type in ("SL", "SL-M") else "open"
                    break
            else:
                raise BrokerError(f"Paper: no order {order.id}")
            self._save(st)
        self._emit("order", order.id)
        last = Paper._prices.get(o["key"])
        if last:
            self.on_price(o["key"], last)
        return order.id

    def cancel(self, order: Order) -> str:
        with self.lock:
            st = self._load()
            for o in st["orders"]:
                if o["id"] == order.id:
                    if o["status"] not in ("open", "trigger_pending"):
                        raise Rejected(f"Paper: order {order.id} is already {o['status']}")
                    o["status"], o["message"] = "cancelled", "cancelled by you"
                    break
            else:
                raise BrokerError(f"Paper: no order {order.id}")
            self._save(st)
        self._emit("order", order.id)
        return order.id

    def orders(self) -> list[Order]:
        st = self._load()
        return [Order(id=o["id"], symbol=o["symbol"], exchange=o.get("exchange", ""), side=o["side"], qty=o["qty"],
                      filled=o["filled"], pending=o["pending"], order_type=o["order_type"], product=o["product"],
                      price=o["price"], trigger=o["trigger"], avg_price=o["avg_price"], status=o["status"],
                      message=o.get("message", ""), time=o["time"], token=o["key"], tag=o.get("tag", ""), raw=o)
                for o in reversed(st["orders"])]

    def positions(self) -> list[Position]:
        st = self._load()
        out = []
        for p in st["positions"].values():
            ltp = Paper._prices.get(p["key"], p["avg"])
            unreal = (ltp - p["avg"]) * p["qty"] if p["qty"] else 0.0
            out.append(Position(symbol=p["symbol"], exchange=p.get("exchange", ""), product=p["product"], qty=p["qty"],
                                avg=p["avg"], ltp=ltp, pnl=p["realised"] + unreal, realised=p["realised"],
                                unrealised=unreal, buy_qty=p["buy_qty"], sell_qty=p["sell_qty"],
                                buy_avg=p["buy_val"] / p["buy_qty"] if p["buy_qty"] else 0.0,
                                sell_avg=p["sell_val"] / p["sell_qty"] if p["sell_qty"] else 0.0,
                                token=p["key"], raw=p))
        return out

    def holdings(self) -> list:
        return []

    def funds(self) -> Funds:
        st = self._load()
        pos = self.positions()
        unreal = sum(p.unrealised for p in pos)
        used = sum(abs(p.qty) * p.avg for p in pos)
        balance = st["start"] + st["realised"] - st["charges"]
        return Funds(available=balance + unreal - used, used=used, total=balance + unreal,
                     raw={"start": st["start"], "realised": st["realised"], "charges": st["charges"],
                          "balance": balance, "unrealised": unreal})

    def exit_position(self, pos: Position) -> str:
        if not pos.qty:
            raise BrokerError("nothing to close")
        return self.place(OrderRequest(symbol=pos.symbol, side="SELL" if pos.qty > 0 else "BUY", qty=abs(pos.qty),
                                       product=pos.product or "INTRADAY", tag="chartlab_exit"))

    def ltp(self, instruments) -> dict:
        return {i.token: Paper._prices[_key(i.token)] for i in instruments if _key(i.token) in Paper._prices}


def _working(st: dict) -> set:
    return {o["key"] for o in st.get("orders", []) if o.get("status") in ("open", "trigger_pending")}


def _num(x, default: float) -> float:
    try:
        v = float(str(x).replace(",", ""))
        return v if v > 0 or default == 0.0 else default
    except (TypeError, ValueError):
        return default
