"""Local web app: company URL (+ pitch deck) -> classification -> competitors -> investment memo;
Financials tab: company financials (.xlsx) -> growth equity metrics table and charts.

    streamlit run app.py
"""

import io
import re
from pathlib import Path

import altair as alt
import anthropic
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import financials
import metrics
import pipeline
from deck import load
from research import MODEL, Analyst, Refused
from verify import EDITED, MARK, MISSING, UNVERIFIED, VERIFIED

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
load_dotenv(HERE.parent / "tracker" / ".env")  # reuse the tracker's key if this folder has none

STATUS = {VERIFIED: "✅ 확인됨", UNVERIFIED: "🔴 확인 필요", MISSING: "⚪ Not Found", EDITED: "✏️ 직접 수정"}
STATUS_BACK = {v: k for k, v in STATUS.items()}

st.set_page_config(page_title="스타트업 분석", page_icon="🔎", layout="wide")
st.title("스타트업 분석")

state = st.session_state


def _reset(*keys):
    for k in keys:
        state.pop(k, None)


def _run(label: str, fn):
    """Run one step with a status box and turn API errors into messages."""
    with st.status(label, expanded=False) as box:
        try:
            result = fn()
        except ValueError as e:
            box.update(label=str(e), state="error")
            return None
        except anthropic.AuthenticationError:
            box.update(label="ANTHROPIC_API_KEY가 없거나 잘못되었습니다. analyst/.env를 확인해 주세요.", state="error")
            return None
        except anthropic.APIError as e:
            box.update(label=f"Claude API 오류: {e}", state="error")
            return None
        except (Refused, RuntimeError) as e:
            box.update(label=str(e), state="error")
            return None
        box.update(label=f"{label} 완료", state="complete")
        return result


def _md(text: str) -> str:
    return text.replace(MARK, ":red[**확인 필요**]")


tab_startup, tab_fin = st.tabs(["회사 분석", "Financials"])

with tab_startup:
    st.caption("회사 URL을 넣으면 회사 분류 → 경쟁사 → 투자 메모 순서로 분석합니다. 모든 값에는 출처가 붙고, "
               "출처에서 확인되지 않은 값은 빨간 [확인 필요]로 남깁니다.")

    # --- Input ---------------------------------------------------------------------------------------

    url = st.text_input("회사 URL", placeholder="https://example.com")
    deck_file = st.file_uploader("Pitch Deck (선택, .pdf / .pptx)", type=["pdf", "pptx"])

    if st.button("분석하기", type="primary", disabled=not url.strip()):
        _reset("company", "candidates", "competitors", "memo")
        try:
            deck = load(deck_file.name, deck_file.getvalue()) if deck_file else None
        except ValueError as e:
            st.error(str(e))
            st.stop()
        target = url.strip() if re.match(r"https?://", url.strip()) else f"https://{url.strip()}"
        state["analyst"] = Analyst(target, deck)
        state["run"] = state.get("run", 0) + 1
        company = _run("① 회사 분류 중… (보통 1~3분)", lambda: pipeline.classify(state["analyst"]))
        if company:
            state["company"] = company

    analyst: Analyst | None = state.get("analyst")
    run = state.get("run", 0)

    # --- Step 1: classification ----------------------------------------------------------------------

    if "company" in state:
        st.subheader("① 회사 분류")
        st.info("아래 회사 분류가 맞는지 확인하세요. '값' 칸은 직접 고칠 수 있습니다.")
        rows = [{
            "key": key,
            "항목": label,
            "값": state["company"][key].value,
            "상태": STATUS[state["company"][key].status],
            "Claude가 쓴 값 (근거 부족)": state["company"][key].claimed,
            "근거": " / ".join(state["company"][key].evidence),
            "출처": " ".join(dict.fromkeys(state["company"][key].sources)),
        } for key, label, _ in pipeline.CLASSIFICATION_FIELDS]
        edited = st.data_editor(
            pd.DataFrame(rows), key=f"company_{run}", hide_index=True, use_container_width=True,
            column_config={"key": None},
            disabled=["항목", "상태", "Claude가 쓴 값 (근거 부족)", "근거", "출처"],
        )
        if st.button("확인 → 경쟁사 찾기", type="primary"):
            state["company"] = pipeline.apply_edits(state["company"], dict(zip(edited["key"], edited["값"].astype(str))))
            _reset("candidates", "competitors", "memo")
            candidates = _run("② 경쟁사 찾는 중… (보통 2~4분)",
                              lambda: pipeline.find_competitors(analyst, state["company"]))
            if candidates is not None:
                state["candidates"] = candidates
                st.rerun()

    # --- Step 2: competitor list ---------------------------------------------------------------------

    if "candidates" in state:
        st.subheader("② 경쟁사 목록")
        st.info("아래 경쟁사 목록이 맞는지 확인하세요. 체크한 회사만 프로필을 수집합니다. 맨 아래 행에 직접 추가할 수도 있습니다.")
        if not state["candidates"]:
            st.write("확실한 직접 경쟁사를 찾지 못했습니다. 아는 경쟁사가 있으면 아래에 직접 추가해 주세요.")
        table = pd.DataFrame(state["candidates"] or [], columns=[
            "include", "company_name", "website", "competitive_overlap", "selection_reason", "status", "sources"])
        table["status"] = table["status"].map(lambda s: STATUS.get(s, s))
        chosen = st.data_editor(
            table, key=f"candidates_{run}", hide_index=True, use_container_width=True, num_rows="dynamic",
            column_config={
                "include": st.column_config.CheckboxColumn("포함", default=True),
                "company_name": "회사", "website": "웹사이트", "competitive_overlap": "겹치는 영역",
                "selection_reason": "선정 이유", "status": "상태", "sources": "출처",
            },
            disabled=["status", "sources"],
        )
        n = int(chosen["include"].fillna(False).astype(bool).sum())
        if st.button(f"확인 → {n}개 회사 프로필 수집 + 투자 메모 작성", type="primary", disabled=n == 0):
            rows = chosen.fillna("").to_dict("records")
            for r in rows:
                r["include"] = bool(r["include"])
            _reset("competitors", "memo")
            bar = st.progress(0.0, text="③ 경쟁사 프로필 수집 중…")
            profiles = _run("③ 경쟁사 프로필 수집 중… (회사당 1~2분, 4개씩 동시에)", lambda: pipeline.profile_competitors(
                analyst, state["company"], rows,
                on_progress=lambda d, t: bar.progress(d / max(t, 1), text=f"③ 경쟁사 프로필 {d}/{t}")))
            if profiles is not None:
                state["competitors"] = profiles
                memo = _run("④ 투자 메모 작성 중…", lambda: pipeline.write_memo(analyst, state["company"], profiles))
                if memo:
                    state["memo"] = memo
                    st.rerun()

    # --- Step 3-4: competitor table and memo ---------------------------------------------------------

    if "competitors" in state:
        st.subheader("③ 경쟁사 비교")
        table = pd.DataFrame([{
            "회사": c["company_name"], "웹사이트": c["website"], "겹치는 영역": c["competitive_overlap"],
            **{label: c["facts"][key].value for key, label, _ in pipeline.PROFILE_FIELDS},
        } for c in state["competitors"]])
        st.dataframe(table.style.map(lambda v: "color: #d62728; font-weight: 600" if v == MARK else ""),
                     hide_index=True, use_container_width=True)
        with st.expander("값별 근거와 출처"):
            for c in state["competitors"]:
                st.markdown(f"**{c['company_name']}**")
                for key, label, _ in pipeline.PROFILE_FIELDS:
                    f = c["facts"][key]
                    if f.evidence:
                        note = f" (Claude가 쓴 값: {f.claimed})" if f.claimed else ""
                        st.markdown(f"- {label}{note}: " + " / ".join(f"“{q}”" for q in f.evidence)
                                    + " — " + " ".join(dict.fromkeys(f.sources)))

    memo = state.get("memo")
    if memo:
        st.subheader("④ 투자 메모")
        company = state["company"]
        c1, c2, c3 = st.columns(3)
        c1.metric("경쟁사", len(state["competitors"]))
        c2.metric("[확인 필요] 값", sum(f.status == UNVERIFIED for f in company.values())
                  + sum(f.status == UNVERIFIED for c in state["competitors"] for f in c["facts"].values()))
        c3.metric("메모에서 근거 없는 숫자", memo.flagged)
        if memo.flagged:
            st.warning(f"메모의 {memo.flagged}개 줄에 확인된 사실에 없는 숫자가 있어 [확인 필요]를 붙였습니다.")
        st.markdown("**Executive Summary**\n\n" + _md(memo.executive_summary))
        st.markdown(_md(memo.markdown))
        name = re.sub(r'[\\/:*?"<>|]', "", company["company_name"].value if company["company_name"].value != MARK
                      else "company") or "company"
        d1, d2 = st.columns(2)
        d1.download_button("메모 다운로드 (.md)", f"# {name} Investment Memo\n\n## Executive Summary\n\n"
                           f"{memo.executive_summary}\n\n{memo.markdown}\n", file_name=f"{name} memo.md",
                           mime="text/markdown")
        d2.download_button("전체 결과 다운로드 (.json, 근거 포함)",
                           pipeline.export(analyst, company, state["competitors"], memo),
                           file_name=f"{name} analysis.json", mime="application/json")

    if analyst and analyst.usage.calls:
        u = analyst.usage
        st.caption(f"{MODEL} · 호출 {u.calls}회 · 웹검색 {u.searches}회 · 입력 {u.input_tokens + u.cache_read + u.cache_write:,} "
                   f"토큰 · 출력 {u.output_tokens:,} 토큰 · 대략 ${u.cost:.2f}")

# --- Financials ----------------------------------------------------------------------------------

CHART_AXES = {metrics.MONEY: "금액 (백만)", metrics.PCT: "비율 (%)", metrics.X: "배수 (x)", metrics.MONTHS: "개월",
              metrics.COUNT: "수"}
DEFAULT_CHARTS = ["ARR", "Revenue", "YoY ARR Growth", "Net Dollar Retention", "Gross Dollar Retention", "Rule of 40"]


def _metric_table(rows: list[metrics.Metric], periods: list[metrics.Period], currency: str, raw: bool) -> pd.DataFrame:
    """Metrics down the side, periods across the top. raw=True keeps numbers for the Excel download."""
    data = {}
    for m in rows:
        name = f"{m.name} ({currency})" if m.fmt == metrics.MONEY and currency != "unknown" else m.name
        data[name] = {p.label: (m.values.get(p) if raw else metrics.fmt(m.values.get(p), m.fmt)) for p in periods}
    return pd.DataFrame.from_dict(data, orient="index", columns=[p.label for p in periods])


def _xlsx(tables: dict[str, pd.DataFrame], formulas: pd.DataFrame, mapping: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        for name, df in tables.items():
            df.to_excel(xl, sheet_name=re.sub(r"[\\/*?:\[\]]", "", name)[:31])
        formulas.to_excel(xl, sheet_name="계산 방법", index=False)
        mapping.to_excel(xl, sheet_name="항목 매핑", index=False)
    return buf.getvalue()


with tab_fin:
    st.caption("Series C/D 회사의 financials 엑셀을 넣으면 growth equity 지표를 기간별 표와 차트로 정리합니다. "
               "Claude는 어느 행이 어떤 항목인지만 찾고, 숫자는 엑셀 셀에서 그대로 읽어 공식으로 계산합니다. "
               "엑셀에 없는 지표는 나오지 않고, 좋다/나쁘다는 판단하지 않습니다.")
    xlsx = st.file_uploader("Financials 엑셀 (.xlsx / .xlsm)", type=["xlsx", "xlsm"], key="fin_file")
    if st.button("엑셀 정리하기", type="primary", disabled=xlsx is None, key="fin_go"):
        _reset("fin_wb", "fin_mapping", "fin_edited")
        try:
            wb = financials.load(xlsx.name, xlsx.getvalue())
        except ValueError as e:
            wb = None
            st.error(str(e))
        if wb:
            state["fin_analyst"] = Analyst(f"file:{xlsx.name}")
            state["fin_run"] = state.get("fin_run", 0) + 1
            mapping = _run("엑셀 구조 읽는 중… (보통 1분 이내)",
                           lambda: financials.map_items(state["fin_analyst"], wb))
            if mapping:
                state["fin_wb"], state["fin_mapping"] = wb, mapping

    if "fin_mapping" in state:
        wb: financials.Workbook = state["fin_wb"]
        mapping: financials.Mapping = state["fin_mapping"]
        fin_run = state.get("fin_run", 0)
        ex = financials.extract(wb, mapping, state.get("fin_edited", set()))
        if wb.uncached_formulas:
            st.warning(f"수식 셀 {wb.uncached_formulas}개에 저장된 결과값이 없어 읽지 못했습니다. "
                       "엑셀에서 파일을 한 번 열어 저장한 뒤 다시 올리면 읽힙니다.")

        grans = list(dict.fromkeys(p.gran for p in ex.periods))
        names = {"month": "월별", "quarter": "분기별", "year": "연도별"}
        gran = st.radio("기간 단위", grans, format_func=names.get, horizontal=True) if len(grans) > 1 else \
            (grans[0] if grans else None)
        periods = [p for p in ex.periods if p.gran == gran]
        rows = metrics.compute(ex.series, periods) if periods else []

        st.subheader("지표")
        currency = mapping.currency.strip() or "unknown"
        st.caption(f"통화: {currency} · 금액의 M은 백만 · 열은 엑셀의 기간 그대로입니다 (E = 전망치).")
        if not rows:
            st.warning("계산할 수 있는 지표가 없습니다. 아래 '항목 매핑'에서 Claude가 찾은 행을 확인해 주세요.")
        tables = {}
        for cat in metrics.CATEGORIES:
            cat_rows = [m for m in rows if m.category == cat]
            if cat_rows:
                st.markdown(f"**{cat}**")
                st.dataframe(_metric_table(cat_rows, periods, currency, raw=False), use_container_width=True,
                             height=35 * (len(cat_rows) + 1) + 3)  # every row visible, no inner scroll
                tables[cat] = _metric_table(cat_rows, periods, currency, raw=True)

        if rows:
            st.subheader("차트")
            options = [m.name for m in rows]
            picked = st.multiselect("지표 선택", options, default=[n for n in DEFAULT_CHARTS if n in options],
                                    key=f"fin_chart_{fin_run}_{gran}")
            by_name = {m.name: m for m in rows}
            order = [p.label for p in periods]
            for kind, axis in CHART_AXES.items():
                group = [by_name[n] for n in picked if by_name[n].fmt == kind]
                if not group:
                    continue
                scale = {metrics.MONEY: 1e-6, metrics.PCT: 100}.get(kind, 1)
                long = pd.DataFrame([{"기간": p.label, "지표": m.name, axis: v * scale}
                                     for m in group for p, v in m.values.items()])
                chart = alt.Chart(long).mark_line(point=True).encode(
                    x=alt.X("기간:N", sort=order), y=alt.Y(f"{axis}:Q"), color="지표:N",
                    tooltip=["기간", "지표", alt.Tooltip(f"{axis}:Q", format=",.2f")])
                st.altair_chart(chart, use_container_width=True)

        formulas = pd.DataFrame([{"분류": m.category, "지표": m.name, "공식": m.how} for m in rows],
                                columns=["분류", "지표", "공식"])
        with st.expander("계산 방법"):
            st.dataframe(formulas, hide_index=True, use_container_width=True)

        st.subheader("항목 매핑")
        st.info("Claude가 엑셀에서 찾은 행입니다. 각 행의 이름이 실제 엑셀과 같은지 프로그램이 확인했습니다. "
                "틀린 행은 '시트'·'행'·'단위'를 고치거나 맨 아래에 추가하고 **다시 계산**을 누르세요 (Claude를 다시 부르지 않습니다).")
        map_df = pd.DataFrame([{
            "item": c.item, "설명": financials.ITEMS[c.item][0], "시트": c.sheet, "행": c.row, "단위": c.unit,
            "Claude가 말한 이름": c.label, "엑셀의 행 내용": c.found, "상태": STATUS[c.status], "메모": c.note,
        } for c in sorted(ex.checks, key=lambda c: c.status not in (UNVERIFIED, MISSING))], columns=["item", "설명", "시트", "행", "단위", "Claude가 말한 이름", "엑셀의 행 내용",
                                        "상태", "메모"])
        edited_map = st.data_editor(
            map_df, key=f"fin_map_{fin_run}", hide_index=True, use_container_width=True, num_rows="dynamic",
            column_config={
                "item": st.column_config.SelectboxColumn("항목", options=list(financials.ITEM_KEYS), required=True),
                "시트": st.column_config.SelectboxColumn("시트", options=list(wb.sheets)),
                "행": st.column_config.NumberColumn("행", min_value=1, step=1),
                "단위": st.column_config.SelectboxColumn("단위", options=list(financials.UNITS)),
            },
            disabled=["설명", "Claude가 말한 이름", "엑셀의 행 내용", "상태", "메모"],
        )
        with st.expander("기간 헤더 (Claude가 찾은 위치)"):
            time_df = st.data_editor(
                pd.DataFrame([t.model_dump() for t in mapping.timelines],
                             columns=["sheet", "header_row", "first_col", "last_col", "granularity"]),
                key=f"fin_time_{fin_run}", hide_index=True, use_container_width=True, num_rows="dynamic",
                column_config={
                    "sheet": st.column_config.SelectboxColumn("시트", options=list(wb.sheets)),
                    "header_row": st.column_config.NumberColumn("헤더 행", min_value=1, step=1),
                    "first_col": "첫 기간 열", "last_col": "마지막 기간 열",
                    "granularity": st.column_config.SelectboxColumn("단위", options=["month", "quarter", "year"]),
                })

        c1, c2 = st.columns(2)
        if c1.button("수정한 매핑으로 다시 계산"):
            before = {(r.item, r.sheet, r.row, r.unit) for r in mapping.items}
            items, edited = [], set(state.get("fin_edited", set()))
            for r in edited_map.to_dict("records"):
                if not r.get("item") or not r.get("시트") or pd.isna(r.get("행")):
                    continue
                key = (r["item"], r["시트"], int(r["행"]), r.get("단위") or "units")
                if key not in before:
                    edited.add(r["item"])
                label = r["Claude가 말한 이름"] if key in before else wb.row_text(r["시트"], int(r["행"]))
                items.append(financials.ItemRef(item=key[0], sheet=key[1], row=key[2], label=label or "", unit=key[3]))
            timelines = [financials.Timeline(sheet=t["sheet"], header_row=int(t["header_row"]),
                                             first_col=str(t["first_col"]), last_col=str(t["last_col"]),
                                             granularity=t["granularity"])
                         for t in time_df.to_dict("records")
                         if t.get("sheet") and not pd.isna(t.get("header_row")) and t.get("first_col")
                         and t.get("last_col") and t.get("granularity")]
            state["fin_mapping"] = mapping.model_copy(update={"items": items, "timelines": timelines})
            state["fin_edited"] = edited
            state["fin_run"] = fin_run + 1
            st.rerun()
        stem = re.sub(r"\.xls[xm]$", "", wb.name, flags=re.I)
        c2.download_button("정리된 표 다운로드 (.xlsx)", _xlsx(tables, formulas, map_df.drop(columns=["item"])),
                           file_name=f"{stem} metrics.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           disabled=not tables)

    fin_analyst: Analyst | None = state.get("fin_analyst")
    if fin_analyst and fin_analyst.usage.calls:
        u = fin_analyst.usage
        st.caption(f"{MODEL} · 호출 {u.calls}회 · 입력 {u.input_tokens + u.cache_read + u.cache_write:,} 토큰 · "
                   f"출력 {u.output_tokens:,} 토큰 · 대략 ${u.cost:.3f}")
