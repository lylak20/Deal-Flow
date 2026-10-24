"""Shared helpers: HTTP with rate-limit handling, theme matching, dates."""

import logging
import re
import time
from datetime import date, timedelta
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)


class RateLimited(Exception):
    """The API won't take more requests for a while; the source is skipped for this run."""


class Http:
    def __init__(self, user_agent: str, timeout: int = 30):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.timeout = timeout

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None, attempts: int = 3):
        for attempt in range(1, attempts + 1):
            resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            if resp.status_code in (403, 429) and _rate_limited(resp):
                wait = _wait_seconds(resp)
                if wait is None or wait > 90 or attempt == attempts:
                    raise RateLimited(f"{urlsplit(url).netloc} rate limit reached")
                log.info("Rate limited by %s; waiting %ds", urlsplit(url).netloc, wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500 and attempt < attempts:
                time.sleep(5 * attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        raise AssertionError("unreachable")


def _rate_limited(resp) -> bool:
    return resp.status_code == 429 or resp.headers.get("X-RateLimit-Remaining") == "0" or "rate limit" in resp.text.lower()


def _wait_seconds(resp) -> int | None:
    if resp.headers.get("Retry-After", "").isdigit():
        return int(resp.headers["Retry-After"]) + 1
    reset = resp.headers.get("X-RateLimit-Reset")
    if reset and reset.isdigit():
        return max(1, int(reset) - int(time.time()) + 1)
    return None


def compile_themes(themes: dict) -> dict[str, re.Pattern]:
    return {
        name: re.compile(r"\b(" + "|".join(re.escape(k) for k in spec["keywords"]) + r")s?\b", re.IGNORECASE)
        for name, spec in themes.items()
    }


def themes_of(text: str, patterns: dict[str, re.Pattern]) -> list[str]:
    return [name for name, p in patterns.items() if p.search(text or "")]


def months_ago(today: date, months: int) -> date:
    return today - timedelta(days=round(months * 30.44))


def months_between(start: date | None, end: date) -> float:
    if not start:
        return 0.0
    return max((end - start).days / 30.44, 0.5)  # a repo from yesterday isn't "1000 stars per month"


def domain(url: str | None) -> str:
    if not url:
        return ""
    host = urlsplit(url if "//" in url else f"https://{url}").netloc.lower()
    return host.removeprefix("www.")


def parse_day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
