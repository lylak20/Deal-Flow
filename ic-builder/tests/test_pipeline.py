import io
import re

from PIL import Image
from pptx import Presentation
from pptx.util import Inches, Pt

import pipeline
import template
import writer
from fill import (Checker, DeckAnalysis, Filler, NewSlide, Paragraph, RowRef, SlideDecision, SlideProposals,
                  SlotDecision, VisualDecision)
from sources import Source
from template import MARK


def _png() -> io.BytesIO:
    img = io.BytesIO()
    Image.new("RGB", (40, 40), "blue").save(img, format="PNG")
    img.seek(0)
    return img


def make_deck() -> bytes:
    """Title slide, one content slide (slide 2), and a closing slide."""
    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[0]).shapes.title.text = "Northzone XI IC"

    slide = prs.slides.add_slide(prs.slide_layouts[5])  # title only
    slide.shapes.title.text = "Fund Overview"
    box = slide.shapes.add_textbox(Inches(0.5), Inches(1.5), Inches(5), Inches(2)).text_frame
    p = box.paragraphs[0]
    label = p.add_run()
    label.text = "Fund Size:"
    label.font.bold = True
    value = p.add_run()
    value.text = " $500M"
    value.font.size = Pt(14)
    for text in ["Northzone is a European VC", "Founded in 1996", "Portfolio: Spotify, Klarna"]:
        para = box.add_paragraph()
        para.text = text
        para.level = 1
    table = slide.shapes.add_table(3, 2, Inches(0.5), Inches(4), Inches(5), Inches(1.5)).table
    for r, row in enumerate([("Fund", "Size"), ("NZ I", "6.5"), ("NZ II", "16.9")]):
        for c, text in enumerate(row):
            table.cell(r, c).text = text
    slide.shapes.add_picture(_png(), Inches(6), Inches(1.5))
    slide.notes_slide.notes_text_frame.text = "Talk about Northzone returns"

    prs.slides.add_slide(prs.slide_layouts[5]).shapes.title.text = "End of Document"
    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


def slot_ids(slide):
    return {s.paragraphs[0]: s.id for s in slide.slots if s.paragraphs}


def content_slide(prs):
    return template.extract(prs)[1]


def test_extract_finds_text_table_cells_and_picture():
    slide = content_slide(Presentation(io.BytesIO(make_deck())))
    texts = [s.paragraphs for s in slide.slots]
    assert ["Fund Overview"] in texts
    assert ["Fund Size: $500M", "Northzone is a European VC", "Founded in 1996", "Portfolio: Spotify, Klarna"] in texts
    assert sum(s.kind == "table_cell" for s in slide.slots) == 6
    assert slide.tables[0].rows == [["Fund", "Size"], ["NZ I", "6.5"], ["NZ II", "16.9"]]
    assert [v.kind for v in slide.visuals] == ["picture"]


def test_writer_keeps_label_run_and_marks_red():
    prs = Presentation(io.BytesIO(make_deck()))
    body = slot_ids(content_slide(prs))["Fund Size: $500M"]
    writer.apply(prs, {2: writer.SlideEdits(texts={body: ["Fund Size: $750M", "VR Fund invests in XR", MARK]})})

    tf = template.text_frames(prs.slides[1])[body]
    paras = [p for p in tf.paragraphs if p.text.strip()]
    assert [p.text for p in paras] == ["Fund Size: $750M", "VR Fund invests in XR", MARK]
    first = paras[0].runs
    assert first[0].text == "Fund Size:" and first[0].font.bold
    assert first[1].text == " $750M" and first[1].font.size == Pt(14)
    assert paras[1].level == 1
    assert paras[2].runs[0].font.color.rgb == writer.RED


def test_writer_adds_paragraphs_by_cloning_the_last():
    prs = Presentation(io.BytesIO(make_deck()))
    body = slot_ids(content_slide(prs))["Fund Size: $500M"]
    new = ["Fund Size: $750M", "a", "b", "c", "d", "e"]
    writer.apply(prs, {2: writer.SlideEdits(texts={body: new})})
    tf = template.text_frames(prs.slides[1])[body]
    assert [p.text for p in tf.paragraphs if p.text.strip()] == new


def test_inline_marker_only_colours_the_marker():
    prs = Presentation(io.BytesIO(make_deck()))
    body = slot_ids(content_slide(prs))["Fund Size: $500M"]
    writer.apply(prs, {2: writer.SlideEdits(texts={body: [f"Fund Size: {MARK}"]})})
    runs = template.text_frames(prs.slides[1])[body].paragraphs[0].runs
    assert [r.text for r in runs] == ["Fund Size:", " ", MARK]
    assert runs[2].font.color.rgb == writer.RED
    assert runs[1].font.color and runs[1].font.size == Pt(14)


def test_checker_numbers_need_evidence():
    checker = Checker([Source("lpa.pdf", "pptx", "The Fund targets commitments of USD 750,000,000. Vintage 2025.")])
    original = ["Fund Size: $500M"]
    assert checker.paragraph_ok(Paragraph(text="Fund Size: $750M", evidence=["commitments of USD 750,000,000"]), original)
    # number not in the quote
    assert not checker.paragraph_ok(Paragraph(text="Fund Size: $800M", evidence=["commitments of USD 750,000,000"]), original)
    # quote not in the sources
    assert not checker.paragraph_ok(Paragraph(text="Fund Size: $800M", evidence=["USD 800,000,000"]), original)
    # facts with no evidence
    assert not checker.paragraph_ok(Paragraph(text="Led by Jane Doe", evidence=[]), original)
    # unchanged template text and bare markers pass
    assert checker.paragraph_ok(Paragraph(text="Fund Size: $500M", evidence=[]), original)
    assert checker.paragraph_ok(Paragraph(text=f"Fund Size: {MARK}", evidence=[]), original)


def test_checker_demotes_but_keeps_label():
    checker = Checker([Source("a", "pptx", "nothing relevant here")])
    slot = template.Slot("1", "text", "box", ["Fund Size: $500M"], 0, 0, 0)
    texts, demoted = checker.check(slot, [Paragraph(text="Fund Size: $900M", evidence=["$900M"])])
    assert texts == [f"Fund Size: {MARK}"] and demoted == 1


# --- Tables -------------------------------------------------------------------------------------


def merged_table_slide():
    """Header row, then "Realized" merged down rows 1-3 of column 0, then a "Live" row; a logo on each row."""
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    frame = slide.shapes.add_table(5, 2, Inches(1), Inches(1), Inches(4), Inches(2.5))
    for row in frame.table.rows:
        row.height = Inches(0.5)
    t = frame.table
    t.cell(0, 0).text, t.cell(0, 1).text = "Group", "Fund"
    t.cell(1, 0).merge(t.cell(3, 0))
    t.cell(1, 0).text = "Realized"
    t.cell(4, 0).text = "Live"
    for r, name in enumerate(["NZ I", "NZ II", "NZ III", "NZ IV"], start=1):
        t.cell(r, 1).text = name
    logos = [slide.shapes.add_picture(_png(), Inches(4), Inches(1 + 0.5 * r + 0.1), Inches(0.3), Inches(0.3))
             for r in range(1, 5)]
    return prs, slide, frame, logos


def spans_are_consistent(tbl) -> bool:
    """Every merge origin's rowSpan covers exactly its vMerge cells, within the table."""
    trs = tbl.tr_lst
    for c in range(len(trs[0].tc_lst)):
        r = 0
        while r < len(trs):
            tc = trs[r].tc_lst[c]
            if tc.get("vMerge"):
                return False  # a continuation without an origin above it
            span = int(tc.get("rowSpan", "1"))
            if r + span > len(trs) or any(not trs[r + k].tc_lst[c].get("vMerge") for k in range(1, span)):
                return False
            r += span
    return True


def test_delete_merge_origin_row_moves_the_merged_cell_down():
    prs, slide, frame, logos = merged_table_slide()
    logo_ids = [l.shape_id for l in logos]
    assert writer.delete_rows(slide, frame, [1]) == 1
    t = frame.table
    assert [t.cell(r, 0).text for r in (1, 3)] == ["Realized", "Live"]
    assert [t.cell(r, 1).text for r in range(1, 4)] == ["NZ II", "NZ III", "NZ IV"]
    assert t._tbl.tr_lst[1].tc_lst[0].get("rowSpan") == "2"
    assert spans_are_consistent(t._tbl)
    remaining = {sh.shape_id: sh for sh in slide.shapes}
    assert logo_ids[0] not in remaining  # the deleted row's logo went with it
    assert remaining[logo_ids[1]].top == Inches(1 + 0.5 * 1 + 0.1)  # the next logo moved up a row


def test_delete_middle_rows_shrinks_the_merge():
    prs, slide, frame, logos = merged_table_slide()
    assert writer.delete_rows(slide, frame, [2, 3, 0]) == 2  # the header row is never deleted
    t = frame.table
    assert [t.cell(r, 1).text for r in range(3)] == ["Fund", "NZ I", "NZ IV"]
    assert "rowSpan" not in t._tbl.tr_lst[1].tc_lst[0].attrib
    assert spans_are_consistent(t._tbl)


def test_tables_are_never_split_across_batches():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    t = slide.shapes.add_table(12, 3, Inches(0.5), Inches(0.5), Inches(8), Inches(5)).table
    for r in range(12):
        for c in range(3):
            t.cell(r, c).text = f"r{r}c{c}"
    for i in range(25):
        slide.shapes.add_textbox(Inches(0.1), Inches(0.1), Inches(1), Inches(0.3)).text_frame.text = f"box {i}"
    info = template.extract(prs)[0]

    seen = []

    class Messages:
        def stream(self, output_format, messages, **kwargs):
            request = messages[0]["content"][-1]["text"]
            seen.append(request)
            ids = re.findall(r'"slot_id": "([^"]+)"', request)
            return FakeResponse(SlideDecision(
                slots=[SlotDecision(slot_id=i, action="keep", paragraphs=[]) for i in ids],
                visuals=[], delete_rows=[]))

    Filler([], "", client=type("C", (), {"messages": Messages()})()).fill_slide(info, 1)
    with_table = [r for r in seen if '"table_id"' in r]
    assert len(with_table) == 1 and with_table[0].count('"row":') == 12
    assert all(r.count('"slot_id"') <= 20 for r in seen)


# --- New slides -----------------------------------------------------------------------------------


def test_duplicate_slide_copies_text_and_tables_but_not_pictures():
    prs = Presentation(io.BytesIO(make_deck()))
    copy = writer.duplicate_slide(prs, prs.slides[1])
    assert len(prs.slides) == 4
    kinds = [("table" if sh.has_table else "picture" if sh.shape_type == 13 else "text") for sh in copy.shapes]
    assert "picture" not in kinds and "table" in kinds
    assert "Fund Overview" in [sh.text_frame.text for sh in copy.shapes if sh.has_text_frame]
    out = io.BytesIO()
    prs.save(out)
    Presentation(io.BytesIO(out.getvalue()))  # still opens


class FakeResponse:
    def __init__(self, parsed, stop_reason="end_turn"):
        self.parsed_output = parsed
        self.stop_reason = stop_reason

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self


class FakeMessages:
    def __init__(self, deck):
        self.slide = content_slide(Presentation(io.BytesIO(deck)))

    def stream(self, output_format, messages, **kwargs):
        request = messages[0]["content"][-1]["text"]
        if output_format is DeckAnalysis:
            return FakeResponse(DeckAnalysis(
                old_fund_name="Northzone", new_fund_name="Virtual Reality Fund",
                old_fund_terms=["Northzone Partners", "Spotify", "Klarna"], last_slide_is_closing=True))
        if output_format is SlideProposals:
            return FakeResponse(SlideProposals(slides=[
                NewSlide(topic="Fund III terms", why="IC needs the terms", base_slide=2),
                NewSlide(topic="Not a real slide", why="bad base", base_slide=3),  # closing slide: dropped
            ]))
        ids = slot_ids(self.slide)
        table = self.slide.tables[0].id
        if "NEW slide" in request:
            return FakeResponse(SlideDecision(
                slots=[
                    SlotDecision(slot_id=ids["Fund Overview"], action="fill",
                                 paragraphs=[Paragraph(text="Fund III Terms", evidence=["Fund III"])]),
                    SlotDecision(slot_id=ids["Fund Size: $500M"], action="fill", paragraphs=[
                        Paragraph(text="Management fee: 2.5%", evidence=["management fee of 2.5%"])]),
                ],
                visuals=[], delete_rows=[RowRef(table_id=table, row=2)]))
        if "Rewrite slide 2 of" not in request:
            found = re.findall(r'"slot_id": "([^"]+)"', request)
            return FakeResponse(SlideDecision(
                slots=[SlotDecision(slot_id=i, action="keep", paragraphs=[]) for i in found],
                visuals=[], delete_rows=[]))
        return FakeResponse(SlideDecision(
            slots=[
                SlotDecision(slot_id=ids["Fund Overview"], action="keep", paragraphs=[]),
                SlotDecision(slot_id=ids["Fund Size: $500M"], action="fill", paragraphs=[
                    Paragraph(text="Fund Size: $750M", evidence=["USD 750,000,000"]),
                    Paragraph(text="Virtual Reality Fund backs XR startups", evidence=["backs XR startups"]),
                    Paragraph(text="Founded in 2019", evidence=["founded in 2018"]),  # wrong year
                ]),
                SlotDecision(slot_id=ids["NZ I"], action="fill", paragraphs=[Paragraph(text="VRF I", evidence=["VRF I"])]),
                SlotDecision(slot_id=ids["6.5"], action="fill", paragraphs=[Paragraph(text="43", evidence=["$43M"])]),
            ],
            visuals=[VisualDecision(visual_id=self.slide.visuals[0].id, action="flag")],
            delete_rows=[RowRef(table_id=table, row=2), RowRef(table_id=table, row=0)],
        ))


class FakeClient:
    def __init__(self, deck):
        self.messages = FakeMessages(deck)


def test_pipeline_end_to_end():
    deck = make_deck()
    src = Source("deck.pptx", "pptx", "Virtual Reality Fund backs XR startups. USD 750,000,000. VRF I raised $43M. "
                                      "The firm was founded in 2018. Fund III charges a management fee of 2.5%.")
    result = pipeline.run(deck, [src], client=FakeClient(deck), workers=1)
    prs = Presentation(io.BytesIO(result.pptx))
    assert len(prs.slides) == 4

    # slide 2: text filled, wrong year demoted, NZ II row deleted, header kept, picture labelled
    texts = [tf.text for tf in template.text_frames(prs.slides[1]).values()]
    assert f"Fund Size: $750M\nVirtual Reality Fund backs XR startups\n{MARK}" in texts
    table = template.tables(prs.slides[1])
    rows = [[c.text for c in row.cells] for row in next(iter(table.values())).rows]
    assert rows == [["Fund", "Size"], ["VRF I", "43"]]
    assert result.demoted == 1 and result.deleted_rows == 2
    assert result.flagged_visuals == {2: 1}

    # the new slide sits before "End of Document", tagged, with its fact checked
    assert result.new_slides == [(3, "Fund III terms")]
    new_texts = [tf.text for tf in template.text_frames(prs.slides[2]).values()]
    assert "Fund III Terms" in new_texts and "Management fee: 2.5%" in new_texts
    assert writer.NEW_SLIDE_TAG in new_texts
    assert prs.slides[3].shapes.title.text == "End of Document"
    # the new slide's untouched NZ I row still names the old fund, and the notes do too
    assert (2, "Northzone", "노트") in result.leftovers


class TruncatingMessages:
    """Runs out of output tokens whenever asked about more than two shapes at once."""

    def __init__(self):
        self.batch_sizes = []

    def stream(self, output_format, messages, **kwargs):
        request = messages[0]["content"][-1]["text"]
        ids = re.findall(r'"slot_id": "([^"]+)"', request)
        self.batch_sizes.append(len(ids))
        if len(ids) > 2:
            return FakeResponse(None, stop_reason="max_tokens")
        return FakeResponse(SlideDecision(
            slots=[SlotDecision(slot_id=i, action="keep", paragraphs=[]) for i in ids], visuals=[], delete_rows=[]))


def test_long_slides_are_split_until_they_fit():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for i in range(5):
        slide.shapes.add_textbox(Inches(0.1), Inches(0.1), Inches(1), Inches(0.3)).text_frame.text = f"box {i}"
    info = template.extract(prs)[0]
    fake = TruncatingMessages()
    filler = Filler([], "", client=type("C", (), {"messages": fake})())
    decision = filler.fill_slide(info, 1)
    assert sorted(d.slot_id for d in decision.slots) == sorted(s.id for s in info.slots)
    answered = [n for n in fake.batch_sizes if n <= 2]
    assert fake.batch_sizes[0] == 5 and sum(answered) == 5


def test_empty_columns_are_dropped_and_width_kept():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    frame = slide.shapes.add_table(3, 4, Inches(1), Inches(1), Inches(8), Inches(1.5))
    t = frame.table
    t.cell(0, 0).merge(t.cell(0, 1))  # a header spanning two columns keeps both
    t.cell(0, 0).text = "Function"
    t.cell(1, 1).text = "Affinity"
    t.cell(1, 0).text = "Platform"
    t.cell(2, 3).text = "note"
    before = sum(c.width for c in t.columns)
    writer.delete_empty_columns(frame)
    assert len(t.columns) == 3
    assert [t.cell(1, c).text for c in range(3)] == ["Platform", "Affinity", ""]
    assert abs(sum(c.width for c in t.columns) - before) < 10


def test_old_names_are_scrubbed_on_whole_words_only():
    checker = Checker([], ["Northzone", "Stream"])
    assert checker.scrub(["Northzone Entry", "Streamlined Operations"]) == [f"{MARK} Entry", "Streamlined Operations"]


def test_leftovers_match_whole_words():
    prs = Presentation(io.BytesIO(make_deck()))
    found = writer.leftovers(prs, ["Stream", "Klarna"])
    assert (2, "Klarna", "본문") in found and not any(t == "Stream" for _, t, _ in found)


def test_kept_text_with_old_numbers_is_marked():
    checker = Checker([Source("a", "pptx", "Fund I (2016) raised $43M.")])
    slot = template.Slot("1", "text", "box", ["Total Cost: €20.5M", "Vintage 2016", "2", "Company"], 0, 0, 0)
    texts, demoted = checker.check_kept(slot)
    assert texts == [f"Total Cost: {MARK}", "Vintage 2016", "2", "Company"] and demoted == 1


def test_a_stream_that_breaks_midway_is_retried(monkeypatch):
    import httpx2

    import fill

    monkeypatch.setattr(fill.time, "sleep", lambda s: None)
    calls = []

    class Flaky:
        def stream(self, output_format, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise httpx2.RemoteProtocolError("peer closed connection without sending complete message body")
            return FakeResponse(DeckAnalysis(old_fund_name="A", new_fund_name="B", old_fund_terms=[],
                                             last_slide_is_closing=False))

    analysis = Filler([], "", client=type("C", (), {"messages": Flaky()})()).analyze()
    assert analysis.new_fund_name == "B" and len(calls) == 2
