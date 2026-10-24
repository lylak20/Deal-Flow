"""2. Article Parser: fetch an article page and extract its main text."""

import logging

import requests
import trafilatura

from collector import Article, is_headline_only

log = logging.getLogger(__name__)


def article_text(article: Article, config: dict) -> str:
    """Main text of the article, falling back to the RSS title and summary if the page can't be read."""
    fallback = f"{article.title}\n\n{article.summary}".strip()
    if is_headline_only(article):
        return fallback[: config["max_article_chars"]]
    try:
        resp = requests.get(
            article.url,
            headers={"User-Agent": config["http"]["user_agent"]},
            timeout=config["http"]["timeout_seconds"],
        )
        resp.raise_for_status()
        body = trafilatura.extract(resp.text, include_comments=False, include_tables=False) or ""
    except Exception as e:
        log.warning("Could not read %s (%s); using RSS summary", article.url, e)
        body = ""
    text = f"{article.title}\n\n{body}" if len(body) > len(article.summary) else fallback
    return text[: config["max_article_chars"]]
