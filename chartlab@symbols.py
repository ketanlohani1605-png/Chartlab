"""Every NSE stock with its name and tick size (assets/nse_symbols.json, built from Dhan's public instrument list) -
so Symbol Search finds BHEL, BHARATFORG ... the way TradingView's does, the legend shows the company's name and
the price scale uses the stock's own tick ('1,358.5' for RELIANCE, which moves in 0.10 steps)."""
from __future__ import annotations

import json

_DATA: dict | None = None


def _load() -> dict:
    global _DATA
    if _DATA is None:
        out = {"list": [], "name": {}, "tick": {}}
        try:
            from .data import ROOT
            raw = json.loads((ROOT / "assets" / "nse_symbols.json").read_text(encoding="utf-8"))
            for sym, name, tick in raw.get("eq", []):
                s = str(sym).upper()
                out["list"].append(s)
                out["name"][s] = str(name)
                out["tick"][s] = float(tick) / 100.0          # paise -> rupees
        except Exception:  # noqa: BLE001 - no list: search still knows the watchlist and the usual names
            pass
        _DATA = out
    return _DATA


def nse_symbols() -> list[str]:
    return _load()["list"]


def _key(sym: str) -> str:
    s = (sym or "").strip().upper()
    if ":" in s:
        ex, s = s.split(":", 1)
        if ex not in ("NSE", ""):
            return ""
    for suf in (".NS",):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s


def name_of(sym: str) -> str:
    """The company name for an NSE symbol ('' when unknown)."""
    return _load()["name"].get(_key(sym), "")


def tick_of(sym: str) -> float | None:
    """The stock's tick size in rupees on NSE (0.05, 0.10, 1.00 ...), None when unknown."""
    return _load()["tick"].get(_key(sym))


def tick_decimals(sym: str) -> int | None:
    """Decimals TradingView shows for the symbol: 0.05 -> 2, 0.10 -> 1, 1.00 -> 0."""
    t = tick_of(sym)
    if not t:
        return None
    for d in range(0, 5):
        x = t * (10 ** d)
        if abs(x - round(x)) < 1e-6:
            return d
    return 2


def variable_tick(sym: str) -> bool:
    """NSE stocks move in steps that depend on the price (TradingView formats each price with the step of its own
    level: HDFCBANK 809.20, RELIANCE 1,358.5, BAJAJ-AUTO 10,587). ETFs and indices keep 0.01."""
    k = _key(sym)
    if not k or k not in _load()["tick"]:
        return False
    n = _load()["name"].get(k, "").upper()
    return not ("ETF" in n or k.endswith("BEES") or k.endswith("ETF") or "FUND" in n)


def band_decimals(price: float) -> int:
    """NSE's price-band tick (2025): below 1,000 -> 0.01 / 0.05 (2 decimals), 1,000 - 10,000 -> 0.10 / 0.50 (1),
    from 10,000 -> 1 / 5 rupees (0). TradingView: 809.20, 1,001.5, 9,123.5, 10,587, 128,310."""
    p = abs(float(price))
    return 2 if p < 1000 else (1 if p < 10000 else 0)
