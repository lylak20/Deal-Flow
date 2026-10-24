# News Tracker

**로보틱스** 스타트업 펀딩 뉴스를 매일 오전 7시(미국 태평양 시간)에 수집하는 프로그램이에요. TechCrunch, PR Newswire를 먼저 보고, 그다음 로보틱스 전문 매체와 Google News를 봐요. Claude Haiku 5.5(한도가 차면 Gemini)로 펀딩 정보를 뽑아 Google Sheet **"VC Fundraising Tracker"** 에 추가해요.
설계 문서: [../docs/news-tracker-design.md](../docs/news-tracker-design.md)

```
collector.py  → parser.py   → extractor.py → sheets.py
(RSS + 키워드)   (기사 본문)    (Claude JSON)   (중복 제거 후 append)
```

## 두 개의 데이터베이스 탭과 뉴스 소스 (2026-10-23부터)

한 번 실행하면 같은 열 구조의 탭 두 개가 함께 채워져요. 기사 하나당 LLM 호출은 한 번이고, 결과를 두 탭에 나눠 써요.

| 탭 | 무엇이 들어가나 | 소스 |
|---|---|---|
| **Raw Database** | 모든 분야의 펀딩 라운드 | TechCrunch, PR Newswire, Crunchbase News |
| **Robotics Database** | 로보틱스 라운드만 | 위 3개 + TechCrunch Robotics, The Robot Report, Robotics & Automation News, Google News |

TechCrunch 기사에 로보틱스 회사가 나오면 두 탭 모두에 들어가요. 각 탭에는 대시보드가 따로 있어요(**Funding Dashboard**, **Robotics Dashboard**). 소스마다 어느 탭에 들어갈지는 `config.yaml`의 `tracks`로 정해요.

**로보틱스로 보는 것:** 로봇·휴머노이드·Physical AI(로봇 파운데이션 모델 포함), 드론·배송 로봇, 자율주행, 제조·물류 자동화, 로봇 부품·소프트웨어. **국방·무기 시스템은 제외**해요.

| 우선순위 | 소스 | 비고 |
|---|---|---|
| 1 | TechCrunch(펀딩·벤처), PR Newswire(VC) | 항상 먼저 처리 |
| 2 | TechCrunch Robotics, The Robot Report, Robotics & Automation News, Crunchbase News | 로보틱스 전문·주요 매체 |
| 3 | Google News 검색 (로보틱스 + 펀딩, 미국, 최근 1일) | 여러 매체를 한 번에 잡는 "그물망". 링크로 본문을 열 수 없어 **제목만으로** 추출해요. Article Log의 Source 열에는 `매체명 (via Google News)`로 나와요 |

**LLM 호출을 아끼는 규칙**
- 제목과 요약에 로보틱스 단어가 없는 기사는 LLM에 보내지 않아요(`focus.keywords`). 최종 판단은 LLM이 해요. 로보틱스가 아닌 라운드는 Article Log에 `skipped_not_in_focus`로 남아요.
- 같은 회사를 다룬 기사가 여러 개면 우선순위가 높은 기사 1건만 LLM에 보내요. 나머지는 `skipped_same_story`로 처리해요.
- Google News 제목이 최근 7일 안에 이미 기록된 회사에 관한 것이면 `skipped_known_company`, 3일이 지난 것이면 `skipped_stale`로 건너뛰어요.
- 한 번 실행할 때 LLM 호출은 최대 `max_llm_calls_per_run`(30)번이에요. 이 상한에 걸리거나 Claude·Gemini 한도가 모두 차면 남은 기사는 `pending`으로 다음 실행에 넘겨요. 다음 실행에서도 TechCrunch·PR Newswire 새 기사를 먼저 처리해요.

## 처음 한 번만 하는 설정

### 1. Anthropic API 키
1. https://platform.claude.com/settings/keys 에서 키를 만들어요. API 요금은 claude.ai 구독과 별개라서 크레딧을 미리 충전해야 해요.
2. 키를 복사해 두세요. 아래 `.env`와 GitHub Secrets에 넣어요.
3. 예상 비용: 기사 1건에 약 $0.0005, 한 달 약 $0.3 (하루 20건 기준).

**Gemini 자동 대체:** Claude의 월 사용 한도나 크레딧이 다 떨어지면, 그 실행의 나머지 기사는 자동으로 Gemini 무료 등급(`gemini-3.8-flash`, 하루 약 20회)으로 처리해요. https://aistudio.google.com/apikey 에서 키를 받아 `.env`와 GitHub Secrets에 `GEMINI_API_KEY`로 넣어 두세요. 키가 없으면 대체 없이 Claude만 써요. Claude 한도를 다시 올리면 다음 실행부터 자동으로 Claude를 먼저 써요. 두 서비스가 모두 한도에 걸리면 실행을 멈추고, 남은 기사는 다음 실행으로 넘기고, 실패 이메일을 보내요.

### 2. Google 서비스 계정 (프로그램이 시트에 쓰기 위한 계정)
1. https://console.cloud.google.com 에서 새 프로젝트를 만들어요. 예: `deal-flow`
2. **APIs & Services → Library**에서 **Google Sheets API**를 찾아 **Enable**을 눌러요.
3. **APIs & Services → Credentials → Create credentials → Service account**에서 이름을 아무거나 정하고 만들어요. 역할은 지정하지 않아도 돼요.
4. 만든 서비스 계정 → **Keys → Add key → Create new key → JSON**을 누르면 파일이 다운로드돼요.
5. 다운로드한 파일을 `tracker/service-account.json`으로 저장해요. 이 파일은 `.gitignore`에 들어 있어서 커밋되지 않아요.
6. 서비스 계정 이메일(`...@...iam.gserviceaccount.com`)을 복사해 두세요.

### 3. Google Sheet
1. https://sheets.new 에서 빈 시트를 만들고 이름을 **VC Fundraising Tracker**로 바꿔요.
2. **공유**를 눌러 2-6에서 복사한 서비스 계정 이메일을 **편집자**로 추가해요.
3. 주소창의 `/d/` 와 `/edit` 사이 긴 문자열이 `SHEET_ID`예요.

### 4. 로컬 실행 준비 (Windows PowerShell 기준, `tracker` 폴더에서)
```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # 그다음 .env에 키와 SHEET_ID를 채워요
.venv\Scripts\python setup_sheet.py   # 탭, 헤더, 대시보드 수식 생성
```

### 5. GitHub에서 자동 실행
1. 저장소를 GitHub에 올려요. 공개 저장소는 60일 동안 커밋이 없으면 예약 실행이 꺼지니, 그때는 Actions 탭에서 다시 켜요.
2. 저장소 **Settings → Secrets and variables → Actions → New repository secret**에서 다음 3개를 추가해요.
   | 이름 | 값 |
   |---|---|
   | `ANTHROPIC_API_KEY` | Anthropic API 키 |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | `service-account.json` 파일 **내용 전체**를 붙여넣기 |
   | `SHEET_ID` | 시트 ID |
   | `GEMINI_API_KEY` | (선택) Gemini 키. Claude 한도가 찼을 때 자동으로 대신 써요. |
3. **Actions → News Tracker → Run workflow**로 한 번 수동 실행해 확인해요.
4. 이후에는 매일 **오전 7시 PT**(미국 태평양 시간, 서머타임 자동 반영)에 자동으로 실행돼요. 이전에 기록한 행은 그대로 두고 새 회사만 아래에 추가해요. 실행이 실패하면 GitHub이 이메일을 보내요.

## 실행 방법

```powershell
.venv\Scripts\python main.py --dry-run    # 후보 기사 목록만 보기 (키 없어도 됨)
.venv\Scripts\python main.py --no-write   # 추출해서 출력만, 시트에는 안 씀
.venv\Scripts\python main.py              # 실제 실행
.venv\Scripts\python -m pytest            # 테스트
```

## 시트 구성

탭 색: 노란색은 펀딩 탭(Raw Database ~ Robotics Dashboard), 초록색은 Sourcing 탭(Signal Snapshots부터 오른쪽)이에요.

| 탭 | 내용 |
|---|---|
| **Raw Database** / **Robotics Database** | 펀딩 라운드 1건이 1행이에요(두 탭 모두 같은 열, A~L열). **Founders**(I열)에는 기사에 창업자(founder/co-founder)로 나온 사람의 이름이 들어가요. 이름마다 링크가 따로 걸려 있어서, 기사에 LinkedIn 프로필 링크가 있으면 그 프로필로, 없으면 `이름 + 회사명 + linkedin` Google 검색 결과로 가요. **Company**(C열)를 누르면 회사 웹사이트로 가요. 웹사이트는 기사 본문과 기사 안의 링크에서만 찾고, 별도 열 없이 회사명 링크로만 저장해요. 웹사이트를 찾지 못하면 `회사명 + 분야 + official website` Google 검색 결과로 연결돼요. 기사 출처는 **Article URL**로 확인해요. 새 라운드는 맨 아래에 추가해요. 같은 라운드의 더 자세한 소식이 나오면 기존 행을 지우고 최신 내용으로 다시 써요(아래 중복 규칙 참고). **Valuation**(밸류에이션)은 기사에 나와 있을 때만 채워요. B열 **✉️ Email**을 누르면 그 회사에 맞게 채워진 Gmail 초안이 열려요. 받는 사람은 비어 있어요. |
| **Email Template** | Email 버튼이 쓰는 제목(B1)과 본문(B2)이에요. 기본 본문은 LG Technology Ventures 소개와 라운드 참여 문의예요. 여기만 고치면 모든 버튼에 반영돼요. 쓸 수 있는 자리표시자는 `{Company}` `{Round}` `{Amount}` `{Lead}`예요. Gmail 초안 링크에는 서식을 넣을 수 없어서 하이퍼링크는 URL 그대로 들어가요(Gmail이 클릭 가능한 링크로 바꿔 줘요). 초안이 열리면 `[Name]`에 받는 사람 이름을 넣어 주세요. |
| **Funding Dashboard** / **Robotics Dashboard** | 각 데이터베이스 탭의 라운드별 열에 회사명이 최신순으로 나와요. 수식으로 자동 생성돼서 프로그램이 건드리지 않아요. |
| **Article Log** (숨김) | 처리한 기사 기록이에요. 같은 기사를 LLM에 두 번 보내지 않게 하고, 다음 실행으로 넘긴 기사와 재시도를 관리해요. 지우면 안 돼요. 보기 → 숨겨진 시트에서 다시 볼 수 있어요. Status는 `pending` → `extracted` / `skipped_not_funding` / `error` 순서로 바뀌고, 3번 실패하면 `failed`가 돼요. |

- **중복 규칙:** 같은 회사(이름 표기 차이는 무시)의 같은 라운드가 **1년(365일)** 안에 이미 있으면 같은 라운드로 봐요. 한쪽 라운드가 `Undisclosed`인 경우도 같은 라운드로 봐요.
  - 새 소식에 기존 행에 없던 정보(라운드 이름, 금액, 밸류에이션, 투자자)가 있으면 기존 행을 **지우고** 최신 내용으로 맨 아래에 다시 써요. 새 소식에 없는 정보는 기존 행에서 가져와요. 예: 어제 `Warhound | Undisclosed | $200M` → 오늘 `Warhound | Seed | $200M` 한 행만 남아요.
  - 새 정보가 없으면(같은 소식의 반복) 건너뛰어요.
  - 같은 회사라도 **다른 라운드**(예: 6개월 전 Series A, 이번에 Series B)는 새 행으로 따로 추가해요.
  - 이미 처리한 기사 URL은 Article Log로 걸러서 다시 처리하지 않아요.
- **오류 처리:** `error` 상태인 기사는 다음 실행 때 다시 시도해요. 직접 다시 처리하고 싶으면 Article Log에서 Status를 `pending`으로, Attempts를 `0`으로 바꾸면 돼요.
- **job이 실패로 표시되는 경우:** 모든 피드를 읽지 못했을 때, 또는 3번 재시도해도 추출에 실패한 기사가 있을 때예요.

## 설정 바꾸기 (`config.yaml`)
- 소스 추가/끄기: `sources`에 항목을 추가하거나 `enabled: false`로 설정해요. VentureBeat는 봇 차단(HTTP 429) 때문에 현재 꺼져 있어요.
- 모델: `llm.provider`로 Claude와 Gemini 중에 고르고, `llm.claude.model` / `llm.gemini.model`로 모델을 정해요.
- 키워드, 조회 기간, 중복 판단 기간도 여기서 바꿔요.

---

# Sourcing (유망 기회 발굴)

Google Docs "AI 기반 스타트업 분석 에이전트 만들기"의 **Sourcing** 블록이에요. 뉴스 수집이 끝난 뒤 같은 GitHub Actions 실행에서 매일 돌고, 결과를 같은 시트의 새 탭에 써요.
수집과 점수 계산은 **무료 공개 데이터와 코드**로 해요. Claude(Haiku)는 월요일 **주간 파트너 브리프** 한 번(약 $0.002)에만 쓰고, 창업팀 조회는 꺼져 있어요.

```
sourcing_main.py
 ├─ research.py    Research Scout + Lab Tracker      ← OpenAlex (논문, 인용, 소속, 연구비)
 ├─ opensource.py  Open Source Momentum Tracker      ← GitHub 검색, Hugging Face 트렌딩
 ├─ startups.py    Startup Formation Tracker         ← YC 디렉터리, 이 시트의 Raw Database
 ├─ claude.py      Founding Team Tracker (꺼짐), 주간 브리프
 ├─ scoring.py     Opportunity Scoring Engine        (Research/Lab/Momentum/Team/Formation 각 0~20점)
 └─ store.py       Deal Flow Dashboard + 시트 탭
```

## 처음 한 번
- 추가로 할 일은 없어요. 시트 탭은 첫 실행 때 자동으로 만들어져요.
- **권장:** https://openalex.org/settings/api 에서 무료 키를 받아 GitHub Secrets에 `OPENALEX_API_KEY`로 넣어요. 키가 없으면 GitHub 서버를 같이 쓰는 모든 사람과 하루 $0.10어치 무료 한도를 나눠 써서, 가끔 논문 수집이 실패할 수 있어요(실패해도 나머지는 정상 실행돼요).

## 실행
```powershell
.venv\Scripts\python sourcing_main.py --no-write   # 수집 + 점수만, 상위 25개 출력 (시트·Claude 안 씀, 무료)
.venv\Scripts\python sourcing_main.py              # 시트에 쓰기
```
로컬에서 GitHub 토큰 없이 돌리면 GitHub 검색 제한(분당 10회) 때문에 2~3분 걸려요.

## 시트 탭
| 탭 | 내용 |
|---|---|
| **Sourcing Dashboard** | 전체 개수, 이번 주 신규, 티어별 개수, 테마별 개수·평균 점수·추세, 상위 20개, 종류별 상위 5개, Action Queue, 최신 주간 브리프 |
| **Opportunities** | 모든 기회의 순위표. 점수 5개, 근거 신호(Key Signals), 추천 행동, 담당(Associate/Principal/Partner), 처음 발견한 날 |
| **Research Scout** | **로보틱스 논문만**(`sourcing.research.themes`). 추적 대학(Stanford, MIT 등 8곳) 또는 기업 공저자가 있는 최근 12개월 논문을 검색마다 인용 많은 순으로 50개까지 봐요. 🔥 = 2개 이상 조건 충족 |
| **Lab Tracker** | 위 로보틱스 논문의 마지막 저자(PI) 기준 연구실. 🔥 = 3개 이상 조건 충족 |
| **Open Source Momentum** | 최근 12개월 내 만들어진 GitHub 저장소와 Hugging Face 트렌딩 모델. 성장 가속도(Acceleration) 포함 |
| **Startup Formation** | 최근 24개월 YC 배치와 Raw Database의 Pre-Seed/Seed/Series A 회사 |
| **Signal Snapshots** | 매일 별 수·테마별 개수 기록(추가만 함). 가속도와 테마 추세 계산에 써요. 지우면 기록이 처음부터 다시 쌓여요 |
| **Partner Brief** | 월요일마다 Claude가 쓴 주간 브리프(추가만 함). 입력에 없는 숫자가 나온 줄에는 [확인 필요]가 붙어요 |

## 점수 읽는 법
- **Total = Research + Lab + Momentum + Team + Formation** (각 0~20, 문서와 같은 구조). 점수마다 Key Signals에 근거가 적혀 있어요.
- **Acceleration (0~100):** 연속된 세 기간의 성장량을 비교해요. 100 → 300 → 900이면 97(Explosive), 1000 → 700 → 300이면 24(Decelerating). 스냅샷이 3주 쌓이면 7일 단위, 90일 쌓이면 30일 단위로 계산해요. GitHub Actions에서는 별 3,000개 이하 저장소의 과거 별 기록을 하루 10개씩 바로 불러와요.
- **티어 기준은 [40, 28, 20]이에요** (`sourcing.scoring.tier_thresholds`). 문서의 90/75/60은 창업자·스핀아웃 정보가 있어야 닿는 점수라, 무료 데이터만으로는 아무것도 Tier 1이 될 수 없어요.
- 첫 실행 날은 모든 기회가 "New This Week"로 나와요. 다음 날부터 정상이에요.

## 아직 안 되는 것
- **특허(USPTO), 졸업생 창업 이력, 기여자 수 증가**: 무료로 안정적으로 얻을 수 없어서 넣지 않았어요.
- **Founding Team Tracker**: `sourcing.claude.founding_team: true`로 켜면 상위 스타트업 5곳의 창업자를 Claude Haiku가 웹에서 찾아요(회사당 약 $0.02~0.05). 가져온 페이지에 이름과 인용문이 실제로 있는 창업자만 남겨요.
- 테마는 키워드로 나눠서, 가끔 엉뚱한 테마가 붙어요. `config.yaml`의 `sourcing.themes` 키워드를 고치면 바로 반영돼요.
- 대기업(google, nvidia, alibaba 등) 저장소와 모델은 `sourcing.github.exclude_owners`로 빼요.
