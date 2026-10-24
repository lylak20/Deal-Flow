"""Daily Sourcing run: collect free public signals -> score -> write the sheet's Sourcing tabs.

    python sourcing_main.py              # full run (writes the sheet)
    python sourcing_main.py --no-write   # collect and score, print the top opportunities, touch nothing

No API key is needed for --no-write. GITHUB_TOKEN raises the GitHub rate limit and enables star-history
rebuilding; OPENALEX_API_KEY gives OpenAlex its own daily budget; ANTHROPIC_API_KEY is used only for the
weekly brief and founding-team lookups.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import urllib3.util.connection
import yaml
from dotenv import load_dotenv

from sourcing import opensource, research, scoring, startups, store
from sourcing.common import Http

HERE = Path(__file__).parent
log = logging.getLogger("sourcing")


def collect(name: str, fn, failed: list[str]):
    try:
        out = fn()
        log.info("%s: %d", name, len(out))
        return out
    except Exception as e:  # one source down shouldn't stop the others
        log.warning("%s failed: %s", name, e)
        failed.append(name)
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-write", action="store_true", help="collect and score, but do not touch the sheet")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(HERE / ".env")
    config = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))
    if config["http"].get("ipv4_only"):
        urllib3.util.connection.HAS_IPV6 = False
    cfg = config["sourcing"]
    themes = cfg["themes"]
    today = datetime.now(timezone.utc).date()
    http = Http(config["http"]["user_agent"], timeout=60)

    sheet = None
    raw_rows, snapshot_rows, first_seen, last_brief = [], [], {}, ("", "")
    if not args.no_write:
        from sheets import open_spreadsheet
        sheet = store.SourcingStore(open_spreadsheet())
        raw_rows, snapshot_rows = sheet.raw_database(), sheet.snapshots()
        first_seen, last_brief = sheet.first_seen(), sheet.latest_brief()

    failed: list[str] = []
    papers = collect("OpenAlex papers", lambda: research.papers(http, cfg, themes, today), failed)
    labs = research.labs(papers, cfg)
    projects = collect("GitHub repositories", lambda: opensource.github_projects(http, cfg, themes, today), failed)
    projects += collect("Hugging Face models", lambda: opensource.huggingface_projects(http, cfg, themes), failed)
    opensource.apply_momentum(projects, store.history(snapshot_rows, "stars"), today, http, cfg)
    yc = collect("YC companies", lambda: startups.yc_startups(http, cfg, themes, today), failed)
    funded = startups.funded_startups(raw_rows, cfg, themes, today)
    companies = startups.merge(yc + funded)
    log.info("Labs: %d, startups: %d (YC %d, funding news %d)", len(labs), len(companies), len(yc), len(funded))

    opps = scoring.build(companies, projects, papers, labs, cfg, today)
    teams = {}
    claude_cfg = cfg["claude"]
    client = None
    # --no-write never calls Claude, so a local trial run costs nothing.
    if not args.no_write and os.environ.get("ANTHROPIC_API_KEY") \
            and (claude_cfg["founding_team"] or claude_cfg["weekly_brief"]):
        import anthropic
        client = anthropic.Anthropic(max_retries=3)
    if client and claude_cfg["founding_team"]:
        teams = _founding_teams(client, claude_cfg, opps, cfg, today, first_seen)

    trends = scoring.theme_trends(opps, store.counts_a_week_ago(snapshot_rows, today))
    opps = scoring.rank(opps, cfg, trends, first_seen, today)

    brief_week, brief = last_brief
    if client and claude_cfg["weekly_brief"] and today.weekday() == claude_cfg["brief_weekday"] \
            and brief_week != today.isoformat() and opps:
        from sourcing.claude import weekly_brief
        try:
            brief, brief_week = weekly_brief(client, claude_cfg["model"], opps), today.isoformat()
        except Exception as e:
            log.warning("Weekly brief failed: %s", e)
            brief_week = ""

    if sheet is None:
        _print(opps, trends)
    else:
        sheet.write_table(store.OPPORTUNITIES, store.OPP_HEADERS, store.opportunity_rows(opps, today))
        sheet.write_table(store.RESEARCH, store.RESEARCH_HEADERS, store.research_rows(papers))
        sheet.write_table(store.LABS, store.LAB_HEADERS, store.lab_rows(labs))
        sheet.write_table(store.OPEN_SOURCE, store.PROJECT_HEADERS, store.project_rows(projects))
        sheet.write_table(store.STARTUPS, store.STARTUP_HEADERS, store.startup_rows(companies))
        if teams:
            sheet.write_table(store.TEAMS, ["Startup", "Founder", "Role", "Technical", "Previous Startup",
                                            "Research Background", "Quote", "Source"], [
                [name, f.name, f.role, f.technical, f.previous_startup, f.research_background, f.quote, f.source_url]
                for name, team in teams.items() for f in team.founders])
        sheet.append(store.SNAPSHOTS, store.SNAPSHOT_HEADERS, store.snapshot_rows(projects, opps, today))
        if brief and brief_week == today.isoformat() and last_brief[0] != brief_week:
            sheet.append(store.BRIEF, store.BRIEF_HEADERS, [[brief_week, brief]])
        sheet.write_dashboard(store.dashboard_rows(opps, trends, today, brief))
        log.info("Sheet updated: %d opportunities", len(opps))

    log.info("Done. opportunities=%d papers=%d labs=%d projects=%d startups=%d failed_sources=%s",
             len(opps), len(papers), len(labs), len(projects), len(companies), failed or "none")
    return 1 if len(failed) == 4 else 0


def _founding_teams(client, claude_cfg: dict, opps, cfg: dict, today, first_seen) -> dict:
    """Look up founders for the top startups once; add their Team points."""
    from sourcing.claude import founding_team, team_points
    ranked = scoring.rank(list(opps), cfg, {}, first_seen, today)
    teams = {}
    for o in [o for o in ranked if o.kind == "Startup"][:claude_cfg["founding_team_top_n"]]:
        try:
            team = founding_team(client, claude_cfg["model"], o.name, o.url, o.description)
        except Exception as e:
            log.warning("Founding team lookup failed for %s: %s", o.name, e)
            continue
        teams[o.name] = team
        for points, signal in team_points(team):
            o.add("team", points, signal)
    return teams


def _print(opps, trends) -> None:
    print(f"\nThemes: {trends}\n")
    for n, o in enumerate(opps[:25], start=1):
        print(f"{n:2}. [{o.tier}] {o.total:5.1f}  {o.kind:11} {o.name[:60]}  ({o.theme})")
        print(f"      R{o.research} L{o.lab} M{o.momentum} T{o.team} F{o.formation} · {o.action} · "
              + " | ".join(o.signals[:3])[:220])


if __name__ == "__main__":
    sys.exit(main())
