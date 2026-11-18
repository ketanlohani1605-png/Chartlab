"""The chart's built-in indicators (TradingView's "Indicators" list): what each one computes, how it is drawn,
its inputs and colours, and where it goes - on the price chart or in a pane of its own below it.

Every study is one entry in STUDIES. The chart builds the lines from calc(df, params) and knows nothing else
about the maths, so a new indicator is one function and one entry here."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import indicators as ta

UP, DOWN = "#089981", "#f23645"
BLUE, ORANGE, PURPLE, YELLOW, GREY = "#2962ff", "#ff6d00", "#7e57c2", "#ffeb3b", "#787b86"


@dataclass
class Out:
    """One drawn series. y has one value per bar (x = bar index + offset); 'step' lines give x / y with NaN gaps
    and keep the per-bar value for the legend in vals."""
    y: np.ndarray
    color: str
    kind: str = "line"            # line | hist | dots | step
    width: float = 1.3
    offset: int = 0
    style: str = "solid"          # solid | dash | dot
    idx: np.ndarray | None = None  # hist: palette index per bar
    palette: tuple = ()
    legend: bool = True           # show its value in the legend
    x: np.ndarray | None = None
    vals: np.ndarray | None = None


@dataclass
class Fill:
    a: int                        # fill between outs[a] and outs[b]
    b: int
    color: str
    alpha: int = 22
    color_dn: str | None = None   # second colour where outs[b] is above outs[a] (Ichimoku cloud)


@dataclass
class Study:
    key: str
    title: str                    # name in the Indicators dialog and the settings window
    cat: str
    pane: bool                    # True: a pane of its own under the price chart
    inputs: list                  # (name, label, default, lo, hi)
    colors: list                  # (name, label, default)
    calc: object                  # (df, params) -> (list[Out], list[Fill])
    legend: object                # params -> legend title
    levels: tuple = ()            # (value, 'dash' | 'dot')
    band: tuple | None = None     # (low, high) shaded between two levels
    band_color: str = PURPLE
    fmt: str = "num"              # num | price | vol: how values are written in the legend / scale
    search: str = ""
    volume: bool = False          # needs real volume (an index has none)
    extra: dict = field(default_factory=dict)


def _a(s) -> np.ndarray:
    return np.asarray(s, float)


def _ma(kind):
    def calc(df, P):
        f = ta.sma if kind == "sma" else ta.ema if kind == "ema" else ta.wma if kind == "wma" else ta.hma
        return [Out(_a(f(df["Close"], int(P["len"]))), P["color"])], []
    return calc


def _bb(df, P):
    b = ta.bollinger(df["Close"], int(P["len"]), float(P["mult"]))
    return ([Out(_a(b["mid"]), P["color"]), Out(_a(b["upper"]), P["color_up"]), Out(_a(b["lower"]), P["color_dn"])],
            [Fill(1, 2, "#2196f3", 20)])


def _vwap(df, P):
    return [Out(_a(ta.vwap(df)), P["color"])], []


def _supertrend(df, P):
    st = ta.supertrend(df, int(P["len"]), float(P["mult"]))
    line, d = _a(st["st"]), _a(st["dir"])
    return [Out(np.where(d > 0, line, np.nan), P["color_up"], width=1.6),
            Out(np.where(d < 0, line, np.nan), P["color_dn"], width=1.6)], []


def _psar(df, P):
    return [Out(_a(ta.psar(df, float(P["start"]), float(P["inc"]), float(P["max"]))), P["color"], kind="dots")], []


def _ichimoku(df, P):
    ic = ta.ichimoku(df, int(P["conv"]), int(P["base"]), int(P["span_b"]))
    d = int(P["disp"]) - 1
    outs = [Out(_a(ic["conversion"]), P["color"]), Out(_a(ic["base"]), P["color_base"]),
            Out(_a(ic["lagging"]), P["color_lag"], offset=-d),
            Out(_a(ic["span_a"]), P["color_a"], offset=d), Out(_a(ic["span_b"]), P["color_b"], offset=d)]
    return outs, [Fill(3, 4, "#43a047", 28, color_dn="#f44336")]


def _donchian(df, P):
    dc = ta.donchian(df, int(P["len"]))
    return ([Out(_a(dc["upper"]), P["color"]), Out(_a(dc["basis"]), P["color_mid"]), Out(_a(dc["lower"]), P["color"])],
            [Fill(0, 2, "#2196f3", 14)])


def _keltner(df, P):
    kc = ta.keltner(df, int(P["len"]), float(P["mult"]), int(P["atr_len"]))
    return ([Out(_a(kc["upper"]), P["color"]), Out(_a(kc["basis"]), P["color"]), Out(_a(kc["lower"]), P["color"])],
            [Fill(0, 2, "#2196f3", 14)])


def _pivots(df, P):
    pv = ta.pivots(df)
    period = pv["period"].to_numpy()
    starts = np.flatnonzero(np.r_[True, period[1:] != period[:-1]])
    ends = np.r_[starts[1:], len(df)]
    outs = []
    for name in ("R3", "R2", "R1", "P", "S1", "S2", "S3"):
        v = _a(pv[name])
        xs, ys = [], []
        for a, b in zip(starts.tolist(), ends.tolist()):
            if np.isfinite(v[a]):
                xs += [a - 0.5, b - 0.5, np.nan]
                ys += [v[a], v[a], np.nan]
        col = P["color_p"] if name == "P" else (P["color_r"] if name.startswith("R") else P["color_s"])
        outs.append(Out(np.array(ys, float), col, kind="step", x=np.array(xs, float), vals=v, legend=False,
                        width=1.0))
    return outs, []


def _rsi(df, P):
    r = ta.rsi(df["Close"], int(P["len"]))
    outs = [Out(_a(r), P["color"])]
    if int(P.get("ma_len", 0)) > 1:
        outs.append(Out(_a(ta.sma(r, int(P["ma_len"]))), P["color_ma"]))
    return outs, []


_MACD_PALETTE = ("#26a69a", "#b2dfdb", "#ffcdd2", "#ff5252")


def _macd(df, P):
    m = ta.macd(df["Close"], int(P["fast"]), int(P["slow"]), int(P["signal"]))
    h = _a(m["hist"])
    prev = np.r_[np.nan, h[:-1]]
    idx = np.where(h >= 0, np.where(h >= prev, 0, 1), np.where(h < prev, 3, 2))   # TradingView's 4 colours
    return [Out(h, _MACD_PALETTE[0], kind="hist", idx=idx, palette=_MACD_PALETTE),
            Out(_a(m["macd"]), P["color"]), Out(_a(m["signal"]), P["color_sig"])], []


def _stoch(df, P):
    s = ta.stoch(df, int(P["k"]), int(P["smooth"]), int(P["d"]))
    return [Out(_a(s["k"]), P["color"]), Out(_a(s["d"]), P["color_d"])], []


def _stochrsi(df, P):
    s = ta.stoch_rsi(df["Close"], int(P["rsi_len"]), int(P["stoch_len"]), int(P["k"]), int(P["d"]))
    return [Out(_a(s["k"]), P["color"]), Out(_a(s["d"]), P["color_d"])], []


def _one(fn):
    def calc(df, P):
        return [Out(_a(fn(df, P)), P["color"])], []
    return calc


def _dmi(df, P):
    d = ta.dmi(df, int(P["len"]), int(P["adx_len"]))
    return [Out(_a(d["adx"]), P["color"]), Out(_a(d["plus"]), P["color_plus"]), Out(_a(d["minus"]), P["color_minus"])], []


S = Study
STUDY_LIST = [
    # ---------------------------------------------------------------- on the price chart
    S("sma20", "Moving Average Simple", "Moving averages", False, [("len", "Length", 20, 1, 2000)],
      [("color", "Line", "#2962ff")], _ma("sma"), lambda P: f"SMA {P['len']}", fmt="price", search="sma ma moving average"),
    S("sma50", "Moving Average Simple", "Moving averages", False, [("len", "Length", 50, 1, 2000)],
      [("color", "Line", "#ff9800")], _ma("sma"), lambda P: f"SMA {P['len']}", fmt="price", search="sma ma moving average"),
    S("sma200", "Moving Average Simple", "Moving averages", False, [("len", "Length", 200, 1, 2000)],
      [("color", "Line", "#e040fb")], _ma("sma"), lambda P: f"SMA {P['len']}", fmt="price", search="sma ma moving average"),
    S("ema20", "Moving Average Exponential", "Moving averages", False, [("len", "Length", 20, 1, 2000)],
      [("color", "Line", "#00bcd4")], _ma("ema"), lambda P: f"EMA {P['len']}", fmt="price", search="ema ma exponential"),
    S("ema50", "Moving Average Exponential", "Moving averages", False, [("len", "Length", 50, 1, 2000)],
      [("color", "Line", "#ffeb3b")], _ma("ema"), lambda P: f"EMA {P['len']}", fmt="price", search="ema ma exponential"),
    S("wma", "Moving Average Weighted", "Moving averages", False, [("len", "Length", 9, 1, 2000)],
      [("color", "Line", "#ab47bc")], _ma("wma"), lambda P: f"WMA {P['len']}", fmt="price", search="wma weighted"),
    S("hma", "Hull Moving Average", "Moving averages", False, [("len", "Length", 9, 2, 2000)],
      [("color", "Line", "#ffa726")], _ma("hma"), lambda P: f"HMA {P['len']}", fmt="price", search="hma hull"),
    S("bb", "Bollinger Bands", "Volatility", False, [("len", "Length", 20, 1, 1000), ("mult", "StdDev", 2.0, 0.1, 50)],
      [("color", "Basis", "#2962ff"), ("color_up", "Upper", "#f23645"), ("color_dn", "Lower", "#089981")], _bb,
      lambda P: f"BB {P['len']} {P['mult']:g}", fmt="price", search="bb bollinger bands"),
    S("keltner", "Keltner Channels", "Volatility", False,
      [("len", "Length", 20, 1, 1000), ("mult", "Multiplier", 2.0, 0.1, 50), ("atr_len", "ATR length", 10, 1, 500)],
      [("color", "Lines", "#2962ff")], _keltner, lambda P: f"KC {P['len']} {P['mult']:g}", fmt="price",
      search="kc keltner channel"),
    S("donchian", "Donchian Channels", "Volatility", False, [("len", "Length", 20, 1, 1000)],
      [("color", "Upper / lower", "#2962ff"), ("color_mid", "Basis", "#ff6d00")], _donchian,
      lambda P: f"DC {P['len']}", fmt="price", search="dc donchian channel breakout"),
    S("supertrend", "Supertrend", "Trend", False, [("len", "ATR length", 10, 1, 500), ("mult", "Factor", 3.0, 0.1, 50)],
      [("color_up", "Up trend", UP), ("color_dn", "Down trend", DOWN)], _supertrend,
      lambda P: f"Supertrend {P['len']} {P['mult']:g}", fmt="price", search="super trend atr"),
    S("psar", "Parabolic SAR", "Trend", False,
      [("start", "Start", 0.02, 0.001, 1), ("inc", "Increment", 0.02, 0.001, 1), ("max", "Max value", 0.2, 0.01, 1)],
      [("color", "Dots", "#2962ff")], _psar, lambda P: f"SAR {P['start']:g} {P['inc']:g} {P['max']:g}", fmt="price",
      search="psar parabolic stop reverse"),
    S("ichimoku", "Ichimoku Cloud", "Trend", False,
      [("conv", "Conversion line", 9, 1, 500), ("base", "Base line", 26, 1, 500), ("span_b", "Leading span B", 52, 1, 500),
       ("disp", "Displacement", 26, 1, 500)],
      [("color", "Conversion", "#2962ff"), ("color_base", "Base", "#b71c1c"), ("color_lag", "Lagging span", "#43a047"),
       ("color_a", "Leading span A", "#a5d6a7"), ("color_b", "Leading span B", "#ef9a9a")], _ichimoku,
      lambda P: f"Ichimoku {P['conv']} {P['base']} {P['span_b']} {P['disp']}", fmt="price", search="ichimoku cloud kumo"),
    S("vwap", "VWAP", "Volume", False, [], [("color", "Line", "#2962ff")], _vwap, lambda P: "VWAP", fmt="price",
      search="vwap volume weighted average price", volume=True),
    S("pivots", "Pivot Points Standard", "Support / resistance", False, [],
      [("color_p", "P", "#ff9800"), ("color_r", "R levels", "#f23645"), ("color_s", "S levels", "#089981")], _pivots,
      lambda P: "Pivots Standard", fmt="price", search="pivot points support resistance floor"),
    # ---------------------------------------------------------------- panes of their own
    S("rsi", "Relative Strength Index", "Oscillators", True, [("len", "Length", 14, 1, 500), ("ma_len", "MA length (0 = off)", 14, 0, 500)],
      [("color", "RSI", PURPLE), ("color_ma", "RSI-based MA", YELLOW)], _rsi,
      lambda P: f"RSI {P['len']}", levels=((70, "dash"), (50, "dot"), (30, "dash")), band=(30, 70),
      search="rsi relative strength oscillator momentum"),
    S("macd", "MACD", "Oscillators", True,
      [("fast", "Fast length", 12, 1, 500), ("slow", "Slow length", 26, 1, 500), ("signal", "Signal smoothing", 9, 1, 500)],
      [("color", "MACD", BLUE), ("color_sig", "Signal", ORANGE)], _macd,
      lambda P: f"MACD {P['fast']} {P['slow']} close {P['signal']}", search="macd moving average convergence divergence"),
    S("stoch", "Stochastic", "Oscillators", True,
      [("k", "%K length", 14, 1, 500), ("smooth", "%K smoothing", 1, 1, 100), ("d", "%D smoothing", 3, 1, 100)],
      [("color", "%K", BLUE), ("color_d", "%D", ORANGE)], _stoch, lambda P: f"Stoch {P['k']} {P['smooth']} {P['d']}",
      levels=((80, "dash"), (50, "dot"), (20, "dash")), band=(20, 80), band_color="#2196f3", search="stochastic stoch kd"),
    S("stochrsi", "Stochastic RSI", "Oscillators", True,
      [("k", "K", 3, 1, 100), ("d", "D", 3, 1, 100), ("rsi_len", "RSI length", 14, 1, 500), ("stoch_len", "Stochastic length", 14, 1, 500)],
      [("color", "K", BLUE), ("color_d", "D", ORANGE)], _stochrsi,
      lambda P: f"Stoch RSI {P['k']} {P['d']} {P['rsi_len']} {P['stoch_len']}",
      levels=((80, "dash"), (50, "dot"), (20, "dash")), band=(20, 80), band_color="#2196f3", search="stoch rsi stochastic"),
    S("cci", "Commodity Channel Index", "Oscillators", True, [("len", "Length", 20, 2, 500)], [("color", "CCI", BLUE)],
      _one(lambda df, P: ta.cci(df, int(P["len"]))), lambda P: f"CCI {P['len']}",
      levels=((100, "dash"), (0, "dot"), (-100, "dash")), band=(-100, 100), band_color="#2196f3", search="cci commodity channel"),
    S("willr", "Williams %R", "Oscillators", True, [("len", "Length", 14, 1, 500)], [("color", "%R", PURPLE)],
      _one(lambda df, P: ta.williams_r(df, int(P["len"]))), lambda P: f"%R {P['len']}",
      levels=((-20, "dash"), (-50, "dot"), (-80, "dash")), band=(-80, -20), search="williams r percent range"),
    S("mfi", "Money Flow Index", "Volume", True, [("len", "Length", 14, 1, 500)], [("color", "MFI", PURPLE)],
      _one(lambda df, P: ta.mfi(df, int(P["len"]))), lambda P: f"MFI {P['len']}",
      levels=((80, "dash"), (50, "dot"), (20, "dash")), band=(20, 80), search="mfi money flow", volume=True),
    S("roc", "Rate Of Change", "Oscillators", True, [("len", "Length", 9, 1, 500)], [("color", "ROC", BLUE)],
      _one(lambda df, P: ta.roc(df["Close"], int(P["len"]))), lambda P: f"ROC {P['len']}", levels=((0, "dash"),),
      search="roc rate of change momentum"),
    S("mom", "Momentum", "Oscillators", True, [("len", "Length", 10, 1, 500)], [("color", "Momentum", BLUE)],
      _one(lambda df, P: ta.momentum(df["Close"], int(P["len"]))), lambda P: f"Mom {P['len']}", fmt="price",
      search="momentum mom"),
    S("adx", "Directional Movement Index", "Trend", True,
      [("len", "DI length", 14, 1, 500), ("adx_len", "ADX smoothing", 14, 1, 500)],
      [("color", "ADX", "#f50057"), ("color_plus", "+DI", BLUE), ("color_minus", "-DI", ORANGE)], _dmi,
      lambda P: f"DMI {P['len']} {P['adx_len']}", search="adx dmi directional movement trend strength di"),
    S("atr", "Average True Range", "Volatility", True, [("len", "Length", 14, 1, 500)], [("color", "ATR", "#b71c1c")],
      _one(lambda df, P: ta.atr(df, int(P["len"]))), lambda P: f"ATR {P['len']}", fmt="price",
      search="atr average true range volatility"),
    S("obv", "On Balance Volume", "Volume", True, [], [("color", "OBV", BLUE)], _one(lambda df, P: ta.obv(df)),
      lambda P: "OBV", fmt="vol", search="obv on balance volume", volume=True),
]
STUDIES = {s.key: s for s in STUDY_LIST}
KEYS = [s.key for s in STUDY_LIST]
CATEGORIES = ["Moving averages", "Trend", "Oscillators", "Volatility", "Volume", "Support / resistance"]


def defaults(key: str) -> dict:
    st = STUDIES[key]
    out = {name: dflt for name, _lab, dflt, _lo, _hi in st.inputs}
    out.update({name: dflt for name, _lab, dflt in st.colors})
    return out


def params(key: str, saved: dict | None) -> dict:
    """Defaults overlaid with what the user changed (unknown / broken values are ignored)."""
    P = defaults(key)
    for k, v in (saved or {}).items():
        if k in P and v is not None:
            try:
                P[k] = type(P[k])(v) if not isinstance(P[k], str) else str(v)
            except (TypeError, ValueError):
                pass
    return P


def compute(key: str, df: pd.DataFrame, P: dict):
    return STUDIES[key].calc(df, P)


def fmt_value(st: Study, v: float, price_fmt) -> str:
    if not np.isfinite(v):
        return "—"
    if st.fmt == "price":
        return price_fmt(v)
    if st.fmt == "vol":
        a = abs(v)
        for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
            if a >= div:
                return f"{v / div:,.2f}{suf}"
        return f"{v:,.0f}"
    return f"{v:,.2f}"
