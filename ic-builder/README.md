# IC 자료 만들기

기존 IC 자료(.pptx)의 구조·순서·디자인은 그대로 두고, 새 펀드 자료(.pptx/.pdf)의 내용으로 채운 새 IC 자료를 만듭니다.
내 PC에서만 돌고, 파일은 Claude API로만 보내집니다.

## 처음 한 번

```bash
cd ic-builder
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
```

`.env.example`을 `.env`로 복사해 `ANTHROPIC_API_KEY`를 넣습니다. `.env`가 없으면 `tracker/.env`의 키를 씁니다.

## 실행

```bash
.venv/Scripts/streamlit run app.py
```

브라우저에서 ① 기존 IC 자료 ② 새 펀드 자료(여러 개)를 올리고, 필요하면 메모를 적은 뒤 **생성**을 누릅니다.

## 결과물 읽는 법

- **빨간 `[확인 필요]`**: 새 자료에서 근거를 찾지 못한 칸입니다. Claude가 채웠더라도 숫자나 인용이 원자료에서 확인되지 않으면 여기로 바뀝니다.
- **빨간 `[차트/이미지 업데이트 필요]`**: 기존 펀드의 로고, 팀 사진, 차트 등입니다. 직접 교체해야 합니다.
- **화면의 경고**: 결과물(스피커 노트 포함)에 남아 있는 기존 펀드 이름을 보여 줍니다.

## 동작 방식

1. `template.py`: 슬라이드마다 텍스트 칸, 표 셀, 그림, 차트를 뽑습니다.
2. `sources.py`: PDF는 원본 그대로 Claude에게 보내고, pptx는 텍스트로 바꿉니다.
3. `fill.py`: 슬라이드마다 Claude(`claude-haiku-5-5`)가 칸별로 유지/교체를 정하고, 쓴 문장마다 원문 인용을 붙입니다. 숫자가 인용에 없거나 인용이 원자료에 없으면 `[확인 필요]`로 바꿉니다.
4. `writer.py`: 원래 run의 서식(굵게 처리된 라벨, 글꼴, 크기, 색)을 유지한 채 텍스트만 바꿉니다.

PDF는 합계 22MB, 600페이지까지 넣을 수 있습니다.

## 보안

이 레포는 public입니다. `.gitignore`가 `ic-builder/` 안의 `.pptx`, `.pdf`, `.env`를 막고 있지만, IC 자료는 이 폴더 밖에 두는 것을 권장합니다.

## 테스트

```bash
.venv/Scripts/python -m pytest
```
