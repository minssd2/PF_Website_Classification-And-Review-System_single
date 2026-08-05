-- =============================================================================
-- 웹사이트 분류·검수 시스템 — DB INIT 스키마 정의
-- 대상: SQLite 3 (data/sites.db)
-- =============================================================================
--
-- [이 파일의 위치]
--   현재 시점의 기준(source of truth)은 아직 `src/db.py` 의 SCHEMA 문자열이다.
--   이 파일은 그것을 SQL 로 옮긴 것이며, 다음 두 가지를 함께 담는다.
--
--     1. 현행 테이블 — src/db.py SCHEMA + _ADDED_COLUMNS 마이그레이션까지 반영한
--        "지금 돌아가는 DB 의 실제 모습". 이 파일만으로 빈 DB 를 만들 수 있다.
--     2. v2 확장 테이블 — 소스/작업/릴리즈 3계층용. 아직 코드가 쓰지 않는다.
--
--   구현 1단계에서 `src/db.py` 가 이 파일을 읽도록 바꾼다. 그 전까지는
--   db.py 를 고치면 이 파일도 같이 고쳐야 한다. (설계 근거: DATA_TIERS.md)
--
-- [수동 생성]
--   sqlite3 data/sites.db < schema/schema.sql
--
-- =============================================================================


-- =============================================================================
-- 1. 작업데이터 (Working data) — 현행 파이프라인이 쓰는 테이블
-- =============================================================================
-- 수집 → 한국어 판정 → 분류 → 검수 의 상태가 전부 여기 있다.
-- 모든 테이블이 domain 을 키로 물려 있고, 작업 공간은 하나뿐이다.

-- 수집 대상과 크롤링 결과
CREATE TABLE IF NOT EXISTS sites (
	rank          INTEGER,                          -- 소스의 순위. v2 에서 source_rank 로 이관 예정
	domain        TEXT PRIMARY KEY,
	fetch_status  TEXT NOT NULL DEFAULT 'pending',  -- pending|ok|failed|dead|blocked
	attempt_count INTEGER NOT NULL DEFAULT 0,
	error         TEXT,
	http_status   INTEGER,
	final_url     TEXT,                             -- 리다이렉트 최종 도착지
	charset       TEXT,
	title         TEXT,
	description   TEXT,
	html_lang     TEXT,
	og_locale     TEXT,
	text_sample   TEXT,                             -- 본문 발췌. compact 명령이 비한국어분을 지운다
	fetched_at    TEXT,

	-- v2 확장: 어느 소스데이터에서 전달됐는지.
	-- FK 를 걸지 않는다. 기존 DB 는 ALTER TABLE ADD COLUMN 으로 채우는데
	-- 그러면 신규 DB 와 모양이 달라지고, 소스를 지웠을 때 sites 입력이 막힌다.
	-- 무결성은 source_datasets 카탈로그를 단일 진입점으로 강제해서 지킨다.
	source_id     INTEGER,
	source_rank   INTEGER,                          -- 소스 기준 순위 (rank 대체)
	added_at      TEXT                              -- 작업데이터에 들어온 시각
);
CREATE INDEX IF NOT EXISTS idx_sites_status ON sites(fetch_status);
CREATE INDEX IF NOT EXISTS idx_sites_rank   ON sites(rank);
CREATE INDEX IF NOT EXISTS idx_sites_source ON sites(source_id);

-- 한국어 서비스 여부 판정 결과
CREATE TABLE IF NOT EXISTS korean (
	domain    TEXT PRIMARY KEY,
	is_korean INTEGER NOT NULL DEFAULT 0,
	score     REAL    NOT NULL DEFAULT 0,
	reasons   TEXT,                                 -- 점수를 준 신호 목록
	judged_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_korean_flag ON korean(is_korean);

-- 카테고리 분류 결과
CREATE TABLE IF NOT EXISTS classification (
	domain           TEXT PRIMARY KEY,
	categories       TEXT,                          -- JSON 배열. 복수 카테고리 가능
	primary_category TEXT,
	method           TEXT,                          -- rule|llm|manual|unclassified
	confidence       REAL,
	evidence         TEXT,
	classified_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_cls_primary ON classification(primary_category);
CREATE INDEX IF NOT EXISTS idx_cls_method  ON classification(method);

-- 사람이 직접 고친 값. 재크롤링·재분류가 덮어쓰지 않는다
CREATE TABLE IF NOT EXISTS manual_labels (
	domain        TEXT PRIMARY KEY,
	is_korean     INTEGER,
	categories    TEXT,                             -- JSON 배열
	title         TEXT,
	note          TEXT,
	updated_at    TEXT,
	-- 라벨이 생긴 경위.
	--   'edit'   = 사람이 수정 화면에서 직접 고침
	--   'review' = 검수완료/제외를 누를 때 그 시점 값을 자동으로 박제한 것
	-- 둘을 구분해야 '수정됨' 배지와 분류방식 표시가 검수 때문에 오염되지 않는다
	source        TEXT NOT NULL DEFAULT 'edit',
	-- 박제 직전의 분류 방식(rule/llm/unclassified). 화면에 원래 근거를 그대로 보여주려고 남긴다
	origin_method TEXT
);

-- 사람이 최종 확인한 상태. 분류 라벨과 독립적이다
-- (라벨은 그대로 두고 "확인만 했다"거나 "결과에서 빼겠다"를 표시할 수 있어야 한다)
CREATE TABLE IF NOT EXISTS review_status (
	domain     TEXT PRIMARY KEY,
	status     TEXT NOT NULL,                       -- reviewed|excluded
	reason     TEXT,
	updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_status ON review_status(status);


-- =============================================================================
-- 2. 분류 기준 정보
-- =============================================================================

-- 판정 기준값. 웹 /settings 화면이 이 목록을 그대로 폼으로 그린다
CREATE TABLE IF NOT EXISTS settings (
	key         TEXT PRIMARY KEY,
	value       TEXT,
	value_type  TEXT NOT NULL DEFAULT 'str',        -- str|int|float|bool
	description TEXT
);

-- 카테고리별 분류 규칙
CREATE TABLE IF NOT EXISTS category_rules (
	id         INTEGER PRIMARY KEY AUTOINCREMENT,
	category   TEXT NOT NULL,
	rule_type  TEXT NOT NULL,                       -- domain|keyword|exclude
	pattern    TEXT NOT NULL,
	weight     REAL NOT NULL DEFAULT 1.0,
	enabled    INTEGER NOT NULL DEFAULT 1,
	updated_at TEXT,
	UNIQUE(category, rule_type, pattern)
);
CREATE INDEX IF NOT EXISTS idx_rules_cat ON category_rules(category);


-- =============================================================================
-- 3. 작업 실행 상태
-- =============================================================================

-- 백그라운드 작업 진행률. 웹 UI 가 3초마다 읽는다
CREATE TABLE IF NOT EXISTS job_runs (
	id         INTEGER PRIMARY KEY AUTOINCREMENT,
	job_name   TEXT NOT NULL,
	status     TEXT NOT NULL,                       -- running|done|failed|stopped
	total      INTEGER NOT NULL DEFAULT 0,
	processed  INTEGER NOT NULL DEFAULT 0,
	ok_count   INTEGER NOT NULL DEFAULT 0,
	fail_count INTEGER NOT NULL DEFAULT 0,
	started_at TEXT,
	updated_at TEXT,
	message    TEXT
);

-- LLM 세션 판정 배치
CREATE TABLE IF NOT EXISTS llm_batches (
	batch_id     TEXT PRIMARY KEY,
	status       TEXT NOT NULL,                     -- exported|imported|partial|cancelled
	size         INTEGER NOT NULL DEFAULT 0,
	request_path TEXT,
	result_path  TEXT,
	exported_at  TEXT,
	imported_at  TEXT
);

CREATE TABLE IF NOT EXISTS llm_queue (
	domain    TEXT PRIMARY KEY,
	batch_id  TEXT,
	queued_at TEXT,
	-- 사람이 직접 요청한 건인지 자동 선정된 건인지. 배치를 취소할 때 구분이 필요하다
	manual    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_queue_batch ON llm_queue(batch_id);


-- =============================================================================
-- 4. [v2] 소스데이터 (Source data)
-- =============================================================================
-- 업로드한 CSV 원본. 업로드 1건마다 물리 테이블 하나를 만든다.
-- CSV 마다 컬럼 구성이 다르므로 원본 컬럼을 살린 별도 테이블이 맞다.

CREATE TABLE IF NOT EXISTS source_datasets (
	id                INTEGER PRIMARY KEY AUTOINCREMENT,
	name              TEXT NOT NULL,                -- 사람이 붙이는 이름. 예: 'tranco top-1m 2026-08'
	table_name        TEXT NOT NULL UNIQUE,         -- 실제 테이블명. 예: 'src_0001_tranco_top1m'
	original_filename TEXT,
	-- CSV 헤더 → 역할 매핑. 예: {"domain":"Domain","rank":"Rank","extra":["Category"]}
	-- 헤더 1행을 읽어 자동 추정하고, 못 찾은 역할은 NULL 로 둔다
	column_map        TEXT,
	row_count         INTEGER NOT NULL DEFAULT 0,
	pushed_count      INTEGER NOT NULL DEFAULT 0,   -- 작업데이터로 전달된 누적 건수
	uploaded_at       TEXT,
	last_pushed_at    TEXT,
	note              TEXT
);

-- --- 소스별 테이블 템플릿 (업로드 시 동적 생성) --------------------------------
-- CREATE TABLE src_0001_tranco_top1m (
-- 	id          INTEGER PRIMARY KEY AUTOINCREMENT,
-- 	domain      TEXT,          -- column_map 으로 찾아낸 도메인. 정규화 후 저장
-- 	source_rank INTEGER,       -- column_map 으로 찾아낸 순위. 없으면 NULL
-- 	raw         TEXT           -- 원본 행 전체 JSON. 예상 못 한 컬럼도 잃지 않는다
-- );
-- CREATE INDEX idx_src_0001_domain ON src_0001_tranco_top1m(domain);


-- =============================================================================
-- 5. [v2] 릴리즈데이터 (Release data)
-- =============================================================================
-- 검수완료분을 버전으로 고정한 스냅샷. 버전마다 물리 테이블 하나.
-- 릴리즈 테이블은 PCFILTER default_website_t 스키마를 그대로 따른다.

CREATE TABLE IF NOT EXISTS releases (
	id         INTEGER PRIMARY KEY AUTOINCREMENT,
	version    TEXT NOT NULL UNIQUE,                -- 예: 'v1.0.0'
	table_name TEXT NOT NULL UNIQUE,                -- 예: 'release_v1_0_0'
	status     TEXT NOT NULL DEFAULT 'draft',       -- draft|fixed
	row_count  INTEGER NOT NULL DEFAULT 0,
	based_on   TEXT,                                -- 직전 버전. 비교 기능용(추후)
	created_at TEXT,
	fixed_at   TEXT,                                -- 고정 시각. 이후 내용 변경 금지
	note       TEXT
);

-- 우리 카테고리명 → PCFILTER category 코드
CREATE TABLE IF NOT EXISTS category_codes (
	category       TEXT PRIMARY KEY,                -- db.CATEGORIES 의 한글 이름
	code           INTEGER NOT NULL,                -- PCFILTER default_website_t.category
	ranky_category TEXT                             -- PCFILTER ranky_category. 카테고리명(한글)
);

INSERT OR IGNORE INTO category_codes (category, code, ranky_category) VALUES
	('웹메일',       1,  '웹메일'),
	('쇼핑',         2,  '쇼핑'),
	('증권',         3,  '증권'),
	('취업',         4,  '취업'),
	('게임',         5,  '게임'),
	('엔터테인먼트', 6,  '엔터테인먼트'),
	('뉴스',         7,  '뉴스'),
	('SNS',          8,  'SNS'),
	('웹하드',       9,  '웹하드'),
	('기타',         10, '기타'),
	('생성형AI',     11, '생성형AI');

-- --- 릴리즈 버전별 테이블 템플릿 (릴리즈 생성 시 동적 생성) --------------------
-- PCFILTER 원본 DDL (PostgreSQL):
--   CREATE TABLE public.default_website_t (
--     pno            bigserial     NOT NULL,
--     hash_id        varchar(32)   NOT NULL,
--     url_address    varchar       NOT NULL,
--     site_name      varchar       NULL,
--     category       int4          NULL,
--     ranky_category varchar       NULL,
--     block          int4 DEFAULT 3 NOT NULL,
--     modify_time    timestamptz DEFAULT now() NULL,
--     flag           bpchar(1) DEFAULT '0'::bpchar NOT NULL,
--     CONSTRAINT default_website_t_hash_id_key UNIQUE (hash_id),
--     CONSTRAINT default_website_t_pkey PRIMARY KEY (pno)
--   );
--
-- SQLite 대응:
-- CREATE TABLE release_v1_0_0 (
-- 	pno            INTEGER PRIMARY KEY AUTOINCREMENT,   -- bigserial
-- 	hash_id        TEXT    NOT NULL UNIQUE,             -- varchar(32). MD5 hex 32자
-- 	url_address    TEXT    NOT NULL,                    -- 프로토콜·경로 제거. www/포트는 유지
-- 	site_name      TEXT,                                -- 사이트 제목
-- 	category       INTEGER,                             -- category_codes.code
-- 	ranky_category TEXT,                                -- 카테고리명(한글)
-- 	block          INTEGER NOT NULL DEFAULT 3,          -- 고정값 3
-- 	modify_time    TEXT    NOT NULL DEFAULT (datetime('now')),
-- 	flag           TEXT    NOT NULL DEFAULT '0'         -- 고정값 '0'
-- );
--
-- 추적용 사이드 테이블. PCFILTER 로 내보내지 않는다.
-- 릴리즈 행이 어느 도메인에서 어떤 근거로 나왔는지 역추적하려고 남긴다.
-- CREATE TABLE release_v1_0_0_map (
-- 	pno              INTEGER PRIMARY KEY,
-- 	domain           TEXT NOT NULL,
-- 	primary_category TEXT,
-- 	categories       TEXT,      -- JSON 배열. 복수 카테고리 원본
-- 	method           TEXT,      -- rule|llm|manual
-- 	confidence       REAL,
-- 	source_id        INTEGER,
-- 	reviewed_at      TEXT
-- );
-- CREATE INDEX idx_release_v1_0_0_map_domain ON release_v1_0_0_map(domain);
