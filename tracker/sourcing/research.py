"""Research Scout (promising papers) and Lab Tracker (the labs behind them), from OpenAlex.

Each theme in sourcing.research.themes is searched twice: papers with an author at a tracked university, and papers with an author at a
company (an industry partnership signal). PI = the last author at a tracked university, by convention.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date

from .common import Http, compile_themes, months_ago, months_between, parse_day, themes_of

log = logging.getLogger(__name__)

OPENALEX = "https://api.openalex.org/works"
# Surveys, reviews and clinical guidelines collect citations quickly but aren't new research a company
# could be built on.
_NOT_RESEARCH = re.compile(r"\b(survey|review|overview|perspectives?|tutorial|guidelines?|recommendations?|"
                           r"consensus|executive summary|position statement)\b", re.IGNORECASE)
# Every theme is about computing, so a paper must also say so (keeps out e.g. drug trials matching "clinical").
_COMPUTING = re.compile(r"\b(machine learning|deep learning|artificial intelligence|AI|neural|language models?|"
                        r"LLMs?|transformers?|foundation models?|algorithms?|robots?|robotic|computer vision|"
                        r"reinforcement learning|semiconductors?|chips?|photonic|software|computing)\b", re.IGNORECASE)
_SELECT = ("id,doi,title,publication_date,cited_by_count,citation_normalized_percentile,fwci,"
           "primary_location,authorships,funders,abstract_inverted_index")


@dataclass
class Author:
    name: str
    id: str
    position: str  # "first" | "middle" | "last"
    institutions: list[tuple[str, str, str]]  # (OpenAlex id, name, type)


@dataclass
class Paper:
    id: str
    title: str
    url: str
    venue: str
    published: date | None
    themes: list[str]
    cited_by: int
    percentile: float | None  # citations vs. papers of the same field and year, 0-1
    authors: list[Author]
    funders: list[str]
    tracked: list[str] = field(default_factory=list)  # tracked universities among the authors
    companies: list[str] = field(default_factory=list)  # company affiliations among the authors
    flags: list[str] = field(default_factory=list)

    @property
    def velocity(self) -> float:
        """Citations per month since publication."""
        return self.cited_by / months_between(self.published, date.today()) if self.published else 0.0


@dataclass
class Lab:
    pi: str
    pi_id: str
    institution: str
    papers: list[Paper] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.pi} lab ({self.institution})"

    @property
    def themes(self) -> list[str]:
        return sorted({t for p in self.papers for t in p.themes})

    @property
    def citations(self) -> int:
        return sum(p.cited_by for p in self.papers)

    @property
    def best_percentile(self) -> float:
        return max((p.percentile or 0.0) for p in self.papers)

    @property
    def industry_partners(self) -> list[str]:
        return sorted({c for p in self.papers for c in p.companies})

    @property
    def funders(self) -> list[str]:
        return sorted({f for p in self.papers for f in p.funders})


def _params(filters: list[str], per_page: int) -> dict:
    params = {"filter": ",".join(filters), "sort": "cited_by_count:desc", "per_page": per_page, "select": _SELECT}
    if os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = os.environ["OPENALEX_API_KEY"]
    return params


def abstract(inverted: dict | None) -> str:
    """OpenAlex stores abstracts as {word: [positions]}."""
    if not inverted:
        return ""
    words = sorted((pos, word) for word, positions in inverted.items() for pos in positions)
    return " ".join(word for _, word in words)


def _paper(w: dict, themes: list[str], tracked_ids: dict[str, str]) -> Paper:
    authors = [Author(
        name=a["author"]["display_name"], id=a["author"]["id"], position=a.get("author_position", ""),
        institutions=[(i["id"].rsplit("/", 1)[-1], i["display_name"], i.get("type") or "") for i in a["institutions"]],
    ) for a in w.get("authorships", [])]
    inst = [i for a in authors for i in a.institutions]
    pct = (w.get("citation_normalized_percentile") or {}).get("value")
    return Paper(
        id=w["id"].rsplit("/", 1)[-1], title=(w.get("title") or "").strip(), url=w.get("doi") or w["id"],
        venue=((w.get("primary_location") or {}).get("source") or {}).get("display_name") or "",
        published=parse_day(w.get("publication_date")), themes=list(themes), cited_by=w.get("cited_by_count", 0),
        percentile=pct, authors=authors, funders=[f["display_name"] for f in w.get("funders") or []],
        tracked=sorted({tracked_ids[i] for i, _, _ in inst if i in tracked_ids}),
        companies=sorted({name for _, name, kind in inst if kind == "company"}),
    )


def papers(http: Http, config: dict, themes: dict, today: date) -> list[Paper]:
    rc = config["research"]
    tracked_ids = {v: k for k, v in rc["institutions"].items()}
    since = months_ago(today, rc["lookback_months"]).isoformat()
    # Only the themes the research search is set to cover (all when unset); papers are tagged with these only.
    themes = {t: spec for t, spec in themes.items() if t in rc.get("themes", themes)}
    patterns = compile_themes(themes)
    found: dict[str, Paper] = {}
    for theme, spec in themes.items():
        terms = "|".join(k.replace(",", " ") for k in spec["keywords"])
        base = [f"title_and_abstract.search:{terms}", f"from_publication_date:{since}", "type:article|preprint"]
        if rc.get("fields"):
            base.append("primary_topic.field.id:" + "|".join(str(f) for f in rc["fields"]))
        for scope in (f"authorships.institutions.lineage:{'|'.join(tracked_ids)}",
                      "authorships.institutions.type:company"):
            data = http.get_json(OPENALEX, params=_params(base + [scope], rc["papers_per_theme"]))
            for w in data.get("results", []):
                if not w.get("title") or _NOT_RESEARCH.search(w["title"]):
                    continue
                # The search matches loosely, so a theme counts only when its keywords are really there.
                text = f"{w['title']} {abstract(w.get('abstract_inverted_index'))}"
                matched = themes_of(text, patterns)
                if matched and _COMPUTING.search(text):
                    found.setdefault(w["id"].rsplit("/", 1)[-1], _paper(w, matched, tracked_ids))
    out = list(found.values())
    _flag_papers(out)
    return out


def _flag_papers(papers: list[Paper]) -> None:
    """The doc flags a research opportunity that meets 2 or more conditions."""
    velocities = sorted(p.velocity for p in papers)
    top_quartile = velocities[int(len(velocities) * 0.75)] if velocities else 0
    for p in papers:
        checks = [
            ((p.percentile or 0) >= 0.9, "High academic impact (top 10% citations in its field)"),
            (p.velocity > 0 and p.velocity >= top_quartile, "Fast citation velocity (top 25% scanned)"),
            (bool(p.companies), "Industry partnership: " + ", ".join(p.companies[:3])),
            (bool(p.funders), "Grant funding: " + ", ".join(p.funders[:2])),
        ]
        p.flags = [label for ok, label in checks if ok]


def labs(papers: list[Paper], config: dict) -> list[Lab]:
    """Group papers by their PI: the last author who is at a tracked university."""
    tracked_ids = {v: k for k, v in config["research"]["institutions"].items()}
    found: dict[tuple[str, str], Lab] = {}
    for p in papers:
        last = next((a for a in reversed(p.authors) if any(i in tracked_ids for i, _, _ in a.institutions)), None)
        if last is None or last.position != "last":
            continue
        inst = next(tracked_ids[i] for i, _, _ in last.institutions if i in tracked_ids)
        lab = found.setdefault((last.id, inst), Lab(last.name, last.id, inst))
        lab.papers.append(p)
    out = list(found.values())
    for lab in out:
        checks = [
            (lab.best_percentile >= 0.9, "High academic strength (a top-10% paper)"),
            (bool(lab.industry_partners), "Industry partnership: " + ", ".join(lab.industry_partners[:3])),
            (bool(lab.funders), "Grant funding: " + ", ".join(lab.funders[:2])),
            (len(lab.papers) >= 2, f"{len(lab.papers)} papers in the scan"),
        ]
        lab.flags = [label for ok, label in checks if ok]
    return out
