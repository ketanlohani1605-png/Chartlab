"""Strategy runner + long-only backtester.

A strategy is plain Python:

    def strategy(df):                      # df has Open, High, Low, Close, Volume
        fast, slow = ta.sma(df.Close, 20), ta.sma(df.Close, 50)
        return {
            "buy":  ta.crossover(fast, slow),     # boolean Series
            "sell": ta.crossunder(fast, slow),    # boolean Series
            "plot_SMA 20": fast,                  # optional: drawn on the price chart
        }

Execution model: a signal on bar i is filled at the OPEN of bar i+1 (no look-ahead).
100% of equity is used per trade, long-only, one position at a time, % fee charged per side.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators

TRADE_COLUMNS = ["entry_idx", "exit_idx", "entry_time", "exit_time", "entry_price", "exit_price",
                 "qty", "pnl", "pnl_pct", "bars", "open", "side", "reason", "fees"]


@dataclass
class Signals:
    buy: pd.Series
    sell: pd.Series
    overlays: dict
    long: pd.Series | None = None       # optional explicit entries / exits / target position
    short: pd.Series | None = None
    exits: pd.Series | None = None
    position: pd.Series | None = None   # target position per bar: 1 long, -1 short, 0 flat


@dataclass
class Config:
    capital: float = 100_000.0
    fee_pct: float = 0.05            # per side, % of traded value
    slippage_pct: float = 0.0        # adverse, per market fill (not applied to take-profit limits)
    direction: str = "long"          # long | short | both
    size_pct: float = 100.0          # % of current equity per trade (>100 = leverage)
    fill: str = "next_open"          # next_open | close
    sl_pct: float = 0.0              # stop loss % from entry (0 = off)
    tp_pct: float = 0.0              # take profit % from entry (0 = off)
    trail_pct: float = 0.0           # trailing stop % from best price since entry (0 = off)
    square_off: str = ""             # "15:15": flat from this time of day, no new entries until next day


@dataclass
class Result:
    trades: pd.DataFrame
    equity: pd.Series
    buy_hold: pd.Series
    metrics: dict


# --------------------------------------------------------------------------- running user code
def _align(x, index: pd.DatetimeIndex) -> pd.Series:
    if isinstance(x, pd.Series):
        if x.index.equals(index):
            return x
        if len(x) == len(index):
            return pd.Series(x.to_numpy(), index=index)
        return x.reindex(index)
    return pd.Series(np.asarray(x), index=index)


def run_user_strategy(code: str, df: pd.DataFrame) -> Signals:
    ns = {"__name__": "user_strategy", "np": np, "pd": pd, "ta": indicators}
    exec(compile(code, "<strategy>", "exec"), ns)
    fn = ns.get("strategy")
    if not callable(fn):
        raise ValueError("Your code must define a function:  def strategy(df): ...")
    out = fn(df.copy())
    if isinstance(out, pd.DataFrame):
        out = {c: out[c] for c in out.columns}
    if isinstance(out, pd.Series):                       # a bare position / signal series
        out = {"position": out}
    if not isinstance(out, dict):
        raise ValueError("strategy(df) must return a dict / DataFrame. Use any of: buy+sell, long/short/exits, "
                         "entries/exits, position (-1/0/1). Keys named plot_<name> are drawn on the chart.")
    low = {str(k).lower(): v for k, v in out.items()}
    for alias, real in (("entries", "long"), ("signal", "position"), ("pos", "position"),
                        ("long_entry", "long"), ("short_entry", "short"), ("exit", "exits")):
        if alias in low and real not in low:
            low[real] = low[alias]
    if not any(k in low for k in ("buy", "sell", "long", "short", "position")):
        raise ValueError("strategy(df) returned none of: buy/sell, long/short/exits, position. Got keys: "
                         + ", ".join(map(str, out.keys())))

    def b(key):
        return None if key not in low else _align(low[key], df.index).fillna(0).astype(bool)

    def pos():
        if "position" not in low:
            return None
        return _align(low["position"], df.index).fillna(0).astype(float).clip(-1, 1)

    false = pd.Series(False, index=df.index)
    buy, sell = b("buy"), b("sell")
    overlays = {
        str(k)[5:]: _align(v, df.index).astype(float)
        for k, v in out.items() if str(k).startswith("plot_")
    }
    return Signals(buy if buy is not None else false, sell if sell is not None else false, overlays,
                   long=b("long"), short=b("short"), exits=b("exits"), position=pos())
# --------------------------------------------------------------------------- events
def to_events(sig: Signals, direction: str, n: int):
    """Normalise every supported strategy output into four boolean arrays: long entry/exit, short entry/exit."""
    z = np.zeros(n, bool)
    if sig.position is not None:
        p = sig.position.to_numpy(float)
        prev = np.concatenate([[0.0], p[:-1]])
        le, lx = (p > 0) & (prev <= 0), (prev > 0) & (p <= 0)
        se, sx = (p < 0) & (prev >= 0), (prev < 0) & (p >= 0)
    elif sig.long is not None or sig.short is not None:
        le = sig.long.to_numpy(bool) if sig.long is not None else z
        se = sig.short.to_numpy(bool) if sig.short is not None else z
        ex = sig.exits.to_numpy(bool) if sig.exits is not None else z
        lx, sx = ex.copy(), ex.copy()
    else:
        buy, sell = sig.buy.to_numpy(bool), sig.sell.to_numpy(bool)
        if direction == "short":
            le, lx, se, sx = z, z, sell, buy
        elif direction == "both":
            le, lx, se, sx = buy, sell, sell, buy
        else:
            le, lx, se, sx = buy, sell, z, z
    if direction == "long":
        se, sx = z, z
    elif direction == "short":
        le, lx = z, z
    return le.copy(), lx.copy(), se.copy(), sx.copy()


# --------------------------------------------------------------------------- engine
def run_backtest(df: pd.DataFrame, sig: Signals, cfg: Config | None = None) -> Result:
    """Deterministic bar-by-bar simulation.

    * next_open: a signal on bar i-1 fills at the open of bar i (no look-ahead). close: fills at the close of bar i.
    * Stops/targets are checked against each bar's High/Low. If a stop and a target are both reachable inside
      the same bar the STOP is assumed to hit first (conservative). Gaps fill at the open.
    * Fees are charged per side on traded value; slippage moves every market fill against you.
    """
    cfg = cfg or Config()
    n = len(df)
    o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    le, lx, se, sx = to_events(sig, cfg.direction, n)
    fee, slip = cfg.fee_pct / 100.0, cfg.slippage_pct / 100.0
    sl, tp, tr = cfg.sl_pct / 100.0, cfg.tp_pct / 100.0, cfg.trail_pct / 100.0
    next_open = cfg.fill != "close"
    sq = -1
    if cfg.square_off:
        hh, mm = cfg.square_off.split(":")
        sq = int(hh) * 60 + int(mm)
    tod = (df.index.hour * 60 + df.index.minute).to_numpy()

    cash = float(cfg.capital)
    side = 0                                   # +1 long, -1 short, 0 flat
    qty = e_px = peak = 0.0
    e_i, e_fees, e_notional = 0, 0.0, 0.0
    total_fees = 0.0
    equity = np.empty(n)
    exposure = np.zeros(n, bool)
    trades = []

    def close_pos(i, px, reason, is_open=False):
        nonlocal cash, side, qty, total_fees
        fees = qty * px * fee
        if side == 1:
            cash += qty * px - fees
            pnl = qty * (px - e_px) - fees - e_fees
        else:
            cash -= qty * px + fees
            pnl = qty * (e_px - px) - fees - e_fees
        total_fees += fees
        trades.append({
            "entry_idx": e_i, "exit_idx": int(i), "entry_time": df.index[e_i], "exit_time": df.index[int(i)],
            "entry_price": e_px, "exit_price": px, "qty": qty, "pnl": pnl,
            "pnl_pct": pnl / e_notional if e_notional else 0.0, "bars": int(i) - e_i, "open": is_open,
            "side": "long" if side == 1 else "short", "reason": reason, "fees": fees + e_fees})
        side, qty = 0, 0.0

    def open_pos(i, new_side, px):
        nonlocal cash, side, qty, e_px, peak, e_i, e_fees, e_notional, total_fees
        eq = cash
        notional = max(eq, 0.0) * cfg.size_pct / 100.0
        if px <= 0 or notional <= 0:
            return
        qty = notional / (px * (1 + fee)) if new_side == 1 else notional / (px * (1 + fee))
        fees = qty * px * fee
        cash += (-qty * px - fees) if new_side == 1 else (qty * px - fees)
        total_fees += fees
        side, e_px, peak, e_i, e_fees, e_notional = new_side, px, px, i, fees, qty * px

    def fill_px(px, s_, entering):
        """Apply slippage against the trader. s_ = +1 buying, -1 selling."""
        return px * (1 + slip) if s_ == 1 else px * (1 - slip)

    def act(i, px_base):
        """Execute the pending signals (decided on the previous bar or this bar) at price px_base."""
        nonlocal side
        blocked = sq >= 0 and tod[i] >= sq
        want_l = (le[k] and not se[k])
        want_s = (se[k] and not le[k])
        if side == 1:
            if lx[k] or (want_s):
                close_pos(i, fill_px(px_base, -1, False), "signal")
        elif side == -1:
            if sx[k] or (want_l):
                close_pos(i, fill_px(px_base, 1, False), "signal")
        if side == 0 and not blocked:
            if want_l:
                open_pos(i, 1, fill_px(px_base, 1, True))
            elif want_s:
                open_pos(i, -1, fill_px(px_base, -1, True))

    for i in range(n):
        entered_now = False
        if sq >= 0 and side != 0 and tod[i] >= sq:       # time exit at this bar's open
            close_pos(i, fill_px(o[i], -side, False), "square_off")
        if next_open and i > 0:
            k = i - 1
            was = side
            act(i, o[i])
            entered_now = side != 0 and e_i == i
        # --- protective exits inside this bar
        if side != 0 and (sl or tp or tr):
            if side == 1:
                stops = []
                if sl:
                    stops.append(e_px * (1 - sl))
                if tr:
                    stops.append(peak * (1 - tr))
                stop = max(stops) if stops else None
                hit_stop = stop is not None and l[i] <= stop
                hit_tp = bool(tp) and h[i] >= e_px * (1 + tp)
                if hit_stop:
                    px = min(stop, o[i])
                    close_pos(i, fill_px(px, -1, False), "trail" if (tr and stop == peak * (1 - tr) and not (sl and stop == e_px * (1 - sl))) else "sl")
                elif hit_tp:
                    close_pos(i, max(e_px * (1 + tp), o[i]), "tp")
                else:
                    peak = max(peak, h[i])
            else:
                stops = []
                if sl:
                    stops.append(e_px * (1 + sl))
                if tr:
                    stops.append(peak * (1 + tr))
                stop = min(stops) if stops else None
                hit_stop = stop is not None and h[i] >= stop
                hit_tp = bool(tp) and l[i] <= e_px * (1 - tp)
                if hit_stop:
                    px = max(stop, o[i])
                    close_pos(i, fill_px(px, 1, False), "trail" if (tr and stop == peak * (1 + tr) and not (sl and stop == e_px * (1 + sl))) else "sl")
                elif hit_tp:
                    close_pos(i, min(e_px * (1 - tp), o[i]), "tp")
                else:
                    peak = min(peak, l[i])
        if not next_open:
            k = i
            act(i, c[i])
        exposure[i] = side != 0
        equity[i] = cash + side * qty * c[i]

    if side != 0:   # open at the end: mark to market, no exit fee
        fees_saved = fee
        pnl_px = c[-1]
        pnl = (qty * (pnl_px - e_px) if side == 1 else qty * (e_px - pnl_px)) - e_fees
        trades.append({
            "entry_idx": e_i, "exit_idx": n - 1, "entry_time": df.index[e_i], "exit_time": df.index[n - 1],
            "entry_price": e_px, "exit_price": pnl_px, "qty": qty, "pnl": pnl,
            "pnl_pct": pnl / e_notional if e_notional else 0.0, "bars": n - 1 - e_i, "open": True,
            "side": "long" if side == 1 else "short", "reason": "open", "fees": e_fees})

    tdf = pd.DataFrame(trades, columns=TRADE_COLUMNS)
    eq = pd.Series(equity, index=df.index, name="Strategy")
    bh = pd.Series(c / c[0] * cfg.capital, index=df.index, name="Buy & hold")
    m = _metrics(df, eq, bh, tdf, cfg.capital)
    m["exposure"] = float(exposure.mean()) if n else np.nan
    m["fees_paid"] = float(total_fees)
    return Result(tdf, eq, bh, m)


def backtest(df: pd.DataFrame, buy: pd.Series, sell: pd.Series,
             capital: float = 100_000.0, fee_pct: float = 0.05, **kw) -> Result:
    """Backwards-compatible wrapper: long-only buy/sell signals."""
    return run_backtest(df, Signals(buy, sell, {}), Config(capital=capital, fee_pct=fee_pct, **kw))


def _metrics(df, eq, bh, trades, capital) -> dict:
    n = len(df)
    years = max((df.index[-1] - df.index[0]).days, 1) / 365.25
    final = float(eq.iloc[-1])
    cagr = (final / capital) ** (1 / years) - 1 if years > 0.05 and final > 0 else np.nan
    dd_series = eq / eq.cummax() - 1
    dd = float(dd_series.min())
    daily = eq.resample("1D").last().dropna() if n > 2 else eq
    rets = daily.pct_change().dropna()
    sharpe = rets.mean() / rets.std() * np.sqrt(252) if len(rets) > 3 and rets.std() > 0 else np.nan
    down = rets[rets < 0]
    sortino = rets.mean() / down.std() * np.sqrt(252) if len(down) > 2 and down.std() > 0 else np.nan
    calmar = cagr / abs(dd) if np.isfinite(cagr) and dd < 0 else np.nan

    if len(trades):
        wins, losses = trades[trades.pnl > 0], trades[trades.pnl <= 0]
        gross_win, gross_loss = wins.pnl.sum(), -losses.pnl.sum()
        pf = gross_win / gross_loss if gross_loss > 0 else (np.inf if gross_win > 0 else np.nan)
        win_rate = len(wins) / len(trades)
        avg, best, worst = trades.pnl_pct.mean(), trades.pnl_pct.max(), trades.pnl_pct.min()
        avg_bars = trades.bars.mean()
        avg_win = wins.pnl.mean() if len(wins) else np.nan
        avg_loss = losses.pnl.mean() if len(losses) else np.nan
        expectancy = trades.pnl.mean()
        streak = best_streak = 0
        for v in (trades.pnl <= 0).to_numpy():
            streak = streak + 1 if v else 0
            best_streak = max(best_streak, streak)
        n_long, n_short = int((trades.side == "long").sum()), int((trades.side == "short").sum())
    else:
        pf = win_rate = avg = best = worst = avg_bars = avg_win = avg_loss = expectancy = np.nan
        best_streak, n_long, n_short = 0, 0, 0

    return {
        "net_profit": final - capital, "final_equity": final, "total_return": final / capital - 1,
        "buy_hold": float(bh.iloc[-1] / capital - 1), "cagr": cagr, "max_drawdown": dd,
        "sharpe": sharpe, "sortino": sortino, "calmar": calmar, "trades": int(len(trades)),
        "win_rate": win_rate, "profit_factor": pf, "avg_trade": avg, "best_trade": best, "worst_trade": worst,
        "avg_bars": avg_bars, "avg_win": avg_win, "avg_loss": avg_loss, "expectancy": expectancy,
        "max_consec_losses": best_streak, "long_trades": n_long, "short_trades": n_short,
    }


def format_metrics(m: dict) -> list[tuple[str, str]]:
    def ok(x):
        return x is not None and np.isfinite(x)

    def pct(x):
        return f"{x * 100:,.2f}%" if ok(x) else "—"

    def num(x, d=2):
        return f"{x:,.{d}f}" if ok(x) else ("∞" if x == np.inf else "—")

    return [
        ("Net profit", f"{m['net_profit']:,.2f}  ({pct(m['total_return'])})"),
        ("Final equity", num(m["final_equity"])),
        ("Buy & hold return", pct(m["buy_hold"])),
        ("CAGR", pct(m["cagr"])),
        ("Max drawdown", pct(m["max_drawdown"])),
        ("Sharpe (daily, ann.)", num(m["sharpe"])),
        ("Total trades", str(m["trades"])),
        ("Win rate", pct(m["win_rate"])),
        ("Profit factor", num(m["profit_factor"])),
        ("Avg trade", pct(m["avg_trade"])),
        ("Best trade", pct(m["best_trade"])),
        ("Worst trade", pct(m["worst_trade"])),
        ("Avg bars in trade", num(m["avg_bars"], 1)),
        ("Sortino", num(m.get("sortino"))),
        ("Calmar", num(m.get("calmar"))),
        ("Expectancy / trade", num(m.get("expectancy"))),
        ("Avg win / avg loss", f"{num(m.get('avg_win'))} / {num(m.get('avg_loss'))}"),
        ("Max consecutive losses", str(m.get("max_consec_losses", 0))),
        ("Long / short trades", f"{m.get('long_trades', 0)} / {m.get('short_trades', 0)}"),
        ("Time in market", pct(m.get("exposure"))),
        ("Fees paid", num(m.get("fees_paid"))),
    ]
