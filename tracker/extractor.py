"""3. Fundraise Extractor: ask an LLM (Claude or Gemini, set in config.yaml) for structured funding events."""

import logging
import re
import time

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
- valuation: the company's valuation stated for this round (post-money if both are given), same format
  as amount, e.g. "$8.8B". Empty if the article doesn't state one.
- date: announcement date as YYYY-MM-DD. Empty if the article doesn't say.
- lead_investor: the lead investor(s), comma-separated. Empty if not stated.
- other_investors: other participating investors.
- category: one short sector label, e.g. "AI Infrastructure", "Robotics", "Fintech", "Healthcare",
  "Biotech", "Defense", "Climate", "Developer Tools", "Security", "Consumer", "Enterprise Software".
- in_focus: true for every event when no focus sector is given.
Never guess: leave a field empty rather than invent it.
"""


def system_instruction(focus: dict | None) -> str:
    """The base instruction plus, when config has a focus sector, how to judge in_focus and category."""
    if not focus:
        return SYSTEM_INSTRUCTION
    categories = ", ".join(f'"{c}"' for c in focus.get("categories", []))
    return SYSTEM_INSTRUCTION + f"""
Focus sector: {focus["name"]}. {focus["description"]}
Still report every funding event in the article, but set in_focus to true only when the company fits
the focus sector, judged by what the company builds, not by the headline's wording. For in-focus
events, category must be one of: {categories}. Some articles give only a headline: extract what it
states and leave the rest empty.
"""


class FundingEvent(BaseModel):
    company: str
    round: str
    amount: str
    valuation: str
    date: str
    lead_investor: str
    other_investors: list[str]
    category: str
    in_focus: bool


class Extraction(BaseModel):
    is_funding_announcement: bool
    events: list[FundingEvent]


class QuotaExhausted(Exception):
    """The provider won't accept more requests for a long time (daily quota hit, credits used up)."""


def _prompt(article: Article, text: str) -> str:
    published = article.published.date().isoformat() if article.published else "unknown"
    return f"Source: {article.source}\nPublished: {published}\nURL: {article.url}\n\n{text}"


class ClaudeExtractor:
    def __init__(self, api_key: str, settings: dict, system: str = SYSTEM_INSTRUCTION):
        import anthropic

        self._system = system

        self._anthropic = anthropic
        # The SDK itself retries 429/5xx/connection errors with backoff.
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=settings["max_retries"])
        self._model = settings["model"]
        self._effort = settings["effort"]

    def extract(self, article: Article, text: str) -> Extraction:
        try:
            resp = self._client.messages.parse(
                model=self._model,
                max_tokens=4096,
                system=self._system,
                messages=[{"role": "user", "content": _prompt(article, text)}],
                output_format=Extraction,
                output_config={"effort": self._effort},
            )
        except self._anthropic.BadRequestError as e:
            message = str(e).lower()
            if "credit balance" in message:
                raise QuotaExhausted("Anthropic credit balance is too low; top up at platform.claude.com") from e
            if "usage limit" in message:
                raise QuotaExhausted(f"Anthropic spend limit reached; raise it in the Console Limits page ({e})") from e
            raise
        if resp.stop_reason == "refusal":
            raise RuntimeError(f"Claude declined this article ({getattr(resp.stop_details, 'category', None)})")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("Response was cut off at max_tokens")
        return resp.parsed_output


def _retry_delay_seconds(error: Exception) -> float | None:
    """The server-suggested wait from a Gemini 429 response, if any."""
    m = re.search(r"retryDelay'?\"?:\s*'?\"?(\d+(?:\.\d+)?)s", str(error))
    return float(m.group(1)) if m else None


class GeminiExtractor:
    def __init__(self, api_key: str, settings: dict, system: str = SYSTEM_INSTRUCTION):
        from google import genai
        from google.genai import types

        self._system = system

        self._types = types
        self._client = genai.Client(api_key=api_key)
        # Tried in order. Free-tier quotas are per model, and one model can be overloaded while another isn't.
        self._models = list(settings.get("models") or [settings["model"]])
        self._min_interval = settings["min_seconds_between_calls"]
        self._max_retries = settings["max_retries"]
        self._last_call = 0.0

    def extract(self, article: Article, text: str) -> Extraction:
        last_error: Exception | None = None
        for model in list(self._models):
            try:
                return self._extract_with(model, article, text)
            except QuotaExhausted as e:
                # Out for the rest of the run; later articles go straight to the next model.
                self._models.remove(model)
                log.warning("%s; trying the next Gemini model", e)
                last_error = e
            except Exception as e:
                if getattr(e, "code", None) not in (500, 503):
                    raise
                # Overloaded right now: try the next model for this article, but keep this one for later.
                log.warning("%s is unavailable (%s); trying the next Gemini model", model, getattr(e, "code", None))
                last_error = e
        if not self._models:
            raise QuotaExhausted("Every Gemini model's quota is exhausted") from last_error
        raise last_error or QuotaExhausted("No Gemini models configured")

    def _extract_with(self, model: str, article: Article, text: str) -> Extraction:
        config = self._types.GenerateContentConfig(
            system_instruction=self._system,
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
                resp = self._client.models.generate_content(
                    model=model, contents=_prompt(article, text), config=config)
                return resp.parsed if isinstance(resp.parsed, Extraction) else Extraction.model_validate_json(resp.text)
            except Exception as e:
                delay = _retry_delay_seconds(e) if getattr(e, "code", None) == 429 else None
                if delay is not None and delay > 120:
                    raise QuotaExhausted(f"Gemini quota exhausted for {model}; resets in ~{delay / 3600:.1f}h") from e
                if attempt == self._max_retries:
                    raise
                backoff = delay or 10 * attempt
                log.warning("Gemini call failed (%s); retrying in %ds", e, backoff)
                time.sleep(backoff)
        raise AssertionError("unreachable")


PROVIDERS = {
    "claude": (ClaudeExtractor, "ANTHROPIC_API_KEY"),
    "gemini": (GeminiExtractor, "GEMINI_API_KEY"),
}


class FallbackExtractor:
    """Uses the first provider until its quota runs out, then moves to the next one for the rest of the run."""

    def __init__(self, providers: list[tuple[str, object]]):
        self._providers = providers

    def extract(self, article: Article, text: str) -> Extraction:
        while True:
            name, extractor = self._providers[0]
            try:
                return extractor.extract(article, text)
            except QuotaExhausted as e:
                if len(self._providers) == 1:
                    raise
                self._providers.pop(0)
                log.warning("%s. Switching to %s for the rest of this run.", e, self._providers[0][0])


def make_extractor(config: dict, env: dict) -> FallbackExtractor:
    llm = config["llm"]
    primary = llm["provider"]
    system = system_instruction(config.get("focus"))
    chain = []
    for name in [primary, *llm.get("fallbacks", [])]:
        cls, key_var = PROVIDERS[name]
        if not env.get(key_var):
            if name == primary:
                raise SystemExit(f"{key_var} is not set (needed for llm.provider: {name})")
            log.warning("%s is not set, so the %s fallback is unavailable", key_var, name)
            continue
        chain.append((name, cls(env[key_var], llm[name], system)))
    return FallbackExtractor(chain)
