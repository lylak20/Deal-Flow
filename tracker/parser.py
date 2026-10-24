"""2. Article Parser: fetch an article page and extract its main text."""

import logging
import re

import requests
import trafilatura

from collector import Article, is_headline_only
from normalize import company_website, site_domain

log = logging.getLogger(__name__)

MAX_LINKS = 15


def outbound_sites(html: str, article_url: str) -> list[str]:
    """Websites the article's main text links to (not news, social or the outlet itself), in order of appearance."""
    marked = trafilatura.extract(html, include_links=True, output_format="markdown", include_comments=False) or ""
    own = site_domain(article_url)
    sites = []
    for url in re.findall(r"\]\((https?://[^)\s]+)\)", marked):
        site = company_website(url)
        if site and site_domain(site) != own and site not in sites:
            sites.append(site)
    return sites[:MAX_LINKS]


_LINKEDIN_PROFILE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/([A-Za-z0-9%_-]+)", re.IGNORECASE)


def linkedin_profiles(html: str) -> list[str]:
    """LinkedIn profiles (linkedin.com/in/...) the article links to, in order, as https://www.linkedin.com/in/<id>."""
    profiles = []
    for slug in _LINKEDIN_PROFILE.findall(html):
        url = f"https://www.linkedin.com/in/{slug.lower()}"
        if url not in profiles:
            profiles.append(url)
    return profiles[:MAX_LINKS]


def article_text(article: Article, config: dict) -> str:
    """Main text of the article, falling back to the RSS title and summary if the page can't be read.

    Sites and LinkedIn profiles the article links to are listed at the end, so the company's website and its
    founders' profiles can be picked from them.
    """
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
        sites = outbound_sites(resp.text, article.url)
        profiles = linkedin_profiles(resp.text)
    except Exception as e:
        log.warning("Could not read %s (%s); using RSS summary", article.url, e)
        body, sites, profiles = "", [], []
    text = f"{article.title}\n\n{body}" if len(body) > len(article.summary) else fallback
    links = "\n\nLinks in the article:\n" + "\n".join(sites) if sites else ""
    links += "\n\nLinkedIn profiles in the article:\n" + "\n".join(profiles) if profiles else ""
    return text[: config["max_article_chars"] - len(links)] + links
