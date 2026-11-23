"""Every NSE stock with its name, tick size and sector (assets/nse_symbols.json - built from NSE's own equity, SME
and ETF lists, Dhan's tick sizes and NSE's sector classification of the Nifty Total Market companies) - so Symbol
Search finds BHEL, BHARATFORG ... the way TradingView's does, the legend shows the company's real name ('ABB India
Limited', not 'ABB Limited'), the price scale uses the stock's own tick ('1,358.5' for RELIANCE, which moves in
0.10 steps) and an old symbol finds the new one (ZOMATO -> ETERNAL)."""
from __future__ import annotations

import json

_DATA: dict | None = None


def _load() -> dict:
    global _DATA
    if _DATA is None:
        out = {"list": [], "name": {}, "tick": {}, "sector": {}, "nifty50": [], "renamed": {}, "etf": set()}
        try:
            from .data import ROOT
            raw = json.loads((ROOT / "assets" / "nse_symbols.json").read_text(encoding="utf-8"))
            for row in raw.get("eq", []):
                sym, name, tick = row[0], row[1], row[2]
                s = str(sym).upper()
                if "NSETEST" in s:                        # the exchange's test symbols (011NSETEST ...): not stocks
                    continue
                out["list"].append(s)
                out["name"][s] = str(name)
                out["tick"][s] = float(tick) / 100.0          # paise -> rupees
                if len(row) > 3 and row[3]:
                    out["sector"][s] = str(row[3])
            out["nifty50"] = [str(x).upper() for x in raw.get("nifty50", [])]
            out["renamed"] = {str(k).upper(): str(v).upper() for k, v in (raw.get("renamed") or {}).items()}
            out["etf"] = {str(x).upper() for x in raw.get("etf", [])}
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


def sector_of(sym: str) -> str:
    """NSE's sector for a company ('Financial Services', 'Information Technology' ...; '' when it is not one of the
    750 companies NSE classifies in the Nifty Total Market)."""
    return _load()["sector"].get(_key(sym), "")


def nifty50() -> list[str]:
    """The NIFTY 50's members as NSE lists them (empty when the list is missing)."""
    return list(_load()["nifty50"])


def renamed() -> dict:
    """Old NSE symbol -> the one it trades under now (ZOMATO -> ETERNAL, MCDOWELL-N -> UNITDSPR ...)."""
    return dict(_load()["renamed"])


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


def is_etf(sym: str) -> bool:
    """On NSE's list of ETFs (NIFTYBEES, GOLDBEES, BANKCASE, MOMMIDCAP ... - not all say 'ETF' in their name)."""
    k = _key(sym)
    if not k:
        return False
    if k in _load()["etf"]:
        return True
    n = _load()["name"].get(k, "").upper()
    return k.endswith(("BEES", "ETF")) or " ETF" in f" {n}"


def variable_tick(sym: str) -> bool:
    """NSE stocks move in steps that depend on the price (TradingView formats each price with the step of its own
    level: HDFCBANK 809.20, RELIANCE 1,358.5, BAJAJ-AUTO 10,587). ETFs and indices keep 0.01."""
    k = _key(sym)
    if not k or k not in _load()["tick"]:
        return False
    return not (is_etf(k) or "FUND" in _load()["name"].get(k, "").upper())


def band_decimals(price: float) -> int:
    """NSE's price-band tick (2025): below 1,000 -> 0.01 / 0.05 (2 decimals), 1,000 - 10,000 -> 0.10 / 0.50 (1),
    from 10,000 -> 1 / 5 rupees (0). TradingView: 809.20, 1,001.5, 9,123.5, 10,587, 128,310."""
    p = abs(float(price))
    return 2 if p < 1000 else (1 if p < 10000 else 0)


_LOTS: dict | None = None


def fo_lot(underlying: str, month: str | None = None) -> int | None:
    """NSE's lot size for an F&O underlying (assets/fo_lots.json, from NSE's market-lot list): NIFTY 65, BANKNIFTY
    30, RELIANCE 500 ... for the contract month 'YYYY-MM' - lots change from a given expiry (TCS 225 in November
    2026, 250 from January 2027) - else the nearest month listed. None when it is not traded in F&O."""
    global _LOTS
    if _LOTS is None:
        try:
            from .data import ROOT
            _LOTS = json.loads((ROOT / "assets" / "fo_lots.json").read_text(encoding="utf-8")).get("lots", {})
        except Exception:  # noqa: BLE001
            _LOTS = {}
    m = _LOTS.get((underlying or "").strip().upper())
    if not m:
        return None
    if month and month in m:
        return int(m[month])
    keys = sorted(m)
    if month:
        later = [k for k in keys if k >= month]
        return int(m[later[0]] if later else m[keys[-1]])
    return int(m[keys[0]])
