import re
from datetime import datetime, timezone

from collector import Article, is_candidate, parse_feed
from extractor import Extraction, FundingEvent
from main import event_rows
from normalize import EventRecord, event_key

FEED = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test</title>
<item>
  <title>Eka raises $150M Series A led by a16z</title>
  <link>https://techcrunch.com/2026/10/20/eka/?utm_source=rss</link>
  <pubDate>Tue, 20 Oct 2026 14:00:00 +0000</pubDate>
  <description><![CDATA[<p>The robotics startup &amp;amp; its investors.</p>]]></description>
</item>
<item>
  <title>Acme launches a new app</title>
  <link>https://example.com/acme-launch</link>
  <pubDate>Tue, 20 Oct 2026 15:00:00 +0000</pubDate>
  <description>Product news.</description>
</item>
<item>
  <title>Old Co raises $5M seed round</title>
  <link>https://example.com/old</link>
  <pubDate>Mon, 01 Sep 2026 10:00:00 +0000</pubDate>
  <description>Old news.</description>
</item>
</channel></rss>"""

PATTERNS = [re.compile(p, re.I) for p in [r"\brais(e|es|ed|ing)\b", r"\bseed (round|funding)\b"]]


def test_parse_feed_and_filter():
    articles = parse_feed(FEED, "TechCrunch")
    assert [a.url for a in articles][0] == "https://techcrunch.com/2026/10/20/eka"
    assert articles[0].summary == "The robotics startup & its investors."
    since = datetime(2026, 10, 19, tzinfo=timezone.utc)
    kept = [a.title for a in articles if is_candidate(a, PATTERNS, since)]
    assert kept == ["Eka raises $150M Series A led by a16z"]


def _event(**kw):
    base = dict(company="Eka Robotics", round="Series A", amount="$150 million", valuation="", date="2026-10-20",
                lead_investor="a16z", other_investors=["F-Prime", " "], category="Robotics")
    return FundingEvent(**{**base, **kw})


def _sheet_row(row, company, round_, date, **values):
    base = {"Date": date, "Company": company, "Funding Round": round_, "Amount Raised": "Undisclosed",
            "Valuation": "", "Lead Investor": "", "Other Investors": "", "Category": "", "Source": "TechCrunch",
            "Article URL": f"https://example.com/{row}", "Date Added": date}
    return EventRecord(event_key(company, round_, date), {**base, **values}, row=row)


ARTICLE = Article("https://techcrunch.com/eka", "TechCrunch", "t", "", datetime(2026, 10, 21, tzinfo=timezone.utc))


def test_event_rows_normalizes_and_dedups():
    existing = [_sheet_row(2, "Blacksmith", "Series C", "2026-10-20")]
    extraction = Extraction(is_funding_announcement=True, events=[
        _event(valuation="$1.45 billion"),
        _event(company="EKA", date="2026-10-21", valuation="1.45B"),  # same round, nothing new -> skipped
        _event(company="Nova", round="Series F", amount="", date=""),  # no date -> article date
    ])
    added, updated, skipped = event_rows(ARTICLE, extraction, existing, 7, "2026-10-22")
    assert (len(added), len(updated), skipped) == (2, 0, 1)
    assert added[0].values == {
        "Date": "2026-10-20", "Company": "Eka Robotics", "Funding Round": "Series A", "Amount Raised": "$150M",
        "Valuation": "$1.45B", "Lead Investor": "a16z", "Other Investors": "F-Prime", "Category": "Robotics",
        "Source": "TechCrunch", "Article URL": "https://techcrunch.com/eka", "Date Added": "2026-10-22",
    }
    nova = added[1].values
    assert [nova[h] for h in ("Date", "Company", "Funding Round", "Amount Raised")] == \
        ["2026-10-21", "Nova", "Series E+", "Undisclosed"]
    # Accepted events are remembered for later articles in the same run, and none is in the sheet yet.
    assert len(existing) == 3 and [e.row for e in existing] == [2, None, None]


def test_later_report_of_the_same_round_replaces_the_row():
    # Yesterday: round unknown. Today: the same raise reported as a Seed round.
    existing = [_sheet_row(5, "Warhound", "Undisclosed", "2026-10-22", **{"Amount Raised": "$200M",
                                                                         "Category": "Defense"})]
    extraction = Extraction(is_funding_announcement=True, events=[
        _event(company="Warhound", round="Seed", amount="$200M", date="2026-10-23", lead_investor="",
               other_investors=["Anduril"], category=""),
    ])
    added, updated, skipped = event_rows(ARTICLE, extraction, existing, 365, "2026-10-23")
    assert (len(added), len(updated), skipped) == (0, 1, 0)
    rec = updated[0]
    assert rec.replaces == [5] and rec.row is None and existing == [rec]
    v = rec.values
    # Today's report wins; what it doesn't say is kept from yesterday's row.
    assert (v["Date"], v["Funding Round"], v["Amount Raised"], v["Other Investors"], v["Category"]) == \
        ("2026-10-23", "Seed", "$200M", "Anduril", "Defense")


def test_a_new_round_of_a_known_company_is_a_new_row():
    existing = [_sheet_row(5, "Warhound", "Seed", "2026-04-01", **{"Amount Raised": "$20M"})]
    extraction = Extraction(is_funding_announcement=True, events=[
        _event(company="Warhound", round="Series A", amount="$200M", date="2026-10-23"),
    ])
    added, updated, skipped = event_rows(ARTICLE, extraction, existing, 365, "2026-10-23")
    assert (len(added), len(updated), skipped) == (1, 0, 0)
    assert existing[0].row == 5 and added[0].replaces == []


def test_update_of_a_round_added_earlier_in_the_same_run_stays_one_row():
    existing = []
    extraction = Extraction(is_funding_announcement=True, events=[
        _event(company="Warhound", round="", amount="$200M", date="2026-10-23"),
        _event(company="Warhound", round="Seed", amount="$200M", date="2026-10-23"),
    ])
    added, updated, skipped = event_rows(ARTICLE, extraction, existing, 365, "2026-10-23")
    assert (len(added), len(updated)) == (1, 0)
    assert added[0].values["Funding Round"] == "Seed" and len(existing) == 1
