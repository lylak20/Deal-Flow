"""3-4. Fill: ask Claude to rewrite each slide's slots from the sources, then fact-check its numbers and quotes."""

import base64
import io
import json
import re
import time
from typing import Literal

import anthropic
import httpx2
from pydantic import BaseModel

from sources import Source, content_blocks
from template import MARK, SlideInfo, Slot

MODEL = "claude-haiku-5-5"
MAX_TOKENS = 64000
STREAM_ATTEMPTS = 3
# Text boxes per request; a table always goes in a request of its own so its rows stay consistent.
SLOTS_PER_CALL = 20

SYSTEM = f"""\
You help the investment team at LG Tech Ventures prepare an Investment Committee (IC) deck for a
fund-of-funds commitment. They reuse the IC deck of a fund they invested in before as a template:
its structure, slide order and design stay; every fact about the old fund is replaced with facts
about the new fund taken from the source materials.

Rules:
- Facts come only from the source materials and the analyst's notes. Do not use outside knowledge,
  estimate, or derive new figures (sums, ratios, currency or unit conversions) the sources don't state.
- When the sources don't support something a slot needs, write {MARK} in place of that value or
  sentence. A marked gap is always better than a guess.
- Keep, word for word, text that isn't about the old fund: section titles, field labels such as
  "Fund Size", LG Tech Ventures' own name, page furniture, disclaimers.
- Nothing specific to the old fund may remain: its name, GP, people, portfolio companies, figures,
  dates, track record, terms, or LG Tech Ventures' views about it. Replace it or mark it {MARK}.
- Opinions and recommendations (investment thesis, risks, IC recommendation) count as facts: fill
  them only from the sources or the analyst's notes, otherwise mark them {MARK}.
- Match the template's language (keep Korean where it is Korean, English where English), tone,
  units, date format, bullet style and length, so the new text fits the same box.
- For every paragraph you write that contains facts, give evidence: short verbatim quotes copied
  exactly from the sources that contain those facts, including every number you write in the form
  the source writes it."""


class Paragraph(BaseModel):
    text: str
    evidence: list[str]


class SlotDecision(BaseModel):
    slot_id: str
    action: Literal["keep", "fill", "remove"]
    paragraphs: list[Paragraph]  # the slot's new paragraphs when action is "fill"


class VisualDecision(BaseModel):
    visual_id: str
    action: Literal["keep", "flag"]


class RowRef(BaseModel):
    table_id: str
    row: int


class SlideDecision(BaseModel):
    slots: list[SlotDecision]
    visuals: list[VisualDecision]
    delete_rows: list[RowRef]


class Truncated(Exception):
    """The answer hit MAX_TOKENS."""


class DeckAnalysis(BaseModel):
    old_fund_name: str
    new_fund_name: str
    old_fund_terms: list[str]
    last_slide_is_closing: bool


class NewSlide(BaseModel):
    topic: str
    why: str
    base_slide: int


class SlideProposals(BaseModel):
    slides: list[NewSlide]


# --- Calling Claude ---------------------------------------------------------------------------


class Filler:
    def __init__(self, sources: list[Source], deck_outline: str, client=None):
        if client is None:
            client = anthropic.Anthropic(max_retries=4, timeout=900)
        self._client = client
        # Identical for every call, so it's cached once and reused by each slide.
        self._prefix = [
            *_source_blocks(sources),
            {
                "type": "text",
                "text": f"<template_deck>\n{deck_outline}\n</template_deck>",
                "cache_control": {"type": "ephemeral"},
            },
        ]

    def _ask(self, extra: list[dict], schema):
        # Streamed so the large output limit doesn't run into HTTP timeouts. The SDK retries a request that
        # fails to start; a stream that breaks halfway (dropped connection, overload) is retried here.
        for attempt in range(STREAM_ATTEMPTS):
            try:
                with self._client.messages.stream(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=SYSTEM,
                    messages=[{"role": "user", "content": [*self._prefix, *extra]}],
                    output_format=schema,
                    output_config={"effort": "high"},
                ) as stream:
                    resp = stream.get_final_message()
                break
            except (anthropic.APIConnectionError, httpx2.TransportError, anthropic.InternalServerError):
                if attempt == STREAM_ATTEMPTS - 1:
                    raise
                time.sleep(5 * 3 ** attempt)
        if resp.stop_reason == "refusal":
            raise RuntimeError("Claude가 이 요청을 거절했습니다.")
        if resp.stop_reason == "max_tokens":
            raise Truncated()
        return resp.parsed_output

    def analyze(self) -> DeckAnalysis:
        try:
            return self._ask([{"type": "text", "text": (
                "Before the slides are rewritten: name the old fund the template deck is about and the new fund "
                "the sources are about. List old_fund_terms: proper nouns in the template that belong to the old "
                "fund and must not survive (fund and GP names, people, portfolio companies, fund-specific "
                "product or program names). Leave out LG Tech Ventures and generic words. "
                "Set last_slide_is_closing if the template's last slide is a closing page (\"End of Document\", "
                "\"Thank you\", contact page) rather than content."
            )}], DeckAnalysis)
        except Truncated:
            raise RuntimeError("덱 분석 응답이 너무 길어 잘렸습니다.") from None

    def propose_slides(self, filled_deck: str, candidates: list[int], limit: int) -> list[NewSlide]:
        """Topics the sources cover well that the rewritten deck has no place for."""
        try:
            proposals = self._ask([{"type": "text", "text": (
                f"<rewritten_deck>\n{filled_deck}\n</rewritten_deck>\n\n"
                "The template deck has been rewritten for the new fund (above). Its structure came from the old "
                "fund, so the sources may hold important information for an IC decision that the deck has no "
                f"place for. Propose up to {limit} new slides for such topics, most important first, e.g. fund "
                "strategy, terms, pipeline, team, track record details or risks that the deck doesn't already "
                "cover. Propose only topics the sources support with enough concrete facts for a full slide; "
                "propose none if nothing qualifies. Don't repeat what the rewritten deck already says.\n"
                f"For each, pick base_slide: the template slide whose layout suits it best (one of {candidates}); "
                "the new slide copies that slide's design. topic: one line describing the slide's content; "
                "why: one line on why the IC needs it."
            )}], SlideProposals)
        except Truncated:
            raise RuntimeError("새 슬라이드 제안 응답이 너무 길어 잘렸습니다.") from None
        allowed = set(candidates)
        return [p for p in proposals.slides if p.base_slide in allowed][:limit]

    def fill_slide(self, slide: SlideInfo, total: int, topic: str | None = None) -> SlideDecision:
        """Fill a slide in batches of whole shapes, so a table is always seen in one piece.

        A batch that still runs too long is split in half and retried.
        """
        batches, current = [], []
        for unit in _units(slide):
            if unit[0].kind == "table_cell":
                batches.append([unit])
                continue
            if current and sum(len(u) for u in current) + len(unit) > SLOTS_PER_CALL:
                batches.append(current)
                current = []
            current.append(unit)
        if current or not batches:
            batches.append(current)
        result = SlideDecision(slots=[], visuals=[], delete_rows=[])
        for n, batch in enumerate(batches):
            part = self._fill_batch(slide, total, batch, slide.visuals if n == 0 else [], topic)
            result.slots += part.slots
            result.visuals += part.visuals
            result.delete_rows += part.delete_rows
        return result

    def _fill_batch(self, slide: SlideInfo, total: int, units: list[list[Slot]], visuals: list,
                    topic: str | None) -> SlideDecision:
        try:
            return self._ask(self._slide_request(slide, total, units, visuals, topic), SlideDecision)
        except Truncated:
            if len(units) <= 1:
                raise RuntimeError(f"슬라이드 {slide.number}의 응답이 너무 길어 잘렸습니다.") from None
        half = len(units) // 2
        first = self._fill_batch(slide, total, units[:half], visuals, topic)
        second = self._fill_batch(slide, total, units[half:], [], topic)
        return SlideDecision(slots=first.slots + second.slots, visuals=first.visuals,
                             delete_rows=first.delete_rows + second.delete_rows)

    def _slide_request(self, slide: SlideInfo, total: int, units: list[list[Slot]], visuals: list,
                       topic: str | None) -> list[dict]:
        text_slots = [
            {"slot_id": s.id, "shape": s.shape_name, "paragraphs": s.paragraphs,
             "position": f"top {s.top_pct}%, left {s.left_pct}%, width {s.width_pct}%"}
            for unit in units for s in unit if s.kind == "text"
        ]
        table_ids = {unit[0].id.split("/")[0] for unit in units if unit[0].kind == "table_cell"}
        table_list = [
            {"table_id": t.id, "rows": [{"row": r, "cells": cells} for r, cells in enumerate(t.rows)]}
            for t in slide.tables if t.id in table_ids
        ]
        visual_list = [{"visual_id": v.id, "kind": v.kind, "description": v.description} for v in visuals]
        content: list[dict] = []
        for v in visuals:
            block = _image_block(v.image) if v.image else None
            if block:
                content += [{"type": "text", "text": f"Image of visual {v.id}:"}, block]
        partial = sum(len(u) for u in units) < len(slide.slots)
        if topic:
            intro = (
                f"This is a NEW slide, added to the end of the deck, about: {topic}\n"
                "It is a copy of a template slide, so all its text is still about the old fund. Rewrite every "
                "slot for this topic from the sources; section titles and headings change to fit the topic. "
                "Use \"remove\" for a text box the topic doesn't need. In tables, rewrite the header row for the "
                "topic too, and delete rows you have no content for. New headings need evidence too: quote "
                "the source words their subject comes from."
            )
        else:
            intro = f"Rewrite slide {slide.number} of {total} for the new fund."
        content.append({"type": "text", "text": (
            intro
            + (" This request covers only some of the slide's shapes; the rest are handled separately." if partial else "")
            + f"\n\nText slots:\n{json.dumps(text_slots, ensure_ascii=False, indent=1)}\n\n"
            "Tables (cells are 0-based; null marks a cell merged into its neighbour):\n"
            f"{json.dumps(table_list, ensure_ascii=False, indent=1)}\n\n"
            f"Visuals:\n{json.dumps(visual_list, ensure_ascii=False, indent=1)}\n\n"
            "Return decisions for the slots and visuals listed here.\n"
            "- Slot \"keep\": the text has nothing specific to the old fund (leave paragraphs empty).\n"
            "- Slot \"fill\": give the slot's new paragraphs in order. Aim for the template's paragraph count; "
            "fewer or more is fine when the sources call for it. A field label stays as written and only "
            f"its value changes, e.g. \"Fund Size: {MARK}\".\n"
            + ("- Slot \"remove\": delete this text box (new slides only).\n" if topic else
               "- Never use \"remove\" here.\n")
            + "- Table cells: slot_id is \"<table_id>/r<row>c<col>\". Each data row describes one entity (a fund, "
            "a company, a person): every value you put in a row must be about that row's entity, so never copy "
            "a value from another row. Header rows and group labels (e.g. \"Realized funds\") stay unless they "
            "name the old fund. Empty cells can be filled too.\n"
            "- delete_rows: when the new fund has fewer entities than the table has data rows, list the "
            "surplus rows here instead of marking each of their cells. Never delete a header row. When rows are "
            "grouped under a label (e.g. \"Realized funds\" / \"Live funds\"), put each entity in the group it "
            "belongs to, fill each group from its top, and delete the surplus rows within each group.\n"
            "- Evidence quotes: keep each one short, just the words that hold the fact.\n"
            "- Visual \"flag\": a picture or chart showing the old fund (its logo, team, data, portfolio) "
            "or anything you can't tell; \"keep\": generic (LG Tech Ventures logo, icons, decoration)."
        )})
        return content


def _units(slide: SlideInfo) -> list[list[Slot]]:
    """The slide's slots grouped by shape: a text box is one unit, a whole table is one unit."""
    units: dict[str, list[Slot]] = {}
    for s in slide.slots:
        units.setdefault(s.id.split("/")[0] if s.kind == "table_cell" else s.id, []).append(s)
    return list(units.values())


def _source_blocks(sources: list[Source]) -> list[dict]:
    return [{"type": "text", "text": "Source materials about the new fund:"}, *content_blocks(sources)]


def _image_block(image: tuple[str, bytes]) -> dict | None:
    """A downscaled PNG of a slide picture, or None for formats the API can't read (EMF, WMF, ...)."""
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(image[1]))
        img.thumbnail((800, 800))
        buf = io.BytesIO()
        img.convert("RGBA").save(buf, format="PNG")
    except (OSError, ValueError):
        return None
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.b64encode(buf.getvalue()).decode()}}


# --- Fact-checking ------------------------------------------------------------------------------

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_ENUMERATOR = re.compile(r"^\s*\(?\d{1,2}[.)]\s+")
# A source's "500,000,000" may be written "500M", "5억" etc. in the deck.
_SCALES = (1, 1e2, 1e3, 1e4, 1e6, 1e8, 1e9, 1e12)


def _numbers(text: str) -> set[float]:
    out = set()
    for m in _NUMBER.findall(text):
        try:
            out.add(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def _supported(n: float, pool: set[float]) -> bool:
    return any(abs(n * k - p) <= 1e-6 * max(1.0, p) for p in pool for k in _SCALES) or any(
        abs(n - p * k) <= 1e-6 * max(1.0, n) for p in pool for k in _SCALES[1:])


def _squash(text: str) -> str:
    return re.sub(r"[\W_]+", "", text).lower()


def term_pattern(terms: list[str]) -> re.Pattern | None:
    """Whole-word, case-insensitive match for any of the old fund's names ("Stream" doesn't hit "Streamlined")."""
    terms = sorted({t.strip() for t in terms if len(t.strip()) >= 3}, key=len, reverse=True)
    if not terms:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in terms) + r")(?!\w)", re.IGNORECASE)


class Checker:
    def __init__(self, sources: list[Source], old_terms: list[str] = ()):
        self._corpus = _squash("\n".join(s.text for s in sources))
        # Quotes from a scanned PDF can't be found in extracted text, so only check when all text is there.
        self._check_quotes = bool(sources) and all(s.text_is_reliable for s in sources)
        self._old = term_pattern(list(old_terms))
        self._source_numbers = _numbers("\n".join(s.text for s in sources))

    def kept_ok(self, text: str) -> bool:
        """Text Claude chose to keep may not carry figures the sources don't have (the old fund's numbers)."""
        if re.fullmatch(r"\s*\d{1,2}\s*", text):
            return True  # a section badge or page number
        return all(_supported(n, self._source_numbers) for n in _numbers(_ENUMERATOR.sub("", text)))

    def check_kept(self, slot: Slot) -> tuple[list[str], int]:
        out, demoted = [], 0
        for text in slot.paragraphs:
            if self.kept_ok(text):
                out.append(text)
            else:
                out.append(_demote(text, slot))
                demoted += 1
        return out, demoted

    def scrub(self, texts: list[str]) -> list[str]:
        """Replace any old-fund name Claude left in place with the marker."""
        if self._old is None:
            return texts
        return [self._old.sub(MARK, t) for t in texts]

    def paragraph_ok(self, para: Paragraph, original: list[str]) -> bool:
        text = para.text.replace(MARK, " ")
        if para.text.strip() in original or not _squash(text):
            return True  # unchanged template text, or only a marker
        if MARK in para.text and _squash(text) in _squash(" ".join(original)):
            return True  # a template label with its value marked ("Fund Size: [확인 필요]")
        if not para.evidence:
            return False
        if self._check_quotes and not all(_squash(q) in self._corpus for q in para.evidence if _squash(q)):
            return False
        pool = set().union(*(_numbers(q) for q in para.evidence))
        return all(_supported(n, pool) for n in _numbers(_ENUMERATOR.sub("", text)))

    def check(self, slot: Slot, paragraphs: list[Paragraph]) -> tuple[list[str], int]:
        """The slot's final paragraph texts and how many were demoted to a marker."""
        out, demoted = [], 0
        for para in paragraphs:
            if self.paragraph_ok(para, slot.paragraphs):
                out.append(para.text.strip())
            else:
                out.append(_demote(para.text, slot))
                demoted += 1
        return out, demoted


def _demote(text: str, slot: Slot) -> str:
    """Keep a template label ("Fund Size:") and mark the rest."""
    m = re.match(r"^\s*([^:：]{1,40}[:：])", text)
    if m and m.group(1).strip() in slot.text:
        return f"{m.group(1).strip()} {MARK}"
    return MARK
