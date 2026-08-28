from __future__ import annotations

import hashlib
import math
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


DEFAULT_FEEDS = (
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://feeds.bbci.co.uk/news/politics/rss.xml",
    "https://feeds.bbci.co.uk/sport/rss.xml",
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.sec.gov/news/pressreleases.rss",
)

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "before", "by", "do", "does",
    "for", "from", "has", "have", "in", "is", "it", "of", "on", "or", "the",
    "this", "to", "will", "with", "win", "wins", "market", "election", "event",
}
POSITIVE_TERMS = {
    "accepts", "approved", "approves", "ahead", "beats", "confirmed", "confirms",
    "gains", "launch", "launches", "lead", "leading", "leads", "passes", "rises",
    "surges", "victory", "wins", "won",
}
NEGATIVE_TERMS = {
    "blocked", "cancels", "cancelled", "collapse", "declines", "denied", "denies",
    "drops", "fails", "falls", "loses", "lost", "rejects", "rejected", "resigns",
    "withdraws", "withdrawn",
}

DEFAULT_SEARCH_QUERIES = (
    "(election OR president OR congress OR court OR inflation OR Federal Reserve OR bitcoin OR crypto) when:12h",
    "(war OR ceasefire OR technology OR IPO OR sports OR championship) when:12h",
)


def tokens(text: str) -> set[str]:
    return {
        value for value in re.findall(r"[a-z0-9]+", text.lower())
        if len(value) >= 3 and value not in STOP_WORDS
    }


def parse_timestamp(value: str | None, fallback: float) -> float:
    if not value:
        return fallback
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, OverflowError):
            return fallback


@dataclass(frozen=True)
class NewsItem:
    id: str
    source: str
    title: str
    link: str
    published_at: float
    fetched_at: float


@dataclass(frozen=True)
class NewsSignal:
    relevance: float = 0.0
    direction: float = 0.0
    sources: int = 0
    headlines: int = 0
    newest_at: float | None = None

    @property
    def confirmed(self) -> bool:
        return self.sources >= 2 and self.relevance >= 0.20 and abs(self.direction) >= 0.20


class NewsClient:
    """Read-only RSS/Atom client. A failed source never blocks a paper cycle."""

    def __init__(self, feeds: tuple[str, ...] | None = None, timeout: float = 8.0) -> None:
        configured = tuple(
            value.strip() for value in os.getenv("POLYALPHA_NEWS_FEEDS", "").split(",")
            if value.strip()
        )
        self.feeds = feeds or configured or DEFAULT_FEEDS
        self.timeout = timeout
        self.ssl_context = ssl.create_default_context()
        if ssl.get_default_verify_paths().cafile is None:
            for candidate in (Path("/etc/ssl/cert.pem"), Path("/private/etc/ssl/cert.pem")):
                if candidate.is_file():
                    self.ssl_context.load_verify_locations(cafile=str(candidate))
                    break

    @staticmethod
    def _text(node: ET.Element, names: tuple[str, ...]) -> str:
        for child in node.iter():
            name = child.tag.rsplit("}", 1)[-1].lower()
            if name in names and child.text and child.text.strip():
                return child.text.strip()
        return ""

    def _fetch_feed(self, url: str) -> list[NewsItem]:
        fetched_at = time.time()
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/rss+xml, application/atom+xml, application/xml", "User-Agent": "polyalpha-news/2.2"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout, context=self.ssl_context) as response:
            root = ET.fromstring(response.read())
        feed_title = self._text(root, ("title",)) or urllib.parse.urlparse(url).netloc
        output: list[NewsItem] = []
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1].lower() not in {"item", "entry"}:
                continue
            title = self._text(node, ("title",))
            link = self._text(node, ("link", "guid", "id"))
            if not link:
                link_node = next(
                    (child for child in node.iter() if child.tag.rsplit("}", 1)[-1].lower() == "link"),
                    None,
                )
                link = str(link_node.attrib.get("href") or "") if link_node is not None else ""
            published = self._text(node, ("pubdate", "published", "updated", "date"))
            if not title:
                continue
            item_source = self._text(node, ("source",)) or feed_title
            if urllib.parse.urlparse(url).netloc == "news.google.com" and " - " in title:
                title, publisher = title.rsplit(" - ", 1)
                if publisher.strip():
                    item_source = publisher.strip()
            digest = hashlib.sha256(f"{item_source}|{title}|{link}".encode()).hexdigest()
            output.append(
                NewsItem(digest, item_source[:120], title[:500], link[:1000],
                         parse_timestamp(published, fetched_at), fetched_at)
            )
        return output

    def _fetch_search(self, query: str) -> list[NewsItem]:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({
            "q": query,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        })
        return self._fetch_feed(url)

    def fetch(self) -> tuple[list[NewsItem], list[str]]:
        items: dict[str, NewsItem] = {}
        errors: list[str] = []
        for url in self.feeds:
            try:
                for item in self._fetch_feed(url):
                    items[item.id] = item
            except Exception as exc:
                errors.append(f"{url}: {type(exc).__name__}")
        for query in DEFAULT_SEARCH_QUERIES:
            try:
                for item in self._fetch_search(query):
                    items[item.id] = item
            except Exception as exc:
                errors.append(f"news search: {type(exc).__name__}")
        return list(items.values()), errors


class NewsMatcher:
    def __init__(self, items: list[NewsItem], max_age_seconds: float = 12 * 3600) -> None:
        now = time.time()
        self.now = now
        self.items = [item for item in items if 0 <= now - item.published_at <= max_age_seconds]

    def score(self, question: str) -> NewsSignal:
        question_tokens = tokens(question)
        if len(question_tokens) < 2:
            return NewsSignal()
        matches: list[tuple[NewsItem, float, float]] = []
        for item in self.items:
            headline_tokens = tokens(item.title)
            overlap = question_tokens & headline_tokens
            minimum = 1 if len(question_tokens) <= 3 else 2
            if len(overlap) < minimum:
                continue
            lexical = len(overlap) / max(2.0, min(8.0, len(question_tokens)))
            recency = math.exp(-max(0.0, self.now - item.published_at) / (4 * 3600))
            relevance = min(1.0, lexical * (0.65 + 0.35 * recency))
            positive = len(headline_tokens & POSITIVE_TERMS)
            negative = len(headline_tokens & NEGATIVE_TERMS)
            direction = max(-1.0, min(1.0, float(positive - negative)))
            matches.append((item, relevance, direction))
        if not matches:
            return NewsSignal()
        sources = {item.source for item, _, _ in matches}
        total_weight = sum(relevance for _, relevance, _ in matches)
        direction = (
            sum(relevance * value for _, relevance, value in matches) / max(1e-9, total_weight)
        )
        return NewsSignal(
            relevance=min(1.0, total_weight / max(1.0, len(matches))),
            direction=max(-1.0, min(1.0, direction)),
            sources=len(sources),
            headlines=len(matches),
            newest_at=max(item.published_at for item, _, _ in matches),
        )
