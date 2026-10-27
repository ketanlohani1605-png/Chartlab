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
                 "qty", "pnl", "pnl_pct", "bars", "open"]


@dataclass
class Signals:
    buy: pd.Series
    sell: pd.Series
    overlays: dict


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
    if not isinstance(out, dict) or "buy" not in out or "sell" not in out:
        raise ValueError(
            "strategy(df) must return a dict (or DataFrame) with 'buy' and 'sell' boolean Series. "
            "Extra keys named plot_<name> are drawn on the price chart."
        )
    buy = _align(out["buy"], df.index).fillna(False).astype(bool)
    sell = _align(out["sell"], df.index).fillna(False).astype(bool)
    overlays = {
        str(k)[5:]: _align(v, df.index).astype(float)
        for k, v in out.items() if str(k).startswith("plot_")
    }
    return Signals(buy, sell, overlays)


# --------------------------------------------------------------------------- backtest
def backtest(df: pd.DataFrame, buy: pd.Series, sell: pd.Series,
             capital: float = 100_000.0, fee_pct: float = 0.05) -> Result:
    n = len(df)
    o, c = df["Open"].to_numpy(float), df["Close"].to_numpy(float)
    b, s = buy.to_numpy(bool), sell.to_numpy(bool)
    fee = fee_pct / 100.0

    cash, qty, in_pos = float(capital), 0.0, False
    entry_cash = e_i = e_px = 0.0
    pending = 0
    equity = np.empty(n)
    trades = []

    def record(ei, xi, epx, xpx, q, pnl, pct, is_open):
        trades.append({
            "entry_idx": int(ei), "exit_idx": int(xi),
            "entry_time": df.index[int(ei)], "exit_time": df.index[int(xi)],
            "entry_price": epx, "exit_price": xpx, "qty": q,
            "pnl": pnl, "pnl_pct": pct, "bars": int(xi - ei), "open": is_open,
        })

    for i in range(n):
        if pending == 1 and not in_pos and o[i] > 0:
            entry_cash, e_i, e_px = cash, i, o[i]
            qty = cash / (o[i] * (1 + fee))
            cash, in_pos = 0.0, True
        elif pending == -1 and in_pos:
            proceeds = qty * o[i] * (1 - fee)
            record(e_i, i, e_px, o[i], qty, proceeds - entry_cash, proceeds / entry_cash - 1, False)
            cash, qty, in_pos = proceeds, 0.0, False
        pending = 0
        equity[i] = cash + qty * c[i]
        if b[i] and not in_pos:
            pending = 1
        elif s[i] and in_pos:
            pending = -1

    if in_pos:  # still open at the end: mark to market
        value = qty * c[-1]
        record(e_i, n - 1, e_px, c[-1], qty, value - entry_cash, value / entry_cash - 1, True)

    tdf = pd.DataFrame(trades, columns=TRADE_COLUMNS)
    eq = pd.Series(equity, index=df.index, name="Strategy")
    bh = pd.Series(c / c[0] * capital, index=df.index, name="Buy & hold")
    return Result(tdf, eq, bh, _metrics(df, eq, bh, tdf, capital))


def _metrics(df, eq, bh, trades, capital) -> dict:
    n = len(df)
    years = max((df.index[-1] - df.index[0]).days, 1) / 365.25
    final = float(eq.iloc[-1])
    cagr = (final / capital) ** (1 / years) - 1 if years > 0.05 and final > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    rets = eq.pct_change().dropna()
    sharpe = rets.mean() / rets.std() * np.sqrt(n / years) if len(rets) > 2 and rets.std() > 0 else np.nan

    if len(trades):
        wins, losses = trades[trades.pnl > 0], trades[trades.pnl <= 0]
        gross_win, gross_loss = wins.pnl.sum(), -losses.pnl.sum()
        pf = gross_win / gross_loss if gross_loss > 0 else (np.inf if gross_win > 0 else np.nan)
        win_rate = len(wins) / len(trades)
        avg, best, worst = trades.pnl_pct.mean(), trades.pnl_pct.max(), trades.pnl_pct.min()
        avg_bars = trades.bars.mean()
    else:
        pf = win_rate = avg = best = worst = avg_bars = np.nan

    return {
        "net_profit": final - capital, "final_equity": final, "total_return": final / capital - 1,
        "buy_hold": float(bh.iloc[-1] / capital - 1), "cagr": cagr, "max_drawdown": float(dd),
        "sharpe": sharpe, "trades": int(len(trades)), "win_rate": win_rate, "profit_factor": pf,
        "avg_trade": avg, "best_trade": best, "worst_trade": worst, "avg_bars": avg_bars,
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
        ("Sharpe ratio (annualised)", num(m["sharpe"])),
        ("Total trades", str(m["trades"])),
        ("Win rate", pct(m["win_rate"])),
        ("Profit factor", num(m["profit_factor"])),
        ("Avg trade", pct(m["avg_trade"])),
        ("Best trade", pct(m["best_trade"])),
        ("Worst trade", pct(m["worst_trade"])),
        ("Avg bars in trade", num(m["avg_bars"], 1)),
    ]
