from datetime import date, timedelta
from types import SimpleNamespace as NS

import pytest
import yaml
from pathlib import Path

from sourcing import claude, momentum, opensource, research, scoring, startups, store

TODAY = date(2026, 10, 23)
CONFIG = yaml.safe_load((Path(__file__).parent.parent / "config.yaml").read_text(encoding="utf-8"))["sourcing"]
THEMES = CONFIG["themes"]


# --- Momentum ------------------------------------------------------------------------------------

def test_acceleration_matches_the_docs_examples():
    assert momentum.rating(momentum.acceleration_score([100, 300, 900])) == "Explosive"
    assert momentum.rating(momentum.acceleration_score([1000, 700, 300])) == "Decelerating"
    assert momentum.acceleration_score([500, 500, 500]) == 50
    assert momentum.acceleration_score([0, 0, 0]) is None


def test_window_gains_need_enough_history():
    weekly = [(TODAY - timedelta(days=d), 1000 + (21 - d) * 10) for d in range(0, 22)]
    gains, basis = momentum.window_gains(weekly, TODAY)
    assert gains == [70, 70, 70] and basis.startswith("7-day")
    assert momentum.window_gains(weekly[:10], TODAY) is None
    monthly = [(TODAY - timedelta(days=90), 0), (TODAY - timedelta(days=60), 100), (TODAY - timedelta(days=30), 400),
               (TODAY, 1300)]
    gains, basis = momentum.window_gains(monthly, TODAY)
    assert gains == [100, 300, 900] and basis.startswith("30-day")


def test_gains_from_star_dates():
    stars = [TODAY - timedelta(days=d) for d in (85, 50, 45, 10, 5, 1, 1)]
    assert momentum.gains_from_events(stars, TODAY) == [1, 2, 4]


# --- Fake HTTP -----------------------------------------------------------------------------------

def work(wid, title, abstract, authors, cited=50, pct=0.95, funders=()):
    words = abstract.split()
    return {
        "id": f"https://openalex.org/{wid}", "doi": f"https://doi.org/{wid}", "title": title,
        "publication_date": "2026-04-01", "cited_by_count": cited,
        "citation_normalized_percentile": {"value": pct}, "primary_location": {"source": {"display_name": "Nature"}},
        "abstract_inverted_index": {w: [i] for i, w in enumerate(words)},
        "funders": [{"display_name": f} for f in funders],
        "authorships": [{"author": {"display_name": n, "id": f"https://openalex.org/{aid}"}, "author_position": pos,
                         "institutions": [{"id": f"https://openalex.org/{i}", "display_name": iname, "type": kind}
                                          for i, iname, kind in insts]}
                        for n, aid, pos, insts in authors],
    }


STANFORD = ("I97018004", "Stanford University", "education")
ACME = ("I1", "Acme Robotics", "company")
WORKS = [
    work("W1", "Humanoid robot learning from video", "We train a neural policy for humanoid robots",
         [("Ann", "A1", "first", [STANFORD]), ("Bob", "A2", "middle", [ACME]), ("Prof Kim", "A3", "last", [STANFORD])],
         funders=["NSF"]),
    work("W2", "Dexterous manipulation with diffusion", "A robot manipulation algorithm",
         [("Cy", "A4", "first", [STANFORD]), ("Prof Kim", "A3", "last", [STANFORD])], cited=10, pct=0.5),
    work("W3", "A survey of robot learning", "robot learning survey", [("Dee", "A5", "last", [STANFORD])]),
    work("W4", "Clinical guidance for sepsis care", "clinical trial of a drug in patients",
         [("Eve", "A6", "last", [STANFORD])]),
]


class FakeHttp:
    def __init__(self):
        self.calls = []

    def get_json(self, url, params=None, headers=None):
        self.calls.append((url, params))
        if "openalex" in url:
            return {"results": WORKS if "Robotics" in self._theme(params) else []}
        if "search/repositories" in url:
            return {"items": [
                {"full_name": "acme/robo-sim", "owner": {"login": "acme", "type": "Organization"},
                 "html_url": "https://github.com/acme/robo-sim", "description": "Humanoid robot simulator",
                 "topics": ["robotics"], "created_at": "2026-04-23T00:00:00Z", "stargazers_count": 3000,
                 "forks_count": 400, "homepage": "https://www.acmerobotics.ai"},
                {"full_name": "google/big", "owner": {"login": "google", "type": "Organization"},
                 "html_url": "x", "description": "", "topics": [], "created_at": "2026-04-01", "stargazers_count": 9,
                 "forks_count": 1, "homepage": ""},
            ]} if "topic:robotics" in params["q"] else {"items": []}
        if "huggingface" in url:
            return [{"id": "solo/tiny-agent", "author": "solo", "likes": 300, "downloads": 200_000,
                     "createdAt": "2026-09-23T00:00:00Z", "pipeline_tag": "text-generation", "tags": ["ai agent"]}]
        if url.endswith("meta.json"):
            return {"batches": {"summer-2026": {"name": "Summer 2026", "api": "yc/summer-2026"},
                                "winter-2020": {"name": "Winter 2020", "api": "yc/winter-2020"}}}
        if url == "yc/summer-2026":
            return [{"name": "Acme Robotics", "website": "https://acmerobotics.ai", "one_liner": "Humanoid robots",
                     "long_description": "", "tags": ["Robotics"], "industries": [], "status": "Active",
                     "team_size": 4, "url": "https://www.ycombinator.com/companies/acme"},
                    {"name": "Bakery", "website": "", "one_liner": "Bread delivery", "long_description": "",
                     "tags": [], "industries": [], "status": "Active"}]
        raise AssertionError(url)

    @staticmethod
    def _theme(params):
        return "Robotics" if "humanoid" in params["filter"] else ""


@pytest.fixture
def collected(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(opensource, "SEARCH_PAUSE_NO_TOKEN", 0)
    http = FakeHttp()
    papers = research.papers(http, CONFIG, THEMES, TODAY)
    labs = research.labs(papers, CONFIG)
    projects = opensource.github_projects(http, CONFIG, THEMES, TODAY) + \
        opensource.huggingface_projects(http, CONFIG, THEMES)
    yc = startups.yc_startups(http, CONFIG, THEMES, TODAY)
    funded = startups.funded_startups([
        {"Date": "2026-09-01", "Company": "Acme Robotics Inc.", "Funding Round": "Seed", "Amount Raised": "$5M",
         "Lead Investor": "Sequoia", "Category": "Robotics & Physical AI", "Article URL": "https://news/acme"},
        {"Date": "2026-09-01", "Company": "LateCo", "Funding Round": "Series C", "Amount Raised": "$90M"},
    ], CONFIG, THEMES, TODAY)
    return http, papers, labs, projects, startups.merge(yc + funded)


def test_research_keeps_real_research_and_finds_the_lab(collected):
    _, papers, labs, _, _ = collected
    assert sorted(p.id for p in papers) == ["W1", "W2"]  # the survey and the drug trial are left out
    w1 = next(p for p in papers if p.id == "W1")
    assert w1.themes == ["Robotics & Physical AI"] and w1.tracked == ["Stanford"] and w1.companies == ["Acme Robotics"]
    assert len(w1.flags) >= 2
    assert [(l.name, len(l.papers)) for l in labs] == [("Prof Kim lab (Stanford)", 2)]


def test_open_source_skips_big_companies_and_flags(collected):
    _, _, _, projects, _ = collected
    assert [p.id for p in projects] == ["acme/robo-sim", "solo/tiny-agent"]
    gh = projects[0]
    assert gh.owner_is_org and gh.domain == "acmerobotics.ai" and gh.themes == ["Robotics & Physical AI"]
    history = {"acme/robo-sim": [(TODAY - timedelta(days=d), 3000 - d * d) for d in range(0, 22)]}
    opensource.apply_momentum(projects, history, TODAY, None, CONFIG)
    assert gh.gains and gh.acceleration is not None and gh.acceleration < 50  # growth is slowing down
    assert projects[1].acceleration is None  # no history yet


def test_startups_merge_yc_and_funding_news(collected):
    *_, companies = collected
    assert [s.name for s in companies] == ["Acme Robotics"]  # Bakery matches no theme, Series C isn't early
    s = companies[0]
    assert s.yc_batch == "Summer 2026" and [r.round for r in s.rounds] == ["Seed"]
    assert startups.batch_date("Fall 2025") == date(2025, 9, 1)


def test_scoring_links_signals_to_the_startup(collected):
    _, papers, labs, projects, companies = collected
    opensource.apply_momentum(projects, {}, TODAY, None, CONFIG)
    opps = scoring.build(companies, projects, papers, labs, CONFIG, TODAY)
    opps = scoring.rank(opps, CONFIG, scoring.theme_trends(opps, {}), {}, TODAY)
    acme = next(o for o in opps if o.name == "Acme Robotics")
    assert [p.id for p in acme.projects] == ["acme/robo-sim"]  # joined by homepage domain
    assert acme.formation == 20 and acme.team == 8 and acme.research == 10 and acme.lab == 10
    assert any("Seed $5M" in s for s in acme.signals)
    assert opps[0] is acme and acme.tier == "Tier 1" and acme.action == "Founder Outreach" and acme.owner == "Partner"
    kinds = sorted(o.kind for o in opps)
    assert kinds == ["Lab", "Open Source", "Startup"]  # W1 is represented by its lab, not listed twice


def test_theme_trends_and_dashboard(collected):
    _, papers, labs, projects, companies = collected
    opps = scoring.build(companies, projects, papers, labs, CONFIG, TODAY)
    trends = scoring.theme_trends(opps, {"Robotics & Physical AI": 0})
    assert trends["Robotics & Physical AI"] == "Rising" and trends["AI Agents"] == "New"
    opps = scoring.rank(opps, CONFIG, trends, {"Startup:acme": "2026-01-01"}, TODAY)
    rows = store.opportunity_rows(opps, TODAY)
    acme_row = next(r for r in rows if r[1] == "Acme Robotics")
    assert acme_row[16] == "2026-01-01" and acme_row[17] == ""  # seen before: not new
    dash = store.dashboard_rows(opps, trends, TODAY, brief="brief text")
    labels = [r[0] for r in dash if r]
    for section in ("Total Opportunities", "Theme", "Top Opportunities", "Top Startup", "Action Queue",
                    "Weekly Partner Brief"):
        assert section in labels


def test_snapshot_history_round_trip(collected):
    _, papers, labs, projects, companies = collected
    opps = scoring.build(companies, projects, papers, labs, CONFIG, TODAY)
    old = store.snapshot_rows(projects, opps, TODAY - timedelta(days=8))
    new = store.snapshot_rows(projects, opps, TODAY)
    rows = [[str(c) for c in r] for r in old + new]
    assert store.history(rows, "stars")["acme/robo-sim"][0] == (TODAY - timedelta(days=8), 3000.0)
    assert store.counts_a_week_ago(rows, TODAY) == scoring.theme_counts(opps)


# --- Claude parts (fake client) ------------------------------------------------------------------

def test_brief_flags_numbers_not_in_the_data():
    text = "- Acme: 3,000 stars\n- Acme raised $9M\n## Themes"
    out = claude.flag_new_numbers(text, '{"signals": ["3,000 stars", "Seed $5M"]}')
    assert out.splitlines() == ["- Acme: 3,000 stars", f"- Acme raised $9M {claude.MARK}", "## Themes"]


def test_founding_team_keeps_only_founders_found_on_fetched_pages():
    page = NS(type="web_fetch_result", url="https://acme.ai/about",
              content=NS(source=NS(type="text", data="Acme was founded by Jane Doe, a former Stanford roboticist.")))
    research_resp = NS(stop_reason="end_turn", content=[
        NS(type="web_fetch_tool_result", content=page), NS(type="text", text="Founders: Jane Doe, John Roe.")])
    parsed = claude.TeamFindings(founders=[
        claude.Founder(name="Jane Doe", role="CEO", technical=True, previous_startup=False, research_background=True,
                       quote="Acme was founded by Jane Doe", source_url="https://acme.ai/about"),
        claude.Founder(name="John Roe", role="CTO", technical=True, previous_startup=True, research_background=False,
                       quote="John Roe co-founded Acme", source_url="https://news"),
    ])

    class Messages:
        def create(self, **kw):
            assert kw["tools"][0]["type"] == "web_search_20250305"
            return research_resp

        def parse(self, **kw):
            return NS(parsed_output=parsed)

    team = claude.founding_team(NS(messages=Messages()), "claude-haiku-5-5", "Acme", "https://acme.ai", "")
    assert [f.name for f in team.founders] == ["Jane Doe"] and team.dropped == ["John Roe"]
    assert [s for _, s in claude.team_points(team)] == [
        "Founders found: Jane Doe", "Technical founder", "Founder with research background"]
