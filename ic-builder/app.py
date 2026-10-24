"""Local web app: old IC deck + new fund materials -> new IC deck.

    streamlit run app.py
"""

import re
from pathlib import Path

import anthropic
import streamlit as st
from dotenv import load_dotenv

import pipeline
from sources import Source, load

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
load_dotenv(HERE.parent / "tracker" / ".env")  # reuse the tracker's key if this folder has none

st.set_page_config(page_title="IC 자료 만들기", page_icon="📑")
st.title("IC 자료 만들기")
st.caption("기존 IC 자료의 구조·디자인은 그대로 두고, 새 펀드 자료의 내용으로 채웁니다. "
           "자료에 근거가 없는 칸은 빨간 [확인 필요]로 남깁니다.")

template_file = st.file_uploader("1) 기존 IC 자료 (.pptx)", type=["pptx"])
source_files = st.file_uploader("2) 새 펀드 자료 (.pptx / .pdf, 여러 개 가능)", type=["pptx", "pdf"],
                                accept_multiple_files=True)
notes = st.text_area("추가 메모 (선택)", placeholder="자료에 없는 사실이나 우리 팀 의견이 있으면 적어 주세요. 이것도 근거로 씁니다.")

if st.button("생성", type="primary", disabled=not (template_file and (source_files or notes.strip()))):
    try:
        sources = [load(f.name, f.getvalue()) for f in source_files]
        if notes.strip():
            sources.append(Source("분석가 메모", "notes", notes.strip()))
        bar = st.progress(0.0, text="자료를 읽는 중…")

        def progress(done, total, stage):
            label = "슬라이드 채우는 중" if stage == "fill" else "새 슬라이드 만드는 중"
            bar.progress(done / max(total, 1), text=f"{label}… {done}/{total}")

        result = pipeline.run(template_file.getvalue(), sources, on_progress=progress)
        bar.progress(1.0, text="완료")
        st.session_state["result"] = result
        st.session_state["filename"] = Path(template_file.name).stem
    except ValueError as e:
        st.error(str(e))
    except anthropic.AuthenticationError:
        st.error("ANTHROPIC_API_KEY가 없거나 잘못되었습니다. ic-builder/.env를 확인해 주세요.")
    except anthropic.BadRequestError as e:
        if "usage limit" in str(e).lower() or "credit balance" in str(e).lower():
            st.error("Anthropic API 사용 한도에 도달했습니다. platform.claude.com의 Limits/Billing에서 한도를 올리거나 충전해 주세요.\n\n" + str(e))
        else:
            st.error(f"Claude API 오류: {e}")
    except anthropic.APIError as e:
        st.error(f"Claude API 오류: {e}")
    except RuntimeError as e:
        st.error(str(e))

result = st.session_state.get("result")
if result:
    st.subheader(f"{result.old_fund} → {result.new_fund}")
    c1, c2, c3 = st.columns(3)
    c1.metric("채운 문단", result.filled)
    c2.metric("[확인 필요]", sum(result.marked.values()))
    c3.metric("업데이트 필요 차트/이미지", sum(result.flagged_visuals.values()))
    if result.deleted_rows:
        st.write(f"새 펀드에 해당 내용이 없는 표 행 {result.deleted_rows}개를 지웠습니다.")
    if result.new_slides:
        st.success("새로 추가한 슬라이드 ([신규 슬라이드] 표시):\n\n" + "\n".join(
            f"- 슬라이드 {n}: {topic}" for n, topic in result.new_slides))
    if result.demoted:
        st.info(f"Claude가 쓴 문단 중 {result.demoted}개는 숫자나 인용이 원자료에서 확인되지 않아 [확인 필요]로 바꿨습니다.")
    if result.leftovers:
        st.warning("기존 펀드 이름이 아직 남아 있습니다:\n\n" + "\n".join(
            f"- 슬라이드 {n} ({where}): {term}" for n, term, where in result.leftovers))
    if result.marked or result.flagged_visuals:
        pages = sorted(set(result.marked) | set(result.flagged_visuals))
        st.write("확인할 슬라이드: " + ", ".join(
            f"{n} ({result.marked.get(n, 0)}칸" + (f", 시각자료 {result.flagged_visuals[n]}" if n in result.flagged_visuals else "") + ")"
            for n in pages))
    st.download_button("새 IC 자료 다운로드 (.pptx)", result.pptx,
                       file_name=re.sub(r'[\\/:*?"<>|]', "", result.new_fund or st.session_state["filename"]) + " IC.pptx",
                       mime="application/vnd.openxmlformats-officedocument.presentationml.presentation")
