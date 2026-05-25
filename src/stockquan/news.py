from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser


@dataclass(frozen=True)
class NewsItem:
    title: str
    link: str
    source: str
    published: str
    summary: str


class NewsCrawler:
    def __init__(self, feeds: list[str]):
        self.feeds = feeds

    def fetch(self, limit: int = 30) -> list[NewsItem]:
        items: list[NewsItem] = []
        for feed_url in self.feeds:
            parsed = feedparser.parse(feed_url)
            source = parsed.feed.get("title", feed_url)
            for entry in parsed.entries[:limit]:
                items.append(
                    NewsItem(
                        title=entry.get("title", "Untitled"),
                        link=entry.get("link", ""),
                        source=source,
                        published=entry.get("published", datetime.now(timezone.utc).isoformat()),
                        summary=entry.get("summary", ""),
                    )
                )
        return items[:limit]

