from datetime import date

import pytest

from normalize import (
    company_key, event_key, is_duplicate, normalize_amount, normalize_round, normalize_url,
)


@pytest.mark.parametrize("raw, expected", [
    ("Series A", "Series A"),
    ("series-b extension", "Series B"),
    ("Series A1", "Series A"),
    ("Series F", "Series E+"),
    ("Seed", "Seed"),
    ("Series Seed", "Seed"),
    ("pre-seed round", "Pre-Seed"),
    ("Preseed", "Pre-Seed"),
    ("growth equity", "Growth"),
    ("late-stage", "Late Stage"),
    ("", "Undisclosed"),
    (None, "Undisclosed"),
    ("strategic investment", "Undisclosed"),
])
def test_normalize_round(raw, expected):
    assert normalize_round(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("$150M", "$150M"),
    ("$150 million", "$150M"),
    ("$1.2 billion", "$1.2B"),
    ("USD 1,500 million", "$1.5B"),
    ("€20 million", "€20M"),
    ("EUR 3.5m", "€3.5M"),
    ("$500K", "$500K"),
    ("$2,500,000", "$2.5M"),
    ("undisclosed", "Undisclosed"),
    ("", "Undisclosed"),
    ("several million dollars", "several million dollars"),
])
def test_normalize_amount(raw, expected):
    assert normalize_amount(raw) == expected


def test_company_key_ignores_suffix_case_and_punctuation():
    assert company_key("Eka Robotics") == company_key("Eka") == company_key("EKA, Inc.")
    assert company_key("Instinct AI") == company_key("Instinct")
    # A name made only of suffix words still gets a key.
    assert company_key("AI Labs") == "ailabs"


def test_normalize_url():
    assert normalize_url("https://TechCrunch.com/2026/10/20/eka/?utm_source=x#top") == \
        "https://techcrunch.com/2026/10/20/eka"


def test_duplicate_rule_from_spec():
    existing = [event_key("Eka", "Series A", "2026-10-20")]
    # Same company, round and date -> skip.
    assert is_duplicate(event_key("Eka Robotics", "Series A", "2026-10-20"), existing, 7)
    # Same round reported a day later by another outlet -> skip (±7 day window).
    assert is_duplicate(event_key("Eka", "Series A", "2026-10-21"), existing, 7)
    # New round -> add.
    assert not is_duplicate(event_key("Eka", "Series B", "2027-04-10"), existing, 7)
    # Same round letter much later (e.g. a separate extension) -> add.
    assert not is_duplicate(event_key("Eka", "Series A", "2026-12-01"), existing, 7)
    # Different company -> add.
    assert not is_duplicate(event_key("Blacksmith", "Series A", "2026-10-20"), existing, 7)
    assert date(2026, 10, 20) == existing[0].date
