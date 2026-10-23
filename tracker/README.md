# News Tracker

TechCrunch, PR Newswire, Crunchbase News에서 스타트업 펀딩 뉴스를 매일 2회 수집하는 프로그램이에요. Gemini로 펀딩 정보만 뽑아서 Google Sheet **"VC Fundraising Tracker"** 에 계속 추가해요.
설계 문서: [../docs/news-tracker-design.md](../docs/news-tracker-design.md)

```
collector.py  → parser.py   → extractor.py → sheets.py
(RSS + 키워드)   (기사 본문)    (Gemini JSON)   (중복 제거 후 append)
```

## 처음 한 번만 하는 설정

### 1. Gemini API 키
1. https://aistudio.google.com/apikey 에서 **Create API key**를 눌러요.
2. 키를 복사해 두세요. 아래 `.env`와 GitHub Secrets에 넣어요.

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
1. 저장소를 **비공개(Private)** 로 GitHub에 올려요. 공개 저장소는 60일 동안 활동이 없으면 예약 실행이 꺼져요.
2. 저장소 **Settings → Secrets and variables → Actions → New repository secret**에서 다음 3개를 추가해요.
   | 이름 | 값 |
   |---|---|
   | `GEMINI_API_KEY` | Gemini 키 |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | `service-account.json` 파일 **내용 전체**를 붙여넣기 |
   | `SHEET_ID` | 시트 ID |
3. **Actions → News Tracker → Run workflow**로 한 번 수동 실행해 확인해요.
4. 이후에는 매일 한국시간 07:00, 19:00에 자동으로 실행돼요. 실행이 실패하면 GitHub이 이메일을 보내요.

## 실행 방법

```powershell
.venv\Scripts\python main.py --dry-run    # 후보 기사 목록만 보기 (키 없어도 됨)
.venv\Scripts\python main.py --no-write   # Gemini로 추출해서 출력만, 시트에는 안 씀
.venv\Scripts\python main.py              # 실제 실행
.venv\Scripts\python -m pytest            # 테스트
```

## 시트 구성

| 탭 | 내용 |
|---|---|
| **Raw Database** | 펀딩 이벤트 1건이 1행이에요. 프로그램은 **추가만** 하고 기존 행을 고치거나 지우지 않아요. |
| **Funding Dashboard** | 라운드별 열에 `회사명 (날짜)`가 최신순으로 나와요. 수식으로 자동 생성돼서 프로그램이 건드리지 않아요. |
| **Article Log** | 처리한 기사 기록이에요. Status는 `pending` → `extracted` / `skipped_not_funding` / `error` 순서로 바뀌고, 3번 실패하면 `failed`가 돼요. |

- **중복 규칙:** 같은 회사(이름 표기 차이는 무시)의 같은 라운드가 **±7일** 안에 이미 있으면 건너뛰어요.
- **오류 처리:** `error` 상태인 기사는 다음 실행 때 다시 시도해요. 직접 다시 처리하고 싶으면 Article Log에서 Status를 `pending`으로, Attempts를 `0`으로 바꾸면 돼요.
- **job이 실패로 표시되는 경우:** 모든 피드를 읽지 못했을 때, 또는 3번 재시도해도 추출에 실패한 기사가 있을 때예요.

## 설정 바꾸기 (`config.yaml`)
- 소스 추가/끄기: `sources`에 항목을 추가하거나 `enabled: false`로 설정해요. VentureBeat는 봇 차단(HTTP 429) 때문에 현재 꺼져 있어요.
- 모델: `gemini.model`. 더 저렴하게 쓰고 싶으면 Flash-Lite 계열로 바꿀 수 있어요.
- 키워드, 조회 기간, 중복 판단 기간도 여기서 바꿔요.
