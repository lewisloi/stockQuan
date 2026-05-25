import pandas as pd

from stockquan.models import OrderSide
from stockquan.strategy import MovingAverageCrossStrategy


def test_strategy_generates_buy_signal_on_cross_up():
    prices = [10] * 50 + [12, 14, 16, 18, 20]
    history = pd.DataFrame({"close": prices})
    strategy = MovingAverageCrossStrategy(short_window=3, long_window=5, risk_fraction=0.1)

    signal = strategy.evaluate("AAPL", history, cash=10000)

    assert signal is not None
    assert signal.side == OrderSide.BUY
    assert signal.quantity > 0

