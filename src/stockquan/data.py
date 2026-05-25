from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    price: float
    as_of: str


class MarketDataClient:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def history(self, symbol: str, period: str = "6mo", interval: str = "1d") -> pd.DataFrame:
        ticker = symbol.upper().strip()
        frame = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)
        if frame.empty:
            raise ValueError(f"No market data returned for {ticker}.")
        frame = frame.reset_index()
        frame.columns = [str(column).lower().replace(" ", "_") for column in frame.columns]
        frame["symbol"] = ticker
        self._cache_history(ticker, frame)
        return frame

    def latest_price(self, symbol: str) -> MarketSnapshot:
        frame = self.history(symbol, period="5d", interval="1d")
        close_column = "close"
        latest = frame.dropna(subset=[close_column]).iloc[-1]
        return MarketSnapshot(
            symbol=symbol.upper().strip(),
            price=float(latest[close_column]),
            as_of=datetime.now(timezone.utc).isoformat(),
        )

    def _cache_history(self, symbol: str, frame: pd.DataFrame) -> None:
        path = self.cache_dir / f"{symbol}_history.csv"
        frame.to_csv(path, index=False)

