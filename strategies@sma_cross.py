# SMA crossover  -  buy when fast SMA crosses above slow SMA, sell when it crosses below.
#
# Available inside your code:
#   df  -> DataFrame with Open, High, Low, Close, Volume (index = date/time)
#   ta  -> indicators: sma, ema, rsi, macd, bollinger, atr, highest, lowest, crossover, crossunder
#   np, pd
#
# Return a dict with boolean Series "buy" and "sell".
# Any extra key starting with plot_ is drawn on the price chart (e.g. "plot_SMA 20").
# A signal on bar i is filled at the open of bar i+1. print() output shows in the Log tab.

def strategy(df):
    fast = ta.sma(df["Close"], 20)
    slow = ta.sma(df["Close"], 50)
    return {
        "buy": ta.crossover(fast, slow),
        "sell": ta.crossunder(fast, slow),
        "plot_SMA 20": fast,
        "plot_SMA 50": slow,
    }
