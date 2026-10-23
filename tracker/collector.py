"""1. News Collector: read RSS feeds and keep recent articles that look like funding news in the focus sector."""

import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import feedparser
import requests

from normalize import company_key, normalize_url

log = logging.getLogger(__name__)

GOOGLE_NEWS_HOST = "news.google.com"


@dataclass
class Article:
    url: str
    source: str
    title: str
    summary: str
    published: datetime | None
    priority: int = 2  # lower is processed first (config sources[].priority)
    headline_only: bool = False  # the page can't be read; extract from title + summary
    tracks: tuple[str, ...] = ("all", "robotics")  # database tabs this article qualified for


def is_headline_only(article: Article) -> bool:
    # Google News links are encoded redirects. Checking the host also covers articles rebuilt from the
    # Article Log for a retry, which don't carry the source's flags.
    return article.headline_only or GOOGLE_NEWS_HOST in article.url


# "<Company> raises $20M ...", "<Company> secures funding ...", "<Company>'s valuation leaps ..." -> None
_HEADLINE_COMPANY = re.compile(
    r"^(?:exclusive:\s*)?(?P<name>.+?)\s+(?:raises|raised|secures|secured|closes|closed|lands|landed|bags|nabs|"
    r"gets|receives|announces|completes|scores|snags|picks up)\b",
    re.IGNORECASE,
)


def guess_company(title: str) -> str | None:
    """Comparison key for the company a funding headline is about, or None if it can't tell."""
    m = _HEADLINE_COMPANY.search(title or "")
    if not m:
        return None
    name = m.group("name")
    # Drop descriptors before the name: "Chicago robotics startup Formic", "Sam Altman-backed Coco Robotics".
    name = re.split(r"\b(?:startup|company|firm|maker|developer|provider|platform)\s+", name, flags=re.I)[-1]
    name = re.sub(r"^.*-backed\s+", "", name, flags=re.I)
    if len(name.split()) > 5:
        return None
    return company_key(name) or None


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


def parse_feed(xml: bytes, source: str, priority: int = 2, headline_only: bool = False) -> list[Article]:
    feed = feedparser.parse(xml)
    articles = []
    for entry in feed.entries:
        link = entry.get("link")
        if not link:
            continue
        title, name = _clean(entry.get("title", "")), source
        publisher = _clean((entry.get("source") or {}).get("title", ""))
        if publisher and GOOGLE_NEWS_HOST in link:
            # Google News titles end in " - <publisher>"; name the real outlet in the Source column.
            title = title.removesuffix(f" - {publisher}")
            name = f"{publisher} (via Google News)"
        articles.append(Article(
            url=normalize_url(link),
            source=name,
            title=title,
            summary=_clean(entry.get("summary", "")),
            published=_published(entry),
            priority=priority,
            headline_only=headline_only,
        ))
    return articles


def is_candidate(article: Article, patterns: list[re.Pattern], since: datetime,
                 focus: list[re.Pattern] | None = None) -> bool:
    if article.published and article.published < since:
        return False
    text = f"{article.title} {article.summary}"
    if focus and not any(p.search(text) for p in focus):
        return False
    return any(p.search(text) for p in patterns)


def collect(config: dict, now: datetime | None = None) -> tuple[list[Article], list[str]]:
    """Return (candidate articles, names of sources that could not be read)."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=config["lookback_hours"])
    patterns = [re.compile(k, re.IGNORECASE) for k in config["keywords"]]
    focus = [re.compile(k, re.IGNORECASE) for k in config.get("focus", {}).get("keywords", [])]
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
            items = parse_feed(resp.content, src["name"], src.get("priority", 2), src.get("headline_only", False))
        except Exception as e:
            log.warning("Feed failed: %s: %s", label, e)
            failed.append(label)
            continue
        own = [re.compile(k, re.IGNORECASE) for k in src.get("keywords", [])]
        tracks = tuple(src.get("tracks", ["all", "robotics"]))
        kept = []
        for a in items:
            # "all" takes any funding article; "robotics" also needs a focus keyword.
            a.tracks = tuple(t for t in tracks if is_candidate(a, own or patterns, since,
                                                              focus if t == "robotics" else None))
            if a.tracks:
                kept.append(a)
        log.info("%s: %d items, %d candidates", label, len(items), len(kept))
        for a in kept:
            # The same article can appear in two feeds (TechCrunch funding tag + venture category);
            # keep the copy from the higher-priority feed, qualified for the tracks of both.
            prev = candidates.get(a.url)
            if prev is None or a.priority < prev.priority:
                candidates[a.url] = a
            if prev is not None:
                candidates[a.url].tracks = tuple(dict.fromkeys(prev.tracks + a.tracks))
    return list(candidates.values()), failed
