# 스타트업 분석

회사 URL(과 선택으로 Pitch Deck)을 넣으면 **회사 분류 → 경쟁사 → 투자 메모** 순서로 분석합니다.
Google Docs "AI 기반 스타트업 분석 에이전트 만들기"의 Analysis 블록 중 앞의 세 에이전트(Company Classification, Competitor Analyst, Investment Memo Writer)를 구현한 첫 버전입니다.
내 PC에서만 돌고, Pitch Deck은 Claude API로만 보내집니다.

## 처음 한 번

```bash
cd analyst
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
```

`.env.example`을 `.env`로 복사해 `ANTHROPIC_API_KEY`를 넣습니다. `.env`가 없으면 `tracker/.env`의 키를 씁니다.

## 실행

```bash
.venv/Scripts/streamlit run app.py
```

1. 회사 URL을 넣고(필요하면 Pitch Deck도 올리고) **분석하기**를 누릅니다.
2. **① 회사 분류** 표를 확인합니다. 틀린 값은 '값' 칸에서 바로 고치고 **확인 → 경쟁사 찾기**를 누릅니다.
3. **② 경쟁사 목록**에서 뺄 회사의 체크를 풀고, 빠진 회사는 맨 아래 행에 추가한 뒤 **확인**을 누릅니다.
4. **③ 경쟁사 비교**와 **④ 투자 메모**가 나옵니다. 메모(.md)와 전체 결과(.json, 값마다 근거 포함)를 내려받을 수 있습니다.

## 결과물 읽는 법

- **✅ 확인됨**: Claude가 붙인 인용문이 실제로 읽은 페이지나 Pitch Deck에 있고, 값의 숫자와 이름이 인용문에 들어 있습니다.
- **🔴 확인 필요 / 빨간 [확인 필요]**: Claude가 값을 썼지만 위 검사를 통과하지 못했습니다. 'Claude가 쓴 값' 칸과 근거를 보고 직접 확인하세요.
- **⚪ Not Found**: 공개 자료에서 찾지 못했습니다.
- **✏️ 직접 수정**: 분석가가 고친 값입니다. 다음 단계에서는 확인된 값으로 씁니다.
- 메모에서 확인된 사실에 없는 숫자가 나온 줄에는 끝에 [확인 필요]가 붙습니다.
- 메모의 Market, Founder, Moat, Customer Traction, Financial 섹션은 아직 "아직 분석하지 않음"으로 나옵니다.

## 동작 방식

1. `research.py`: 단계마다 Claude(`claude-haiku-5-5`)가 웹검색과 웹페이지 읽기로 조사하고, 인용한 원문 구절을 모두 모아 둡니다. 그다음 도구 없이 그 구절만 보고 JSON을 채웁니다.
2. `verify.py`: 값마다 붙은 인용문이 모아 둔 원문에 실제로 있는지, 값의 숫자와 이름이 인용문에 있는지 검사합니다. 실패하면 [확인 필요]로 바꿉니다. 숫자 검사는 `ic-builder`와 같습니다.
3. `pipeline.py`: 분류 → (확인) → 경쟁사 목록 → (확인) → 경쟁사별 프로필(4개씩 동시에) → 메모.
4. `prompts/`: Google Docs의 에이전트 프롬프트입니다. 이 파일만 고쳐도 분석 방식이 바뀝니다.

다음 에이전트(Market, Founder, Customer Traction, Moat, Financial)는 `prompts/`에 파일 하나와 `pipeline.py`에 단계 하나를 더하는 식으로 붙입니다.

## Financials 탭 (Growth Equity Financial Analyst)

Series C/D 회사가 보낸 financials 엑셀(.xlsx/.xlsm)을 올리면 growth equity 지표를 **기간별 표 + line chart**로 정리합니다.

1. **Financials** 탭에서 엑셀을 올리고 **엑셀 정리하기**를 누릅니다.
2. Claude(Haiku, 1회 호출)는 "어느 시트 몇 행이 ARR / New Logo ARR / S&M 비용 …인지"와 기간 헤더 위치만 찾습니다. 숫자는 만들지 않습니다.
3. 프로그램이 그 행의 이름이 실제 엑셀과 같은지 확인한 뒤 셀 값을 그대로 읽고, `metrics.py`의 공식(Google Docs 그대로)으로 ARR Growth, GDR, NDR, Quick Ratio, Magic Number, Rule of 40, LTV/CAC, CAC Payback, Burn Multiple, Runway, Gross/FCF Margin 등을 계산합니다.
4. 엑셀에 입력값이 없는 지표는 표와 차트에 나오지 않습니다. 좋다/나쁘다 판단이나 벤치마크 비교는 하지 않습니다.
5. **항목 매핑** 표에서 잘못 잡힌 행을 고치고 **다시 계산**을 누르면 Claude를 다시 부르지 않고 재계산합니다. 정리된 표는 .xlsx로 내려받습니다.

회사 엑셀은 기밀이라 `.gitignore`가 `analyst/` 안의 `.xlsx`/`.xlsm`을 막습니다. 실제 샘플 파일 자리는 `tests/fixtures/`입니다 (아직 비어 있음).

## 비용과 시간

화면 맨 아래에 이번 분석의 호출 수, 웹검색 수, 토큰, 대략적인 비용이 나옵니다.

기본 모델은 비용이 가장 싼 `claude-haiku-5-5`입니다. 처음 테스트할 때 Opus 5.5로 돌린 회사 분류 1회가 $1.5~3.2였고, Haiku는 토큰 단가가 그 약 40분의 1입니다. 웹검색은 모델과 관계없이 1회 $0.01입니다.

다른 모델을 쓰려면 `.env`에 `ANALYST_MODEL=...`을 넣습니다. 더 비싼 모델은 비용이 크게 늘어나니 필요할 때만 쓰세요.

## 보안

이 레포는 public입니다. `.gitignore`가 `analyst/` 안의 `.pdf`, `.pptx`, `.env`를 막고 있지만, Pitch Deck은 이 폴더 밖에 두는 것을 권장합니다.

## 테스트

```bash
.venv/Scripts/python -m pytest
```
