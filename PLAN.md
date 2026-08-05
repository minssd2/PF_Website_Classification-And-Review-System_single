# Tranco 100만 사이트 → 한국어 서비스 → 제품 카테고리 분류 파이프라인 (+ 관리 웹 UI)

> **문서 성격** — 아래는 2026-08-03 착수 시점에 승인받은 **계획 원문**이다.
> 구현하며 달라진 부분이 있으므로, 실제 동작은 문서 끝의
> [구현 결과와 계획의 차이](#구현-결과와-계획의-차이)를 함께 본다.
> 실행 절차는 [GUIDE.md](GUIDE.md), 설계·구조는 [README.md](README.md).

## Context

`top-1m.csv` (tranco 상위 100만 도메인, `rank,domain` 형식)에서 **① 한국어 서비스를 제공하는 사이트**를 추려내고, 그중 **② 제품에서 쓰는 10개 카테고리에 해당하는 사이트**를 다시 추려낸다.

100만 건 전량 HTTP 접속 검사는 수 시간~하루가 걸리므로 **어느 시점에 중단해도 상태가 보존되고 다음 실행에서 이어지는 구조**가 설계의 핵심 제약이다. 모든 상태는 SQLite에 저장하고, 각 단계는 "아직 처리되지 않은 행"만 골라 처리하는 멱등 작업으로 만든다.

또한 분류 기준(한국어 판정 가중치, 카테고리 키워드 규칙)은 실제 데이터를 보면서 계속 손보게 되므로, **진행 상황 확인과 기준 정보 수정을 브라우저에서 할 수 있는 로컬 웹 UI**를 함께 만든다. 이 때문에 규칙과 설정은 YAML 파일이 아니라 **DB 테이블**에 저장한다 (YAML은 최초 시드용으로만 사용).

**대상 카테고리 (10개)**: 웹메일, 쇼핑, 증권, 취업, 게임, 엔터테인먼트, 뉴스, SNS, 웹하드, 생성형AI

**결정된 방침** (사용자 확인 완료)
- 한국어 판별: 100만 건 전량 HTTP 접속 검사
- 카테고리 분류: 하이브리드 (규칙 우선 → 애매한 건만 LLM)
- **LLM 판정은 Anthropic API를 쓰지 않고, 콘솔에 열어둔 Claude Code 세션에 배치 파일을 주고받는 방식**으로 처리 (API 키·과금 없음)
- 스택: Python + 프로젝트 전용 venv, 저장소는 SQLite, 관리 UI는 FastAPI

---

## 아키텍처

```
[CLI 워커]  fetch / detect-korean / classify           쓰기
                        ↘                            ↗
                     SQLite (data/sites.db, WAL 모드)
                        ↗                            ↘
[웹 UI]  진행률 대시보드 / 규칙 편집 / 사이트 검수      읽기 + 규칙 쓰기
```

무거운 크롤링은 CLI 워커가 백그라운드로 돌고, 웹 UI는 같은 DB를 읽어 진행률을 보여준다. WAL 모드라 워커가 쓰는 중에도 웹은 막힘 없이 읽는다 (`busy_timeout=5000`).

### 저장소 스키마 (`data/sites.db`)

```sql
-- ① 크롤링 원본 (한 번 수집하면 재사용, 규칙 튜닝 시 재크롤링 불필요)
sites(
  rank INTEGER, domain TEXT PRIMARY KEY,
  fetch_status TEXT DEFAULT 'pending',   -- pending | ok | failed | dead
  attempt_count INTEGER DEFAULT 0, error TEXT,
  http_status INTEGER, final_url TEXT, charset TEXT,
  title TEXT, description TEXT, html_lang TEXT, og_locale TEXT,
  text_sample TEXT,                       -- 본문 앞 500자
  fetched_at TEXT
)

-- ② 한국어 판정 (sites로부터 재계산 가능한 파생 데이터)
korean(domain TEXT PRIMARY KEY, is_korean INTEGER, score REAL,
       reasons TEXT, judged_at TEXT)

-- ③ 카테고리 분류
classification(domain TEXT PRIMARY KEY, categories TEXT,  -- JSON 배열 (멀티라벨)
       primary_category TEXT, method TEXT,                -- rule | llm | manual
       confidence REAL, evidence TEXT, classified_at TEXT)

-- ④ 기준 정보 — 웹에서 편집하는 대상
settings(key TEXT PRIMARY KEY, value TEXT, value_type TEXT, description TEXT)
category_rules(id INTEGER PK, category TEXT, rule_type TEXT,  -- domain | keyword | exclude
       pattern TEXT, weight REAL DEFAULT 1.0, enabled INTEGER DEFAULT 1,
       updated_at TEXT)

-- ⑤ 사람이 직접 고친 라벨 — 재분류해도 덮어쓰지 않음
manual_labels(domain TEXT PRIMARY KEY, is_korean INTEGER, categories TEXT,
       note TEXT, updated_at TEXT)

-- ⑥ 작업 진행률 (워커가 기록 → 웹이 표시)
job_runs(id INTEGER PK, job_name TEXT, status TEXT,  -- running | done | stopped | error
       total INTEGER, processed INTEGER, ok_count INTEGER, fail_count INTEGER,
       started_at TEXT, updated_at TEXT, message TEXT)

-- ⑦ Claude Code 세션 판정 배치 (파일 핸드오프 상태 추적)
llm_batches(batch_id TEXT PRIMARY KEY, status TEXT,  -- exported | imported
       size INTEGER, request_path TEXT, result_path TEXT,
       exported_at TEXT, imported_at TEXT)
llm_queue(domain TEXT PRIMARY KEY, batch_id TEXT, queued_at TEXT)
```

인덱스: `sites(fetch_status)`, `sites(rank)`, `korean(is_korean)`, `classification(primary_category)`, `category_rules(category)`.

### 디렉토리 구조

```
PF_Website_Classification-And-Review-System_single/
├── top-1m.csv                      (기존)
├── requirements.txt / venv/
├── seed/
│   ├── categories.seed.yaml        카테고리 규칙 초기값 → DB에 주입
│   └── settings.seed.yaml          기본 임계값/동시성/모델명
├── src/
│   ├── db.py             스키마, 커넥션, 배치 커밋, WAL 설정
│   ├── seed.py           top-1m.csv 적재 + 규칙/설정 시드 주입
│   ├── fetch.py          비동기 크롤러 (httpx + asyncio)
│   ├── extract.py        HTML → title/desc/lang/og:locale/본문 (selectolax)
│   ├── korean.py         한국어 판정 (DB settings의 가중치 사용)
│   ├── classify_rule.py  category_rules 기반 규칙 분류
│   ├── llm_batch.py      판정 대기 배치 export / 결과 import
│   ├── jobs.py           job_runs 진행률 기록 + 재계산 작업 실행
│   └── export.py         최종 CSV/XLSX 산출
├── web/
│   ├── app.py            FastAPI 앱 + API 엔드포인트
│   ├── templates/        dashboard / categories / settings / sites (Jinja2)
│   └── static/           단일 CSS + 바닐라 JS (빌드 도구 없음)
├── data/sites.db
├── llm/
│   ├── requests/         판정 요청 배치 파일 (batch_0001.md)
│   └── results/          Claude Code 세션이 작성한 결과 (batch_0001.json)
├── out/                            최종 결과물
└── run.py                          CLI 엔트리포인트
```

들여쓰기는 **탭 문자** (글로벌 규칙).

주요 의존성: `httpx`, `selectolax`(빠른 HTML 파서), `charset-normalizer`, `fastapi`, `uvicorn`, `jinja2`, `pyyaml`. (LLM은 세션 핸드오프 방식이라 SDK 불필요)

---

## 파이프라인 (CLI)

각 명령은 독립 실행 가능하고, 언제든 Ctrl+C로 끊고 같은 명령을 다시 실행하면 이어서 진행된다.

```bash
python run.py init                                    # DB 생성 + 100만 행 적재 + 규칙 시드
python run.py fetch --limit 50000 --concurrency 150   # 크롤링, 끊어서 반복 실행
python run.py retry --max-attempts 3                  # 일시 실패건만 재시도
python run.py detect-korean [--force]                 # 한국어 판정 (네트워크 불필요)
python run.py classify-rule [--force]                 # 규칙 분류 (네트워크 불필요)
python run.py llm-export --size 40                    # 애매한 건 → 판정 요청 배치 파일 생성
python run.py llm-import llm/results/batch_0001.json  # 세션이 답한 결과를 DB에 반영
python run.py export                                  # 최종 산출물
python run.py web --port 8000                         # 관리 웹 UI 실행
```

### 1단계: `init`
`top-1m.csv`를 `sites`에 배치 삽입. `seed/*.yaml`의 카테고리 규칙·기본 설정을 `category_rules`/`settings`에 주입 (이미 있으면 건너뜀 — 웹에서 수정한 값을 덮어쓰지 않음).

### 2단계: `fetch` — 크롤링 (가장 오래 걸림)

- `httpx.AsyncClient` + `asyncio.Semaphore(concurrency)`, 기본 동시 150
- `https://{domain}` → 실패 시 `http://` 폴백, 리다이렉트 최대 5회
- 타임아웃: connect 5s / read 8s
- **`Accept-Language: ko-KR,ko;q=0.9,en;q=0.8`** 헤더 전송 → 다국어 사이트도 한국어 페이지를 받도록 (google.com, youtube.com 등이 한국어 서비스로 포착됨)
- 본문은 **앞 200KB만 스트리밍 수신 후 중단**
- 인코딩: HTTP 헤더 charset → `<meta charset>` → `charset-normalizer` (EUC-KR/CP949 대응)
- DNS NXDOMAIN 등 회복 불가 오류는 `dead`로 기록해 재시도 대상에서 제외
- **체크포인트**: 200건마다 커밋 + `job_runs.processed` 갱신 → 웹 대시보드에 실시간 반영
- `SIGINT` 핸들러: 진행 중 요청을 마무리하고 커밋 후 `job_runs.status='stopped'`로 종료
- 재개: `WHERE fetch_status='pending' ORDER BY rank LIMIT ?` 로만 큐 구성

예상 소요: 동시 150 기준 60~90 req/s → **전량 약 4~8시간** (여러 세션으로 분할 실행)

용량 관리: `text_sample` 500자 제한. `detect-korean` 이후 `is_korean=0`인 행의 `text_sample`을 비우고 VACUUM.

### 3단계: `detect-korean` — 한국어 판정

네트워크 없이 몇 분 내 재실행 가능한 순수 계산 단계. 가중치·임계값은 **DB `settings`에서 읽으므로 웹에서 고친 값이 즉시 반영**된다.

| 신호 | 기본 점수 |
|---|---|
| `.kr` / `.한국` TLD | 100 (확정, 접속 실패해도 적용) |
| `html lang="ko*"` 또는 `og:locale=ko_KR` | 80 |
| 본문 한글 음절(가–힣) 비율 ≥ 10% | 80 |
| charset EUC-KR / CP949 | 60 |
| title/description에 한글 포함 | 40 |
| 본문 한글 비율 3~10% | 30 |

임계값 기본 100점. `reasons` 컬럼에 근거를 기록해, "TLD만으로 잡힌 것"과 "실제 한국어 콘텐츠"를 사후 분리할 수 있게 한다.

### 4단계: `classify-rule` — 규칙 기반 분류

`category_rules` 테이블을 읽어 카테고리별 점수 산출:
- `domain` 규칙: 도메인 문자열 매칭 (예: 쇼핑 ← `shop`, `mall`, `coupang`)
- `keyword` 규칙: title/description/본문 매칭 (예: 증권 ← `코스피`, `코스닥`, `HTS`)
- `exclude` 규칙: 해당 카테고리에서 제외 (예: 쇼핑에서 `쇼핑몰 제작`, `호스팅`)

단일 카테고리가 고득점 & 2위와 격차가 크면 `method='rule'`로 확정, 애매하면 LLM 단계로 이월. **멀티라벨 허용** — 포털처럼 뉴스·웹메일·쇼핑에 동시 해당하면 `categories` 배열에 담고 대표값을 `primary_category`에 저장.

`manual_labels`에 있는 도메인은 항상 사람 라벨을 우선하고 덮어쓰지 않는다.

### 5단계: LLM 판정 — Claude Code 세션 파일 핸드오프

API를 호출하지 않는다. 스크립트가 세션에 직접 질문을 넣을 수는 없으므로, **파일을 사이에 두고 주고받는 방식**으로 구현한다. 콘솔에 Claude Code 세션을 열어두고 아래 루프를 반복한다.

```
① python run.py llm-export --size 40
     → llm/requests/batch_0001.md 생성 (판정 대기 40건)
       llm_queue / llm_batches에 배치 상태 기록 (status='exported')

② 열어둔 Claude Code 세션에서:
     "llm/requests/batch_0001.md 를 읽고 분류해서
      llm/results/batch_0001.json 으로 저장해줘"
     → 세션이 파일을 Read → 분류 → Write

③ python run.py llm-import llm/results/batch_0001.json
     → classification에 method='llm'으로 반영, 배치 status='imported'

④ ①로 돌아가 다음 배치 (llm-export는 자동으로 다음 미판정 건을 집어옴)
```

**요청 파일 형식** (`batch_0001.md`) — 세션이 읽기 좋게 마크다운으로. 파일 머리에 10개 카테고리 정의와 출력 JSON 스키마를 매번 포함시켜, 어느 세션에서 열어도 앞선 대화 맥락 없이 그대로 처리 가능하게 만든다.
```markdown
# 분류 요청 batch_0001 (40건)
카테고리: 웹메일 / 쇼핑 / 증권 / 취업 / 게임 / 엔터테인먼트 / 뉴스 / SNS / 웹하드 / 생성형AI
해당 없으면 "none". 복수 해당 가능.
출력: llm/results/batch_0001.json 에 아래 스키마로 저장
  [{"domain": "...", "categories": ["..."], "confidence": 0.0~1.0, "evidence": "판단 근거 한 줄"}]

---
## 1. example.co.kr
- title: ...
- description: ...
- 본문: (앞 300자)
```

**배치 크기**: 기본 40건 (건당 ~400자 → 배치당 약 1.6만 자). 세션 컨텍스트에 부담 없고, 배치 하나를 끝내면 그 내용은 잊어도 되므로 100배치를 연속 처리해도 컨텍스트가 누적되지 않는다.

**중단/재개**: 배치 상태가 DB에 있으므로 어느 지점에서 멈춰도 `llm-export`가 아직 판정되지 않은 건부터 다시 만들어 준다. `llm-import`는 같은 파일을 두 번 넣어도 안전(upsert).

- 대상 선정: `is_korean=1` AND (분류 없음 OR 규칙 미확정) AND 수동 라벨 없음
- `llm-import` 시 스키마 검증 — 정의되지 않은 카테고리명이나 누락 도메인은 오류로 보고하고 해당 건만 큐에 남긴다
- `--auto-prompt` 옵션으로 ②에 붙여넣을 문장을 콘솔에 출력해 복사 부담을 줄인다

### 6단계: `export`

`out/` 아래에 `korean_sites.csv`, `classified_sites.csv`, `by_category/{카테고리}.csv` 10개, `summary.md`(크롤링 성공/실패, 한국어 건수, 카테고리별 건수, LLM 호출 수).

---

## 관리 웹 UI (`python run.py web`)

FastAPI + Jinja2 + 바닐라 JS. 빌드 도구 없이 `http://localhost:8000`에서 바로 뜨는 로컬 전용 화면. **localhost 바인딩 고정**(외부 노출 안 함).

### `/` 대시보드 — 진행 상황
- 크롤링 진행률 바: `pending / ok / failed / dead` 건수와 %, 현재 처리 속도(건/분), 남은 시간 추정
- 한국어 판정 현황: 판정 완료 수, `is_korean=1` 수
- 분류 현황: 규칙 확정 / LLM 확정 / 수동 / 미분류 수, **카테고리별 건수 막대**
- LLM 배치 현황: 내보낸 배치 수 / 반영된 배치 수 / 판정 대기 건수, 아직 import 안 된 배치 파일 목록
- 실행 중인 `job_runs` 상태와 마지막 메시지
- 3초 폴링으로 자동 갱신 (`GET /api/stats`)

### `/categories` — 카테고리 기준 정보 수정 ★
- 10개 카테고리별로 `domain / keyword / exclude` 규칙을 표로 표시
- 규칙 추가·수정·삭제·on/off 토글, 가중치 조정 → 저장 시 `category_rules`에 반영
- **미리보기**: 규칙을 저장하기 전에 "이 규칙을 적용하면 몇 건이 매칭되는지 + 샘플 20건"을 조회 (`POST /api/rules/preview`) → 잘못된 규칙을 전량 재분류 전에 확인
- "이 카테고리만 재분류" 버튼 → 백그라운드로 `classify-rule` 부분 실행

### `/settings` — 판정 기준 수정
- 한국어 판정 가중치 6종과 임계값, 크롤러 동시성/타임아웃, LLM 배치 크기·본문 발췌 길이를 폼으로 편집 → `settings` 테이블 저장
- "한국어 재판정 실행" 버튼 (네트워크 불필요, 수 분 소요) → 진행률은 대시보드에 표시

### `/sites` — 사이트 조회·검수
- 필터: 도메인 검색, 순위 구간, 한국어 여부, 카테고리, 분류 방식(rule/llm/manual)
- 각 행에서 판정 근거(`reasons`, `evidence`)와 수집된 title/description 확인
- **수동 라벨 오버라이드**: 한국어 여부·카테고리를 직접 고쳐 `manual_labels`에 저장 → 이후 재분류에도 보존
- 오분류를 발견하면 그 자리에서 "이 키워드를 규칙에 추가" 버튼으로 `/categories`에 반영

### API 엔드포인트
`GET /api/stats`, `GET /api/sites`, `GET|POST|DELETE /api/rules`, `POST /api/rules/preview`, `GET|POST /api/settings`, `POST /api/labels`, `POST /api/jobs/{detect-korean|classify-rule}`, `GET /api/jobs`

재계산 작업은 웹에서 트리거하되 `asyncio` 백그라운드 태스크로 돌리고 진행률은 `job_runs`에 기록한다. **크롤링(`fetch`)은 장시간 작업이라 웹 버튼이 아닌 CLI 전용**으로 둔다. LLM 배치는 `llm-export`까지만 웹에서 트리거할 수 있게 하고(파일 생성만 하므로 안전), 실제 판정과 `llm-import`는 콘솔에서 수행한다.

---

## 검증 (Verification)

**파일럿 우선** — 전량 실행 전에 상위 2,000개 도메인으로 전 파이프라인을 한 번 돌린다.
```bash
python run.py init
python run.py fetch --limit 2000 --concurrency 50
python run.py detect-korean && python run.py classify-rule
python run.py llm-export --size 40      # 배치 1개 → 세션에서 판정 → llm-import 로 왕복 검증
python run.py web                       # 브라우저에서 결과 검수
```
확인 항목:
1. **한국어 판정 정확도** — naver.com·coupang.com·dcinside.com이 잡히는가, amazon.de·baidu.com이 오탐되지 않는가
2. **카테고리 정확도** — `/sites`에서 카테고리별로 훑어보며 `/categories`에서 규칙 튜닝 → 재분류 → 재확인 (이 루프가 웹 UI의 핵심 용도)
3. **중단/재개** — `fetch` 실행 중 Ctrl+C → 대시보드에서 진행률 확인 → 다시 `fetch` 실행 시 처리된 건을 건너뛰는지 확인. `llm-export` → `llm-import` 왕복 후 같은 도메인이 다음 배치에 다시 나오지 않는지 확인
4. **동시 접근** — 크롤러가 도는 중에 웹 UI가 잠금 없이 조회·규칙 저장되는지 확인
5. **처리 속도 측정** — 파일럿 결과로 전량 소요 시간 역산 → 동시성 확정

파일럿에서 규칙이 안정되면 전량 실행으로 넘어간다.

---

## 주의사항

- 홈페이지 1회 요청이지만 100만 도메인 대상이므로 동시성은 150 이하로 유지하고 실패 시 무한 재시도하지 않는다.
- `detect-korean` / `classify-rule`은 네트워크 없이 재실행 가능하므로, 규칙 튜닝은 재크롤링 없이 반복할 수 있다. 크롤링 원본을 보존적으로 저장하는 이유다.
- 웹 UI는 인증 없는 로컬 전용이므로 `127.0.0.1`에만 바인딩한다.
- 작업 완료 시 `/Users/msk/Documents/Claude/Projects/PF_Website_Classification-And-Review-System_single/WORK_LOG.md`에 기록한다.

## 구현 순서

1. `db.py` + `seed.py` + `run.py init` — 스키마와 데이터 적재
2. `fetch.py` + `extract.py` — 크롤러 (중단/재개 + job_runs 기록 포함)
3. `korean.py` + `run.py detect-korean`
4. `web/app.py` 대시보드 + `/settings` — 여기서부터 진행 상황을 눈으로 보며 작업
5. `classify_rule.py` + `/categories` 규칙 편집 화면
6. `/sites` 검수 화면 + `manual_labels`
7. `llm_batch.py` (`llm-export` / `llm-import`)
8. `export.py`

---

# 구현 결과와 계획의 차이

계획은 그대로 구현되었고, 아래 항목만 달라졌다. (2026-08-03 구현 완료)

## 1. 설계를 바꾼 것 — 계획대로 하면 문제가 생기는 부분

**`.kr` 도메인을 접속 전에 판정하지 않는다**

계획에는 "`.kr` TLD는 접속 실패해도 100점 확정"이라고 적었는데, 이대로 만들었더니
**아직 크롤링도 하지 않은** `.kr` 도메인 4,760건이 본문 없이 한국어로 확정됐다.
나중에 크롤링해서 본문이 생겨도 이미 `korean` 테이블에 있어 재판정되지 않는다.

→ 판정 대상을 **크롤링 시도가 끝난 행(`ok`/`failed`/`dead`)** 으로 한정했다.
접속에 실패한 `.kr` 도메인이 TLD 점수만으로 한국어가 되는 동작 자체는 그대로다.

**"이 카테고리만 재분류" 기능을 없앴다**

한 카테고리의 규칙만 적용하면 나머지 카테고리 점수가 0이 되어, 다른 카테고리로
분류돼 있던 기존 결과가 전부 지워진다.

→ 분류는 **항상 전체 규칙으로** 수행한다. CLI `--category` 옵션과 웹의
카테고리별 재분류 버튼을 제거했다. 한국어 사이트만 대상이라 전체 재분류도 수 분이면 끝난다.

**도메인 규칙 매칭을 토큰 정확 일치로 구체화했다**

계획에는 "도메인 문자열 매칭"이라고만 적었는데, 단순 부분일치로 하면
생성형AI의 `ai` 가 `mail`·`air` 에 걸리는 오탐이 대량으로 발생한다.

→ 기본은 **도메인 라벨 토큰과의 정확 일치**. 부분일치가 필요하면 패턴에 `.` 을 넣거나
(`sec.co.kr`) `*` 와일드카드를 쓴다 (`*sec*`). 웹 UI에도 이 규칙을 안내로 넣었다.

**부분일치 확장은 weight 2.0 이상이어야 의미가 있다**

토큰 정확 일치의 대가로 `daisomall`·`wedisk`·`koreatimes` 처럼 토큰이 붙어 있는
도메인을 통째로 놓친다. 뉴스에 `*news*` 를 넣어 41건이 살아난 뒤 나머지 9개
카테고리도 같은 방식으로 점검했는데, 여기서 두 가지가 걸렸다.

첫째, `classify.min_score` 가 2.0 이라 **weight 1.5 짜리 패턴은 도메인 규칙만으로는
절대 분류되지 않는다.** `shop`·`mall`·`store`·`cafe`·`daily`·`press`·`times`·
`cloud`·`invest` 가 전부 1.5였다. 부분일치를 추가할 때는 weight 도 2.0 이상으로 준다.

둘째, 미분류 사이트의 절반가량은 **제목도 본문도 없다**(2,177건 중 제목 없음 1,042건).
키워드·exclude 규칙이 원천적으로 못 붙으므로 이들에게는 도메인 규칙이 유일한 수단이다.
`makeshop` 이 `쇼핑몰 솔루션` exclude 로 걸러지는 건 제목이 있을 때뿐이다.

→ 부분일치는 **실측으로 검증한 것만** 추가한다. 미분류 도메인에 패턴을 대입해
매칭 건수와 샘플을 먼저 뽑고(`preview_rule` 또는 전체 재분류 시뮬레이션),
오탐이 보이면 반려한다. 실제로 `*flo*`(tensorflow·floorplanner),
`*press*`(nespresso·aliexpress), `*cafe*`(cafe24 호스팅), `*stock*`(istockphoto),
`*tmon*`(tmoney), `*novel*`(novelship) 등 11개를 이 방식으로 걸렀다.

**TLD 와 겹치는 패턴은 레이블 끝(`*shop.*`)으로 제한한다**

`*shop*` 은 `.shop` TLD 를 통째로 삼킨다. tranco 100만에 `.shop` 5,192건 /
`.store` 1,335건이 있고, 싸게 풀린 TLD라 도박·불법 스트리밍 비중이 높다
(`dnsguide.shop`, `alternatifpandora188.store` 가 실제로 쇼핑으로 잡혔다).

→ TLD 로도 존재하는 단어는 `*shop.*`·`*store.*` 처럼 **뒤에 점을 붙여** 도메인 라벨
끝일 때만 매칭시킨다. `makeshop.co.kr`·`tcgshop.co.kr` 은 잡고 `dnsguide.shop` 은
거른다. 대신 `shopbop`·`shopee` 처럼 단어가 앞에 오는 건은 놓친다 — 감수한 손실이다.
`.disk`·`.file`·`.mail` 은 TLD 가 존재하지 않아 `*disk*` 형태를 그대로 썼다.

**검수한 사이트는 검수 시점 값을 박제한다**

계획에는 "사람 손을 탄 데이터는 재분류가 덮어쓰지 않는다"고만 적었고, 실제 보호 장치는
`classification.method = 'manual'` 하나였다. 즉 **라벨을 달았느냐**가 기준이었다.
그런데 검수 화면의 `제외` 버튼은 `review_status` 행만 쓰고 라벨을 만들지 않는다.
결과적으로 제외 처리한 891건이 통째로 무방비였고, LLM 판정 반영이 실제로 23건의
카테고리를 바꿔 놓았다(`wix.com` SNS→없음, `daum.net` 없음→뉴스 등).

제목은 별개로 더 뚫려 있었다. 검수 화면은 `제목 !== 수집제목` 일 때만 저장해서,
제목을 안 고치고 검수하면 `manual_labels.title` 이 NULL 이 된다. 화면은
`COALESCE(m.title, s.title)` 로 폴백하므로 **재수집되면 검수한 사이트의 제목이 바뀐다.**

→ 검수·제외를 누르는 시점에 그때의 제목·카테고리·한국어 여부를 `manual_labels` 에
박제한다(`_freeze_reviewed`). 기존 `method = 'manual'` 보호 장치를 그대로 타므로
LLM 반영·규칙 재분류·LLM 자동선정 세 경로가 한 번에 닫힌다. 검수를 해제하면
자동 박제분만 지운다(`_unfreeze_reviewed`) — 사람이 직접 고친 라벨은 남긴다.

박제가 화면을 오염시키지 않도록 `manual_labels` 에 두 컬럼을 더했다.
`source`(`edit` = 사람이 고침 / `review` = 검수로 박제)와 `origin_method`(박제 직전의
분류 방식). 검수했다는 이유만으로 전부 '수동'이 되면 분류방식 필터가 무의미해지므로
목록과 필터는 `source='review'` 인 건에 대해 `origin_method` 를 그대로 보여준다.
'수정됨' 배지는 `저장된 제목 != 수집한 제목` 으로 판정을 바꿨다.

한국어 판정도 같이 막았다. `detect-korean --force` 와 차단 마킹이 `reasons='manual'`
을 덮어쓰면 검수한 사이트가 `is_korean=0` 이 되어 목록에서 통째로 사라진다.
둘 다 `reasons != 'manual'` 조건을 넣었다.

## 2. 만들지 않은 것

| 계획 | 실제 | 이유 |
|---|---|---|
| `src/jobs.py` | 없음 | 진행률 기록이 `db.start_job` / `db.update_job` 몇 줄로 끝나 `db.py` 에 흡수 |
| `seed/settings.seed.yaml` | 없음 | 기본 설정은 `db.py` 의 `DEFAULT_SETTINGS` 상수(20개)로 관리. 설명 문구를 코드 옆에 두는 편이 나음 |
| `/sites` 의 "이 키워드를 규칙에 추가" 버튼 | 미구현 | 검수 화면에서 규칙 화면으로 이동해 추가하면 되고, 미리보기 없이 규칙이 늘어나는 것이 오히려 위험 |
| `export` 의 XLSX 산출 | CSV만 | `utf-8-sig` 로 저장해 엑셀에서 바로 열린다 |

## 3. 추가한 것

- **`run.py status`** — 콘솔에서 전체 진행 상황을 한 번에 보는 명령. 웹을 띄우지 않고 확인
- **`run.py compact`** — 비한국어 사이트 본문 발췌 정리 + VACUUM (계획엔 동작만 있고 명령이 없었음)
- **`run.py llm-status`** — 반영 대기 중인 배치 확인
- **`classify.llm_conf_threshold` 설정** — 규칙 분류의 확신도가 이 값 미만이면 세션 판정으로
  넘긴다. 계획에는 "애매하면 넘긴다"고만 되어 있어 기준을 설정값으로 뺐다

## 4. CLI 세부 차이

| 계획 | 실제 |
|---|---|
| `retry --max-attempts 3` | `retry` (재시도 상한은 설정 `fetch.max_attempts`) |
| `llm-export --auto-prompt` | 안내 문구 출력이 기본, 끄려면 `--no-prompt` |

## 5. 검증 결과

계획의 파일럿(2,000건)보다 작은 **상위 300건**으로 전 파이프라인을 돌렸다.
검증 항목 5개를 모두 확인했으므로 규모를 더 키우지 않았다.

| 항목 | 결과 |
|---|---|
| 크롤링 | 성공 206 / 일시 실패 18 / 접속 불가 76. 실패 대부분은 CDN·인프라 도메인 |
| 한국어 판정 | 47건. `Accept-Language: ko-KR` 로 google·youtube·netflix가 정상 포착 |
| 규칙 분류 | 15건 확정. 오분류(skype→웹하드 등)는 확신도가 낮아 세션 판정으로 이월됨 |
| 세션 판정 왕복 | export → 분류 → import 정상. 반영된 도메인이 다음 배치에 다시 실리지 않음 |
| 웹 UI | 4개 화면과 API 쓰기 동작 전부 확인 |
| 처리 속도 | 동시 50에서 25~36건/초 |

**남은 확인 사항** — 계획의 검증 항목 1번에 있던 naver.com·coupang.com·dcinside.com은
상위 300위 밖이라 이번 파일럿에 포함되지 않았다. 크롤링을 상위 2만 건 정도까지
진행한 뒤 한 번 확인하는 것이 좋다.

## 6. 확인된 트레이드오프

`Accept-Language: ko-KR` 를 보내는 설계 때문에, 한국어 사이트 목록에
`doubleclick.net`·`googlevideo.com`·`google-analytics.com` 같은 광고·CDN 도메인이
함께 들어온다. 판정 근거가 `korean.reasons` 에 남아 사후 분리가 가능하고 임계값도
웹에서 조정할 수 있지만, **크롤링 규모를 키운 뒤 기준을 다시 정할 필요가 있다.**

---

## 7. 계획 이후 추가된 요구 (2026-08-03)

**모든 작업을 웹 UI에서 실행** — 계획에서는 "크롤링은 장시간 작업이라 CLI 전용"으로
두었으나, 콘솔을 거치지 않고 웹에서 전부 실행하고 싶다는 요구가 들어와 변경했다.

- `fetch.run_fetch_async()` 를 분리해 CLI와 웹이 같은 경로를 쓰게 했다.
  웹에서 호출할 때는 `stop` 이벤트를 넘겨 중지 버튼과 연결하고,
  `install_signal=False` 로 서버의 SIGINT를 가로채지 않게 한다.
- 웹 작업은 전부 백그라운드 태스크로 돌린다. 요청은 즉시 반환하고 진행률은
  `job_runs` 를 3초 폴링해 보여준다. 한 번에 하나만 실행한다.
- 추가된 엔드포인트: `POST /api/jobs/fetch`(mode=pending|retry), `/api/jobs/stop`,
  `/api/jobs/compact`, `/api/jobs/export`, `/api/jobs/llm-import`, `GET /api/jobs/current`
- 대시보드에 크롤링 컨트롤(건수·동시 요청 수·시작·재시도·중지), 배치별 반영 버튼,
  산출물 생성 버튼, 실행 상태 배너를 추가했다.

**구현 중 발견한 함정** — `_start()` 는 `asyncio.create_task` 를 쓰므로 반드시
`async def` 라우트에서 호출해야 한다. 동기(`def`) 라우트는 FastAPI가 스레드풀에서
실행하기 때문에 실행 중인 이벤트 루프가 없어 작업이 시작되지 않는다.
처음에 동기로 만들었다가 후속 작업이 전혀 시작되지 않아 찾아냈다.

**남는 제약** — 크롤링이 웹 서버 프로세스 안에서 돌기 때문에 서버를 끄면 크롤링도
멈춘다. 중단/재개 설계 덕에 데이터 손실은 없지만, 장시간 무인 실행은 콘솔의
`nohup ... run.py fetch` 가 여전히 안전하다.

## 8. 계획 이후 추가된 요구 — 검수 최종 상태 (2026-08-03)

사이트 검수 화면에서 사이트별로 **검수완료 / 제외** 를 지정하고 그것으로 필터링하는
기능을 추가했다.

- `review_status(domain, status, reason, updated_at)` 테이블 신설.
  분류 라벨과 **독립적인 테이블**로 둔 이유는, 라벨은 그대로 두면서 "확인만 했다"거나
  "결과에서 빼겠다"를 표시할 수 있어야 하기 때문이다. 재분류·재판정에도 유지된다.
- 엔드포인트: `POST /api/review`(단건, status=null이면 미검수로 되돌림),
  `POST /api/review/bulk`(현재 페이지 일괄), `GET /api/sites` 에 `review` 필터
  (pending | reviewed | excluded)
- 검수 화면: 상태 필터 드롭다운, 행별 검수/제외/해제 버튼, 페이지 일괄 처리,
  상태 뱃지와 제외 사유 표시(제외 행은 흐리게), 수정 다이얼로그에서도 상태 지정
- 진행 상황 화면과 `run.py status` 에 검수 현황(완료/제외/미검수) 추가

**산출물 반영** — `제외` 는 최종 결과에서 빼겠다는 뜻이므로 export가 이를 따른다.
제외된 사이트는 `korean_sites.csv`·`classified_sites.csv`·`by_category/*.csv` 에서
빠지고 `excluded_sites.csv` 에만 사유와 함께 남는다. `summary.md` 에 검수 현황과
"결과에 포함" 건수를 넣었다.

## 9. 계획 이후 추가된 요구 — 제목 수정 · 카드형 상태 선택 (2026-08-03)

**제목 수정** — `manual_labels` 에 `title` 컬럼을 추가했다(기존 DB를 지우지 않도록
`db._migrate()` 로 `ALTER TABLE` 처리). 크롤링 원본 `sites.title` 은 그대로 두고
조회·검색·산출물에서 `COALESCE(m.title, s.title)` 로 덮어 쓴다. 재크롤링해도
사람이 고친 제목이 살아남는다. 목록에 `수정됨` 표시, 다이얼로그에 원래 제목 안내와
'수집한 제목으로 되돌리기' 버튼을 넣었다.

**카드형 상태 선택** — 수정 다이얼로그의 최종 상태를 `<select>` 에서 카드 3장으로
바꿨다. select는 열고 고르느라 두 번 눌러야 했다. 카드는 한 번이면 된다.

**접근성 함정** — 라디오를 `opacity:0; pointer-events:none` 으로 숨겼더니 접근성
트리에서 사라져 키보드·스크린리더로 선택할 수 없었다. sr-only 패턴(`clip-path:
inset(50%)`)으로 바꾸고 `role="radiogroup"` + 한국어 `aria-label` 을 붙여 해결했다.
브라우저 접근성 트리를 직접 읽어 확인했다.

## 10. 계획 이후 추가된 요구 — 검수완료분만 추출 (2026-08-04)

산출물을 두 갈래로 만들 수 있게 했다.

| | 출력 | 담기는 것 |
|---|---|---|
| 전체 (`run.py export`) | `out/` | 미검수 + 검수완료 (제외분만 뺌) |
| 검수완료분 (`--reviewed-only`) | `out/reviewed/` | `review_status='reviewed'` 인 것만 |

- 출력 폴더를 나눈 이유: 같은 파일명을 쓰므로 한쪽이 다른 쪽을 덮어쓰면
  "지금 이 CSV가 어느 쪽이지?"를 알 수 없게 된다. 나란히 두고 비교할 수 있게 했다.
- `excluded_sites.csv` 는 전체 산출물에만 만든다 (검수완료분에는 의미가 없다).
- `out/reviewed/summary.md` 머리에 "검수완료분만 담았다"는 안내를 넣어,
  파일만 따로 전달받은 사람도 범위를 오해하지 않게 했다.
- 대시보드 산출물 카드에 버튼 두 개를 두고, 검수완료 건수를 문구에 실시간 표시한다.

## 11. 웹 소스 코드 리뷰·리팩토링 (2026-08-04)

기능 추가가 이어지며 `web/` 이 커져서 한 번 정리했다. 계획 문서에는 웹 UI가
"FastAPI 앱 + API 엔드포인트" 한 줄로만 잡혀 있었는데, 실제로는 **엔드포인트 29개,
`app.py` 795줄** 규모가 되었다. 동작은 그대로 두고 구조만 손봤다.

**중복 제거**
- DB 커넥션을 여닫는 `c = conn()` / `try` / `finally: c.close()` 패턴이 16곳에 반복됐다.
  `@contextmanager db_conn()` 을 만들어 `with db_conn() as c:` 한 줄로 통일했다.
  헬퍼 `conn()` 은 없앴다 (app.py 848줄 → 795줄)
- JS 유틸 `escapeHtml()` 이 세 템플릿에 똑같이 복사돼 있었다. `base.html` 로 옮겨
  한 곳에서만 정의한다

**고친 버그 2건**
- `sites.html` 이 행 데이터를 `onclick='openEdit("{...}")'` 처럼 HTML 속성에 JSON으로
  심고 있었다. 제목에 **작은따옴표가 들어가면 속성이 깨져 수정 버튼이 동작하지 않는다**
  (실제로 `adblockplus.org` — "The world's #1 free ad blocker"가 해당). 데이터를 속성에
  넣지 않고 `currentItems[idx]` 로 참조하도록 바꿨다. `setReview`·`addRedirect` 도 동일
- `settings.html` 이 설정값을 이스케이프 없이 `value="..."` 에 넣고 있었다.
  값에 따옴표가 섞이면 입력칸이 깨진다. `escapeHtml()` 적용

**점검했으나 문제 없던 것**
- SQL 문자열 포맷 사용처 9곳 전부 — 값은 모두 파라미터 바인딩이고, 포맷에 끼우는 것은
  코드가 만든 고정 문자열(컬럼명·플레이스홀더)뿐이라 주입 위험 없음
- 미사용 import·죽은 코드 없음

**검증** — 별도 포트에서 페이지 4개와 API 12개 스모크 테스트, 오류 경로 7종
(400/404/409) 확인, 작은따옴표 제목 사이트에서 수정 버튼 동작 확인, 콘솔 에러 없음.
크롤링 코드(`src/fetch.py`·`src/extract.py`)는 손대지 않았고, 진행 중이던 크롤링도
영향 없이 계속 돌았다.

## 12. 계획 이후 추가된 요구 — 검수 화면에서 LLM 분석 요청 (2026-08-04)

계획에서 LLM 판정 대상은 "규칙으로 확정되지 않은 건"을 자동 선정하는 것뿐이었다.
검수하다 판단이 어려운 사이트를 **사람이 직접 지목해** 판정을 맡길 수 있게 했다.

- 기존 `llm_queue` 를 그대로 쓰되, `batch_id IS NULL` 인 행을 "사람이 요청했으나
  아직 배치로 나가지 않은 건"으로 해석한다. 테이블 추가 없이 상태 하나가 늘었다.
- `select_pending()` 이 두 갈래를 합쳐 고르고, 요청건을 **맨 앞에** 배치한다.
  배치 파일에는 `★` 와 "검수자가 직접 분석을 요청한 사이트" 문구가 붙는다.
- 엔드포인트: `POST /api/llm/request`(단건·일괄), `DELETE /api/llm/request/{domain}`
- 검수 화면: 행별 `LLM 요청` / `LLM 요청됨`+`취소` / `LLM 분석 중`(배치로 나간 뒤),
  페이지 일괄 요청 버튼

**자동 선정 조건을 요청건에는 적용하지 않는다** — 원래 조건(한국어로 판정됐고,
수동 라벨이 없고, 규칙 확정도가 낮을 것)을 그대로 두면 사람이 요청해도 배치에서
빠지는 일이 생긴다. 실제로 `facebook.com`(수동 라벨 보유)이 그랬다. 사람이 명시적으로
요청한 건은 조건을 우회하도록 고쳤다.

다만 반영 단계의 `method != 'manual'` 보호는 유지한다. 수동 지정한 사이트에 요청할
때는 "결과가 카테고리를 덮어쓰지 않는다"고 UI에서 알린다.

## 13. 계획 이후 추가된 요구 — 세션 판정 자동화 (2026-08-04)

계획의 세션 판정은 "콘솔에 세션을 열어두고 배치 파일을 붙여넣는" 수동 절차였다.
`claude` CLI의 headless 모드(`claude -p`)를 불러 **배치 생성 → 판정 → 반영을
자동 반복**하도록 만들었다.

- `src/llm_auto.py` 신설
  - 배치 파일 내용을 **표준입력으로** 넘긴다. CLI에 파일 접근 권한을 주지 않아도 되고,
    결과 JSON만 받아 기존 `llm-import` 경로로 반영하므로 수동 흐름과 결과가 같다
  - 응답에서 JSON 배열을 꺼낼 때 코드펜스·앞뒤 설명이 붙어도 견디게 했다
  - 로그인 실패는 `AuthError` 로 구분해 "터미널에서 `claude` 로 로그인하라"고 안내
- `run.py llm-auto [--rounds N] [--size N] [--check]`, 웹의 `자동 판정 실행` 버튼
  (백그라운드 작업이라 중지 가능)

**구현 중 발견한 결함** — 판정이 실패해도 배치는 그대로 남는다. 그러면 거기 실린
도메인이 `llm_queue` 에서 "이미 나간 건"으로 묶여 **다음 배치에서도 영영 빠진다.**
실제로 인증 실패 테스트 한 번에 40건이 그 상태가 됐다.

→ `cancel_batch()` 를 만들어 실패 시 자동으로 되돌린다. 되돌릴 때 사람이 요청한 건과
자동 선정된 건을 구분해야 해서 `llm_queue.manual` 컬럼을 추가했다(마이그레이션).
사람이 요청한 건은 요청 상태로 복구하고, 자동 선정된 건은 큐에서 지운다.

**현재 상태** — 이 맥의 `claude` CLI는 OAuth 세션이 만료돼 headless 호출이 실패한다.
자동 롤백까지는 검증했고, 로그인만 갱신하면 그대로 동작한다.

## 14. 계획 이후 발견한 데이터 품질 문제 — 국내 차단 사이트 (2026-08-04)

계획에는 없던 상황이다. 불법·유해 사이트에 접속하면 ISP가 방송통신심의위원회
안내 페이지(`warning.or.kr`)로 돌려보내는데, 그 **차단 안내문이 수집**된다.
안내문이 한국어이므로 한국어 판정을 그대로 통과한다.

실제로 **839건이 차단 페이지였고 그중 508건이 한국어로 판정**되어 검수 목록에
들어와 있었다. `xhamster*`, `pornone` 같은 성인 사이트가 "한국어 서비스"로
분류되고 있었던 셈이다. 사용자는 이미 206건을 손으로 제외 처리하던 중이었다.

- `src/blocked.py` 신설 — 도착 URL 호스트로 판정(제목 문구는 보조).
  `fetch_status = 'blocked'` 라는 상태를 하나 추가했다. `failed` 로 두면 재시도
  대상이 되고, `ok` 로 두면 계속 정상 수집물로 취급되기 때문이다.
- 크롤러가 수집 시점에 자동 판정하고, 기존 데이터는 `run.py mark-blocked` 로 정리
- 차단 사이트는 한국어 판정·분류·산출물에서 제외하되 **목록은 남긴다**
  (검수 화면의 `차단여부` 필터로 조회 가능)
- 사람이 지정한 검수 상태·수동 라벨·LLM 판정은 건드리지 않는다. 여러 번 실행해도 안전

**함께 고친 것** — 규칙 재분류가 LLM 판정 결과를 덮어쓰고 있었다.
`manual` 만 보호하고 `llm` 은 보호하지 않아, 전체 재분류 한 번이면 `naver.com` 의
멀티라벨 같은 세션 판정 결과 45건이 규칙 결과로 대체될 상황이었다.
`NOT IN ('manual', 'llm')` 으로 바꿨다.
