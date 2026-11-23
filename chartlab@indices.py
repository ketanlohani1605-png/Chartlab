"""Every NSE / BSE index ChartLab knows, in one table: its symbol, the name TradingView shows, where its data
comes from (Yahoo's ticker, Dhan's security id) and the names people type for it.

Checked on 21 Nov 2026: each Yahoo ticker's close on 20 Nov 2026 equals TradingView's for the same index (NIFTY IT
31,322.65, Nifty Auto 28,787.65, Nifty MidCap Select 14,305.15 ...), names are TradingView's descriptions, Dhan ids
are from Dhan's scrip master. 'daily': Yahoo keeps daily bars for it; the others Yahoo only has hourly / minute bars
for (two years of hours): their daily bars are built from those.

The rest of the app asks here instead of keeping its own lists (the watchlist's sections, the search's kinds, the
legend's names, the Yahoo and broker look-ups all used to know a different handful)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Index:
    sym: str                    # ChartLab's symbol: NIFTY, NIFTYIT ...
    name: str                   # TradingView's description: 'Nifty IT Index'
    yahoo: str                  # '^CNXIT', 'NIFTY_MID_SELECT.NS'
    daily: bool                 # Yahoo has daily bars (else they are built from its hourly ones)
    dhan: str = ""              # Dhan's security id (segment IDX_I)
    exchange: str = "NSE"
    tv: str = ""                # TradingView's ticker (also accepted as a name for it)
    kind: str = "broad"         # broad | sector | theme | vix
    aliases: tuple = field(default_factory=tuple)


_I = Index
INDICES: list[Index] = [
    # -- the headline indices
    _I("NIFTY", "Nifty 50 Index", "^NSEI", True, "13", tv="NIFTY", aliases=("NIFTY50", "NIFTY 50", "CNXNIFTY")),
    _I("BANKNIFTY", "Nifty Bank Index", "^NSEBANK", True, "25", tv="BANKNIFTY", kind="sector",
       aliases=("NIFTYBANK", "NIFTY BANK")),
    _I("FINNIFTY", "Nifty Financial Services Index", "NIFTY_FIN_SERVICE.NS", True, "27", tv="CNXFINANCE",
       kind="sector", aliases=("NIFTYFIN", "NIFTY FIN SERVICE", "NIFTY FINANCIAL SERVICES", "NIFTYFINSERVICE")),
    _I("MIDCPNIFTY", "Nifty MidCap Select Index", "NIFTY_MID_SELECT.NS", False, "442", tv="NIFTY_MID_SELECT",
       aliases=("NIFTY MID SELECT", "NIFTY MIDCAP SELECT", "NIFTYMIDSELECT")),
    _I("NIFTYNXT50", "Nifty Next 50 Index", "^NSMIDCP", True, "38", tv="NIFTYJR",
       aliases=("NIFTY NEXT 50", "NIFTYNEXT50")),
    _I("SENSEX", "BSE Sensex Index", "^BSESN", True, "51", exchange="BSE", tv="SENSEX", aliases=("BSE SENSEX",)),
    _I("BANKEX", "BSE Bankex Index", "BSE-BANK.BO", False, "69", exchange="BSE", tv="BANK", kind="sector",
       aliases=("BSE BANKEX",)),
    _I("INDIAVIX", "India VIX Index", "^INDIAVIX", True, "21", tv="INDIAVIX", kind="vix",
       aliases=("INDIA VIX", "VIX")),
    # -- NSE's sectoral indices
    _I("NIFTYIT", "Nifty IT Index", "^CNXIT", True, "29", tv="CNXIT", kind="sector", aliases=("NIFTY IT",)),
    _I("NIFTYAUTO", "Nifty Auto Index", "^CNXAUTO", True, "14", tv="CNXAUTO", kind="sector",
       aliases=("NIFTY AUTO",)),
    _I("NIFTYPHARMA", "Nifty Pharma Index", "^CNXPHARMA", True, "32", tv="CNXPHARMA", kind="sector",
       aliases=("NIFTY PHARMA",)),
    _I("NIFTYFMCG", "Nifty FMCG Index", "^CNXFMCG", True, "28", tv="CNXFMCG", kind="sector",
       aliases=("NIFTY FMCG",)),
    _I("NIFTYMETAL", "Nifty Metal Index", "^CNXMETAL", True, "31", tv="CNXMETAL", kind="sector",
       aliases=("NIFTY METAL",)),
    _I("NIFTYREALTY", "Nifty Realty Index", "^CNXREALTY", True, "34", tv="CNXREALTY", kind="sector",
       aliases=("NIFTY REALTY",)),
    _I("NIFTYMEDIA", "Nifty Media Index", "^CNXMEDIA", False, "30", tv="CNXMEDIA", kind="sector",
       aliases=("NIFTY MEDIA",)),
    _I("NIFTYPSUBANK", "Nifty PSU Bank Index", "^CNXPSUBANK", True, "33", tv="CNXPSUBANK", kind="sector",
       aliases=("NIFTY PSU BANK",)),
    _I("NIFTYPVTBANK", "Nifty Private Bank Index", "NIFTY_PVT_BANK.NS", False, "15", tv="NIFTYPVTBANK",
       kind="sector", aliases=("NIFTY PVT BANK", "NIFTY PRIVATE BANK")),
    _I("NIFTYHEALTHCARE", "Nifty Healthcare Index", "NIFTY_HEALTHCARE.NS", False, "447", tv="NIFTY_HEALTHCARE",
       kind="sector", aliases=("NIFTY HEALTHCARE",)),
    _I("NIFTYCONSRDURBL", "Nifty Consumer Durables Index", "NIFTY_CONSR_DURBL.NS", False, "466",
       tv="NIFTY_CONSR_DURBL", kind="sector", aliases=("NIFTY CONSR DURBL", "NIFTY CONSUMER DURABLES")),
    _I("NIFTYOILGAS", "Nifty Oil & Gas Index", "NIFTY_OIL_AND_GAS.NS", False, "470", tv="NIFTY_OIL_AND_GAS",
       kind="sector", aliases=("NIFTY OIL AND GAS", "NIFTY OIL & GAS", "NIFTYOILANDGAS")),
    _I("NIFTYCHEMICALS", "Nifty Chemicals Index", "NIFTY_CHEMICALS.NS", False, "", tv="NIFTY_CHEMICALS",
       kind="sector", aliases=("NIFTY CHEMICALS",)),
    # -- themes
    _I("NIFTYENERGY", "Nifty Energy Index", "^CNXENERGY", True, "42", tv="CNXENERGY", kind="theme",
       aliases=("NIFTY ENERGY",)),
    _I("NIFTYINFRA", "Nifty Infrastructure Index", "^CNXINFRA", True, "43", tv="CNXINFRA", kind="theme",
       aliases=("NIFTY INFRA", "NIFTY INFRASTRUCTURE")),
    _I("NIFTYPSE", "Nifty PSE Index", "^CNXPSE", True, "41", tv="CNXPSE", kind="theme", aliases=("NIFTY PSE",)),
    _I("NIFTYCPSE", "Nifty CPSE Index", "NIFTY_CPSE.NS", False, "45", tv="", kind="theme", aliases=("NIFTY CPSE",)),
    _I("NIFTYCOMMODITIES", "Nifty Commodities Index", "^CNXCMDT", False, "39", tv="CNXCOMMODITIES", kind="theme",
       aliases=("NIFTY COMMODITIES",)),
    _I("NIFTYCONSUMPTION", "Nifty India Consumption Index", "^CNXCONSUM", True, "40", tv="CNXCONSUMPTION",
       kind="theme", aliases=("NIFTY CONSUMPTION", "NIFTY INDIA CONSUMPTION")),
    _I("NIFTYMNC", "Nifty MNC Index", "^CNXMNC", True, "44", tv="CNXMNC", kind="theme", aliases=("NIFTY MNC",)),
    _I("NIFTYSERVSECTOR", "Nifty Services Sector Index", "^CNXSERVICE", False, "46", tv="CNXSERVICE", kind="theme",
       aliases=("NIFTY SERV SECTOR", "NIFTY SERVICES SECTOR")),
    _I("NIFTYINDDEFENCE", "Nifty India Defence Index", "NIFTY_IND_DEFENCE.NS", False, "493",
       tv="NIFTY_IND_DEFENCE", kind="theme", aliases=("NIFTY IND DEFENCE", "NIFTY INDIA DEFENCE", "NIFTYDEFENCE")),
    _I("NIFTYCAPITALMKT", "Nifty Capital Markets Index", "NIFTY_CAPITAL_MKT.NS", False, "803",
       tv="NIFTY_CAPITAL_MKT", kind="theme", aliases=("NIFTY CAPITAL MKT", "NIFTY CAPITAL MARKETS")),
    _I("NIFTYEV", "Nifty EV & New Age Automotive Index", "NIFTY_EV.NS", False, "491", tv="NIFTY_EV", kind="theme",
       aliases=("NIFTY EV",)),
    _I("NIFTYRAILWAYSPSU", "Nifty India Railways PSU Index", "NIFTY_RAILWAYSPSU.NS", False, "",
       tv="NIFTY_RAILWAYSPSU", kind="theme", aliases=("NIFTY RAILWAYS PSU", "NIFTY INDIA RAILWAYS PSU")),
    # -- size
    _I("NIFTY100", "Nifty 100 Index", "^CNX100", True, "17", tv="CNX100", aliases=("NIFTY 100",)),
    _I("NIFTY200", "Nifty 200 Index", "^CNX200", True, "18", tv="CNX200", aliases=("NIFTY 200",)),
    _I("NIFTY500", "Nifty 500 Index", "^CRSLDX", True, "19", tv="CNX500", aliases=("NIFTY 500",)),
    _I("NIFTYMIDCAP50", "Nifty MidCap 50 Index", "^NSEMDCP50", True, "20", tv="NIFTYMIDCAP50",
       aliases=("NIFTY MIDCAP 50", "NIFTYMCAP50")),
    _I("NIFTYMIDCAP100", "Nifty MidCap 100 Index", "NIFTY_MIDCAP_100.NS", False, "37", tv="CNXMIDCAP",
       aliases=("NIFTY MIDCAP 100", "NIFTY MID100 FREE", "NIFTYMIDCAP")),
    _I("NIFTYMIDCAP150", "Nifty MidCap 150 Index", "NIFTYMIDCAP150.NS", True, "1", tv="NIFTYMIDCAP150",
       aliases=("NIFTY MIDCAP 150",)),
    _I("NIFTYSMLCAP100", "Nifty SmallCap 100 Index", "^CNXSC", False, "5", tv="CNXSMALLCAP",
       aliases=("NIFTY SMALLCAP 100", "NIFTYSMALLCAP100", "NIFTYSMALLCAP")),
    _I("NIFTYSMLCAP250", "Nifty SmallCap 250 Index", "NIFTYSMLCAP250.NS", True, "3", tv="NIFTYSMLCAP250",
       aliases=("NIFTY SMALLCAP 250", "NIFTYSMALLCAP250")),
    _I("NIFTYMIDSML400", "Nifty MidSmallCap 400 Index", "NIFTYMIDSML400.NS", False, "7", tv="NIFTYMIDSML400",
       aliases=("NIFTY MIDSMALLCAP 400", "NIFTYMIDSMALLCAP400")),
]

# NSE's sectoral indices, in the order the Market panel lists them (the themes follow)
SECTORS = [i.sym for i in INDICES if i.kind == "sector" and i.exchange == "NSE"]


def _norm(text: str) -> str:
    s = (text or "").strip().upper().replace(" ", "").replace("&", "AND")
    for pre in ("NSE:", "BSE:", "INDEX:"):
        if s.startswith(pre):
            s = s[len(pre):]
    return s


_BY: dict[str, Index] = {}
_BY_YAHOO: dict[str, Index] = {}
for _ix in INDICES:
    for _k in (_ix.sym, _ix.tv, *_ix.aliases):
        if _k:
            _BY.setdefault(_norm(_k), _ix)
    _BY_YAHOO[_ix.yahoo.upper()] = _ix
    _BY.setdefault(_norm(_ix.yahoo), _ix)


def get(text: str) -> Index | None:
    """The index a typed name means - 'NIFTY IT', 'CNXIT', '^CNXIT', 'NSE:NIFTYIT' -> NIFTYIT - else None."""
    return _BY.get(_norm(text))


def by_yahoo(ticker: str) -> Index | None:
    return _BY_YAHOO.get((ticker or "").strip().upper())


def is_index(text: str) -> bool:
    return get(text) is not None


def canonical(text: str) -> str:
    """ChartLab's own symbol for an index name ('' when it is not one)."""
    ix = get(text)
    return ix.sym if ix else ""


def name_of(text: str) -> str:
    ix = get(text)
    return ix.name if ix else ""


def all_symbols() -> list[str]:
    return [i.sym for i in INDICES]
