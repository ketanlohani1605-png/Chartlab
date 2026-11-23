"""What a symbol is called, everywhere it is named:

- the order ticket says 'Index · NSE' for NIFTY (the paper account trades it at its price, but it is not a stock),
  'Crypto' for BTC-USD, 'Stock · US' for US:AAPL, 'Call option · ...' for an option
- a crypto pair is crypto whatever it is quoted in (-USD, -USDT, -INR): open all day, priced in its own currency
- SENSEX is a BSE index
"""
import datetime as dt
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["CHARTLAB_OFFLINE"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["HOME"] = tempfile.mkdtemp()

from PySide6 import QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
from chartlab.brokers.paper import Paper  # noqa: E402
from chartlab.trading import ticket_kind  # noqa: E402
from chartlab.watchlist import currency_of, exchange_of, market_state  # noqa: E402

P = Paper({"name": "_labels", "broker": "paper"})
want = {
    "NIFTY": ("Index", "NSE"),
    "BANKNIFTY": ("Index", "NSE"),
    "SENSEX": ("Index", "BSE"),
    "RELIANCE": ("Stock", "NSE"),
    "BSE:RELIANCE": ("Stock", "BSE"),
    "BTC-USD": ("Crypto", ""),
    "ETH-USDT": ("Crypto", ""),
    "US:AAPL": ("Stock", "US"),
}
for sym, kind in want.items():
    got = ticket_kind(sym, P.resolve(sym))
    assert got == kind, (sym, got)
assert ticket_kind("NIFTY 25NOV 26000 CE", P.resolve("NIFTY 25NOV 26000 CE"))[0] == "Call option"

assert [exchange_of(s) for s in ("BTC-USD", "ETH-USDT", "BTC-INR", "SENSEX", "US:AAPL", "TCS")] == \
    ["CRYPTO", "CRYPTO", "CRYPTO", "BSE", "NASDAQ", "NSE"]
assert [currency_of(s) for s in ("BTC-USD", "ETH-USDT", "BTC-INR", "US:AAPL", "TCS")] == \
    ["USD", "USDT", "INR", "USD", "INR"]
sat = dt.datetime(2026, 11, 21, 12, 0, tzinfo=dt.timezone.utc)
assert market_state("ETH-USDT", sat)[0] == "open" and market_state("TCS", sat)[0] == "closed"

# a paper F&O lot is the exchange's lot, for the contract's month (NSE's list)
from chartlab.symbols import fo_lot  # noqa: E402

assert fo_lot("NIFTY") == 65 and fo_lot("BANKNIFTY") == 30 and fo_lot("RELIANCE") == 500 and fo_lot("ITC2") is None
assert fo_lot("TCS", "2026-12") == 225 and fo_lot("TCS", "2027-01") == 250 and fo_lot("TCS", "2027-02") == 250
lots = {s: P.resolve(s).lot for s in ("NIFTY 25NOV 26000 CE", "BANKNIFTY NOV FUT", "TCS 26DEC 2400 PE", "RELIANCE")}
assert lots == {"NIFTY 25NOV 26000 CE": 65, "BANKNIFTY NOV FUT": 30, "TCS 26DEC 2400 PE": 225, "RELIANCE": 1}, lots

print("LABELS OK")
os._exit(0)
