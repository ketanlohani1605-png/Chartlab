# Donchian breakout  -  buy a new 20-bar high, exit on a new 10-bar low.
# shift(1) makes sure we only compare against PREVIOUS bars (no look-ahead).

def strategy(df):
    upper = ta.highest(df["High"], 20).shift(1)
    lower = ta.lowest(df["Low"], 10).shift(1)
    return {
        "buy": df["Close"] > upper,
        "sell": df["Close"] < lower,
        "plot_Entry (20 high)": upper,
        "plot_Exit (10 low)": lower,
    }
