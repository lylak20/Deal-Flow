"""Company URL -> classification -> competitors -> investment memo.

The app calls each step separately so the analyst can confirm the classification and the competitor
list in between, as the Google Doc asks ("사용자가 승인한 이후에만 Competitor Analyst를 실행한다").
"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass

from pydantic import BaseModel

from research import Analyst, prompt
from verify import EDITED, MARK, MISSING, NOT_FOUND, UNVERIFIED, VERIFIED, Checked, Checker, Fact, \
    flag_unsupported_numbers, is_not_found, named

# (key, label shown in the app, value lists names that must each appear in the quotes)
CLASSIFICATION_FIELDS = [
    ("company_name", "회사명", True),
    ("website", "웹사이트", False),
    ("hq_location", "본사", False),
    ("founding_year", "설립연도", False),
    ("founders", "창업자", True),
    ("industry", "산업", False),
    ("product_categories", "제품 카테고리", False),
    ("primary_customer", "주요 고객", False),
    ("secondary_customer", "보조 고객", False),
    ("customer_types", "고객 유형", False),
    ("business_model", "비즈니스 모델", False),
    ("geography_focus", "지역", False),
    ("product_summary", "제품 요약", False),
]

PROFILE_FIELDS = [
    ("hq_location", "본사", False),
    ("founders", "창업자", True),
    ("current_stage", "단계", False),
    ("latest_funding_round", "최근 라운드", False),
    ("total_funding", "누적 투자", False),
    ("key_investors", "주요 투자자", True),
    ("major_customers", "주요 고객", True),
]

FACT_RULES = """\
Rules for every field:
- value: concise, in English. Separate list items with "; ". Write "Not Found" (with empty evidence and
  sources) when the source passages don't support a value. Never fill a value from memory.
- evidence: 1-3 short quotes that contain the fact, copied character for character from the quoted
  sentences in <research_notes> or from <source_passages> (never your own paraphrase), including every number and name in the value, in the form the source writes it.
- Write numbers and dates as the quotes write them (e.g. "March 2025", not "2025-03").
- sources: the URL or file name each quote comes from."""


class Classification(BaseModel):
    company_name: Fact
    website: Fact
    hq_location: Fact
    founding_year: Fact
    founders: Fact
    industry: Fact
    product_categories: Fact
    primary_customer: Fact
    secondary_customer: Fact
    customer_types: Fact
    business_model: Fact
    geography_focus: Fact
    product_summary: Fact


class Candidate(BaseModel):
    company_name: str
    website: str  # "Not Found" when unknown
    competitive_overlap: list[str]
    selection_reason: str
    evidence: list[str]  # quotes naming the competitor or showing the overlap
    sources: list[str]


class CandidateList(BaseModel):
    competitors: list[Candidate]


class CompetitorProfile(BaseModel):
    hq_location: Fact
    founders: Fact
    current_stage: Fact
    latest_funding_round: Fact
    total_funding: Fact
    key_investors: Fact
    major_customers: Fact


class Memo(BaseModel):
    executive_summary: str
    memo_markdown: str
    risks: list[str]
    investment_questions: list[str]


@dataclass
class MemoResult:
    executive_summary: str
    markdown: str
    risks: list[str]
    investment_questions: list[str]
    flagged: int  # memo lines whose numbers weren't in the confirmed facts


def _checker(analyst: Analyst) -> Checker:
    return Checker(analyst.corpus, check_quotes=analyst.quotes_checkable)


def _check_all(analyst: Analyst, parsed: BaseModel, fields) -> dict[str, Checked]:
    checker = _checker(analyst)
    return {key: checker.fact(getattr(parsed, key), names=names) for key, _, names in fields}


def facts_json(facts: dict[str, Checked]) -> dict[str, str]:
    return {key: c.value for key, c in facts.items()}


# --- Step 1: Company Classification --------------------------------------------------------------

def classify(analyst: Analyst) -> dict[str, Checked]:
    findings = analyst.research(
        f"<role>\n{prompt('company_classification')}\n</role>\n\n"
        f"Company URL: {analyst.url}\n\n"
        "Research this company: fetch the URL first, then search for other sources. "
        + ("Use the attached pitch deck first. " if analyst.deck else "")
        + "Write research notes covering every item in Step 1 and Step 2, and say which items you could not find.",
        max_searches=6,
    )
    parsed = analyst.extract(
        findings.render(),
        "Fill the company classification from the research above.\n" + FACT_RULES
        + "\nClassification fields (industry, product_categories, customers, business_model, geography_focus) "
          "are short labels; their evidence is the passage that describes what the company sells and to whom.",
        Classification,
    )
    facts = _check_all(analyst, parsed, CLASSIFICATION_FIELDS)
    facts["website"] = Checked(analyst.url, VERIFIED, sources=[analyst.url])  # the analyst's own input
    return facts


def apply_edits(facts: dict[str, Checked], edited: dict[str, str]) -> dict[str, Checked]:
    """Values the analyst typed over in the confirmation table count as confirmed."""
    out = dict(facts)
    for key, value in edited.items():
        value = value.strip()
        if key in out and value != out[key].value:
            out[key] = Checked(value or NOT_FOUND, EDITED if value and not is_not_found(value) else MISSING)
    return out


# --- Step 2: Competitor discovery ----------------------------------------------------------------

def find_competitors(analyst: Analyst, company: dict[str, Checked]) -> list[dict]:
    target = json.dumps(facts_json(company), ensure_ascii=False, indent=1)
    findings = analyst.research(
        f"<role>\n{prompt('competitor_analyst')}\n</role>\n\n"
        f"<company_classification>\n{target}\n</company_classification>\n\n"
        f"Company URL: {analyst.url}\n\n"
        "Do Step 1 and Step 2 only: find the target's direct competitors, in the priority order given. "
        "Profiles come later. For each competitor, note its website, where it overlaps with the target, why it "
        "is a direct competitor, and where you found it.",
        max_searches=8,
    )
    parsed = analyst.extract(
        findings.render(),
        "List the direct competitors found in the research above (at most 10, none invented to reach a number). "
        "competitive_overlap: short labels such as \"GPU Cloud\". selection_reason: one sentence in English. "
        "evidence: 1-3 quotes copied character for character from the quoted sentences in <research_notes> or "
        "from <source_passages> that name the competitor and show the overlap; sources: the URL of each quote. website: \"Not Found\" if the passages "
        "don't give it.",
        CandidateList,
    )
    checker = _checker(analyst)
    rows = []
    for c in parsed.competitors:
        ok = named(c.company_name, c.evidence) and checker.quotes_ok(c.evidence)
        rows.append({
            "include": bool(ok),
            "company_name": c.company_name.strip(),
            "website": "" if is_not_found(c.website) else c.website.strip(),
            "competitive_overlap": "; ".join(c.competitive_overlap),
            "selection_reason": c.selection_reason.strip(),
            "status": VERIFIED if ok else UNVERIFIED,
            "sources": " ".join(dict.fromkeys(c.sources)),
        })
    return rows


# --- Step 3: Competitor profiles -----------------------------------------------------------------

def profile_competitors(analyst: Analyst, company: dict[str, Checked], rows: list[dict],
                        on_progress=lambda done, total: None, workers: int = 4) -> list[dict]:
    chosen = [r for r in rows if r.get("include") and str(r.get("company_name") or "").strip()]
    target = company["company_name"].value
    out: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_profile, analyst, target, r): i for i, r in enumerate(chosen)}
        for done, fut in enumerate(as_completed(futures), start=1):
            out[futures[fut]] = fut.result()
            on_progress(done, len(chosen))
    return [out[i] for i in range(len(chosen))]


def _profile(analyst: Analyst, target: str, row: dict) -> dict:
    name, website = row["company_name"].strip(), str(row.get("website") or "").strip()
    findings = analyst.research(
        f"<role>\n{prompt('competitor_analyst')}\n</role>\n\n"
        f"Do Step 4 for one competitor of {target}: {name}" + (f" ({website})" if website else "") + ".\n"
        "Collect its HQ location, founders, current stage, latest funding round (name, amount, date), total "
        "funding raised, key investors and publicly named customers. Prefer the most recent source when "
        "sources disagree, and note the date of each funding fact.",
        max_searches=6,
    )
    parsed = analyst.extract(
        findings.render(),
        f"Fill the profile of {name} from the research above.\n" + FACT_RULES
        + "\nlatest_funding_round: round name, amount and date, e.g. \"Series B, $40M, March 2025\".",
        CompetitorProfile,
    )
    return {"company_name": name, "website": website, "competitive_overlap": row.get("competitive_overlap", ""),
            "selection_reason": row.get("selection_reason", ""), "facts": _check_all(analyst, parsed, PROFILE_FIELDS)}


# --- Step 4: Investment memo ---------------------------------------------------------------------

def memo_input(company: dict[str, Checked], competitors: list[dict]) -> str:
    return json.dumps({
        "company_classification": facts_json(company),
        "competitors": [
            {"company_name": c["company_name"], "website": c["website"],
             "competitive_overlap": c["competitive_overlap"], "selection_reason": c["selection_reason"],
             **facts_json(c["facts"])}
            for c in competitors
        ],
    }, ensure_ascii=False, indent=1)


def write_memo(analyst: Analyst, company: dict[str, Checked], competitors: list[dict]) -> MemoResult:
    facts = memo_input(company, competitors)
    memo = analyst.extract(
        f"<role>\n{prompt('investment_memo_writer')}\n</role>\n\n<agent_outputs>\n{facts}\n</agent_outputs>",
        "Write the investment memo from <agent_outputs> only. memo_markdown: the full memo with the ten "
        "sections as level-2 headings. executive_summary: 5-10 sentences. risks: 3-5 items. "
        "investment_questions: 5-10 items. Write in Korean, keeping names in their original form. "
        f"Values shown as {MARK} or {NOT_FOUND} are unknown: say they are unknown, don't fill them.",
        Memo,
    )
    markdown, flagged = flag_unsupported_numbers(memo.memo_markdown, facts)
    summary, flagged_summary = flag_unsupported_numbers(memo.executive_summary, facts)
    return MemoResult(summary, markdown, memo.risks, memo.investment_questions, flagged + flagged_summary)


def export(analyst: Analyst, company: dict[str, Checked], competitors: list[dict], memo: MemoResult) -> str:
    """Everything the analysis produced, with the evidence behind each value."""
    return json.dumps({
        "company_url": analyst.url,
        "company_classification": {k: asdict(c) for k, c in company.items()},
        "competitors": [{**{k: v for k, v in c.items() if k != "facts"},
                         "facts": {k: asdict(f) for k, f in c["facts"].items()}} for c in competitors],
        "memo": asdict(memo),
    }, ensure_ascii=False, indent=1)
