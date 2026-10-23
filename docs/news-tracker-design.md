# News Tracker — 시스템 설계

> 출처: Google Docs "AI Agent 만들기"의 Claude Code 프롬프트 (VC Fundraising Tracker)
> 상태: 1단계 구현 완료 (`tracker/`), 로컬에서 실제 시트에 10건 기록 확인 · 2026-10-23
> 추출 모델은 Gemini에서 **Claude Haiku 5.5**로 바뀌었다 (10절 참고). 아래 3.3절 등의 "Gemini"는 처음 설계할 때 기준이다.

---

## 1. 한 줄 요약

GitHub Actions가 매일 Python 스크립트를 실행 → TechCrunch / PR Newswire RSS에서 새 기사 수집 → Gemini가 펀딩 정보만 JSON으로 추출 → Google Sheets에 **추가만(append)** 한다.
기존 Deal Flow 웹사이트(Vercel)는 이 시트를 **읽어서 보여주는 화면**으로 바뀐다.

```
 ┌──────────────── GitHub Actions (매일 7am PT, 무료) ────────────────┐
 │                                                               │
 │  1. News Collector  →  2. Article Parser  →  3. Fundraise     │
 │     (RSS 수집,           (기사 본문           Extractor        │
 │      키워드 필터,          가져오기)            (Gemini JSON)    │
 │      URL 중복 제거)                              │             │
 │                                                  ▼             │
 │                                       4. Spreadsheet Updater   │
 │                                          (이벤트 중복 제거,       │
 │                                           append only)         │
 └──────────────────────────────────────────────┬────────────────┘
                                                ▼
                         Google Sheet "VC Fundraising Tracker"
                         ├─ Raw Database        (펀딩 이벤트 1건 = 1행)
                         ├─ Funding Dashboard   (수식으로 자동 생성)
                         ├─ Article Log         (처리한 기사 기록)
                         └─ 3-Month Alerts      (수식, 2단계)
                                                ▲
                                                │ 읽기 전용
                         Deal Flow 웹사이트 (Vercel) — api/fetch-funding.js
```

---

## 2. 확인한 사실 (2026-10-22 기준 실제 요청으로 확인)

| 소스 | RSS URL | 항목 수 | 커버 기간 |
|---|---|---|---|
| TechCrunch – Funding 태그 | `https://techcrunch.com/tag/funding/feed/` | 20 | 며칠 (일부 오래된 고정 글 섞임) |
| TechCrunch – Venture 카테고리 | `https://techcrunch.com/category/venture/feed/` | 20 | 며칠 |
| PR Newswire – Venture Capital | `https://www.prnewswire.com/rss/financial-services-latest-news/venture-capital-list.rss` | 20 | **약 34시간** |

**설계에 미치는 영향:** PR Newswire 피드는 20개 항목이 약 하루 반 분량이다. 하루 1번만 돌다가 한 번 실패하면 기사를 놓친다. → **하루 2번(예: 한국시간 오전 7시, 오후 7시) 실행**하고, 중복은 기사 URL로 걸러낸다.

PR Newswire VC 피드에는 ETF 출시, ARR 발표 같은 펀딩 외 보도자료도 섞여 있다. 그래서 키워드 필터 + Gemini 판별을 둘 다 거친다.

---

## 3. 컴포넌트별 설계

### 3.1 News Collector (`collector.py`)
- 소스 목록은 `config.yaml`에 둔다. 나중에 Crunchbase News / VentureBeat를 다시 넣을 때 코드를 고칠 필요가 없다.
- `feedparser`로 RSS를 파싱한다.
- **1차 필터 (무료):** 제목과 요약에 다음 키워드 중 하나라도 있어야 통과한다.
  `raised, raises, funding, fundraise, fundraising, series a/b/c/d/e, seed round, pre-seed, venture funding`
- **기간 필터:** 발행 후 48시간 이내 (설정 가능). 문서에는 "24시간"이라고 되어 있지만, 실행 시각이 어긋나도 놓치지 않게 여유를 둔다. 중복은 URL로 제거되므로 결과는 같다.
- **URL 중복 제거:** `Article Log` 시트에 이미 있는 URL(쿼리스트링, utm 제거 후 비교)은 건너뛴다.
- **처리 전에 메타데이터 저장:** 새 기사를 `Article Log`에 `status=pending`으로 먼저 기록한다. 프롬프트의 "Store article metadata before processing" 요구사항이다. 중간에 실패해도 다음 실행에서 `pending`/`error` 상태부터 다시 처리할 수 있다.

### 3.2 Article Parser (`parser.py`)
- 기사 URL을 가져와 본문을 추출한다 (`trafilatura` 사용, 실패하면 RSS 요약으로 대체).
- Gemini 비용을 줄이려고 본문은 앞부분 약 6,000자만 보낸다. 펀딩 정보는 거의 항상 첫 몇 문단에 있다.

### 3.3 Fundraise Extractor (`extractor.py`) — Gemini
- Gemini API의 **구조화 출력(response schema)** 으로 항상 같은 형태의 JSON을 받는다.
- 모델: Flash 계열의 최신 버전. 정확한 모델 ID는 구현할 때 Gemini 문서에서 확인하고 `config.yaml`에 둔다.
- 출력 스키마 (프롬프트 예시 + 필터링용 필드 2개 추가):

```json
{
  "is_funding_event": true,
  "company": "Eka Robotics",
  "round": "Series A",
  "amount": "$150M",
  "date": "2026-10-20",
  "lead_investor": "a16z",
  "other_investors": ["F-Prime"],
  "category": "Robotics"
}
```

- `is_funding_event=false`이면 저장하지 않는다 (인수, 파트너십, 제품 출시, 인사 발표 등). `Article Log`에는 `skipped_not_funding`으로 남긴다.
- 기사 하나에 여러 회사의 펀딩이 나오면 (예: 주간 펀딩 정리 기사) 배열로 받고 각각 한 행씩 저장한다.
- **라운드 정규화 (Python에서 처리):** Gemini가 준 값을 다음 중 하나로 맞춘다.
  `Pre-Seed · Seed · Series A · Series B · Series C · Series D · Series E+ · Growth · Late Stage · Undisclosed`
  (Series E/F/G… → `Series E+`)
- **금액 정규화:** `$150M`, `$1.2B` 형식. 원화·유로 등은 원래 통화 그대로 둔다 (예: `€20M`). 공개되지 않았으면 `Undisclosed`.
- **날짜:** 기사에 날짜가 없으면 기사 발행일을 쓴다.
- 비용: 하루 펀딩 후보 기사는 30~60건 정도라서 Gemini 무료 사용량 안에 들어갈 가능성이 높다. 분당 호출 제한에 걸리지 않게 호출 사이에 간격을 두고, 실패하면 재시도한다.

### 3.4 Spreadsheet Updater (`sheets.py`)
- 인증: Google Cloud **서비스 계정** JSON 키를 쓴다. 시트를 서비스 계정 이메일에 "편집자"로 공유한다.
- **이벤트 중복 제거 (프롬프트 규칙):** `정규화된 회사명 + 라운드 + 발표일`이 같으면 건너뛴다.
  - 회사명 정규화: 소문자로 바꾸고 `Inc, Labs, AI, Robotics` 같은 접미사를 떼고 공백과 기호를 제거한다. (`Eka` = `Eka Robotics`)
- **append만 한다.** 기존 행을 수정하거나 삭제하는 코드 경로가 아예 없게 만든다.
- 한 번 실행할 때 모은 행을 `append_rows` 한 번으로 저장한다 (API 호출 횟수 최소화).

---

## 4. Google Sheet 구조

### Sheet 1: `Raw Database` (프롬프트 그대로)
| Date | Company | Funding Round | Amount Raised | Lead Investor | Other Investors | Category | Source | Article URL | Date Added |
|---|---|---|---|---|---|---|---|---|---|
| 2026-10-20 | Eka Robotics | Series A | $150M | a16z | F-Prime | Robotics | TechCrunch | https://… | 2026-10-20 |

### Sheet 2: `Funding Dashboard` — 수식으로 자동 생성
열마다 `FILTER` 수식 하나씩 둔다. Raw Database에 행이 추가되면 대시보드가 바로 따라 바뀌고, Python은 이 시트를 건드리지 않는다. 따라서 "기존 기록을 덮어쓰지 않는다"는 조건을 구조적으로 지킨다.

```
A1: Pre-Seed     A2: =IFERROR(SORT(FILTER('Raw Database'!B2:B & " (" & TEXT('Raw Database'!A2:A,"yyyy-mm-dd") & ")", 'Raw Database'!C2:C="Pre-Seed"), 1, FALSE), "")
B1: Seed         B2: (같은 수식, "Seed")
...
G1: Series E+    G2: (같은 수식, "Series E+")
```
(최신 날짜가 위로 오도록 날짜 기준 정렬)

### Sheet 3: `Article Log` (추가 — 중복 제거 및 재처리용)
| Article URL | Source | Title | Published | Fetched At | Status | Error |
|---|---|---|---|---|---|---|

`Status`: `pending` → `extracted` / `skipped_not_funding` / `error`

### Sheet 4: `3-Month Alerts` (2단계 — 원래 계획 문서의 "3 month Alert")
수식으로 펀딩 후 90~97일이 지난 회사를 보여준다. 이메일·Slack 알림은 3단계에서 붙인다.

### 데이터가 너무 많아지지 않을까? (문서에 있던 질문)
- 예상량: 하루 펀딩 이벤트 10~30건 → 1년 약 4,000~11,000행 × 10열 ≈ 11만 셀.
- Google Sheets 한도는 시트 하나당 1,000만 셀이다. **몇 년 써도 Sheets로 충분하다.**
- Supabase(Postgres)는 웹사이트에서 복잡한 검색이나 점수화(Opportunity Scoring)가 필요해질 때 옮기면 된다. Updater를 인터페이스로 분리해 두면 저장소만 바꿔 끼울 수 있다.

---

## 5. 실행 환경 — "손대지 않아도 매일 돌아가는가?"

| 선택지 | 장점 | 단점 | 결론 |
|---|---|---|---|
| **GitHub Actions cron** | 무료(비공개 저장소 월 2,000분, 이 작업은 월 약 100분), Python 그대로 실행, Secrets 관리 내장, 실패하면 이메일로 알림 | 실행 시각이 몇 분 늦을 수 있음 | ✅ **채택** |
| Vercel Cron + Python 함수 | 웹사이트와 같은 곳에서 관리 | Hobby 플랜은 cron이 하루 1회로 제한되고 함수 실행 시간 제한이 있음. 기사 수십 개를 처리하다 시간 초과 위험 | ❌ |
| 내 PC 작업 스케줄러 | 간단함 | PC가 꺼져 있으면 실행 안 됨 | ❌ |

- `.github/workflows/news-tracker.yml`: ~~UTC 22:00, 10:00 (한국시간 07:00, 19:00)~~ → 매일 **07:00 PT** 1회 (10절 참고) + 수동 실행 버튼 `workflow_dispatch`.
- Secrets: `GEMINI_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `SHEET_ID`.
- 주의: **공개** 저장소는 60일 동안 활동이 없으면 예약 실행이 자동으로 꺼진다. 비공개 저장소로 쓰는 것을 권장한다.
- 실패 처리: 소스 하나가 실패해도 나머지는 계속 진행한다. 실행이 끝나면 요약(수집 N / 펀딩 M / 저장 K / 오류 E)을 Actions 로그에 남긴다. 오류가 있으면 job을 실패로 표시해 GitHub이 이메일을 보내게 한다.

---

## 6. 프로젝트 구조

기존 Deal Flow 저장소 안에 `tracker/` 폴더를 따로 만든다. 웹사이트(Node/Vercel)와 수집기(Python/GitHub Actions)가 같은 저장소에 있지만 서로 영향을 주지 않는다.

```
Deal Flow/
├── index.html                  # 기존 웹사이트 (2단계에서 시트 데이터를 보여주도록 수정)
├── api/fetch-funding.js        # 기존 → 2단계에서 "시트 읽기"로 변경
├── vercel.json
├── .vercelignore               # 새로 추가: tracker/ 를 Vercel 배포에서 제외
├── .github/workflows/
│   └── news-tracker.yml        # 매일 7am PT 실행
├── docs/
│   └── news-tracker-design.md  # 이 문서
└── tracker/
    ├── README.md               # 설정 방법 (서비스 계정, 시트 공유, Secrets)
    ├── requirements.txt        # feedparser, trafilatura, google-genai, gspread, pyyaml, python-dotenv
    ├── .env.example
    ├── config.yaml             # 소스 URL, 키워드, 조회 기간, 모델 ID
    ├── main.py                 # 전체 흐름 실행 (--dry-run 지원)
    ├── collector.py
    ├── parser.py
    ├── extractor.py
    ├── normalize.py            # 라운드, 금액, 회사명 정규화 + 중복 판단 키 생성
    ├── sheets.py
    ├── setup_sheet.py          # 최초 1회: 시트 4개 + 헤더 + 대시보드 수식 생성
    └── tests/
        ├── fixtures/           # 실제 RSS, 기사 샘플
        └── test_normalize.py   # 중복 판단 규칙 테스트 (Eka 예시 포함)
```

---

## 7. 기존 코드(`api/fetch-funding.js`)와의 관계

### 지금 상태 분석
| 항목 | 현재 방식 | 문제 |
|---|---|---|
| 소스 | TechCrunch Venture, Crunchbase News, VentureBeat RSS | PR Newswire 없음 |
| 추출 | 정규식 (`/series[\s-]?a/i`, 제목에서 "raises" 앞부분을 회사명으로 사용) | Seed, A, B만 인식. "series about"처럼 엉뚱한 단어도 걸림. 투자자를 추출하지 못함. 인수 기사도 섞임 |
| 저장 | 브라우저 `localStorage` | 다른 기기나 브라우저에서는 데이터가 안 보임. 브라우저 데이터를 지우면 사라짐. 문서에 적힌 "매번 다시 검색" 문제의 원인 |
| 실행 | 사용자가 "Get an Update" 버튼을 누를 때만 | 자동 실행 아님 |
| 커밋 안 된 변경 | `SEVEN_DAYS_AGO`를 7일 → 180일로 변경 | RSS는 최근 20개뿐이라 180일로 늘려도 실제로 더 가져오지 못함. 변수 이름도 실제 값과 맞지 않음 |

### 연결 방식 (단계별)
1. **1단계 — 독립 실행:** Python 트래커만 만든다. 기존 웹사이트는 그대로 두고 두 결과를 나란히 비교한다. 정확도 검증 기간이다.
2. **2단계 — 웹사이트가 시트를 읽음:** `api/fetch-funding.js`의 내용을 "Google Sheets API로 `Raw Database`를 읽어 JSON으로 반환"하도록 바꾼다.
   - 응답 형태(`{ company, stage, amount, url, pubDate, articleUrl }`)를 그대로 유지하면 `index.html`은 거의 고치지 않아도 된다. 필드 매핑: `stage←Funding Round`, `pubDate←Date`, `articleUrl←Article URL`. 투자자와 카테고리 필드는 추가한다.
   - 정규식 추출 코드와 RSS 수집 코드는 삭제한다. Python 트래커가 유일한 수집기가 된다.
   - `index.html`: `localStorage` 병합 로직을 제거하고(시트가 원본), 필터에 Pre-Seed / C / D / E+를 추가하고, 투자자 열을 추가한다.
   - Vercel 환경변수에 같은 서비스 계정 키를 넣는다 (읽기 전용 권한이면 충분).
3. **3단계:** 3-Month Alerts 알림 보내기, 회사를 클릭하면 이메일 초안 자동 작성, Opportunity Scoring.

이렇게 하면 원래 문서의 목표인 "Claude에 종속되지 않는 시스템"과 Interview Simulator의 문제들(Claude API 비용, 저장 안 됨)도 함께 해결된다. 실행 비용은 Gemini 무료 사용량 + GitHub Actions 무료 + Google Sheets 무료다.

---

## 8. 구현 순서 (프롬프트의 "한 번에 하나씩")

1. `setup_sheet.py` + `sheets.py` → 빈 시트 생성, 테스트 행 1개 append
2. `collector.py` → `--dry-run`으로 후보 기사 목록만 출력
3. `parser.py` + `extractor.py` → 실제 기사 5개로 JSON 추출 품질 확인
4. `normalize.py` + 테스트 → 중복 규칙 검증
5. `main.py` 전체 연결 → 로컬에서 1회 실제 실행
6. GitHub Actions 등록 → 수동 실행 1회, 다음 날 자동 실행 확인
7. (2단계) `api/fetch-funding.js`, `index.html` 전환

---

## 9. 확정된 결정 (2026-10-22)

1. **소스:** TechCrunch(Funding 태그 + Venture 카테고리), PR Newswire, Crunchbase News를 쓴다. VentureBeat도 유지하기로 했지만 현재 피드가 봇 차단 확인 화면(HTTP 429)을 돌려줘서 `config.yaml`에서 `enabled: false`로 꺼 두었다. 차단 우회는 하지 않는다.
2. **중복 판단:** ~~±7일~~ → 같은 회사 + 같은 라운드가 **365일** 안에 있으면 중복으로 본다 (`dedup_window_days: 365`, 2026-10-23 변경). 새 라운드는 새 행으로 추가한다.
3. **저장소:** ~~비공개~~ → 기존 공개 저장소 `lylak20/Deal-Flow`를 그대로 쓴다 (2026-10-23 변경). 공개 저장소는 60일 동안 커밋이 없으면 예약 실행이 자동으로 꺼진다. 그러면 Actions 탭에서 다시 켜야 한다. 키는 GitHub Secrets에만 둔다.
4. **추출 모델:** ~~`gemini-3.8-flash`~~ → **`claude-haiku-5-5`** (2026-10-23 변경, 10절 참고). `config.yaml`의 `llm.provider`로 Gemini로 되돌릴 수 있다.
5. **준비물 (직접 해야 하는 것):** Anthropic API 키 발급, Google Cloud 서비스 계정 생성, 시트 공유. 단계별 방법은 [tracker/README.md](../tracker/README.md)에 있다.

## 10. 구현하면서 바뀐 점
- **IPv4 전용 연결 (`http.ipv4_only`):** 개발 PC는 IPv6가 막혀 있어서 요청마다 약 40초씩 지연됐다. IPv4만 쓰도록 바꾼 뒤 수집 시간이 2분에서 3초로 줄었다.
- **추출 모델을 Gemini에서 Claude Haiku 5.5로 변경 (2026-10-23):** Gemini 무료 등급은 `gemini-3.8-flash` 기준 하루 20회까지만 호출할 수 있어서, 첫 실행(후보 13건 + 재시도)에서 한도를 다 썼다. Haiku 5.5($0.10 / $0.50 per MTok, effort `low`)는 기사 1건에 약 $0.0005, 한 달 약 $0.3로 예상된다. 첫 실행에서 13건 중 펀딩 기사 10건을 추출했고, 펀딩이 아닌 3건은 걸러냈다. 원래 목표인 "특정 회사에 종속되지 않기"는 `llm.provider` 설정으로 유지한다. Claude와 Gemini 구현이 모두 `extractor.py`에 있다.
- **한도 초과 처리:** 하루 한도가 다 찼거나(Gemini) 크레딧이 떨어지면(Claude) 실행을 즉시 멈춘다. 남은 기사는 `pending` 상태로 다음 실행에 넘기고, job을 실패로 표시해 이메일 알림을 보낸다.
- **대시보드에 안 나오는 라운드:** 라운드가 `Undisclosed`(예: DeepSeek, Warhound)이거나 `Growth`/`Late Stage`인 행은 Raw Database에는 있지만, 프롬프트에서 정한 대시보드 7개 열에는 나오지 않는다.
- **Email 버튼 추가 (2026-10-23, 원래 계획의 "Automate sending out emails" 1단계):** Raw Database의 Company 왼쪽(B열)에 `✉️ Email` 열을 추가했다. 모든 행에 같은 수식이 들어가고, 각 수식은 `ROW()`로 자기 행의 회사명·라운드·금액을 읽는다. 클릭하면 Gmail 작성 창(`mail.google.com/mail/?view=cm`)이 제목과 본문이 채워진 채로 열린다. 문구는 `Email Template` 탭에서 고친다. 프로그램은 데이터를 RAW로 append한 뒤 새 행의 B열에만 수식을 쓴다. Raw Database의 열 위치는 이제 `RAW_HEADERS`에서 계산한다. 기존 시트는 `setup_sheet.py`를 실행하면 열이 추가되고 버튼이 채워진다. 실제로 메일을 보내는 것은 사람이 한다. 받는 사람 주소(창업자 이메일)는 아직 수집하지 않는다.
- **실행 일정을 하루 1회 07:00 PT로 변경 (2026-10-23):** GitHub cron은 UTC 기준이라 14:00 UTC(PDT일 때 07:00)와 15:00 UTC(PST일 때 07:00)를 둘 다 예약한다. 그리고 첫 단계에서 오늘 LA 시간대에 맞는 쪽만 실행한다. 비교 기준이 실행된 시각이 아니라 예약 문자열(`github.event.schedule`)이라서 GitHub 실행이 늦어져도 하루에 정확히 한 번 돈다. 이전 기록은 지우지 않고 새 회사만 아래에 추가한다. 같은 라운드가 나중 기사에서 다시 나와도 추가되지 않도록 중복 판단 기간을 365일로 늘렸다.
  - **예약 실행 누락에 대비 (2026-10-23):** 첫날 14:00 UTC 예약 실행이 GitHub에서 아예 시작되지 않았다(예약 실행 기록 0건). GitHub 예약 실행은 보장되지 않고, 사용량이 몰리는 매시 정각에 특히 늦어지거나 빠진다. 그래서 예약을 14:00, 15:00, 15:30, 16:30 UTC 네 개로 늘렸다. 첫 단계는 다음 조건을 모두 만족할 때만 실행한다: LA 시간으로 07:00 이후이고, 오늘(PT 날짜) 앞선 예약 실행 중 "Run tracker" 단계를 성공한 것이 없을 때. 결과적으로 07:00 실행이 빠지거나 실패하면 다음 예약이 대신 실행하고, 성공한 뒤에는 나머지가 몇 초 만에 건너뛴다. 이 확인에는 `GITHUB_TOKEN`의 `actions: read` 권한을 쓴다.
  - **알려진 위험:** PR Newswire RSS는 페이지 기능 없이 최근 20건(약 34~38시간)만 준다. 하루 1회 실행이면 평소에는 충분하지만, 보도자료가 몰리는 날 24시간 동안 20건을 넘으면 넘친 기사는 놓친다.
- **Claude → Gemini 자동 대체 (2026-10-23):** Anthropic 월 사용 한도(spend limit)가 다 차서 Claude 호출이 2026-11-01까지 막혔다. 그래서 `llm.fallbacks: [gemini]`를 추가했다. Claude 호출이 `QuotaExhausted`(크레딧 부족, 사용 한도 도달)를 내면, 그 실행의 나머지 기사는 Gemini 무료 등급으로 처리한다. 기사마다 Claude를 다시 시도하지 않는다. 다음 실행은 다시 Claude부터 시도하므로, 한도를 올리면 자동으로 Claude로 돌아간다. 대체 모델 키가 없으면 경고만 남기고 건너뛴다. 실제 실행에서 대기 중이던 Higgsfield 기사를 Gemini가 처리해 11번째 행으로 추가한 것을 확인했다.
- **같은 라운드의 후속 보도는 행을 교체 (2026-10-23):** 프롬프트의 "append only" 규칙을 사용자 요청으로 한 군데 바꿨다. 새 이벤트가 기존 행과 같은 라운드(같은 회사, 같은 라운드 또는 한쪽이 `Undisclosed`, 365일 이내)이면서 기존 행에 없던 정보(라운드 이름, 금액, 밸류에이션, 리드·참여 투자자)를 담고 있으면 둘을 합친다. 합칠 때는 새 값이 우선이고, 새 소식에 없는 값은 기존 행에서 가져온다. 합친 행을 맨 아래에 추가한 **뒤** 기존 행을 삭제하므로 중간에 실패해도 데이터는 남는다. 새 정보가 없으면 건너뛴다. 다른 라운드는 그대로 새 행이다. Raw Database는 이제 헤더 이름으로 읽는다(`existing_records`). 첫 사례로 Warhound의 `Undisclosed`(10/22)와 `Seed`(10/23) 두 행을 하나로 합쳤다.
- **Valuation 열 추가 (2026-10-23):** Amount Raised 오른쪽(F열)에 추가했다. 추출 스키마에 `valuation` 필드를 넣었고(기사에 나온 경우만, post-money 우선), 금액과 같은 형식으로 정규화한다. 기존 시트는 `setup_sheet.py`가 빠진 열을 제자리에 넣는다. 열을 추가하기 전에 기록된 행의 밸류에이션은 비어 있다.
- **로보틱스 한정 + 소스 확장 (2026-10-23):** 일반 소스를 늘리면 범위가 너무 넓어져서, 앞으로는 로보틱스 라운드만 기록한다.
  - **범위:** 로봇·휴머노이드·Physical AI, 드론·배송, 자율주행, 제조 자동화·로봇 부품. 국방은 제외한다. 기존 행은 유지한다.
  - **새 소스:** TechCrunch Robotics, The Robot Report, Robotics & Automation News, 그리고 Google News 검색 RSS(헤드라인 전용).
  - **실제로 확인했지만 쓰지 않은 소스:** IEEE Spectrum, Robotics 24/7은 펀딩 뉴스가 없다. FinSMEs, GlobeNewswire, Fierce Biotech는 봇 차단(403)이다. 일반 US 매체(The SaaS News, GeekWire 등)는 로보틱스 전문이 아니다.
  - **필터:** LLM 앞에서 `focus.keywords`로 1차로 거르고, LLM이 이벤트별 `in_focus`와 로보틱스 하위 카테고리를 정한다.
  - **LLM 예산:** Claude는 11-01까지 한도가 막혀 있고 Gemini 무료 등급은 모델당 하루 약 20회라서 예산을 둔다. 처리 순서는 소스 `priority`(TechCrunch·PR Newswire 1)이고, 실행당 최대 `max_llm_calls_per_run` 회만 호출한다. 같은 회사를 다룬 기사는 한 번만 호출하고(`guess_company`), Google News 헤드라인은 최근 기록된 회사이거나 오래된 것이면 건너뛴다.
  - **Google News 소스 키워드:** 헤드라인의 "raises"가 펀딩이 아닌 경우가 많아서(raises doubts/toll/advisory), 소스별 `keywords`로 금액이나 펀딩 단어를 요구한다.
  - **2026-10-23 드라이런:** 8개 소스, 후보 22건, 같은 소식 건너뛰기 후 예상 LLM 호출은 약 15회다.
- **데이터베이스를 두 탭으로 분리 + 회사 웹사이트 링크 (2026-10-23):** 두 방식이 각각 쓸모 있어서 한 시트에 섞지 않고 탭을 나눴다.
  - **Raw Database** (`all` 트랙): 모든 분야, 소스는 TechCrunch·PR Newswire·Crunchbase News.
  - **Robotics Database** (`robotics` 트랙): 로보틱스만, 소스는 전체. 대시보드도 탭마다 하나씩 있다(**Funding Dashboard**, **Robotics Dashboard**).
  - **트랙 지정:** 소스마다 `tracks`로 정한다. 기사마다 LLM은 한 번만 부르고, 결과는 이렇게 나눈다. 해당 소스가 `all` 트랙이면 모든 라운드가 Raw Database로 간다. `in_focus`인 라운드는 소스와 상관없이 Robotics Database로 간다. 중복 제거와 교체 규칙은 탭마다 따로 적용한다.
  - **기존 행 분리:** 로보틱스 전용 소스에서 온 7행은 Robotics Database로 옮겼다(복사한 뒤 확인하고 나서 원본을 삭제했다). 나머지 20행은 Raw Database에 남겼다. BOB 행은 Category에 투자자 목록이 잘못 들어가 있어서 Fintech로 고쳤다.
  - **웹사이트:** `Website` 열을 맨 끝에 추가했다.
    - 기사 링크 목록: parser가 본문 링크 가운데 뉴스·소셜이 아닌 사이트를 기사 끝에 "Links in the article"로 붙인다.
    - LLM 선택과 확인: LLM은 그 목록이나 본문에서만 회사 사이트를 고른다. 코드는 그 도메인이 실제로 기사에 있었는지 확인한다(`verified_website`).
    - 이름 매칭: LLM이 비워 두면 도메인에 회사명이 들어간 링크를 쓴다(`website_by_name`).
    - Company 열 링크: 셀 서식으로 단다(값은 회사명 그대로). 웹사이트가 없으면 Google 검색 링크를 단다.
    - 쓰지 않은 방법: DuckDuckGo `!ducky`(첫 결과로 바로 이동)는 신생 회사에서 뉴스 기사로 가서 쓰지 않았다.
    - 기존 27행: LLM 없이 기사 링크로 채워서 6행(Legora, Blacksmith, Mave, Coast, 120ft, Dream Labs)이 실제 사이트에 연결됐다.
  - **Sourcing 쪽 영향:** 다른 세션이 작업 중인 Sourcing 코드(`sourcing/store.py`)는 아직 커밋되지 않았다. 이 코드는 Raw Database만 읽기 때문에 로보틱스 전용 소스의 라운드는 보지 못한다.
- **3-Month Alerts 탭은 아직 만들지 않았다.** 2단계에서 추가한다.
- **job 실패 기준:** 피드 하나만 실패하면 경고만 남긴다. job을 실패로 표시하는 경우는 모든 피드를 읽지 못했거나, 기사가 3번 재시도 후에도 실패(`failed`)했을 때뿐이다. 일시적인 오류는 다음 실행에서 자동으로 재시도한다.
