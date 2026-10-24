import json
from types import SimpleNamespace as NS

import pipeline
from research import Analyst
from verify import MARK, MISSING, UNVERIFIED, VERIFIED, Checker, Fact, flag_unsupported_numbers

CORPUS = "Acme raised a $40 million Series B led by Sequoia Capital in March 2025. Founded in 2021 by Jane Doe."


def test_fact_with_real_quote_and_numbers_is_verified():
    c = Checker(CORPUS).fact(Fact(value="Series B, $40M, March 2025", evidence=["raised a $40 million Series B",
                                                                         "in March 2025"], sources=["u"]))
    assert c.status == VERIFIED and c.value == "Series B, $40M, March 2025"


def test_number_missing_from_quotes_is_demoted():
    c = Checker(CORPUS).fact(Fact(value="$45M", evidence=["raised a $40 million Series B"], sources=["u"]))
    assert c.value == MARK and c.status == UNVERIFIED and c.claimed == "$45M"


def test_quote_not_in_sources_is_demoted():
    c = Checker(CORPUS).fact(Fact(value="$40M", evidence=["raised $40 million from Andreessen"], sources=["u"]))
    assert c.status == UNVERIFIED


def test_names_must_appear_in_quotes():
    checker = Checker(CORPUS)
    ok = checker.fact(Fact(value="Sequoia Capital", evidence=["led by Sequoia Capital"], sources=["u"]), names=True)
    bad = checker.fact(Fact(value="Sequoia Capital; Accel", evidence=["led by Sequoia Capital"], sources=["u"]),
                       names=True)
    assert ok.status == VERIFIED and bad.status == UNVERIFIED


def test_not_found_and_missing_evidence():
    checker = Checker(CORPUS)
    assert checker.fact(Fact(value="Not Found", evidence=[], sources=[])).status == MISSING
    assert checker.fact(Fact(value="AI Infrastructure", evidence=[], sources=[])).status == UNVERIFIED


def test_quote_check_can_be_turned_off_for_scanned_decks():
    c = Checker("", check_quotes=False).fact(Fact(value="$40M", evidence=["$40 million"], sources=["deck.pdf"]))
    assert c.status == VERIFIED


def test_memo_lines_with_new_numbers_are_flagged():
    memo = "## 1. Company Overview\n- Acme raised $40M.\n- Revenue is $12M.\n1. 질문"
    out, flagged = flag_unsupported_numbers(memo, '{"total_funding": "$40M"}')
    assert flagged == 1
    assert out.splitlines() == ["## 1. Company Overview", "- Acme raised $40M.", f"- Revenue is $12M. {MARK}", "1. 질문"]


# --- Fake Claude client --------------------------------------------------------------------------

def text(t, citations=()):
    return NS(type="text", text=t, citations=[NS(type="web_search_result_location", cited_text=c, url=u,
                                                    title="page") for c, u in citations])


def resp(content, stop="end_turn", parsed=None):
    return NS(content=content, stop_reason=stop, parsed_output=parsed,
              usage=NS(input_tokens=100, output_tokens=10, cache_read_input_tokens=0, cache_creation_input_tokens=0,
                       server_tool_use=NS(web_search_requests=1)))


def fact(value, quote, source="https://acme.ai"):
    return Fact(value=value, evidence=[quote] if quote else [], sources=[source] if quote else [])


class Stream:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._response


class FakeClient:
    def __init__(self):
        self.calls = []
        self.messages = self

    def stream(self, **kw):
        self.calls.append(kw)
        return Stream(self._answer(kw))

    def _answer(self, kw):
        task = kw["messages"][0]["content"][-1]["text"]
        if "tools" in kw:
            if "Do Step 4" in task and "Rival" in task:
                return resp([text("Rival raised money.", [("Rival closed a $12 million Series A", "https://news/r")])])
            if "Do Step 1 and Step 2" in task:
                return resp([text("Competitor: Rival.", [("Rival also sells GPU cloud to AI startups",
                                                          "https://news/x")])])
            # The classification pauses once, so the continuation must keep the first part's citations.
            if len(kw["messages"]) == 1:
                return resp([text("Acme sells GPU cloud.", [("Acme is a GPU cloud for AI startups",
                                                             "https://acme.ai")])], stop="pause_turn")
            return resp([text("Founded 2021.", [("Founded in 2021 by Jane Doe", "https://acme.ai/about")])])
        schema = kw["output_format"]
        if schema is pipeline.Classification:
            nf = fact("Not Found", None)
            return resp([], parsed=pipeline.Classification(
                company_name=fact("Acme", "Acme is a GPU cloud"), website=nf, hq_location=nf,
                founding_year=fact("2021", "Founded in 2021"), founders=fact("Jane Doe; John Roe", "by Jane Doe"),
                industry=fact("AI Infrastructure", "Acme is a GPU cloud for AI startups"),
                product_categories=fact("GPU Cloud", "GPU cloud"), primary_customer=fact("AI startups", "AI startups"),
                secondary_customer=nf, customer_types=nf, business_model=nf, geography_focus=nf,
                product_summary=fact("GPU cloud for AI startups", "Acme is a GPU cloud for AI startups")))
        if schema is pipeline.CandidateList:
            return resp([], parsed=pipeline.CandidateList(competitors=[
                pipeline.Candidate(company_name="Rival", website="https://rival.ai", competitive_overlap=["GPU Cloud"],
                                   selection_reason="Same customers.", evidence=["Rival also sells GPU cloud"],
                                   sources=["https://news/x"]),
                pipeline.Candidate(company_name="Ghost", website="Not Found", competitive_overlap=["GPU Cloud"],
                                   selection_reason="Made up.", evidence=["Ghost sells GPUs"], sources=["x"]),
            ]))
        if schema is pipeline.CompetitorProfile:
            nf = fact("Not Found", None)
            return resp([], parsed=pipeline.CompetitorProfile(
                hq_location=nf, founders=nf, current_stage=fact("Series A", "Series A", "https://news/r"),
                latest_funding_round=fact("Series A, $12M", "closed a $12 million Series A", "https://news/r"),
                total_funding=fact("$15M", "closed a $12 million Series A", "https://news/r"),
                key_investors=nf, major_customers=nf))
        if schema is pipeline.Memo:
            return resp([], parsed=pipeline.Memo(
                executive_summary="Acme은 2021년 설립된 GPU cloud 회사다.",
                memo_markdown="## Company Overview\nAcme은 GPU cloud를 판다.\n\n## Competition\nRival은 $12M을 투자받았다.\n"
                              "Acme의 매출은 $30M이다.",
                risks=["Competitive Risk"], investment_questions=["Why now?"]))
        raise AssertionError(schema)


def test_pipeline_end_to_end():
    client = FakeClient()
    analyst = Analyst("https://acme.ai", client=client)

    company = pipeline.classify(analyst)
    assert company["company_name"].status == VERIFIED
    assert company["founding_year"].value == "2021"  # quote came from the continued (paused) turn
    assert company["founders"].value == MARK  # "John Roe" is in no quote
    assert company["website"].value == "https://acme.ai"
    assert company["hq_location"].status == MISSING
    extract_prompt = client.calls[2]["messages"][0]["content"][0]["text"]
    assert '"Founded in 2021 by Jane Doe"' in extract_prompt and "[2]" in extract_prompt

    company = pipeline.apply_edits(company, {"founders": "Jane Doe", "company_name": "Acme"})
    assert company["founders"].value == "Jane Doe" and company["founders"].status == "직접 수정"
    assert company["company_name"].status == VERIFIED

    rows = pipeline.find_competitors(analyst, company)
    assert [(r["company_name"], r["include"], r["status"]) for r in rows] == [
        ("Rival", True, VERIFIED), ("Ghost", False, UNVERIFIED)]
    assert rows[1]["website"] == ""

    rows.append({"include": True, "company_name": "  ", "website": ""})  # an empty row the analyst added
    competitors = pipeline.profile_competitors(analyst, company, rows)
    assert [c["company_name"] for c in competitors] == ["Rival"]
    facts = competitors[0]["facts"]
    assert facts["latest_funding_round"].status == VERIFIED
    assert facts["total_funding"].value == MARK  # $15M is not in the quote

    memo = pipeline.write_memo(analyst, company, competitors)
    assert memo.flagged == 1 and memo.markdown.endswith(f"$30M이다. {MARK}")
    assert "Jane Doe" in client.calls[-1]["messages"][0]["content"][0]["text"]

    exported = json.loads(pipeline.export(analyst, company, competitors, memo))
    assert exported["competitors"][0]["facts"]["total_funding"]["claimed"] == "$15M"
    assert analyst.usage.calls == len(client.calls) and analyst.usage.searches == len(client.calls)
    assert all(c["model"] == "claude-haiku-5-5" for c in client.calls)
