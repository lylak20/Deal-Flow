"""Normalization of rounds, amounts, company names and URLs, plus the duplicate rule."""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import urlsplit, urlunsplit

ROUNDS = [
    "Pre-Seed", "Seed", "Series A", "Series B", "Series C", "Series D", "Series E+",
    "Growth", "Late Stage", "Undisclosed",
]

# Trailing words that don't distinguish companies ("Eka Robotics" == "Eka").
_COMPANY_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "llc",
    "gmbh", "sa", "sas", "ag", "bv", "plc", "labs", "lab", "ai", "technologies", "technology",
    "tech", "robotics", "hq", "io", "app",
}

_CURRENCIES = {
    "$": "$", "us$": "$", "usd": "$",
    "€": "€", "eur": "€",
    "£": "£", "gbp": "£",
    "¥": "¥", "jpy": "¥",
    "₩": "₩", "krw": "₩",
    "c$": "C$", "ca$": "C$", "cad": "C$",
    "a$": "A$", "aud": "A$",
    "s$": "S$", "sgd": "S$",
    "chf": "CHF ", "inr": "₹", "₹": "₹",
}
_UNITS = {
    "billion": 1000, "bn": 1000, "b": 1000,
    "million": 1, "mn": 1, "mm": 1, "m": 1,
    "thousand": 0.001, "k": 0.001,
}
_AMOUNT = re.compile(
    r"(?P<cur>us\$|ca\$|c\$|a\$|s\$|usd|eur|gbp|jpy|krw|cad|aud|sgd|chf|inr|[$€£¥₩₹])?\s*"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>billion|million|thousand|bn|mn|mm|b|m|k)?\b",
    re.IGNORECASE,
)


def normalize_round(raw: str | None) -> str:
    s = (raw or "").lower().strip()
    if re.search(r"pre[\s-]?seed", s):
        return "Pre-Seed"
    if "seed" in s:
        return "Seed"
    m = re.search(r"series[\s-]*([a-z])(?![a-z])", s)
    if m:
        letter = m.group(1).upper()
        return f"Series {letter}" if letter in "ABCD" else "Series E+"
    if "growth" in s:
        return "Growth"
    if "late" in s or "pre-ipo" in s:
        return "Late Stage"
    return "Undisclosed"


def _format_millions(value: float, symbol: str) -> str:
    def trim(x: float) -> str:
        return f"{x:.2f}".rstrip("0").rstrip(".")
    if value >= 1000:
        return f"{symbol}{trim(value / 1000)}B"
    if value >= 1:
        return f"{symbol}{trim(value)}M"
    return f"{symbol}{trim(value * 1000)}K"


def normalize_amount(raw: str | None) -> str:
    """'$150 million' -> '$150M', 'EUR 1.2bn' -> '€1.2B'. Unparseable text is kept as-is."""
    s = (raw or "").strip()
    if not s or s.lower() in {"undisclosed", "unknown", "n/a", "none", "not disclosed"}:
        return "Undisclosed"
    m = _AMOUNT.search(s)
    if not m or not (m.group("cur") or m.group("unit")):
        return s
    symbol = _CURRENCIES.get((m.group("cur") or "$").lower(), "$")
    num = float(m.group("num").replace(",", ""))
    unit = (m.group("unit") or "").lower()
    millions = num * _UNITS[unit] if unit else num / 1_000_000
    return _format_millions(millions, symbol)


def company_key(name: str) -> str:
    """Comparison key for company names: case, accents, punctuation and generic suffixes ignored."""
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    tokens = re.findall(r"[a-z0-9]+", text)
    full = "".join(tokens)
    if all(t in _COMPANY_SUFFIXES for t in tokens):
        return full
    while tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return "".join(tokens)


def parse_date(value: str | date | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def normalize_url(url: str) -> str:
    """Drop query string, fragment and trailing slash so the same article always compares equal."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", parts.netloc.lower(), path, "", ""))


# Domains that are never a startup's own website: news sites, wires, data providers, social networks.
NOT_COMPANY_SITES = {
    "techcrunch.com", "prnewswire.com", "crunchbase.com", "therobotreport.com", "roboticsandautomationnews.com",
    "businesswire.com", "globenewswire.com", "cision.com", "news.google.com", "google.com", "bloomberg.com",
    "reuters.com", "wsj.com", "nytimes.com", "forbes.com", "axios.com", "venturebeat.com", "theinformation.com",
    "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com", "youtube.com", "tiktok.com",
    "medium.com", "substack.com", "wikipedia.org", "github.com", "apple.com", "pitchbook.com", "eventbrite.com",
    "wordpress.com", "wp.com", "gravatar.com", "doubleclick.net", "googletagmanager.com", "cloudfront.net",
}


def site_domain(url: str) -> str:
    host = urlsplit(url if "://" in url else f"https://{url}").netloc.lower().split(":")[0]
    return host.removeprefix("www.")


def company_website(url: str | None) -> str:
    """'https://www.formic.co/about?x=1' -> 'https://formic.co'; '' for news/social/blank or malformed URLs."""
    domain = site_domain((url or "").strip())
    if "." not in domain or any(domain == d or domain.endswith("." + d) for d in NOT_COMPANY_SITES):
        return ""
    return f"https://{domain}"


@dataclass(frozen=True)
class EventKey:
    company: str
    round: str
    date: date | None


def event_key(company: str, round_: str, when: str | date | None) -> EventKey:
    return EventKey(company_key(company), round_, parse_date(when))


def find_match(candidate: EventKey, existing: list[EventKey], window_days: int) -> int | None:
    """Index of the existing event that is the same funding round as `candidate`, if any."""
    for i, other in enumerate(existing):
        if other.company != candidate.company:
            continue
        # One article may name the round while another doesn't ("Warhound raises $200M" vs "...$200M seed"):
        # an Undisclosed round matches any round of the same company.
        if other.round != candidate.round and "Undisclosed" not in (other.round, candidate.round):
            continue
        if candidate.date is None or other.date is None:
            return i
        if abs((candidate.date - other.date).days) <= window_days:
            return i
    return None


def is_duplicate(candidate: EventKey, existing: list[EventKey], window_days: int) -> bool:
    return find_match(candidate, existing, window_days) is not None


@dataclass
class EventRecord:
    """One funding round as a Raw Database row (every column except Email)."""
    key: EventKey
    values: dict[str, str]
    row: int | None = None  # sheet row holding it; None until this run appends it
    replaces: list[int] = field(default_factory=list)  # older sheet rows of the same round to delete


_INFO_FIELDS = ("Funding Round", "Amount Raised", "Valuation", "Lead Investor", "Other Investors", "Founders")


def _known(value: str) -> bool:
    return value not in ("", "Undisclosed")


def merge_update(old: dict[str, str], new: dict[str, str]) -> dict[str, str] | None:
    """A newer report of the same round as one row: newer values win, older details it lacks are kept.

    None when the newer report adds nothing (e.g. a repeat mention), so the old row stays as is.
    """
    if not any(_known(new.get(f, "")) and not _known(old.get(f, "")) for f in _INFO_FIELDS):
        return None
    return {k: v if _known(v) else old.get(k, v) for k, v in new.items()}
