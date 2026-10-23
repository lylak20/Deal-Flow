"""Robotics focus, Google News handling, priority order, LLM budget and same-story skips."""

import re
from datetime import datetime, timedelta, timezone

import pytest

import main
from collector import Article, guess_company, is_candidate, is_headline_only, parse_feed
from extractor import Extraction, FundingEvent, QuotaExhausted, system_instruction
from normalize import EventRecord, event_key

NOW = datetime(2026, 10, 23, 15, 0, tzinfo=timezone.utc)
FUNDING = [re.compile(p, re.I) for p in [r"\brais(e|es|ed|ing)\b", r"\bfunding\b"]]
FOCUS = [re.compile(p, re.I) for p in ["robot", r"\bhumanoids?\b", r"\bdrones?\b", r"\bautonomous\b"]]

CONFIG = {
    "dedup_window_days": 365,
    "max_attempts_per_article": 3,
    "max_article_chars": 6000,
    "http": {"user_agent": "test", "timeout_seconds": 1},
    "sources": [{"name": "TechCrunch", "priority": 1}, {"name": "PR Newswire", "priority": 1},
                {"name": "The Robot Report", "priority": 2}, {"name": "Google News", "priority": 3}],
}

GOOGLE_FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Google News</title>
<item>
  <title>Chicago robotics startup Formic raises $50M as production hours surge - The Business Journals</title>
  <link>https://news.google.com/rss/articles/CBMiabc?oc=5</link>
  <pubDate>Fri, 23 Oct 2026 09:00:00 GMT</pubDate>
  <description>Chicago robotics startup Formic raises $50M - The Business Journals</description>
  <source url="https://www.bizjournals.com">The Business Journals</source>
</item>
</channel></rss>"""


def art(title, source="TechCrunch", priority=1, headline_only=False, hours_ago=1, url=None):
    return Article(url or f"https://example.com/{abs(hash(title))}", source, title, "",
                   NOW - timedelta(hours=hours_ago), priority, headline_only)


def ev(company, in_focus=True, round_="Seed", amount="$10M", category="Industrial Automation"):
    return FundingEvent(company=company, round=round_, amount=amount, valuation="", date="2026-10-23",
                        lead_investor="", other_investors=[], category=category, in_focus=in_focus)


class FakeExtractor:
    """Answers from a {title: Extraction} map and records which titles it was asked about."""

    def __init__(self, answers, quota_after=None):
        self.answers, self.calls, self.quota_after = answers, [], quota_after

    def extract(self, article, text):
        if self.quota_after is not None and len(self.calls) >= self.quota_after:
            raise QuotaExhausted("out")
        self.calls.append(article.title)
        return self.answers.get(article.title, Extraction(is_funding_announcement=False, events=[]))


@pytest.fixture(autouse=True)
def no_http(monkeypatch):
    monkeypatch.setattr(main, "article_text", lambda a, c: a.title)


def run(work_articles, fake, existing=None, budget=None):
    config = dict(CONFIG, **({"max_llm_calls_per_run": budget} if budget else {}))
    work = [(i + 2, a, 0) for i, a in enumerate(work_articles)]
    existing = [] if existing is None else existing
    statuses, counts, quota = main.run_extraction(work, fake, existing, config, NOW)
    rows = {r: st for r, st, *_ in statuses}
    by_title = {a.title: rows.get(i + 2, "pending") for i, a in enumerate(work_articles)}
    return by_title, counts, quota, existing


def test_focus_keywords_keep_robotics_and_drop_other_rounds():
    since = NOW - timedelta(days=2)
    assert is_candidate(art("Sam Altman-backed Coco Robotics raises $80M"), FUNDING, since, FOCUS)
    assert not is_candidate(art("Legal AI startup Legora raises $550M Series E"), FUNDING, since, FOCUS)
    # Without a focus every funding article still qualifies (old behaviour).
    assert is_candidate(art("Legal AI startup Legora raises $550M Series E"), FUNDING, since)


@pytest.mark.parametrize("title, expected", [
    ("Sam Altman-backed Coco Robotics raises $80M", "coco"),
    ("Chicago robotics startup Formic raises $50M as production hours surge", "formic"),
    ("Warhound raises $200M to engineer war dogs", "warhound"),
    ("Exclusive: Slingshot Raises $27M In Seed Funding", "slingshot"),
    ("Cygrid Raises €2.66M Pre-seed", "cygrid"),
    ("Kodama raises $7M to bring robotics to forestry", "kodama"),
    ("Legora's valuation leaps to $8.8B, Europe's tech funding rebounds", None),
    ("Investors break down how founders can raise a Series A today", None),
])
def test_guess_company(title, expected):
    assert guess_company(title) == expected


def test_google_news_entry_names_the_publisher_and_is_headline_only():
    [a] = parse_feed(GOOGLE_FEED, "Google News", priority=3, headline_only=True)
    assert a.source == "The Business Journals (via Google News)"
    assert a.title == "Chicago robotics startup Formic raises $50M as production hours surge"
    assert a.priority == 3 and is_headline_only(a)
    # A retry rebuilt from the Article Log has no flags but is still recognised by its host.
    assert is_headline_only(Article(a.url, a.source, a.title, "", None))
    assert main.source_priority(CONFIG, a.source) == 3
    assert main.source_priority(CONFIG, "PR Newswire") == 1


def test_priority_sources_go_first_when_the_budget_is_small():
    google = art("Formic raises $50M", "X (via Google News)", 3, True, hours_ago=1)
    trade = art("Kodama raises $7M", "The Robot Report", 2, hours_ago=1)
    tc = art("Coco Robotics raises $80M", "TechCrunch", 1, hours_ago=20)
    prn = art("Arcanus raises $1M for drones", "PR Newswire", 1, hours_ago=5)
    fake = FakeExtractor({a.title: Extraction(is_funding_announcement=True, events=[ev(guess_company(a.title))])
                          for a in (google, trade, tc, prn)})
    status, counts, _, existing = run([google, trade, tc, prn], fake, budget=2)
    assert fake.calls == [prn.title, tc.title]  # priority 1 first, newest first within a priority
    assert status[google.title] == status[trade.title] == "pending"
    assert counts["left_pending"] == 2 and len(existing) == 2


def test_quota_exhausted_after_priority_sources_leaves_the_rest_pending():
    tc = art("Coco Robotics raises $80M", "TechCrunch", 1)
    google = art("Formic raises $50M", "X (via Google News)", 3, True)
    fake = FakeExtractor({tc.title: Extraction(is_funding_announcement=True, events=[ev("Coco Robotics")])},
                         quota_after=1)
    status, counts, quota, _ = run([google, tc], fake)
    assert status[tc.title] == "extracted" and status[google.title] == "pending"
    assert quota is not None


def test_same_story_from_a_second_source_is_skipped_without_a_call():
    tc = art("Formic raises $50M Series C", "TechCrunch", 1)
    google = art("Chicago robotics startup Formic raises $50M", "Biz (via Google News)", 3, True)
    fake = FakeExtractor({tc.title: Extraction(is_funding_announcement=True, events=[ev("Formic")])})
    status, counts, _, _ = run([google, tc], fake)
    assert fake.calls == [tc.title]
    assert status[google.title] == "skipped_same_story"


def test_headline_about_a_company_added_this_week_is_skipped():
    existing = [EventRecord(event_key("Formic", "Series C", "2026-10-21"),
                            {"Date": "2026-10-21", "Company": "Formic"}, row=5)]
    google = art("Formic raises $50M", "Biz (via Google News)", 3, True)
    fake = FakeExtractor({})
    status, *_ = run([google], fake, existing=existing)
    assert fake.calls == [] and status[google.title] == "skipped_known_company"


def test_stale_headline_is_dropped():
    google = art("Formic raises $50M", "Biz (via Google News)", 3, True, hours_ago=24 * 5)
    status, *_ = run([google], FakeExtractor({}))
    assert status[google.title] == "skipped_stale"


def test_out_of_focus_rounds_are_not_written():
    tc = art("Weekly roundup: robots and lawyers raise money", "TechCrunch", 1)
    fake = FakeExtractor({tc.title: Extraction(is_funding_announcement=True, events=[
        ev("Legora", in_focus=False, category="Legal Tech"), ev("Coco Robotics", category="Drones & Delivery")])})
    status, counts, _, existing = run([tc], fake)
    assert [e.values["Company"] for e in existing] == ["Coco Robotics"]
    assert status[tc.title] == "extracted"

    only_other = art("Legora raises $550M with robotics-free AI", "TechCrunch", 1)
    fake = FakeExtractor({only_other.title: Extraction(is_funding_announcement=True,
                                                       events=[ev("Legora", in_focus=False)])})
    status, counts, _, existing = run([only_other], fake)
    assert existing == [] and status[only_other.title] == "skipped_not_in_focus"


def test_system_instruction_carries_the_focus_definition():
    focus = {"name": "Robotics", "description": "Exclude defense.", "categories": ["Drones & Delivery"]}
    text = system_instruction(focus)
    assert "Focus sector: Robotics" in text and "Exclude defense." in text and '"Drones & Delivery"' in text
    assert "Focus sector" not in system_instruction(None)
