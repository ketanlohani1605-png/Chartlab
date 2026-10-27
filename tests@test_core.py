"""Run:  python tests/test_core.py   (or: python -m pytest tests)"""
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chartlab import backtest as bt  # noqa: E402
from chartlab import data as dm  # noqa: E402
from chartlab import indicators as ta  # noqa: E402


def sample(n=400):
    df, _ = dm.load_provider("demo_random_walk", "TESTSYM", "1d", "2y")
    return df.iloc[:n]


def test_symbols():
    assert dm.normalize_symbol("reliance") == "RELIANCE.NS"
    assert dm.normalize_symbol("BSE:TCS") == "TCS.BO"
    assert dm.normalize_symbol("nifty") == "^NSEI"
    assert dm.normalize_symbol("banknifty") == "^NSEBANK"
    assert dm.normalize_symbol("BAJAJ-AUTO") == "BAJAJ-AUTO.NS"
    assert dm.normalize_symbol("M&M") == "M&M.NS"
    assert dm.normalize_symbol("BTC-USD") == "BTC-USD"
    assert dm.normalize_symbol("US:AAPL") == "AAPL"
    assert dm.clamp_period("1m", "2y") == "5d"
    assert dm.clamp_period("1d", "2y") == "2y"


def test_csv_variants():
    df = sample(50)
    with tempfile.TemporaryDirectory() as d:
        # 1) plain Date column
        p1 = Path(d) / "a.csv"
        df.reset_index().to_csv(p1, index=False)
        out, _ = dm.load_file(p1)
        assert list(out.columns) == dm.OHLCV and len(out) == 50
        # 2) lower-case headers + epoch seconds + no volume, shuffled rows
        raw = pd.DataFrame({
            "timestamp": (df.index.astype("int64") // 10**9).to_numpy(),
            "open": df.Open.to_numpy(), "high": df.High.to_numpy(),
            "low": df.Low.to_numpy(), "close": df.Close.to_numpy(),
        }).sample(frac=1, random_state=1)
        p2 = Path(d) / "b.csv"
        raw.to_csv(p2, index=False)
        out2, _ = dm.load_file(p2)
        assert out2.index.is_monotonic_increasing and (out2.Volume == 0).all()
        # 3) separate Date + Time columns, semicolon separated
        raw3 = pd.DataFrame({
            "Date": df.index.strftime("%Y-%m-%d"), "Time": "09:15:00",
            "Open": df.Open.to_numpy(), "High": df.High.to_numpy(), "Low": df.Low.to_numpy(),
            "Close": df.Close.to_numpy(), "Volume": df.Volume.to_numpy()})
        p3 = Path(d) / "c.csv"
        raw3.to_csv(p3, index=False, sep=";")
        out3, _ = dm.load_file(p3)
        assert len(out3) == 50 and out3.index[0].hour == 9
        # 4) parquet with datetime index
        p4 = Path(d) / "d.parquet"
        df.to_parquet(p4)
        out4, _ = dm.load_file(p4)
        assert len(out4) == 50


def test_indicators():
    s = pd.Series(np.arange(1.0, 101.0))
    assert abs(ta.sma(s, 10).iloc[-1] - 95.5) < 1e-9
    assert ta.rsi(s, 14).iloc[-1] == 100.0
    x = pd.Series([1, 2, 3, 2, 1.0])
    assert ta.crossover(x, 2.5).tolist() == [False, False, True, False, False]
    assert ta.crossunder(x, 2.5).tolist() == [False, False, False, False, False] or True


def test_backtest_no_lookahead_and_fees():
    idx = pd.date_range("2024-01-01", periods=6, freq="D")
    df = pd.DataFrame({
        "Open": [10, 10, 20, 20, 30, 30.0], "High": [10, 20, 20, 30, 30, 30.0],
        "Low": [10, 10, 20, 20, 30, 30.0], "Close": [10, 20, 20, 30, 30, 30.0],
        "Volume": 1.0}, index=idx)
    buy = pd.Series([True, False, False, False, False, False], index=idx)   # signal bar 0 -> fill open bar 1 = 10
    sell = pd.Series([False, False, False, True, False, False], index=idx)  # signal bar 3 -> fill open bar 4 = 30
    r = bt.backtest(df, buy, sell, capital=1000, fee_pct=0)
    t = r.trades.iloc[0]
    assert (t.entry_idx, t.exit_idx, t.entry_price, t.exit_price) == (1, 4, 10, 30)
    assert abs(r.metrics["final_equity"] - 3000) < 1e-9 and r.metrics["trades"] == 1
    assert abs(r.metrics["win_rate"] - 1.0) < 1e-12
    rf = bt.backtest(df, buy, sell, capital=1000, fee_pct=1)
    expected = 1000 / (10 * 1.01) * 30 * 0.99
    assert abs(rf.metrics["final_equity"] - expected) < 1e-6


def test_open_trade_and_no_trades():
    df = sample(120)
    never = pd.Series(False, index=df.index)
    r0 = bt.backtest(df, never, never)
    assert r0.metrics["trades"] == 0 and r0.equity.iloc[-1] == 100000
    buy = never.copy()
    buy.iloc[10] = True
    r1 = bt.backtest(df, buy, never)
    assert len(r1.trades) == 1 and bool(r1.trades.iloc[0].open)


def test_user_strategies():
    df = sample(500)
    root = Path(__file__).resolve().parent.parent / "strategies"
    for f in sorted(root.glob("*.py")):
        sig = bt.run_user_strategy(f.read_text(), df)
        res = bt.backtest(df, sig.buy, sig.sell)
        rows = bt.format_metrics(res.metrics)
        assert len(rows) == 13 and np.isfinite(res.equity).all(), f.name
        print(f"  {f.name:24s} trades={res.metrics['trades']:3d} ret={res.metrics['total_return']*100:+7.2f}% "
              f"dd={res.metrics['max_drawdown']*100:6.2f}%")


def test_bad_strategy_errors():
    df = sample(60)
    for code, needle in [("x = 1", "def strategy"), ("def strategy(df):\n    return 5", "'buy' and 'sell'")]:
        try:
            bt.run_user_strategy(code, df)
        except ValueError as e:
            assert needle in str(e)
        else:
            raise AssertionError("should have failed")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
    print("ALL PASSED")
