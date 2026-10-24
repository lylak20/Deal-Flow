"""Opportunity Scoring Engine: combine every tracker's signals into one ranked list.

Five scores of 0-20 each (Research, Lab, Momentum, Team, Formation) add up to a 0-100 total, as in the
Google Doc. Every point comes with a written signal, so a score can always be traced back to data.
"""

import math
from dataclasses import dataclass, field
from datetime import date

from normalize import company_key

from .common import months_ago
from .opensource import Project
from .research import Lab, Paper
from .startups import Startup

ACTIONS = {
    "Startup": ["Founder Outreach", "Research Further", "Relationship Building", "Monitor"],
    "Open Source": ["Founder Outreach", "Research Further", "Relationship Building", "Monitor"],
    "Lab": ["Meet Professor", "Meet Professor", "Relationship Building", "Monitor"],
    "Research": ["Research Further", "Research Further", "Monitor", "Monitor"],
}
OWNERS = ["Partner", "Principal", "Associate", "Associate"]
TIER_NAMES = ["Tier 1", "Tier 2", "Tier 3", "Tier 4"]


@dataclass
class Opportunity:
    name: str
    kind: str  # Startup | Open Source | Lab | Research
    themes: list[str]
    url: str
    description: str = ""
    research: float = 0
    lab: float = 0
    momentum: float = 0
    team: float = 0
    formation: float = 0
    signals: list[str] = field(default_factory=list)
    acceleration: float | None = None
    timing: str = "Neutral"
    tier: str = ""
    action: str = ""
    owner: str = ""
    first_seen: str = ""
    startup: Startup | None = None
    projects: list[Project] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.kind}:{company_key(self.name) or self.name.lower()}"

    @property
    def total(self) -> float:
        return round(self.research + self.lab + self.momentum + self.team + self.formation, 1)

    @property
    def theme(self) -> str:
        return self.themes[0] if self.themes else "Other"

    def add(self, part: str, points: float, signal: str) -> None:
        if points <= 0:
            return
        setattr(self, part, round(min(20.0, getattr(self, part) + points), 1))
        self.signals.append(signal)


def _scale(x: float, full: float) -> float:
    """0-1 on a log scale, reaching 1 at full: 10 stars a month and 10,000 stars a month stay distinguishable."""
    return min(1.0, math.log10(1 + max(x, 0)) / math.log10(1 + full))


def _project_momentum(p: Project) -> tuple[float, list[str]]:
    points, signals = 0.0, []
    if p.acceleration is not None:
        points += 0.12 * p.acceleration
        signals.append(f"{p.id}: acceleration {p.acceleration:.0f} ({p.acceleration_rating}, {p.gains_basis})")
    if p.source == "GitHub":
        speed = _scale(p.velocity, 10_000)
        if speed:
            signals.append(f"{p.id}: {p.stars:,} stars, {p.velocity:,.0f}/month since {p.created}")
    else:
        speed = max(_scale(p.velocity, 3_000), _scale(p.downloads or 0, 1_000_000))
        if speed:
            signals.append(f"{p.id}: {p.stars:,} likes, {p.downloads or 0:,} downloads in 30 days (Hugging Face)")
    return min(20.0, points + 8 * speed), signals


def _paper_research(p: Paper, velocity_rank: float) -> float:
    return 10 * (p.percentile or 0) + 6 * velocity_rank + 4


def _lab_points(lab: Lab) -> float:
    return min(8, 3 * len(lab.papers)) + 6 * lab.best_percentile + 3 * bool(lab.industry_partners) + 3 * bool(lab.funders)


def build(startups: list[Startup], projects: list[Project], papers: list[Paper], labs: list[Lab],
          config: dict, today: date) -> list[Opportunity]:
    by_key = {s.key: s for s in startups}
    by_domain = {s.domain: s for s in startups if s.domain}
    opps: dict[str, Opportunity] = {}

    for s in startups:
        o = Opportunity(s.name, "Startup", list(s.themes), s.website or s.yc_url, s.description, startup=s)
        opps[s.key] = o
        since = months_ago(today, config["startups"]["formation_months"])
        if s.yc_batch_date and s.yc_batch_date >= since:
            age = (today - s.yc_batch_date).days
            o.add("formation", 8 if age <= 183 else 6 if age <= 365 else 4, f"YC {s.yc_batch}")
            o.add("team", 8, "Selected by YC (team vetted by an accelerator)")
        early = [r for r in s.rounds if not r.date or r.date >= since]
        if early:
            r = max(early, key=lambda r: r.date or date.min)
            o.add("formation", 8, f"{r.round} {r.amount} on {r.date} (lead: {r.lead or 'n/a'})".replace("  ", " "))

    # Projects join their company when the owner name or homepage domain matches; the rest stand alone.
    for p in projects:
        s = by_key.get(company_key(p.owner)) or (by_domain.get(p.domain) if p.domain else None)
        if s:
            o = opps[s.key]
        else:
            o = opps.setdefault(f"oss:{company_key(p.owner) or p.owner.lower()}",
                                Opportunity(p.owner, "Open Source", [], p.url, p.description))
        o.projects.append(p)
        o.themes += [t for t in p.themes if t not in o.themes]
    for o in opps.values():
        if not o.projects:
            continue
        best = max(o.projects, key=lambda p: _project_momentum(p)[0])
        points, signals = _project_momentum(best)
        o.acceleration = best.acceleration
        if points:
            o.momentum = round(points, 1)
            o.signals += signals
        if o.kind == "Open Source":
            o.url = best.url
            o.description = o.description or best.description
            if best.owner_is_org:
                o.add("formation", 4, f"Organization {best.owner} created {best.id} on {best.created}")
                if best.stars >= 1000:
                    o.add("team", 4, "Open source creator team (organization repo with 1,000+ stars)")
        elif any(p.source == "GitHub" for p in o.projects):
            o.add("formation", 4, f"Recent open source release: {o.projects[0].id}")

    # Papers by a company's researchers, and co-authorship with a tracked university.
    for p in papers:
        for c in p.companies:
            s = by_key.get(company_key(c))
            if not s:
                continue
            o = opps[s.key]
            first = o.research == 0
            o.add("research", 10 if first else 5, f"Publishes research: \"{p.title[:80]}\" ({p.venue or 'n/a'})")
            if p.tracked and not o.lab:
                o.add("lab", 10, f"Co-authored with {', '.join(p.tracked)}")

    velocities = sorted(p.velocity for p in papers)
    rank = {id(p): (velocities.index(p.velocity) / max(len(velocities) - 1, 1)) for p in papers}
    lab_of, listed_labs = {}, set()
    for lab in labs:
        for p in lab.papers:
            lab_of[p.id] = lab
        if len(lab.flags) < 2:
            continue
        listed_labs.add(id(lab))
        o = Opportunity(lab.name, "Lab", lab.themes, lab.papers[0].url,
                        f"{len(lab.papers)} papers, {lab.citations} citations")
        o.add("lab", _lab_points(lab), "; ".join(lab.flags))
        best = max(lab.papers, key=lambda p: _paper_research(p, rank[id(p)]))
        o.add("research", _paper_research(best, rank[id(best)]), f"Top paper: \"{best.title[:80]}\" ({best.cited_by} citations)")
        opps[f"lab:{lab.pi_id}:{lab.institution}"] = o
    for p in papers:
        # A paper whose lab is already listed is represented by that lab.
        if len(p.flags) < 2 or id(lab_of.get(p.id)) in listed_labs:
            continue
        o = Opportunity(p.title, "Research", list(p.themes), p.url, f"{p.venue}, {p.published}")
        o.add("research", _paper_research(p, rank[id(p)]), "; ".join(p.flags))
        if p.id in lab_of:
            o.add("lab", _lab_points(lab_of[p.id]), f"From {lab_of[p.id].name}")
        opps[f"paper:{p.id}"] = o
    return list(opps.values())


def theme_trends(opps: list[Opportunity], previous: dict[str, int]) -> dict[str, str]:
    """Rising / Stable / Falling per theme against the counts of a week ago."""
    now = theme_counts(opps)
    out = {}
    for theme, n in now.items():
        before = previous.get(theme)
        if before is None:
            out[theme] = "New"
        elif n >= before * 1.1 and n - before >= 2:
            out[theme] = "Rising"
        elif n <= before * 0.9 and before - n >= 2:
            out[theme] = "Falling"
        else:
            out[theme] = "Stable"
    return out


def theme_counts(opps: list[Opportunity]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for o in opps:
        for t in o.themes or ["Other"]:
            counts[t] = counts.get(t, 0) + 1
    return counts


def rank(opps: list[Opportunity], config: dict, trends: dict[str, str], first_seen: dict[str, str],
         today: date) -> list[Opportunity]:
    """Tier, timing, action and owner for each opportunity, sorted as the dashboard agent asks."""
    thresholds = config["scoring"]["tier_thresholds"]
    for o in opps:
        level = next((i for i, t in enumerate(thresholds) if o.total >= t), 3)
        o.tier, o.owner = TIER_NAMES[level], OWNERS[level]
        o.action = ACTIONS[o.kind][level]
        trend = trends.get(o.theme)
        o.timing = {"Rising": "Strong Tailwind", "Falling": "Weak Tailwind"}.get(trend, "Neutral")
        o.first_seen = first_seen.get(o.key, today.isoformat())
    return sorted(opps, key=lambda o: (-o.total, -(o.acceleration or 0), -o.team, -o.formation, o.name.lower()))
