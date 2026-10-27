# RSI mean reversion  -  buy when RSI climbs back above 30, sell when it pushes above 70.

def strategy(df):
    rsi = ta.rsi(df["Close"], 14)
    return {
        "buy": ta.crossover(rsi, 30),
        "sell": ta.crossover(rsi, 70),
    }
