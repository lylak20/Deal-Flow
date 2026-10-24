"""The only Claude calls in Sourcing: the Weekly Partner Brief and (when turned on) Founding Team lookups.

Both default to Claude Haiku 5.5. Facts must come from the data passed in or from pages Claude fetched:
brief lines with numbers that aren't in the input get [확인 필요], and founders whose names don't appear on
a fetched page are dropped.
"""

import json
import logging
import re
from dataclasses import dataclass, field

from pydantic import BaseModel

from .scoring import Opportunity

log = logging.getLogger(__name__)

MARK = "[확인 필요]"
MAX_CONTINUATIONS = 4
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

BRIEF_SYSTEM = """\
You write the Weekly Partner Brief for the investment team at LG Tech Ventures, a corporate VC.
Use only the opportunity data you are given: no outside facts, no new numbers. Do not recommend investing;
recommend only the next action given in the data. Write in Korean, keeping company, project and person
names in their original form."""

TEAM_SYSTEM = """\
You are the Founding Team Tracker of a venture capital team. Find who founded a startup, using only pages you
read with web_fetch (start with the company's own site: about, team, careers pages). Do not answer from memory.
For each founder, quote the sentence that names them, copied exactly, with the page URL. If you can't find
the founders, say so."""


def _numbers(text: str) -> set[float]:
    return {float(m.replace(",", "")) for m in _NUMBER.findall(text)}


def flag_new_numbers(text: str, source: str) -> str:
    pool = _numbers(source)
    out = []
    for line in text.splitlines():
        body = re.sub(r"^\s*([-*•]|\d{1,2}[.)])\s*", "", line)
        heading = line.lstrip().startswith("#")
        out.append(line if heading or _numbers(body) <= pool else f"{line} {MARK}")
    return "\n".join(out)


def brief_input(opps: list[Opportunity], n: int = 5) -> str:
    return json.dumps([{
        "opportunity": o.name, "type": o.kind, "theme": o.theme, "total_score": o.total, "tier": o.tier,
        "key_signals": o.signals[:5], "recommended_action": o.action, "owner": o.owner, "link": o.url,
    } for o in opps[:n]], ensure_ascii=False, indent=1)


def weekly_brief(client, model: str, opps: list[Opportunity]) -> str:
    data = brief_input(opps)
    resp = client.messages.create(
        model=model, max_tokens=4000, system=BRIEF_SYSTEM, output_config={"effort": "low"},
        messages=[{"role": "user", "content": (
            f"<opportunities>\n{data}\n</opportunities>\n\n"
            "Write this week's partner brief on these opportunities, most important first. For each: one line "
            "on why it matters, its key signals, and the recommended action. End with one line on the themes "
            "they share. Plain text with short bullet lines, at most 25 lines.")}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to write the brief")
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    return flag_new_numbers(text, data)


class Founder(BaseModel):
    name: str
    role: str
    technical: bool  # engineering / research background
    previous_startup: bool  # founded or was early at a startup before
    research_background: bool  # PhD, academic research, or a research lab
    quote: str  # sentence naming this founder, copied from a fetched page
    source_url: str


class TeamFindings(BaseModel):
    founders: list[Founder]


@dataclass
class Team:
    founders: list[Founder] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)  # names not found on any fetched page


def _squash(text: str) -> str:
    return re.sub(r"[\W_]+", "", text).lower()


def founding_team(client, model: str, name: str, website: str, description: str) -> Team:
    user = {"role": "user", "content": (
        f"Startup: {name}\nWebsite: {website or 'unknown'}\nWhat it does: {description or 'unknown'}\n\n"
        "Who are its founders? For each, note their role, whether they have a technical or research "
        "background, and whether they founded a startup before, with quotes and URLs.")}
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3},
             {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 4, "max_content_tokens": 6000}]
    blocks: list = []
    for _ in range(MAX_CONTINUATIONS):
        messages = [user, {"role": "assistant", "content": blocks}] if blocks else [user]
        resp = client.messages.create(model=model, max_tokens=8000, system=TEAM_SYSTEM, tools=tools,
                                      messages=messages, output_config={"effort": "low"})
        blocks += resp.content
        if resp.stop_reason != "pause_turn":
            break
    pages = []
    for b in blocks:
        result = getattr(b, "content", None) if getattr(b, "type", "") == "web_fetch_tool_result" else None
        source = getattr(getattr(result, "content", None), "source", None)
        if getattr(result, "type", "") == "web_fetch_result" and getattr(source, "type", "") == "text":
            pages.append(source.data)
    notes = "".join(b.text for b in blocks if getattr(b, "type", "") == "text")
    parsed = client.messages.parse(
        model=model, max_tokens=4000, output_format=TeamFindings, output_config={"effort": "low"},
        messages=[{"role": "user", "content": (
            f"<notes>\n{notes}\n</notes>\n\nList the founders of {name} that these notes support, with the exact "
            "quote naming each one and its URL. Return an empty list if the notes don't name any.")}],
    ).parsed_output
    corpus = _squash("\n".join(pages))
    team = Team()
    for f in parsed.founders:
        ok = _squash(f.name) in corpus and _squash(f.quote) in corpus
        (team.founders.append(f) if ok else team.dropped.append(f.name))
    return team


def team_points(team: Team) -> list[tuple[float, str]]:
    """Team score contributions (0-20 total with other team signals) from verified founders."""
    f = team.founders
    if not f:
        return []
    out = [(4, f"Founders found: {', '.join(x.name for x in f)}")]
    if any(x.technical for x in f):
        out.append((4, "Technical founder"))
    if any(x.previous_startup for x in f):
        out.append((4, "Founder has started a company before"))
    if any(x.research_background for x in f):
        out.append((4, "Founder with research background"))
    if len(f) >= 2 and any(x.technical for x in f) and not all(x.technical for x in f):
        out.append((4, "Complete team (technical + business founders)"))
    return out
