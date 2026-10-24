"""Sheet tabs for Sourcing. Result tabs are rewritten each run; Signal Snapshots and Partner Brief only grow.

The row builders are plain functions so they can be tested and printed without a sheet.
"""

from datetime import date, timedelta

import gspread

from .common import parse_day
from .opensource import Project
from .research import Lab, Paper
from .scoring import TIER_NAMES, Opportunity, theme_counts
from .startups import Startup

DASHBOARD = "Sourcing Dashboard"
OPPORTUNITIES = "Opportunities"
RESEARCH = "Research Scout"
LABS = "Lab Tracker"
OPEN_SOURCE = "Open Source Momentum"
STARTUPS = "Startup Formation"
TEAMS = "Founding Teams"
SNAPSHOTS = "Signal Snapshots"
BRIEF = "Partner Brief"
RAW = "Raw Database"
TAB_COLOR = "#D9EAD3"  # light green, like the other Sourcing tabs (setup_sheet.py)

OPP_HEADERS = ["Rank", "Opportunity", "Type", "Theme", "Total Score", "Tier", "Research", "Lab", "Momentum", "Team",
               "Formation", "Timing", "Key Signals", "Recommended Action", "Owner", "Link", "First Seen",
               "New This Week", "Key"]
SNAPSHOT_HEADERS = ["Date", "Kind", "ID", "Value"]
BRIEF_HEADERS = ["Week Of", "Brief"]


def _pct(x: float | None) -> str:
    return f"{x * 100:.0f}%" if x is not None else ""


def opportunity_rows(opps: list[Opportunity], today: date) -> list[list]:
    week_ago = (today - timedelta(days=7)).isoformat()
    return [[n, o.name, o.kind, ", ".join(o.themes) or "Other", o.total, o.tier, o.research, o.lab, o.momentum,
             o.team, o.formation, o.timing, " | ".join(o.signals), o.action, o.owner, o.url, o.first_seen,
             "NEW" if o.first_seen >= week_ago else "", o.key]
            for n, o in enumerate(opps, start=1)]


def research_rows(papers: list[Paper]) -> list[list]:
    rows = sorted(papers, key=lambda p: (-len(p.flags), -(p.percentile or 0), -p.cited_by))
    return [["🔥" if len(p.flags) >= 2 else "", p.title, ", ".join(p.themes), p.venue, str(p.published or ""),
             p.cited_by, round(p.velocity, 1), _pct(p.percentile), ", ".join(p.tracked), ", ".join(p.companies[:5]),
             ", ".join(p.funders[:3]), " | ".join(p.flags), p.url] for p in rows]


RESEARCH_HEADERS = ["Flag", "Title", "Themes", "Venue", "Published", "Citations", "Citations / Month",
                    "Field Percentile", "Tracked Universities", "Company Co-authors", "Funders", "Key Signals", "Link"]


def lab_rows(labs: list[Lab]) -> list[list]:
    rows = sorted(labs, key=lambda l: (-len(l.flags), -l.citations))
    return [["🔥" if len(l.flags) >= 3 else "", l.name, l.institution, ", ".join(l.themes), len(l.papers), l.citations,
             _pct(l.best_percentile), ", ".join(l.industry_partners[:5]), ", ".join(l.funders[:3]), " | ".join(l.flags),
             max(l.papers, key=lambda p: p.cited_by).url] for l in rows]


LAB_HEADERS = ["Flag", "Lab (PI)", "Institution", "Themes", "Papers", "Citations", "Best Percentile",
               "Industry Partners", "Funders", "Key Signals", "Top Paper"]


def project_rows(projects: list[Project]) -> list[list]:
    rows = sorted(projects, key=lambda p: (-len(p.flags), -(p.acceleration or 0), -p.velocity))
    owner = {True: "Organization", False: "User", None: ""}
    return [["🔥" if len(p.flags) >= 3 else "", p.id, p.source, ", ".join(p.themes) or "Other", owner[p.owner_is_org],
             str(p.created or ""), p.stars, round(p.velocity), p.downloads if p.downloads is not None else "",
             p.acceleration if p.acceleration is not None else "", p.acceleration_rating or "not enough history",
             " → ".join(f"{g:.0f}" for g in p.gains) if p.gains else "", p.gains_basis, " | ".join(p.flags), p.url]
            for p in rows]


PROJECT_HEADERS = ["Flag", "Project", "Source", "Themes", "Owner Type", "Created", "Stars / Likes",
                   "Per Month", "Downloads (30d)", "Acceleration", "Rating", "Gains (oldest → newest)", "Basis",
                   "Key Signals", "Link"]


def startup_rows(startups: list[Startup]) -> list[list]:
    rows = []
    for s in sorted(startups, key=lambda s: (s.yc_batch_date or date.min, s.name.lower()), reverse=True):
        r = max(s.rounds, key=lambda r: r.date or date.min) if s.rounds else None
        rows.append([s.name, ", ".join(s.themes), s.yc_batch, s.team_size or "", r.round if r else "",
                     r.amount if r else "", r.lead if r else "", str(r.date or "") if r else "", s.website,
                     s.description, s.yc_url or (r.url if r else "")])
    return rows


STARTUP_HEADERS = ["Startup", "Themes", "YC Batch", "Team Size", "Latest Early Round", "Amount", "Lead",
                   "Round Date", "Website", "Description", "Source"]


def dashboard_rows(opps: list[Opportunity], trends: dict[str, str], today: date, brief: str = "") -> list[list]:
    week_ago = (today - timedelta(days=7)).isoformat()
    tiers = {t: sum(o.tier == t for o in opps) for t in TIER_NAMES}
    rows = [
        ["Deal Flow Dashboard", f"Updated {today.isoformat()}"], [],
        ["Total Opportunities", len(opps)],
        ["New This Week", sum(o.first_seen >= week_ago for o in opps)],
        ["Tier 1 · Immediate Partner Review", tiers["Tier 1"]],
        ["Tier 2 · Associate Deep Dive", tiers["Tier 2"]],
        ["Tier 3 · Relationship Building", tiers["Tier 3"]],
        ["Tier 4 · Watchlist", tiers["Tier 4"]], [],
        ["Theme", "Opportunity Count", "Avg Score", "Trend"],
    ]
    counts = theme_counts(opps)
    for theme, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        scores = [o.total for o in opps if theme in (o.themes or ["Other"])]
        rows.append([theme, n, round(sum(scores) / len(scores), 1), trends.get(theme, "")])
    rows += [[], ["Top Opportunities"], ["Rank", "Opportunity", "Type", "Theme", "Score", "Tier", "Recommended Action",
                                         "Owner", "Key Signals"]]
    for n, o in enumerate(opps[:20], start=1):
        rows.append([n, o.name, o.kind, o.theme, o.total, o.tier, o.action, o.owner, " | ".join(o.signals[:3])])
    for kind in ("Startup", "Open Source", "Lab", "Research"):
        best = [o for o in opps if o.kind == kind][:5]
        if best:
            rows += [[], [f"Top {kind}"], ["Opportunity", "Theme", "Score", "Tier", "Recommended Action", "Key Signals"]]
            rows += [[o.name, o.theme, o.total, o.tier, o.action, " | ".join(o.signals[:3])] for o in best]
    rows += [[], ["Action Queue"], ["Action", "Opportunity", "Owner", "Tier"]]
    for o in opps:
        if o.tier != "Tier 4":
            rows.append([o.action, o.name, o.owner, o.tier])
    if brief:
        rows += [[], ["Weekly Partner Brief"], [brief]]
    return rows


def snapshot_rows(projects: list[Project], opps: list[Opportunity], today: date) -> list[list]:
    day = today.isoformat()
    rows = [[day, "stars", p.id, p.stars] for p in projects]
    rows += [[day, "theme", theme, n] for theme, n in theme_counts(opps).items()]
    return rows


def history(rows: list[list], kind: str) -> dict[str, list[tuple[date, float]]]:
    out: dict[str, list[tuple[date, float]]] = {}
    for r in rows:
        if len(r) < 4 or r[1] != kind:
            continue
        d = parse_day(r[0])
        try:
            out.setdefault(r[2], []).append((d, float(r[3])))
        except (TypeError, ValueError):
            continue
    return {k: sorted(v) for k, v in out.items() if all(d for d, _ in v)}


def counts_a_week_ago(rows: list[list], today: date) -> dict[str, int]:
    cutoff = today - timedelta(days=7)
    out = {}
    for theme, series in history(rows, "theme").items():
        earlier = [v for d, v in series if d <= cutoff]
        if earlier:
            out[theme] = int(earlier[-1])
    return out


class SourcingStore:
    def __init__(self, spreadsheet: gspread.Spreadsheet):
        self._ss = spreadsheet

    def _tab(self, title: str, cols: int) -> gspread.Worksheet:
        try:
            return self._ss.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self._ss.add_worksheet(title=title, rows=1000, cols=max(cols, 4))
            ws.update_tab_color(TAB_COLOR)
            return ws

    def raw_database(self) -> list[dict]:
        try:
            values = self._ss.worksheet(RAW).get_all_values()
        except gspread.WorksheetNotFound:
            return []
        if not values:
            return []
        header = values[0]
        return [dict(zip(header, r)) for r in values[1:]]

    def snapshots(self) -> list[list]:
        ws = self._tab(SNAPSHOTS, len(SNAPSHOT_HEADERS))
        values = ws.get_all_values()
        return values[1:] if values else []

    def first_seen(self) -> dict[str, str]:
        try:
            values = self._ss.worksheet(OPPORTUNITIES).get_all_values()
        except gspread.WorksheetNotFound:
            return {}
        if not values:
            return {}
        k, f = values[0].index("Key"), values[0].index("First Seen")
        return {r[k]: r[f] for r in values[1:] if len(r) > max(k, f) and r[k]}

    def latest_brief(self) -> tuple[str, str]:
        """(week of, text) of the last Partner Brief, or ("", "")."""
        try:
            values = self._ss.worksheet(BRIEF).get_all_values()
        except gspread.WorksheetNotFound:
            return "", ""
        return (values[-1][0], values[-1][1]) if len(values) > 1 and len(values[-1]) > 1 else ("", "")

    def write_table(self, title: str, headers: list[str], rows: list[list]) -> None:
        ws = self._tab(title, len(headers))
        if ws.row_count < len(rows) + 1:
            ws.add_rows(len(rows) + 1 - ws.row_count)
        ws.clear()
        ws.update([headers] + rows, "A1", value_input_option="RAW")
        ws.freeze(rows=1)

    def write_dashboard(self, rows: list[list]) -> None:
        ws = self._tab(DASHBOARD, 9)
        ws.clear()
        ws.update(rows, "A1", value_input_option="RAW")

    def append(self, title: str, headers: list[str], rows: list[list]) -> None:
        ws = self._tab(title, len(headers))
        if not ws.row_values(1):
            ws.update([headers], "A1", value_input_option="RAW")
            ws.freeze(rows=1)
        if rows:
            ws.append_rows(rows, value_input_option="RAW", table_range="A1")
