"""Open Source Momentum Tracker: fast-growing new GitHub repositories and trending Hugging Face models."""

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date

from .common import Http, compile_themes, domain, months_ago, months_between, parse_day, themes_of
from .momentum import acceleration_score, gains_from_events, rating, window_gains

log = logging.getLogger(__name__)

GITHUB = "https://api.github.com"
HF = "https://huggingface.co/api/models"
# Unauthenticated GitHub search allows 10 requests a minute.
SEARCH_PAUSE_NO_TOKEN = 6.5


@dataclass
class Project:
    source: str  # "GitHub" or "Hugging Face"
    id: str  # "owner/name"
    owner: str
    owner_is_org: bool | None  # None when the source doesn't say
    url: str
    description: str
    themes: list[str]
    created: date | None
    stars: int  # GitHub stars, or Hugging Face likes
    forks: int | None = None
    downloads: int | None = None  # Hugging Face: last 30 days
    homepage: str = ""
    gains: list[float] | None = None  # stars per window, oldest first
    gains_basis: str = ""
    acceleration: float | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def velocity(self) -> float:
        """Stars (likes) per month since creation."""
        return self.stars / months_between(self.created, date.today()) if self.created else 0.0

    @property
    def acceleration_rating(self) -> str:
        return rating(self.acceleration)

    @property
    def domain(self) -> str:
        return domain(self.homepage)


def _github_headers(accept: str = "application/vnd.github+json") -> dict:
    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return headers


def github_projects(http: Http, config: dict, themes: dict, today: date) -> list[Project]:
    gh = config["github"]
    since = months_ago(today, gh["lookback_months"]).isoformat()
    patterns = compile_themes(themes)
    found: dict[str, Project] = {}
    excluded = {o.lower() for o in gh.get("exclude_owners", [])}
    topics = [(t, name) for name, spec in themes.items() for t in spec.get("github_topics", [])]
    for n, (topic, theme) in enumerate(topics):
        if n and not os.environ.get("GITHUB_TOKEN"):
            time.sleep(SEARCH_PAUSE_NO_TOKEN)
        data = http.get_json(f"{GITHUB}/search/repositories", headers=_github_headers(), params={
            "q": f"topic:{topic} created:>{since} stars:>={gh['min_stars']} fork:false archived:false",
            "sort": "stars", "order": "desc", "per_page": gh["repos_per_topic"],
        })
        for item in data.get("items", []):
            if item["owner"]["login"].lower() in excluded:
                continue
            text = " ".join([item["full_name"], item.get("description") or "", " ".join(item.get("topics", []))])
            p = found.get(item["full_name"]) or Project(
                source="GitHub", id=item["full_name"], owner=item["owner"]["login"],
                owner_is_org=item["owner"]["type"] == "Organization", url=item["html_url"],
                description=(item.get("description") or "").strip(), themes=themes_of(text, patterns),
                created=parse_day(item.get("created_at")), stars=item["stargazers_count"],
                forks=item.get("forks_count"), homepage=item.get("homepage") or "",
            )
            if theme not in p.themes:
                p.themes.append(theme)  # found through this theme's topic
            found[p.id] = p
    return list(found.values())


def bootstrap_star_gains(http: Http, project: Project, today: date) -> list[float] | None:
    """Monthly star gains for the last 90 days, from GitHub's star timestamps (newest pages first)."""
    pages = (project.stars + 99) // 100
    cutoff = months_ago(today, 3)
    stamps: list[date] = []
    for page in range(pages, 0, -1):
        batch = http.get_json(f"{GITHUB}/repos/{project.id}/stargazers", params={"per_page": 100, "page": page},
                              headers=_github_headers("application/vnd.github.star+json"))
        days = [d for d in (parse_day(s.get("starred_at")) for s in batch) if d]
        stamps += days
        if days and min(days) < cutoff:
            break
    return gains_from_events(stamps, today) if stamps else None


def huggingface_projects(http: Http, config: dict, themes: dict) -> list[Project]:
    patterns = compile_themes(themes)
    models = http.get_json(HF, params={"sort": "trendingScore", "limit": config["huggingface"]["trending_limit"]})
    excluded = {o.lower() for o in config["github"].get("exclude_owners", [])}
    out = []
    for m in models:
        if (m.get("author") or m["id"].split("/")[0]).lower() in excluded:
            continue
        text = " ".join([m["id"], m.get("pipeline_tag") or "", " ".join(m.get("tags", []))])
        out.append(Project(
            source="Hugging Face", id=m["id"], owner=m.get("author") or m["id"].split("/")[0], owner_is_org=None,
            url=f"https://huggingface.co/{m['id']}", description=m.get("pipeline_tag") or "",
            themes=themes_of(text, patterns), created=parse_day(m.get("createdAt")), stars=m.get("likes", 0),
            downloads=m.get("downloads"),
        ))
    return out


def apply_momentum(projects: list[Project], history: dict[str, list[tuple[date, float]]], today: date,
                   http: Http | None, config: dict) -> None:
    """Fill gains and acceleration from snapshot history, rebuilding GitHub star history where allowed."""
    gh = config["github"]
    budget = gh["bootstrap_per_run"] if http and os.environ.get("GITHUB_TOKEN") else 0
    for p in sorted(projects, key=lambda p: -p.stars):
        result = window_gains(history.get(p.id, []), today)
        if result:
            p.gains, p.gains_basis = result
        elif p.source == "GitHub" and budget and p.stars <= gh["bootstrap_max_stars"]:
            budget -= 1
            try:
                p.gains = bootstrap_star_gains(http, p, today)
                p.gains_basis = "30-day windows (GitHub star history)"
            except Exception as e:  # one repo's history is never worth failing the run
                log.warning("Star history failed for %s: %s", p.id, e)
        if p.gains:
            p.acceleration = acceleration_score(p.gains)
        _flag(p)


def _flag(p: Project) -> None:
    """Flag for VC Review conditions; the doc flags a project that meets 3 or more."""
    checks = [
        (p.velocity >= 500, "High star velocity"),
        (p.acceleration is not None and p.acceleration >= 80, "Acceleration score >= 80"),
        (bool(p.downloads and p.downloads >= 100_000), "High adoption (100K+ downloads / 30 days)"),
        (bool(p.forks and p.stars and p.forks >= 0.1 * p.stars), "Strong ecosystem (forks >= 10% of stars)"),
        (bool(p.owner_is_org), "Organization-owned (commercial signal)"),
    ]
    p.flags = [label for ok, label in checks if ok]
