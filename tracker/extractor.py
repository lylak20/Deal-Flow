"""3. Fundraise Extractor: ask Gemini for structured funding events in an article."""

import logging
import time

from google import genai
from google.genai import types
from pydantic import BaseModel

from collector import Article

log = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = """\
You extract startup fundraising events from news articles for a venture capital database.

Report an event only when the article announces that a startup or private company raised a new
equity financing round (pre-seed, seed, Series A-Z, growth, late stage), including reports that it
"reportedly" raised or is raising one.

Do NOT report: acquisitions or mergers, partnerships, product launches, hires or personnel news,
IPOs or public-company offerings, debt-only financing, grants, revenue/ARR milestones, or a venture
firm closing its own fund. If the article has none, set is_funding_announcement to false and return
an empty events list.

A roundup article may describe several companies: return one event per company.

Field rules:
- company: the company's name as written, without "Inc." etc.
- round: e.g. "Pre-Seed", "Seed", "Series A", "Series B extension", "Growth". Empty if not stated.
- amount: e.g. "$150M", "$1.2B", "€20M". Keep the original currency. Empty if undisclosed.
- date: announcement date as YYYY-MM-DD. Empty if the article doesn't say.
- lead_investor: the lead investor(s), comma-separated. Empty if not stated.
- other_investors: other participating investors.
- category: one short sector label, e.g. "AI Infrastructure", "Robotics", "Fintech", "Healthcare",
  "Biotech", "Defense", "Climate", "Developer Tools", "Security", "Consumer", "Enterprise Software".
Never guess: leave a field empty rather than invent it.
"""


class FundingEvent(BaseModel):
    company: str
    round: str
    amount: str
    date: str
    lead_investor: str
    other_investors: list[str]
    category: str


class Extraction(BaseModel):
    is_funding_announcement: bool
    events: list[FundingEvent]


class Extractor:
    def __init__(self, api_key: str, config: dict):
        self._client = genai.Client(api_key=api_key)
        self._model = config["gemini"]["model"]
        self._min_interval = config["gemini"]["min_seconds_between_calls"]
        self._max_retries = config["gemini"]["max_retries"]
        self._last_call = 0.0

    def extract(self, article: Article, text: str) -> Extraction:
        published = article.published.date().isoformat() if article.published else "unknown"
        prompt = f"Source: {article.source}\nPublished: {published}\nURL: {article.url}\n\n{text}"
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=Extraction,
            temperature=0,
        )
        for attempt in range(1, self._max_retries + 1):
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            try:
                resp = self._client.models.generate_content(model=self._model, contents=prompt, config=config)
                return resp.parsed if isinstance(resp.parsed, Extraction) else Extraction.model_validate_json(resp.text)
            except Exception as e:
                if attempt == self._max_retries:
                    raise
                backoff = 10 * attempt
                log.warning("Gemini call failed (%s); retrying in %ds", e, backoff)
                time.sleep(backoff)
        raise AssertionError("unreachable")
