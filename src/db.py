"""SQLite 저장소 — 스키마 정의, 커넥션 관리, 설정/작업 진행률 헬퍼.

모든 단계(크롤링·한국어 판정·분류)의 상태를 이 DB 하나에 보관한다.
CLI 워커가 쓰는 동안 웹 UI가 읽어야 하므로 WAL 모드를 사용한다.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "sites.db")
SCHEMA_PATH = os.path.join(PROJECT_ROOT, "schema", "schema.sql")

REVIEW_STATUSES = ("reviewed", "excluded")
REVIEW_LABELS = {"reviewed": "검수완료", "excluded": "제외"}

CATEGORIES = [
	"웹메일",
	"쇼핑",
	"증권",
	"취업",
	"게임",
	"엔터테인먼트",
	"뉴스",
	"SNS",
	"웹하드",
	"생성형AI",
]

# 분류가 안 된 사이트를 릴리즈에 넣을 때 쓰는 이름. CATEGORIES 에는 넣지 않는다
# (분류 규칙의 대상이 아니라 릴리즈 단계의 기본값이다).
OTHER_CATEGORY = "기타"


def load_schema() -> str:
	"""schema/schema.sql 을 읽는다. 이 파일이 스키마의 기준(source of truth)이다."""
	with open(SCHEMA_PATH, "r", encoding="utf-8") as fp:
		return fp.read()


_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.I)


def normalize_domain(raw: Optional[str]) -> str:
	"""도메인 정규화. 소스 적재·중복 판정·릴리즈 url_address 가 모두 이 함수를 쓴다.

	프로토콜과 경로/쿼리/프래그먼트를 떼고 소문자화한다.
	`www.` 는 **제거하지 않는다**. `www.a.com` 과 `a.com` 을 다른 사이트로 본다.
	포트는 남긴다.
	"""
	value = (raw or "").strip()
	value = _SCHEME_RE.sub("", value)
	for sep in ("/", "?", "#"):
		value = value.split(sep, 1)[0]
	return value.strip().rstrip(".").lower()


# key -> (기본값, 타입, 설명). 웹 /settings 화면이 이 목록을 그대로 폼으로 그린다.
DEFAULT_SETTINGS: List[tuple] = [
	("korean.threshold", "100", "float", "한국어로 판정할 최소 점수"),
	("korean.score.tld_kr", "100", "float", ".kr / .한국 TLD"),
	("korean.score.lang_ko", "80", "float", "html lang=ko 또는 og:locale=ko_KR"),
	("korean.score.hangul_high", "80", "float", "본문 한글 비율이 high 기준 이상"),
	("korean.score.charset_kr", "60", "float", "charset EUC-KR / CP949"),
	("korean.score.meta_hangul", "40", "float", "title·description에 한글 포함"),
	("korean.score.hangul_low", "30", "float", "본문 한글 비율이 low~high 구간"),
	("korean.hangul_ratio_high", "0.10", "float", "한글 비율 상위 기준 (0~1)"),
	("korean.hangul_ratio_low", "0.03", "float", "한글 비율 하위 기준 (0~1)"),
	("fetch.concurrency", "150", "int", "크롤링 동시 요청 수"),
	("fetch.connect_timeout", "5", "float", "연결 타임아웃(초)"),
	("fetch.read_timeout", "8", "float", "응답 읽기 타임아웃(초)"),
	("fetch.max_bytes", "204800", "int", "응답 본문 최대 수신 바이트"),
	("fetch.text_sample_len", "500", "int", "저장할 본문 발췌 길이"),
	("fetch.max_attempts", "3", "int", "실패 도메인 최대 재시도 횟수"),
	("classify.min_score", "2.0", "float", "규칙 분류 확정 최소 점수"),
	("classify.margin", "1.0", "float", "1위와 2위 카테고리 점수 최소 격차"),
	("classify.llm_conf_threshold", "0.6", "float", "이 확신도 미만이면 LLM 판정으로 넘김"),
	("llm.batch_size", "40", "int", "세션 판정 배치당 사이트 수"),
	("llm.excerpt_len", "300", "int", "배치 파일에 넣을 본문 발췌 길이"),
]


def now() -> str:
	return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(readonly: bool = False) -> sqlite3.Connection:
	os.makedirs(DATA_DIR, exist_ok=True)
	conn = sqlite3.connect(DB_PATH, timeout=30.0)
	conn.row_factory = sqlite3.Row
	conn.execute("PRAGMA journal_mode=WAL")
	conn.execute("PRAGMA busy_timeout=5000")
	conn.execute("PRAGMA synchronous=NORMAL")
	if not readonly:
		conn.execute("PRAGMA foreign_keys=ON")
	return conn


# 스키마가 나중에 늘어난 컬럼들. 기존 DB를 지우지 않고 이어 쓰기 위해 여기서 채운다.
_ADDED_COLUMNS = [
	("manual_labels", "title", "TEXT"),
	# 사람이 직접 요청한 건인지 자동 선정된 건인지. 배치를 취소할 때 구분이 필요하다
	("llm_queue", "manual", "INTEGER NOT NULL DEFAULT 0"),
	# 라벨이 생긴 경위. 'edit' = 사람이 수정 화면에서 고친 것,
	# 'review' = 검수/제외를 누를 때 그 시점 값을 자동으로 박제한 것.
	# 둘을 구분해야 '수정됨' 배지와 분류방식 표시가 검수 때문에 오염되지 않는다.
	("manual_labels", "source", "TEXT NOT NULL DEFAULT 'edit'"),
	# 박제 직전의 분류 방식(rule/llm/unclassified). 화면에 원래 근거를 그대로 보여주려고 남긴다
	("manual_labels", "origin_method", "TEXT"),
	# v2 3계층: 이 도메인이 어느 소스데이터에서 전달됐는지.
	# 기존 100만 행은 NULL 로 남고, top-1m 소급 등록 때 채운다.
	("sites", "source_id", "INTEGER"),
	("sites", "source_rank", "INTEGER"),
	("sites", "added_at", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
	for table, column, coltype in _ADDED_COLUMNS:
		cols = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
		if not cols:
			continue  # 테이블 자체가 아직 없다. executescript 가 만들면서 컬럼도 넣는다
		if column not in cols:
			conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, coltype))
	conn.commit()


def init_schema(conn: sqlite3.Connection) -> None:
	"""스키마를 최신 상태로 맞춘다. 신규 DB 생성과 기존 DB 갱신을 모두 처리한다.

	기존 DB 에서는 컬럼 추가가 먼저 와야 한다. schema.sql 에 새 컬럼을 참조하는
	인덱스(idx_sites_source 등)가 들어 있어서, 컬럼이 없는 상태로 스크립트를 돌리면
	거기서 죽는다. 신규 DB 에서는 테이블이 없어 _migrate 가 그냥 통과한다.
	"""
	_migrate(conn)
	conn.executescript(load_schema())
	conn.commit()
	_migrate(conn)


# ---------------------------------------------------------------- settings

def _cast(value: Optional[str], value_type: str) -> Any:
	if value is None:
		return None
	if value_type == "int":
		return int(float(value))
	if value_type == "float":
		return float(value)
	if value_type == "bool":
		return value.lower() in ("1", "true", "yes", "on")
	return value


def seed_settings(conn: sqlite3.Connection) -> int:
	"""기본 설정을 주입한다. 이미 있는 키는 건드리지 않는다(웹에서 고친 값 보존)."""
	added = 0
	for key, value, vtype, desc in DEFAULT_SETTINGS:
		cur = conn.execute(
			"INSERT OR IGNORE INTO settings(key, value, value_type, description) VALUES (?,?,?,?)",
			(key, value, vtype, desc),
		)
		added += cur.rowcount
	conn.commit()
	return added


def get_settings(conn: sqlite3.Connection) -> Dict[str, Any]:
	rows = conn.execute("SELECT key, value, value_type FROM settings").fetchall()
	return {r["key"]: _cast(r["value"], r["value_type"]) for r in rows}


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
	row = conn.execute(
		"SELECT value, value_type FROM settings WHERE key = ?", (key,)
	).fetchone()
	if row is None:
		return default
	return _cast(row["value"], row["value_type"])


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
	conn.execute(
		"UPDATE settings SET value = ? WHERE key = ?", (str(value), key)
	)
	conn.commit()


# ---------------------------------------------------------------- job_runs

def start_job(conn: sqlite3.Connection, job_name: str, total: int, message: str = "") -> int:
	cur = conn.execute(
		"""INSERT INTO job_runs(job_name, status, total, processed, ok_count, fail_count,
		   started_at, updated_at, message) VALUES (?,'running',?,0,0,0,?,?,?)""",
		(job_name, total, now(), now(), message),
	)
	conn.commit()
	return int(cur.lastrowid)


def update_job(
	conn: sqlite3.Connection,
	job_id: int,
	processed: Optional[int] = None,
	ok_count: Optional[int] = None,
	fail_count: Optional[int] = None,
	status: Optional[str] = None,
	message: Optional[str] = None,
) -> None:
	fields: List[str] = ["updated_at = ?"]
	params: List[Any] = [now()]
	for name, val in (
		("processed", processed),
		("ok_count", ok_count),
		("fail_count", fail_count),
		("status", status),
		("message", message),
	):
		if val is not None:
			fields.append("%s = ?" % name)
			params.append(val)
	params.append(job_id)
	conn.execute("UPDATE job_runs SET %s WHERE id = ?" % ", ".join(fields), params)
	conn.commit()


def finish_stale_jobs(conn: sqlite3.Connection, stale_after_sec: int = 90) -> int:
	"""프로세스가 강제 종료돼 running으로 남은 작업을 정리한다.

	최근에 갱신된 행은 건드리지 않는다. 콘솔에서 크롤링이 도는 중에 웹을 켜면
	멀쩡히 진행 중인 작업까지 stopped 로 바꿔버리기 때문이다.
	"""
	cur = conn.execute(
		"""UPDATE job_runs SET status='stopped', updated_at=?
		   WHERE status='running'
		     AND (julianday(?) - julianday(updated_at)) * 86400 > ?""",
		(now(), now(), stale_after_sec),
	)
	conn.commit()
	return cur.rowcount


# ---------------------------------------------------------------- 공통 조회

def counts_by(conn: sqlite3.Connection, table: str, column: str) -> Dict[str, int]:
	rows = conn.execute(
		"SELECT %s AS k, COUNT(*) AS c FROM %s GROUP BY %s" % (column, table, column)
	).fetchall()
	return {(r["k"] if r["k"] is not None else "(null)"): r["c"] for r in rows}


def json_dumps(value: Any) -> str:
	return json.dumps(value, ensure_ascii=False)


def json_loads(value: Optional[str], default: Any = None) -> Any:
	if not value:
		return default
	try:
		return json.loads(value)
	except (ValueError, TypeError):
		return default
