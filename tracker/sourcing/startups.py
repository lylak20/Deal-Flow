"""Startup Formation Tracker: recently formed companies from the YC directory and the funding news sheet."""

import logging
import re
from dataclasses import dataclass, field
from datetime import date

from normalize import company_key

from .common import Http, compile_themes, domain, months_ago, parse_day, themes_of

log = logging.getLogger(__name__)

YC_META = "https://yc-oss.github.io/api/meta.json"
# Approximate start month of each YC batch season.
_SEASON_MONTH = {"winter": 1, "spring": 4, "summer": 6, "fall": 9}


@dataclass
class Round:
    date: date | None
    round: str
    amount: str
    lead: str
    url: str


@dataclass
class Startup:
    name: str
    website: str = ""
    description: str = ""
    themes: list[str] = field(default_factory=list)
    yc_batch: str = ""
    yc_batch_date: date | None = None
    yc_url: str = ""
    team_size: int | None = None
    rounds: list[Round] = field(default_factory=list)

    @property
    def key(self) -> str:
        return company_key(self.name)

    @property
    def domain(self) -> str:
        return domain(self.website)


def batch_date(name: str) -> date | None:
    m = re.match(r"(winter|spring|summer|fall)\s+(\d{4})", name.strip(), re.IGNORECASE)
    return date(int(m.group(2)), _SEASON_MONTH[m.group(1).lower()], 1) if m else None


def yc_startups(http: Http, config: dict, themes: dict, today: date) -> list[Startup]:
    """Active companies from YC batches in the formation window that match a theme."""
    since = months_ago(today, config["startups"]["formation_months"])
    patterns = compile_themes(themes)
    meta = http.get_json(YC_META)
    out = []
    for batch in meta.get("batches", {}).values():
        start = batch_date(batch["name"])
        if not start or start < since:
            continue
        for c in http.get_json(batch["api"]):
            if c.get("status") not in ("Active", None):
                continue
            text = " ".join([c.get("one_liner") or "", c.get("long_description") or "", " ".join(c.get("tags") or []),
                             " ".join(c.get("industries") or [])])
            matched = themes_of(text, patterns)
            if not matched:
                continue
            out.append(Startup(
                name=c["name"].strip(), website=c.get("website") or "", description=(c.get("one_liner") or "").strip(),
                themes=matched, yc_batch=batch["name"], yc_batch_date=start, yc_url=c.get("url") or "",
                team_size=c.get("team_size"),
            ))
    return out


def funded_startups(rows: list[dict], config: dict, themes: dict, today: date) -> list[Startup]:
    """Early rounds from the News Tracker's Raw Database within the formation window."""
    since = months_ago(today, config["startups"]["formation_months"])
    early = set(config["startups"]["early_rounds"])
    patterns = compile_themes(themes)
    found: dict[str, Startup] = {}
    for r in rows:
        when = parse_day(r.get("Date"))
        if r.get("Funding Round") not in early or (when and when < since) or not r.get("Company"):
            continue
        s = found.setdefault(company_key(r["Company"]), Startup(name=r["Company"].strip()))
        category = r.get("Category") or ""
        for t in ([category] if category in themes else []) + themes_of(category, patterns):
            if t not in s.themes:
                s.themes.append(t)
        s.rounds.append(Round(when, r["Funding Round"], r.get("Amount Raised") or "", r.get("Lead Investor") or "",
                              r.get("Article URL") or ""))
    return list(found.values())


def merge(startups: list[Startup]) -> list[Startup]:
    """One entry per company: YC and funding-news records of the same company are combined."""
    out: dict[str, Startup] = {}
    for s in startups:
        cur = out.get(s.key)
        if cur is None:
            out[s.key] = s
            continue
        cur.website = cur.website or s.website
        cur.description = cur.description or s.description
        cur.themes += [t for t in s.themes if t not in cur.themes]
        cur.yc_batch, cur.yc_batch_date = cur.yc_batch or s.yc_batch, cur.yc_batch_date or s.yc_batch_date
        cur.yc_url = cur.yc_url or s.yc_url
        cur.team_size = cur.team_size or s.team_size
        cur.rounds += s.rounds
    return list(out.values())
