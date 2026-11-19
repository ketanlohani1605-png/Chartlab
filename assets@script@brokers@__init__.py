"""One way to talk to every supported broker.

    from chartlab import brokers
    b = brokers.get(account_dict)          # Dhan, Zerodha, Upstox, Angel One, Fyers or Paper
    b.place(brokers.OrderRequest("NIFTY 24NOV 25000 CE", "BUY", lots=1, order_type="LIMIT", price=120))

Each adapter lists what its broker can do in `caps`; the app shows only what the chosen account supports.
Accounts that are not switched to LIVE never reach a broker: their orders go to a paper account of the same name."""
from __future__ import annotations

import concurrent.futures as cf
import threading
import time

from .angel import Angel
from .base import (DONE_STATES, OPEN_STATES, ORDER_TYPES, PRODUCTS, AccessError, AuthError, Broker, BrokerError,
                   Funds, Holding, Instrument, NetError, Order, OrderRequest, Position, RateLimited, Rejected,
                   SentNoAnswer, parse_symbol)
from .dhan import Dhan
from .fyers import Fyers
from .paper import Paper
from .upstox import Upstox
from .zerodha import Zerodha

REGISTRY: dict = {"dhan": Dhan, "zerodha": Zerodha, "upstox": Upstox, "angel": Angel, "fyers": Fyers,
                  "paper": Paper}
ORDER = ["dhan", "zerodha", "upstox", "angel", "fyers", "paper"]
NAMES = {k: REGISTRY[k].name for k in ORDER}

_CACHE: dict = {}
_LOCK = threading.Lock()


def cls_of(broker_id: str):
    try:
        return REGISTRY[broker_id]
    except KeyError:
        raise BrokerError(f"ChartLab does not know the broker '{broker_id}'") from None


def get(acct: dict, http=None, base_url: str | None = None) -> Broker:
    """The adapter for one saved account (re-used while its credentials stay the same)."""
    c = cls_of(acct.get("broker", "paper"))
    sig = (acct.get("name"), acct.get("broker"), acct.get("access_token"), acct.get("api_key"), acct.get("app_id"),
           acct.get("client_id"), acct.get("client_code"), acct.get("login_time"), base_url)
    if http is not None or base_url is not None:
        return c(acct, http=http, base_url=base_url)
    with _LOCK:
        hit = _CACHE.get(acct.get("name"))
        if hit is not None and hit[0] == sig:
            hit[1].acct.update(acct)
            return hit[1]
        b = c(acct)
        _CACHE[acct.get("name")] = (sig, b)
        return b


def paper_for(acct: dict) -> Paper:
    """The paper account that stands in for a real account while it is not LIVE."""
    name = acct.get("name") or "Paper"
    return get({"name": name if acct.get("broker") == "paper" else f"{name} (paper)", "broker": "paper",
                "capital": acct.get("capital"), "commission": acct.get("commission")})


def trading_broker(acct: dict) -> Broker:
    """Where this account's orders go: the real broker only when the account is switched to LIVE."""
    if acct.get("broker") == "paper" or not acct.get("live"):
        return paper_for(acct)
    return get(acct)


def describe(broker_id: str) -> dict:
    c = cls_of(broker_id)
    return {"id": broker_id, "name": c.name, "fields": list(c.fields), "caps": sorted(c.caps),
            "products": list(c.products), "order_types": list(c.order_types), "exchanges": list(c.exchanges),
            "docs": c.docs, "note": c.data_note}


def place_many(accts: list, req: OrderRequest, live_ok: bool = True) -> list[dict]:
    """Send ONE order to every account at the same instant (threads released together). Each account's instrument
    is looked up first, in its own broker's list; one account failing never blocks the others.
    Accounts that are not LIVE (or when live_ok is False) trade on their paper account."""
    prepared = []
    for a in accts:
        b = trading_broker(a) if live_ok else paper_for(a)
        try:
            data_b = get(a) if a.get("broker") != "paper" and a.get("access_token") else b
            inst = data_b.resolve(req.symbol) if b.id != "paper" else None
            r = OrderRequest(**{**req.__dict__, "instrument": inst})
            mult = float(a.get("qty_mult") or 1)
            if mult != 1:
                lot = inst.lot if inst is not None else 1
                if r.lots:
                    r.lots = max(1, int(round(r.lots * mult)))
                else:
                    r.qty = max(lot, int(round(r.qty * mult / lot)) * lot)
            prepared.append((a, b, b.prepare(r) if b.id != "paper" else r, None))
        except Exception as e:  # noqa: BLE001
            prepared.append((a, b, None, str(e)))
    bar = threading.Barrier(max(1, len(prepared)))

    def one(item):
        a, b, r, err = item
        bar.wait()
        t = time.perf_counter()
        if err:
            return {"account": a.get("name"), "ok": False, "error": err, "ms": 0}
        try:
            oid = b.place(r)
            return {"account": a.get("name"), "ok": True, "order_id": oid, "paper": b.id == "paper",
                    "ms": round((time.perf_counter() - t) * 1000), "broker": b.name}
        except Exception as e:  # noqa: BLE001
            return {"account": a.get("name"), "ok": False, "error": str(e), "kind": getattr(e, "kind", "error"),
                    "ms": round((time.perf_counter() - t) * 1000), "broker": b.name}

    with cf.ThreadPoolExecutor(max(1, len(prepared))) as ex:
        return list(ex.map(one, prepared))


__all__ = ["REGISTRY", "ORDER", "NAMES", "get", "paper_for", "trading_broker", "describe", "place_many", "Broker",
           "BrokerError", "AuthError", "AccessError", "RateLimited", "Rejected", "NetError", "SentNoAnswer",
           "Instrument", "Order", "OrderRequest", "Position", "Holding", "Funds", "OPEN_STATES", "DONE_STATES",
           "ORDER_TYPES", "PRODUCTS", "parse_symbol", "Dhan", "Zerodha", "Upstox", "Angel", "Fyers", "Paper"]
