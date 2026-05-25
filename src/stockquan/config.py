from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    trading_mode: str
    data_dir: Path
    news_rss_feeds: list[str]
    max_order_notional: float
    default_cash: float

    @classmethod
    def from_env(cls) -> "Settings":
        feeds = os.getenv("NEWS_RSS_FEEDS", "").split(",")
        return cls(
            trading_mode=os.getenv("TRADING_MODE", "paper").lower(),
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            news_rss_feeds=[feed.strip() for feed in feeds if feed.strip()],
            max_order_notional=float(os.getenv("MAX_ORDER_NOTIONAL", "10000")),
            default_cash=float(os.getenv("DEFAULT_CASH", "100000")),
        )


settings = Settings.from_env()

