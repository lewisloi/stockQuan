from __future__ import annotations

import pandas as pd

from stockquan.models import OrderSide, TradeSignal


class MovingAverageCrossStrategy:
    def __init__(self, short_window: int = 20, long_window: int = 50, risk_fraction: float = 0.1):
        if short_window >= long_window:
            raise ValueError("short_window must be smaller than long_window.")
        self.short_window = short_window
        self.long_window = long_window
        self.risk_fraction = risk_fraction

    def evaluate(self, symbol: str, history: pd.DataFrame, cash: float) -> TradeSignal | None:
        if len(history) < self.long_window + 2:
            return None

        frame = history.copy()
        frame["short_ma"] = frame["close"].rolling(self.short_window).mean()
        frame["long_ma"] = frame["close"].rolling(self.long_window).mean()
        previous = frame.iloc[-2]
        latest = frame.iloc[-1]

        price = float(latest["close"])
        quantity = max(int((cash * self.risk_fraction) // price), 1)

        if previous["short_ma"] <= previous["long_ma"] and latest["short_ma"] > latest["long_ma"]:
            return TradeSignal(
                symbol=symbol.upper(),
                side=OrderSide.BUY,
                confidence=0.65,
                reason=f"{self.short_window}日均線上穿{self.long_window}日均線",
                price=price,
                quantity=quantity,
            )

        if previous["short_ma"] >= previous["long_ma"] and latest["short_ma"] < latest["long_ma"]:
            return TradeSignal(
                symbol=symbol.upper(),
                side=OrderSide.SELL,
                confidence=0.65,
                reason=f"{self.short_window}日均線下穿{self.long_window}日均線",
                price=price,
                quantity=quantity,
            )

        return None

