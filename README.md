# 웹사이트 분류·검수 시스템<br><sub>PF_Website_Classification-And-Review-System_single</sub>

tranco 상위 100만 사이트에서 **① 한국어 서비스를 하는 사이트**를 추리고,
그중 **② 제품 카테고리 10종에 해당하는 사이트**를 다시 추려낸다.

카테고리: 웹메일 · 쇼핑 · 증권 · 취업 · 게임 · 엔터테인먼트 · 뉴스 · SNS · 웹하드 · 생성형AI

모든 단계는 **중단/재개**가 된다. Ctrl+C로 끊고 같은 명령을 다시 실행하면
아직 처리되지 않은 행부터 이어서 진행한다. 상태는 전부 `data/sites.db`(SQLite)에 있다.

> **문서 안내**
> - 이 문서 — 설계와 구조
> - [GUIDE.md](GUIDE.md) — 실제로 돌리는 절차 (서비스 실행 가이드)
> - [DATA_TIERS.md](DATA_TIERS.md) — 소스/작업/릴리즈 3계층 설계 + PCFILTER 연동 (v2)
> - [PLAN.md](PLAN.md) — 착수 시점 계획 원문 + 구현하며 달라진 부분
> - [schema/schema.sql](schema/schema.sql) — DB INIT 스키마 정의

## 준비

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python run.py init      # DB 생성 + 100만 행 적재 + 규칙 시드
```

## 진행 순서

```bash
# 1. 크롤링 — 가장 오래 걸린다. 끊어서 여러 번 실행하면 된다
./venv/bin/python run.py fetch --limit 50000
./venv/bin/python run.py retry              # 일시 실패한 도메인만 재시도

# 2. 한국어 판정 — 네트워크 불필요, 몇 분이면 끝나고 몇 번이든 다시 돌릴 수 있다
./venv/bin/python run.py detect-korean
./venv/bin/python run.py compact            # 비한국어 사이트 본문 정리 + VACUUM

# 3. 규칙 분류 — 역시 네트워크 불필요
./venv/bin/python run.py classify-rule

# 4. 애매한 건은 Claude Code 세션에 넘겨 판정 (아래 참고)
./venv/bin/python run.py llm-export

# 5. 산출물
./venv/bin/python run.py export                  # out/ — 전체
./venv/bin/python run.py export --reviewed-only  # out/reviewed/ — 검수완료분만

# 6. 릴리즈 — 검수완료분을 버전으로 고정하고 PCFILTER 형식으로 내보낸다
./venv/bin/python run.py release-create v1.0.0 --dry-run   # 집계만, DB 안 건드림
./venv/bin/python run.py release-create v1.0.0
./venv/bin/python run.py release-export v1.0.0   # out/release/v1.0.0/ — CSV + INSERT문
./venv/bin/python run.py release-list

# 언제든지
./venv/bin/python run.py status             # 진행 상황 요약
./venv/bin/python run.py web                # 관리 UI → http://127.0.0.1:8000
```

## 세션 판정 (LLM)

API를 쓰지 않고, 콘솔에 열어둔 Claude Code 세션과 **파일로 주고받는다**.

```bash
./venv/bin/python run.py llm-export --size 40
#   → llm/requests/batch_0001.md 생성, 붙여넣을 문장을 함께 출력한다
#
#   세션에 붙여넣기:
#     llm/requests/batch_0001.md 를 읽고 지시대로 분류해서 llm/results/batch_0001.json 로 저장해줘
#
./venv/bin/python run.py llm-import llm/results/batch_0001.json
./venv/bin/python run.py llm-status         # 남은 배치 확인
```

배치 상태가 DB에 남으므로 어디서 멈춰도 이어서 할 수 있다. 이미 판정된 도메인은
다음 배치에 다시 실리지 않고, 결과에서 빠진 도메인은 큐에 남아 다음 배치로 넘어간다.

## 관리 웹 UI

`run.py web` (localhost 전용, 인증 없음)

| 화면 | 용도 |
|---|---|
| 진행 상황 | 진행률·카테고리별 건수·배치 현황. **크롤링을 포함한 모든 작업 실행** |
| 카테고리 규칙 | 카테고리별 도메인·키워드·제외어 규칙 추가/수정/삭제, **저장 전 매칭 미리보기** |
| 판정 기준 | 한국어 판정 가중치·임계값, 크롤러 설정, 배치 크기 |
| 사이트 검수 | 필터 조회, 판정 근거 확인, **제목·카테고리 수정**, **최종 상태(검수완료/제외)** |

작업은 백그라운드 태스크로 돌고 진행률은 `job_runs` 를 통해 3초마다 갱신된다.
크롤링만 중지할 수 있고 나머지는 몇 분 안에 끝난다. **한 번에 하나만 실행한다** —
작업들이 같은 테이블을 쓰기 때문이다. `init` 만 콘솔 전용이다(DB가 있어야 웹이 뜬다).

크롤링을 웹에서 돌리면 웹 서버 프로세스 안에서 돌아가므로 **서버를 끄면 함께 멈춘다.**
저장된 데이터는 그대로라 다시 시작하면 이어지지만, 장시간 무인 실행은
콘솔에서 `nohup ... run.py fetch` 로 돌리는 편이 안전하다.

## 판정 방식

**한국어 판정** — 신호별 점수 합이 임계값(기본 100) 이상이면 한국어.

| 신호 | 기본 점수 |
|---|---|
| `.kr` / `.한국` TLD | 100 |
| `html lang=ko` 또는 `og:locale=ko_KR` | 80 |
| 본문 한글 비율 ≥ 10% | 80 |
| charset EUC-KR / CP949 | 60 |
| title·description에 한글 | 40 |
| 본문 한글 비율 3~10% | 30 |

크롤링 시 `Accept-Language: ko-KR` 를 보내므로 **다국어 글로벌 사이트도 한국어 페이지를
내주면 한국어로 잡힌다.** 의도한 동작이며, `korean.reasons` 컬럼에 근거가 남으므로
나중에 "TLD로 잡힌 것"과 "실제 한국어 콘텐츠"를 분리할 수 있다.

**카테고리 분류** — 규칙 점수로 확정하고, 확신도가 낮은 것만 세션 판정으로 넘긴다.
도메인 패턴은 오탐을 막기 위해 기본이 **토큰 정확 일치**다 (`ai` 가 `mail` 에 걸리지 않는다).
부분일치가 필요하면 `sec.co.kr` 처럼 `.` 을 넣거나 `*sec*` 와일드카드를 쓴다.

사람이 고친 값(`manual_labels` — 제목·카테고리·한국어 여부)은 재크롤링·재분류가
덮어쓰지 않는다. 조회와 산출물은 `COALESCE(수정본, 수집값)` 으로 표시한다.

**검수 상태** — 사이트 검수 화면에서 사이트마다 `검수완료` / `제외` 를 지정한다
(`review_status` 테이블). 분류 라벨과 독립적이라 재분류·재판정에도 유지된다.
`제외` 로 표시한 사이트는 산출물에서 빠지고 `excluded_sites.csv` 에만 남는다.
`--reviewed-only` 로 내보내면 `검수완료` 로 표시한 사이트만 `out/reviewed/` 에 담긴다.

## 데이터 3계층 (v2, 설계 완료·구현 예정)

수집 대상을 늘리고 결과를 PCFILTER 제품에 넘기기 위해 데이터를 세 계층으로 나눈다.

```
소스데이터            작업데이터                    릴리즈데이터
(업로드 원본)   →     (수집·판정·검수)      →      (검수완료 스냅샷)
소스별 테이블         단일 작업공간                 버전별 테이블
자유 스키마           기존 파이프라인 그대로        PCFILTER 스키마 고정
```

- **소스데이터** — 업로드한 CSV 원본. `top-1m.csv` 가 첫 사례다. 업로드마다 테이블 하나
- **작업데이터** — 지금의 `sites`·`korean`·`classification`·`review_status`. 하나뿐이다
- **릴리즈데이터** — 검수완료분을 버전으로 고정한 스냅샷. PCFILTER `default_website_t` 스키마

소스에서 작업데이터로 전달할 때 **이미 검수완료·제외한 도메인은 자동으로 빠진다.**
설계 전문은 [DATA_TIERS.md](DATA_TIERS.md).

## 구조

```
run.py                  CLI 엔트리포인트
src/db.py               스키마·설정·작업 진행률
src/seed.py             CSV 적재 + 규칙 시드 주입
src/fetch.py            비동기 크롤러 (중단/재개)
src/extract.py          인코딩 판별 + HTML 파싱
src/korean.py           한국어 판정
src/classify_rule.py    규칙 분류
src/llm_batch.py        세션 판정 배치 export/import
src/export.py           최종 산출물
web/                    FastAPI 관리 UI
schema/schema.sql       DB INIT 스키마 정의
seed/                   규칙 초기값 (주입 후에는 DB가 기준)
data/sites.db           모든 상태
llm/requests, llm/results
out/                    결과물
```
