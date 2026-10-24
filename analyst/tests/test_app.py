from pathlib import Path

import anthropic
from streamlit.testing.v1 import AppTest

from test_analyst import FakeClient

APP = str(Path(__file__).parent.parent / "app.py")


def test_app_walks_through_all_steps(monkeypatch):
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: FakeClient())
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert at.button[0].disabled  # nothing to analyse yet

    at.text_input[0].input("acme.ai").run()
    at.button[0].click().run()
    assert not at.exception
    assert any("아래 회사 분류가 맞는지 확인하세요" in i.value for i in at.info)
    assert at.session_state["analyst"].url == "https://acme.ai"

    next(b for b in at.button if "경쟁사 찾기" in b.label).click().run()
    assert any("아래 경쟁사 목록이 맞는지 확인하세요" in i.value for i in at.info)
    profile = next(b for b in at.button if "프로필 수집" in b.label)
    assert "1개 회사" in profile.label  # the unverified candidate starts unchecked

    profile.click().run()
    assert not at.exception
    assert [s.value for s in at.subheader][-2:] == ["③ 경쟁사 비교", "④ 투자 메모"]
    assert any(":red[**확인 필요**]" in m.value for m in at.markdown)
    assert at.session_state["memo"].flagged == 1
