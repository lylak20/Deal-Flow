"""Claude calls: web research with cited sources, then structured extraction from those sources."""

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from deck import Deck

# Haiku keeps API spend low; the team cares about cost more than the last bit of quality.
MODEL = os.environ.get("ANALYST_MODEL", "claude-haiku-5-5")
MAX_TOKENS = 64000
# Claude Haiku 5.5 list prices (USD per million tokens) and web search ($ per search), for the cost estimate.
PRICE_IN, PRICE_OUT, PRICE_CACHE_READ, PRICE_CACHE_WRITE, PRICE_SEARCH = 0.1, 0.5, 0.01, 0.125, 0.01
# Server tools pause after 10 iterations; each continuation resumes the same turn.
MAX_CONTINUATIONS = 6
# Caps what one fetched page adds to the context; company pages rarely need more.
MAX_PAGE_TOKENS = 6000

PROMPTS = Path(__file__).parent / "prompts"

SYSTEM = """\
You support the investment team at LG Tech Ventures, a corporate venture capital firm, in analysing
startups. Each request gives you an analyst role and a task.

Rules:
- Use only facts you can verify in the pitch deck (when given) or in pages you find with web_search and
  read with web_fetch. Do not answer from memory.
- Start from the company's own website, then look for independent sources (press releases, news,
  funding announcements, Crunchbase or LinkedIn pages when reachable).
- When you can't find something, say so. A gap is always better than a guess.
- Do not give investment opinions.
- In your notes, back every fact with the exact sentence it comes from, copied character for character
  in double quotes, followed by the page URL (or the pitch deck's file name). Quote only pages you read
  with web_fetch or the pitch deck: search result snippets can't be checked, so fetch the page first."""


def prompt(name: str) -> str:
    return (PROMPTS / f"{name}.txt").read_text(encoding="utf-8")


def web_tools(max_uses: int) -> list[dict]:
    return [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses},
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": max_uses, "citations": {"enabled": True},
         "max_content_tokens": MAX_PAGE_TOKENS},
    ]


@dataclass
class Passage:
    text: str
    source: str  # URL, or the pitch deck's file name


@dataclass
class Findings:
    notes: str  # Claude's research notes: quoted sentences with their URLs, and [n] markers for cited passages
    passages: list[Passage]

    def render(self) -> str:
        quoted = "\n".join(f'[{n}] {p.source}\n"{p.text}"' for n, p in enumerate(self.passages, start=1))
        return f"<research_notes>\n{self.notes}\n</research_notes>\n\n<source_passages>\n{quoted}\n</source_passages>"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    searches: int = 0
    calls: int = 0

    @property
    def cost(self) -> float:
        return (self.input_tokens * PRICE_IN + self.output_tokens * PRICE_OUT + self.cache_read * PRICE_CACHE_READ
                + self.cache_write * PRICE_CACHE_WRITE) / 1e6 + self.searches * PRICE_SEARCH


class Refused(RuntimeError):
    pass


@dataclass
class Analyst:
    """One analysis session: the pitch deck, everything read so far, and the running token usage."""
    url: str
    deck: Deck | None = None
    client: object = None
    usage: Usage = field(default_factory=Usage)
    passages: list[Passage] = field(default_factory=list)  # every quote read in this session
    pages: list[str] = field(default_factory=list)  # full text of pages fetched in this session

    def __post_init__(self):
        if self.client is None:
            import anthropic

            self.client = anthropic.Anthropic(max_retries=4, timeout=900)
        self._lock = threading.Lock()

    @property
    def corpus(self) -> str:
        with self._lock:
            return "\n".join([self.deck.text if self.deck else "", *self.pages, *(p.text for p in self.passages)])

    @property
    def quotes_checkable(self) -> bool:
        return self.deck is None or self.deck.text_is_reliable

    def _stream(self, **kwargs):
        # Streamed so the large output limit doesn't run into HTTP timeouts.
        with self.client.messages.stream(model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM, **kwargs) as stream:
            resp = stream.get_final_message()
        self._count(resp.usage)
        if resp.stop_reason == "refusal":
            raise Refused("Claude가 이 요청을 거절했습니다.")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("응답이 너무 길어 잘렸습니다.")
        return resp

    def _count(self, usage) -> None:
        if usage is None:
            return
        with self._lock:
            u = self.usage
            u.calls += 1
            u.input_tokens += getattr(usage, "input_tokens", 0) or 0
            u.output_tokens += getattr(usage, "output_tokens", 0) or 0
            u.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
            u.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
            u.searches += getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0

    def research(self, task: str, max_searches: int) -> Findings:
        """Let Claude search and read the web, and keep every passage it cites."""
        first = []
        if self.deck:
            # Identical across steps, so the deck is cached once and reused.
            first.append({**self.deck.block(), "cache_control": {"type": "ephemeral"}})
        first.append({"type": "text", "text": task})
        user = {"role": "user", "content": first}
        blocks: list = []
        for _ in range(MAX_CONTINUATIONS):
            messages = [user, {"role": "assistant", "content": blocks}] if blocks else [user]
            resp = self._stream(messages=messages, tools=web_tools(max_searches), output_config={"effort": "medium"})
            blocks += resp.content
            if resp.stop_reason != "pause_turn":
                break
        return self._findings(blocks)

    def _findings(self, blocks) -> Findings:
        fetched_titles: dict[str, str] = {}  # page title -> URL, to label citations of fetched pages
        pages = []
        for b in blocks:
            if getattr(b, "type", "") != "web_fetch_tool_result":
                continue
            result = b.content
            if getattr(result, "type", "") != "web_fetch_result":
                continue  # an error object, e.g. the page couldn't be fetched
            doc = getattr(result, "content", None)
            source = getattr(doc, "source", None)
            if getattr(source, "type", "") == "text" and source.data:
                pages.append(source.data)
            if getattr(doc, "title", None):
                fetched_titles[doc.title] = result.url

        notes, passages, index = [], [], {}
        for b in blocks:
            if getattr(b, "type", "") != "text":
                continue
            notes.append(b.text)
            for c in getattr(b, "citations", None) or []:
                text = (getattr(c, "cited_text", "") or "").strip()
                if not text:
                    continue
                title = getattr(c, "document_title", None) or getattr(c, "title", None) or ""
                source = getattr(c, "url", None) or fetched_titles.get(title) or title or "unknown"
                key = (text, source)
                if key not in index:
                    passages.append(Passage(text, source))
                    index[key] = len(passages)
                notes.append(f" [{index[key]}]")
        with self._lock:
            self.pages += pages
            self.passages += passages
        return Findings("".join(notes).strip(), passages)

    def extract(self, material: str, instructions: str, schema):
        """Turn research notes into the step's JSON. No tools: only what the material says."""
        resp = self._stream(
            messages=[{"role": "user", "content": [{"type": "text", "text": f"{material}\n\n{instructions}"}]}],
            output_format=schema,
            output_config={"effort": "medium"},
        )
        return resp.parsed_output
