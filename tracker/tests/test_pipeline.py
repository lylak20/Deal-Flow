import re
from datetime import datetime, timezone

from collector import Article, is_candidate, parse_feed
from extractor import Extraction, FundingEvent
from main import event_rows
from normalize import event_key

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
    base = dict(company="Eka Robotics", round="Series A", amount="$150 million", date="2026-10-20",
                lead_investor="a16z", other_investors=["F-Prime", " "], category="Robotics")
    return FundingEvent(**{**base, **kw})


def test_event_rows_normalizes_and_dedups():
    article = Article("https://techcrunch.com/eka", "TechCrunch", "t", "", datetime(2026, 10, 21, tzinfo=timezone.utc))
    existing = [event_key("Blacksmith", "Series C", "2026-10-20")]
    extraction = Extraction(is_funding_announcement=True, events=[
        _event(),
        _event(company="EKA", date="2026-10-21"),           # same round reported again -> skipped
        _event(company="Nova", round="Series F", amount="", date=""),  # no date -> article date
    ])
    rows, skipped = event_rows(article, extraction, existing, 7, "2026-10-22")
    assert skipped == 1
    assert rows[0] == ["2026-10-20", "Eka Robotics", "Series A", "$150M", "a16z", "F-Prime", "Robotics",
                       "TechCrunch", "https://techcrunch.com/eka", "2026-10-22"]
    assert rows[1][:4] == ["2026-10-21", "Nova", "Series E+", "Undisclosed"]
    # Accepted events are remembered for later articles in the same run.
    assert len(existing) == 3
