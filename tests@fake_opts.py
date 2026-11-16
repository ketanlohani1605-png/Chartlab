"""A small fake Dhan instrument list for tests: weekly expiries, a few underlyings."""
import datetime as dt
import pandas as pd


def thursdays(n, start=None):
    d = start or dt.date.today()
    out = []
    while len(out) < n:
        if d.weekday() == 3:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def master():
    rows, sid = [], 40000
    exps = thursdays(8)
    for und, base, step, inst, ex in (("NIFTY", 25000, 50, "OPTIDX", "NSE"), ("BANKNIFTY", 52000, 100, "OPTIDX", "NSE"),
                                      ("RELIANCE", 1300, 10, "OPTSTK", "NSE"), ("SENSEX", 82000, 100, "OPTIDX", "BSE")):
        for e in exps:
            for k in range(-20, 21):
                for typ in ("CE", "PE"):
                    sid += 1
                    rows.append({"EXCH_ID": ex, "SEGMENT": "D", "SECURITY_ID": sid, "INSTRUMENT": inst,
                                 "UNDERLYING_SYMBOL": und, "SYMBOL_NAME": f"{und}-{e}-{base + k * step}-{typ}",
                                 "DISPLAY_NAME": "x", "SERIES": None, "LOT_SIZE": 75.0, "SM_EXPIRY_DATE": str(e),
                                 "STRIKE_PRICE": float(base + k * step), "OPTION_TYPE": typ})
    rows.append({"EXCH_ID": "NSE", "SEGMENT": "E", "SECURITY_ID": 2885, "INSTRUMENT": "EQUITY",
                 "UNDERLYING_SYMBOL": "RELIANCE", "SYMBOL_NAME": "RELIANCE", "DISPLAY_NAME": "Reliance", "SERIES": "EQ",
                 "LOT_SIZE": 1.0, "SM_EXPIRY_DATE": None, "STRIKE_PRICE": None, "OPTION_TYPE": None})
    return pd.DataFrame(rows), exps
