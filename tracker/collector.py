"""1. News Collector: read RSS feeds and keep recent articles that look like funding news."""

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import feedparser
import requests

from normalize import normalize_url

log = logging.getLogger(__name__)


@dataclass
class Article:
    url: str
    source: str
    title: str
    summary: str
    published: datetime | None


def _clean(text: str) -> str:
    # PR Newswire double-escapes entities ("&amp;amp;"), and summaries contain HTML.
    text = html.unescape(html.unescape(text or ""))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def _published(entry) -> datetime | None:
    for field in ("published", "updated"):
        raw = entry.get(field)
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            try:
                dt = datetime.fromisoformat(raw)
            except ValueError:
                continue
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def parse_feed(xml: bytes, source: str) -> list[Article]:
    feed = feedparser.parse(xml)
    articles = []
    for entry in feed.entries:
        link = entry.get("link")
        if not link:
            continue
        articles.append(Article(
            url=normalize_url(link),
            source=source,
            title=_clean(entry.get("title", "")),
            summary=_clean(entry.get("summary", "")),
            published=_published(entry),
        ))
    return articles


def is_candidate(article: Article, patterns: list[re.Pattern], since: datetime) -> bool:
    if article.published and article.published < since:
        return False
    text = f"{article.title} {article.summary}"
    return any(p.search(text) for p in patterns)


def collect(config: dict, now: datetime | None = None) -> tuple[list[Article], list[str]]:
    """Return (candidate articles, names of sources that could not be read)."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=config["lookback_hours"])
    patterns = [re.compile(k, re.IGNORECASE) for k in config["keywords"]]
    headers = {
        "User-Agent": config["http"]["user_agent"],
        "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
    }

    candidates: dict[str, Article] = {}
    failed: list[str] = []
    for src in config["sources"]:
        if not src.get("enabled", True):
            continue
        label = f"{src['name']} ({src['url']})"
        try:
            resp = requests.get(src["url"], headers=headers, timeout=config["http"]["timeout_seconds"])
            resp.raise_for_status()
            items = parse_feed(resp.content, src["name"])
        except Exception as e:
            log.warning("Feed failed: %s: %s", label, e)
            failed.append(label)
            continue
        kept = [a for a in items if is_candidate(a, patterns, since)]
        log.info("%s: %d items, %d candidates", label, len(items), len(kept))
        for a in kept:
            # The same article can appear in two feeds (TechCrunch funding tag + venture category).
            candidates.setdefault(a.url, a)
    return list(candidates.values()), failed
