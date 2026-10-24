"""Fact-checking: every value Claude writes must rest on quotes that really appear in the sources.

The number and quote checks are the ones ic-builder/fill.py uses for IC decks.
"""

import re
from dataclasses import dataclass, field

from pydantic import BaseModel

MARK = "[확인 필요]"
NOT_FOUND = "Not Found"

# Statuses shown in the app.
VERIFIED = "확인됨"
UNVERIFIED = "확인 필요"
MISSING = "Not Found"
EDITED = "직접 수정"


class Fact(BaseModel):
    value: str  # "Not Found" when the sources don't say; lists are separated with "; "
    evidence: list[str]  # short verbatim quotes from the source passages
    sources: list[str]  # URLs (or the pitch deck's file name) the quotes come from


@dataclass
class Checked:
    value: str
    status: str
    evidence: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    claimed: str = ""  # what Claude wrote, when the check replaced it with MARK


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_ENUMERATOR = re.compile(r"^\s*\(?\d{1,2}[.)]\s+")
# A source's "500,000,000" may be written "500M", "5억" etc.
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


def is_not_found(value: str) -> bool:
    return not value.strip() or _squash(value) in {"notfound", "na", "unknown", "none"}


def named(value: str, evidence: list[str]) -> bool:
    """Every "; "-separated name in value appears in the quotes."""
    quoted = _squash(" ".join(evidence))
    items = [_squash(item) for item in value.split(";") if _squash(item)]
    return bool(items) and all(item in quoted for item in items)


class Checker:
    def __init__(self, corpus: str, check_quotes: bool = True):
        self._corpus = _squash(corpus)
        # Quotes from a scanned PDF can't be found in extracted text, so the caller can turn the quote check off.
        self._check_quotes = check_quotes

    def quotes_ok(self, evidence: list[str]) -> bool:
        quotes = [q for q in evidence if _squash(q)]
        if not quotes:
            return False
        return not self._check_quotes or all(_squash(q) in self._corpus for q in quotes)

    def fact(self, f: Fact, names: bool = False) -> Checked:
        """names: the value lists names (people, companies) that must each appear in the quotes."""
        if is_not_found(f.value):
            return Checked(NOT_FOUND, MISSING)
        ok = self.quotes_ok(f.evidence)
        if ok:
            pool = set().union(*(_numbers(q) for q in f.evidence))
            ok = all(_supported(n, pool) for n in _numbers(f.value))
        if ok and names:
            ok = named(f.value, f.evidence)
        if ok:
            return Checked(f.value.strip(), VERIFIED, f.evidence, f.sources)
        return Checked(MARK, UNVERIFIED, f.evidence, f.sources, claimed=f.value.strip())


def flag_unsupported_numbers(markdown: str, facts_text: str) -> tuple[str, int]:
    """Append MARK to memo lines whose numbers don't appear in the facts the memo was written from."""
    pool = _numbers(facts_text)
    out, flagged = [], 0
    for line in markdown.splitlines():
        body = _ENUMERATOR.sub("", line.lstrip("#>-* ").strip())
        if line.lstrip().startswith("#") or MARK in line or not body:
            out.append(line)
            continue
        if all(_supported(n, pool) for n in _numbers(body)):
            out.append(line)
        else:
            out.append(f"{line} {MARK}")
            flagged += 1
    return "\n".join(out), flagged
