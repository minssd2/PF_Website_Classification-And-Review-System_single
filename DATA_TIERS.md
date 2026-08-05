# 데이터 3계층 설계 — 소스 / 작업 / 릴리즈

수집 대상을 늘리고 결과를 외부 제품에 넘기기 위해 데이터를 세 계층으로 나눈다.

```
소스데이터            작업데이터                    릴리즈데이터
(업로드 원본)   →     (수집·판정·검수)      →      (검수완료 스냅샷)
소스별 테이블         단일 작업공간                 버전별 테이블
자유 스키마           기존 파이프라인 그대로        PCFILTER 스키마 고정
```

- **소스데이터** — 업로드한 CSV 원본. `top-1m.csv` 가 첫 사례다. 업로드마다 테이블 하나.
- **작업데이터** — 수집·한국어 판정·분류·검수가 벌어지는 곳. 하나뿐이다.
- **릴리즈데이터** — 검수완료분을 버전으로 고정한 스냅샷. 버전마다 테이블 하나.
  PCFILTER 제품의 `default_website_t` 스키마를 그대로 따른다.

스키마 정의는 [schema/schema.sql](schema/schema.sql) 에 있다.

---

## 1. 왜 작업데이터는 새로 만들지 않는가

3계층 중 소스와 릴리즈만 새로 만들고, **작업데이터는 기존 테이블을 그대로 쓴다.**
`sites` 에 컬럼 3개(`source_id`, `source_rank`, `added_at`)만 추가한다.

이유는 셋이다.

1. `fetch` · `detect-korean` · `classify-rule` · `llm-*` · 검수 UI 전부가
   `sites.domain` 에 물려 있다. 작업데이터를 재설계하면 파이프라인 전체를 다시 검증해야 한다.
2. 검수 450건 · 제외 1,253건 · 수동 라벨 458건이 이 테이블들에 들어 있다.
   이관 자체가 사고 위험이다. (2026-08-03 검수 데이터 삭제 사고 이력 있음)
3. 작업 공간은 어차피 하나다. 소스는 여러 개, 릴리즈도 여러 개지만 작업은 단일하다.

`sites.rank` 는 tranco 전용 순위였다. 소스가 늘면 의미가 깨지므로 `source_rank` 로 옮긴다.
기존 `rank` 컬럼은 당장 지우지 않는다. 지우면 기존 조회 코드가 전부 깨진다.

---

## 2. 소스데이터

### 업로드 규칙

- CSV **1행이 헤더**다.
- 헤더를 읽어 `domain` 과 `rank` 역할 컬럼을 자동 추정한다.
- **없는 헤더는 예외 처리한다.** `rank` 가 없으면 NULL, `domain` 을 못 찾으면
  업로드를 막고 사용자에게 컬럼을 직접 지정하게 한다.
- 예상 못 한 컬럼도 버리지 않는다. 행 전체를 JSON 으로 `raw` 에 남긴다.

도메인 추정에 쓰는 헤더 후보 (대소문자 무시):

| 역할 | 헤더 후보 |
|---|---|
| `domain` | `domain`, `url`, `site`, `host`, `hostname`, `address`, `url_address` |
| `rank` | `rank`, `ranking`, `no`, `order`, `순위` |

헤더가 없는 CSV(`top-1m.csv` 가 그렇다)는 컬럼 번호를 직접 지정한다.

### 테이블

카탈로그 `source_datasets` 가 단일 진입점이다. **테이블 이름을 직접 다루는 코드는
한 군데(`src/source.py`)로 몰아야 한다.** 업로드가 수십 개로 늘면 동적 DDL 관리 비용이 붙는다.

```
source_datasets(id, name, table_name, original_filename, column_map,
                row_count, pushed_count, uploaded_at, last_pushed_at, note)

src_0001_tranco_top1m(id, domain, source_rank, raw)
```

### 기존 top-1m 소급 등록

**원본 `top-1m.csv` 를 읽어서 만든다.** 소스데이터는 "업로드한 원본"이므로 CSV 그대로가
맞다. `sites` 에서 역으로 만들면 웹 UI 로 추가한 사이트(`aws.amazon.com` 등)까지
tranco 소스에 섞여 출처가 틀리게 된다.

실제 수치: CSV 1,000,000행 / `sites` 1,000,047행. 차이 **47건은 웹에서 수동 추가한 것**이라
소스에 넣지 않고 `source_id = NULL` 로 남긴다. 출처가 없다는 뜻이다.

---

## 3. 소스 → 작업 전달

```
전달 대상 = 소스 테이블의 도메인
          - sites 에 이미 있는 도메인            (중복)
          - review_status = 'reviewed' 인 도메인 (검수완료 → 자동 예외)
          - review_status = 'excluded' 인 도메인 (제외 → 자동 예외)
```

전달 후 리포트:

```
소스 고유 도메인    소스 테이블의 중복 제거 건수
신규 추가           sites 에 pending 으로 들어간 건수
검수완료 자동 예외  reviewed 라 건너뛴 건수
제외 자동 예외      excluded 라 건너뛴 건수
이미 작업 중        검수 이력 없이 이미 sites 에 있는 건수
출처 소급 연결      이미 있는데 출처가 비어 있어 이 소스로 표시한 건수
```

릴리즈된 도메인은 모두 `reviewed` 상태이므로 자동 예외에 이미 걸린다. 별도 처리가 필요 없다.

**검수 이력 필터는 `sites` 존재 여부와 겹친다.** `review_status` 에 있는 도메인은
어차피 `sites` 에도 있기 때문이다. 그래도 SQL 에 조건을 따로 둔다. 나중에 작업데이터를
정리하는 기능이 생겨도 검수한 도메인이 되살아나지 않게 하려는 방어다.

### 출처 소급 연결

이미 작업데이터에 있는데 `source_id` 가 비어 있는 행은 이 소스로 연결한다.
100만 행이 그렇게 소스 #1 에 붙었다. `source_rank` 도 함께 채운다.
이미 다른 소스로 표시된 행은 건드리지 않는다.

### 도메인 정규화

전달 시점에 정규화한 값으로 중복을 판정한다.

```
공백 제거 → 소문자화 → 프로토콜 제거 → 경로/쿼리 제거 → 후행 점 제거
```

`www.` 는 **제거하지 않는다.** `www.example.com` 과 `example.com` 을 다른 사이트로 본다.
(주의: `hash_id` 계산에서는 `www.` 를 제거한다. 5절 참고)

---

## 4. 릴리즈데이터

### 생성 절차

1. 대상 선정 — 작업데이터에서 `review_status = 'reviewed'` 인 도메인
2. 값 확정 — 제목·카테고리는 `COALESCE(manual_labels, 수집값)`
3. PCFILTER 스키마로 변환 (5절)
4. `hash_id` 중복 제거
5. `release_v{ver}` 테이블 생성 + 적재, `release_v{ver}_map` 에 역추적 정보 적재
6. `releases.status = 'fixed'` 로 고정. 이후 내용 변경 금지

**전량 스냅샷이다.** 매 버전이 그 시점의 검수완료분 전체를 담는다. 증분(delta)이 아니다.
스냅샷 간 비교는 추후 기능으로 추가한다.

### 미분류 검수완료분 처리

검수완료지만 카테고리가 없는 사이트는 **`기타`(코드 10)** 로 넣는다. (사용자 확정)
`ranky_category` 도 `'기타'` 가 된다.

빼고 싶으면 `--skip-uncategorized` 를 붙인다. v1.0.0 에서는 450건 중 2건이 여기 해당했다
(`crypto.com`, `chatdate.app`).

---

## 5. PCFILTER 스키마 매핑

대상 테이블은 `public.default_website_t` 다. 원본 DDL 은 [schema/schema.sql](schema/schema.sql) 주석에 있다.

| PCFILTER 컬럼 | 타입 | 채우는 값 |
|---|---|---|
| `pno` | bigserial | 자동 증가. **내보내기에서 제외한다** |
| `hash_id` | varchar(32) | 아래 규칙으로 계산한 MD5 hex |
| `url_address` | varchar | 프로토콜·경로 제거한 호스트. `www` 와 포트는 있을 수 있음 |
| `site_name` | varchar | `COALESCE(manual_labels.title, sites.title)` |
| `category` | int4 | `category_codes.code` |
| `ranky_category` | varchar | 카테고리명(한글) |
| `block` | int4 | **고정 3**. 의미 없음 |
| `modify_time` | timestamptz | 릴리즈 고정 시각 |
| `flag` | bpchar(1) | **고정 '0'**. 의미 없음 |

### hash_id 생성 규칙

```
a. 프로토콜 제거          https:// http:// 를 뗀다
b. 맨 앞의 www. 제거
c. SubURL 제거            경로·쿼리·프래그먼트를 뗀다
d. http:// + :80 을 붙이고 전체 소문자화
e. MD5 → hex 32자
```

계산 예시:

```
https://www.Example.com/path?q=1
  a →  www.Example.com/path?q=1
  b →  Example.com/path?q=1
  c →  Example.com
  d →  http://example.com:80
  e →  4526154a0ba94a0191c8c0e2729de7e3
```

검증용 값:

| 입력 | hash 입력 문자열 | hash_id |
|---|---|---|
| `saple.com` | `http://saple.com:80` | `0316654ffc99d00f55abaf1034df5400` |
| `example.com` | `http://example.com:80` | `4526154a0ba94a0191c8c0e2729de7e3` |
| `naver.com` | `http://naver.com:80` | `840bcbf1ce1cdb64018b50762d8a0186` |
| `blog.naver.com` | `http://blog.naver.com:80` | `1323a17cc559c82579371936368b0a63` |

`blog.naver.com` 이 `naver.com` 과 다른 해시인 데서 보이듯, **서브도메인은 유지한다.**
제거하는 것은 맨 앞의 `www.` 뿐이다.

### url_address 와 hash_id 의 비대칭

`url_address` 는 `www.` 를 유지하고, `hash_id` 는 `www.` 를 제거한다. 그래서
`www.example.com` 과 `example.com` 이 **같은 hash_id 를 만든다.**
`hash_id` 는 UNIQUE 제약이 걸려 있으므로 릴리즈 생성 시 반드시 중복을 제거해야 한다.

충돌 시 우선순위:

1. `www.` 없는 쪽을 남긴다 (해시 입력과 형태가 일치)
2. 둘 다 `www.` 가 없거나 둘 다 있으면 `source_rank` 가 낮은(=상위) 쪽
3. 그래도 같으면 도메인 사전순

현재 데이터에서 이 충돌이 실제로 몇 건인지는 릴리즈 생성 시 리포트로 출력한다.

> 확인 필요: 원본에 포트가 붙은 경우(`example.com:8443`). `url_address` 는 포트를
> 유지하지만 `hash_id` 는 `:80` 을 강제한다. 현재 데이터는 전부 포트 없는 도메인이라
> 문제가 없다. 일단 "기존 포트를 제거하고 :80 고정" 으로 구현한다.

### 카테고리 코드표

| 카테고리 | code | ranky_category |
|---|---:|---|
| 웹메일 | 1 | 웹메일 |
| 쇼핑 | 2 | 쇼핑 |
| 증권 | 3 | 증권 |
| 취업 | 4 | 취업 |
| 게임 | 5 | 게임 |
| 엔터테인먼트 | 6 | 엔터테인먼트 |
| 뉴스 | 7 | 뉴스 |
| SNS | 8 | SNS |
| 웹하드 | 9 | 웹하드 |
| 기타 | 10 | 기타 |
| 생성형AI | 11 | 생성형AI |

코드는 하드코딩하지 않고 `category_codes` 테이블에 둔다. PCFILTER 쪽 코드가 바뀌어도
DB 값만 고치면 된다.

`ranky_category` 는 **내부 카테고리명을 그대로 쓴다** (`생성형AI`, 공백 없음). 참고용
컬럼이라 표기를 맞출 필요가 없다는 사용자 확인을 받았다.

### 복수 카테고리

우리 `classification.categories` 는 JSON 배열이라 한 사이트에 카테고리가 여럿일 수 있다.
PCFILTER `category` 는 int 하나다. **`primary_category` 하나만 내보낸다.**
나머지 카테고리는 `release_..._map.categories` 에 원본 그대로 남겨 추적할 수 있게 한다.

### 이름 충돌 주의

우리 코드에 이미 `blocked` 개념이 있다 (`src/blocked.py`, `sites.fetch_status='blocked'`).
이건 **국내에서 차단돼 수집이 안 된 사이트**라는 뜻이다.
PCFILTER `block` 은 **제품이 이 사이트를 차단할지** 정하는 정책 코드로 완전히 다른 개념이다.
코드에서 이름을 반드시 분리한다 (`pcf_block` 등).

---

## 6. 릴리즈 내보내기

두 형식을 모두 만든다. **`pno` 는 양쪽 모두에서 제외한다** — 대상 DB 에서 값이 달라질 수 있다.

### CSV

```
out/release/v1.0.0/default_website_t.csv
```

헤더: `hash_id,url_address,site_name,category,ranky_category,block,modify_time,flag`
인코딩 UTF-8 BOM (Excel 호환), 줄바꿈 CRLF.

### INSERT 문

```
out/release/v1.0.0/default_website_t.sql
```

```sql
INSERT INTO public.default_website_t
	(hash_id, url_address, site_name, category, ranky_category, block, modify_time, flag)
VALUES
	('985c0e3560c03ebfda8380bf6f4fcb46', 'facebook.com', '페이스북', 8, 'SNS', 3, '2026-08-05 15:22:16+09:00', '0'),
	('6d0cfe00ae43ff5e1f28e6b75ad49778', 'jojowiki.com', 'JoJo''s Bizarre Encyclopedia - JoJo Wiki', 6, '엔터테인먼트', 3, '2026-08-05 15:22:16+09:00', '0'),
	...
```

작은따옴표 이스케이프(`''`)를 반드시 적용한다. 사이트 제목에 따옴표가 흔하다.
1,000행 단위로 문장을 끊어 대용량 적재 시 실패 지점을 좁힌다.
`modify_time` 은 릴리즈 고정 시각을 로컬 오프셋과 함께 넣는다 (`+09:00`).

---

## 7. 화면과 명령

### 웹 UI

내비게이션을 3계층으로 재구성한다. 현재 `사이트` 화면이 작업데이터에 해당한다.

| 화면 | 기능 |
|---|---|
| `/sources` (신규) | CSV 업로드, 컬럼 매핑, 미리보기, 소스 목록, **작업데이터로 전달** |
| `/sites` (기존) | 작업데이터. 출처 소스 필터 추가 |
| `/releases` (신규) | 릴리즈 생성, 버전 목록, 미리보기, 내보내기 |

웹 업로드는 2단계다. ① 파일을 받아 헤더 추정 결과를 보여주고 ② 사용자가 컬럼 매핑을
확인한 뒤 등록한다. 업로드 원본은 `data/uploads/` 에 시각을 붙여 남긴다.

### CLI

```bash
# 소스데이터
run.py source-add <csv> --name "tranco 2026-08"    # 헤더 자동 인식
run.py source-add <csv> --name "..." --sniff       # 추정 결과만 보기
run.py source-add <csv> --name "..." --no-header --domain-col 1 --rank-col 0
run.py source-list
run.py source-preview <id>
run.py source-push <id> [--dry-run]                # 작업데이터로 전달

# 릴리즈데이터
run.py release-create v1.0.0 [--dry-run] [--skip-uncategorized] [--replace]
run.py release-list
run.py release-export v1.0.0                       # CSV + SQL 동시 생성
```

테이블 이름은 `src_%04d_{슬러그}` 다. 한글 이름은 슬러그에서 다 깎이므로 알파벳이
하나도 안 남으면 `data` 로 떨어진다 (`src_0002_data`).

---

## 8. 구현 순서

**릴리즈 계층을 먼저 만든다.** 지금까지 판정·검수한 데이터로 바로 릴리즈를 뽑아보는 것이
목표이고, 릴리즈는 소스 계층 없이도 작업데이터만 있으면 만들 수 있다.

| 단계 | 내용 | 상태 |
|---|---|---|
| 0 | DB 백업. 마이그레이션을 **복사본에서 먼저** 검증 | 완료 (2026-08-05) |
| 1 | `schema.sql` 을 `db.py` 가 읽도록 배선. `releases`·`category_codes` 생성 | 완료 |
| 2 | `src/release.py` — hash_id 계산, 스냅샷 생성, 중복 제거 | 완료 |
| 3 | 릴리즈 내보내기 (CSV + INSERT) | 완료 |
| 4 | `/releases` 화면 | 완료 |
| 5 | `sites` 에 `source_id`·`source_rank`·`added_at` 추가 | 완료 |
| 6 | `src/source.py` — 업로드, 소스별 테이블 생성, `/sources` 화면 | 완료 |
| 7 | 전달 기능 (자동 예외 포함) | 완료 |
| 8 | top-1m 을 `src_0001` 로 소급 등록 | 완료 |
| 9 | 스냅샷 간 비교 | 예정 |

**v1.0.0 릴리즈 완료** — 2026-08-05, 검수완료 450건. hash_id 충돌 0건,
미분류 2건은 `기타`(10)로 들어갔다.

**소스 #1 등록 완료** — `src_0001_tranco_top_1m` 100만 행. 작업데이터 100만 행에
출처를 소급 연결했고, 웹에서 수동 추가한 47건은 `source_id = NULL` 로 남았다.
DB 크기는 245MB → 355MB 로 늘었다(원본 행 JSON 보관분).

`init_schema()` 는 기존 DB 에서 **컬럼 추가를 먼저** 하고 `schema.sql` 을 돌린다.
`idx_sites_source` 같은 인덱스가 새 컬럼을 참조하기 때문에 순서가 뒤바뀌면 거기서 죽는다.

---

## 9. 열린 항목

| 항목 | 내용 | 상태 |
|---|---|---|
| 미분류 포함 여부 | 검수완료인데 카테고리가 없는 사이트 | 확정 — `기타`(10)로 포함 |
| `생성형AI` 표기 | `ranky_category` 표기 | 확정 — 내부 이름 그대로 |
| 포트 처리 | `example.com:8443` 의 hash_id 입력 | 미확정. 현재 데이터에 포트 없어 막지 않음 |
| 전달 방식 | 릴리즈 파일을 PCFILTER 쪽에 넘기는 방법 | 미확정. 수동 적재로 가정 |
| 도메인 정규화 | 소스 전달 시 `www.` 를 다른 사이트로 볼지 | 확정 — 다른 사이트로 본다 (`db.normalize_domain`) |
| 소스 삭제 | 소스와 그 테이블을 지우는 기능 | 미구현. 지우면 `sites.source_id` 가 붕 뜬다 |

---

## 10. 위험 요소

- **기존 검수 데이터.** 마이그레이션은 `ALTER TABLE ADD COLUMN` 만 한다. 기존 행은 건드리지 않는다.
  작업 전 백업하고, 스크립트는 반드시 DB 복사본에서 먼저 돌린다.
- **`detect-korean` 미완료.** 670,153건이 아직 미판정이다. 릴리즈 자체는 검수완료분만
  다루므로 영향이 없지만, 판정이 끝나야 작업데이터의 실제 규모가 보인다.
- **동적 DDL.** 소스·릴리즈 테이블 이름을 다루는 코드가 흩어지면 관리가 무너진다.
  `source_datasets` / `releases` 카탈로그를 단일 진입점으로 강제한다.
