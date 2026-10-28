# ChartLab

A native desktop (Qt) charting + backtesting app for Mac, TradingView-style, where **strategies are plain Python**.

**Features:** candle / hollow / line / area charts with crosshair + price/date tags, drawing tools (trend line, ray,
horizontal/vertical line, rectangle, Fib retracement, magnet snapping, saved per symbol), watchlist with live prices,
1 / 2 / 4 chart layouts, Yahoo Finance + local CSV/Parquet + custom API providers, Python strategy editor with
backtester, and a **Claude connector** (Claude can load symbols, write/run strategies, draw, take screenshots).

## Let Claude control the app

In ChartLab: menu **Claude -> Connect to Claude...** -> **Add to Claude Desktop**, then restart Claude Desktop.
Claude then has tools such as `load_data`, `set_strategy`, `run_backtest`, `add_drawing`, `screenshot`.
(The app starts automatically when Claude needs it. Only this computer can reach the link; a private token is
created on every run; Claude can edit/run the strategy but cannot run arbitrary code.)

## Easiest: one Python file

```bash
python3 start.py
```
That's it. First run creates a private `.venv` and installs everything by itself (2-5 min, internet needed);
every run after that opens the app immediately. Nothing is installed system-wide.
(Needs Python 3.9+ from python.org. On Mac you can also right-click `start.py` -> Open With -> Python Launcher.)

## Get a double-click Mac app (no Terminal)

1. Create a new GitHub repo and upload this whole folder (including `.github`).
2. Repo -> **Actions** -> **Build Mac app** -> **Run workflow**.
3. After ~5-10 min open the finished run -> **Artifacts** -> download `ChartLab-mac`, unzip, drag `ChartLab.app` to Applications.
4. First launch only: right-click the app -> **Open** -> **Open** (it is unsigned, so macOS asks once).

Built for Apple Silicon Macs (M1 and newer).

## Run from source (Mac)

```bash
cd ChartLab
chmod +x run.command build_mac_app.sh
./run.command            # first run creates .venv and installs everything
```

Make a real `ChartLab.app`: `./build_mac_app.sh` -> `dist/ChartLab.app`.

## Data sources (toolbar -> first dropdown)

| Source | Notes |
|---|---|
| **Yahoo Finance** | `RELIANCE` -> NSE, `BSE:TCS` -> BSE, `NIFTY`, `BANKNIFTY`, `SENSEX`, `US:AAPL`, `BTC-USD`. Prices are split/dividend adjusted. Intraday history is limited by Yahoo (1m = 7 days, 5-30m = 60 days, 1h = 2 years); NSE data is delayed. |
| **Local file** | CSV / TSV / Parquet. Needs a date column (`Date`, `Datetime`, `Timestamp`, or separate `Date` + `Time`) and `Open High Low Close` (`Volume` optional). Header case doesn't matter, epoch timestamps work. |
| **Provider** | Drop a `.py` file with `fetch(symbol, interval, period)` into `~/ChartLab/providers/` (see `providers/demo_random_walk.py`) to plug in Zerodha / Angel One / Upstox / Binance / any API. |

## Writing a strategy

```python
def strategy(df):                       # df: Open, High, Low, Close, Volume
    fast = ta.sma(df["Close"], 20)
    slow = ta.sma(df["Close"], 50)
    return {
        "buy":  ta.crossover(fast, slow),
        "sell": ta.crossunder(fast, slow),
        "plot_SMA 20": fast,            # optional: any plot_* key is drawn on the chart
    }
```

Available names: `df`, `ta`, `np`, `pd`.
`ta`: `sma ema rsi macd bollinger atr highest lowest crossover crossunder`.
`print()` output and errors appear in the **Log** tab. Press **Cmd+R** to run.

Backtest rules: signal on bar *i* fills at the **open of bar i+1**; long-only; 100% of equity per trade;
one position at a time; fee % charged per side; open trade at the end is marked to market.

## Tests

```bash
python tests/test_core.py                                  # loaders, indicators, backtester
QT_QPA_PLATFORM=offscreen python tests/smoke_ui.py out.png # headless UI render
```

## Security note

Strategies are executed with Python `exec` on your machine - only run strategy code you trust.

## Broker accounts (v0.7)
- Accounts menu -> Manage broker accounts: add Dhan client id + access token (saved only in ~/ChartLab/accounts.json, mode 600).
- Source dropdown shows "API: <name>"; type a symbol (RELIANCE, NSE:TCS, NIFTY) and Load - the app fetches candles itself.
- Accounts -> Run script on multiple accounts: your script runs once per ticked account, all at the same time; inside it call chartlab.account() for that account credentials.
- chartlab.order("RELIANCE","BUY",10) inside your script places an order on that account. Accounts are PAPER by default (only prints); tick "Live orders" in Manage broker accounts to send real orders.
- Fast orders: Accounts -> Run script on multiple accounts -> "One script" mode. chartlab.warmup() opens all broker connections; chartlab.order("NIFTY","BUY",lots=1) or chartlab.order(chartlab.option("NIFTY",24500,"CE"),"BUY",lots=1) hits every ticked account at the same instant (parallel, kept-alive connections). Set a per-account Qty multiplier in Manage broker accounts.
