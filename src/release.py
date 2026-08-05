"""릴리즈데이터 — 검수완료분을 버전 스냅샷으로 고정하고 PCFILTER 스키마로 내보낸다.

버전마다 물리 테이블 하나를 만든다(`release_v1_0_0`). 테이블 모양은 PCFILTER 제품의
`public.default_website_t` 를 그대로 따르고, 우리 쪽 추적 정보는 `_map` 사이드
테이블에 분리한다. 설계 근거는 DATA_TIERS.md.

전량 스냅샷이다. 매 버전이 그 시점의 검수완료분 전체를 담는다.
"""
from __future__ import annotations

import csv
import hashlib
import os
import re
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import db

RELEASE_DIR = os.path.join(db.PROJECT_ROOT, "out", "release")

# PCFILTER default_website_t 컬럼. pno 는 대상 DB 에서 값이 달라질 수 있어 내보내지 않는다
PCF_COLUMNS = ["hash_id", "url_address", "site_name", "category",
               "ranky_category", "block", "modify_time", "flag"]
PCF_TABLE = "public.default_website_t"

# 의미 없는 고정값 (사용자 확인 완료)
PCF_BLOCK = 3
PCF_FLAG = "0"

SQL_CHUNK = 1000  # INSERT 문 하나에 넣을 행 수. 대용량 적재 시 실패 지점을 좁힌다


# ---------------------------------------------------------------- 정규화 / 해시

def url_address(raw: str) -> str:
	"""PCFILTER `url_address` 형식으로 정규화한다.

	프로토콜과 SubURL(경로·쿼리·프래그먼트)을 뗀다. `www.` 와 포트는 남긴다.
	소스 적재·중복 판정과 같은 규칙을 써야 하므로 db.normalize_domain 을 그대로 쓴다.
	"""
	return db.normalize_domain(raw)


def hash_source(addr: str) -> str:
	"""`hash_id` 계산에 넣을 문자열.

	url_address 에서 맨 앞의 `www.` 와 포트를 떼고 `http://{host}:80` 으로 조립한다.
	url_address 는 www 를 남기고 여기서는 떼기 때문에 `www.a.com` 과 `a.com` 이
	같은 해시가 된다. 중복 제거는 dedupe() 가 맡는다.
	"""
	host = addr
	if host.startswith("www."):
		host = host[4:]
	host = host.split(":", 1)[0]  # 원본 포트는 버리고 :80 고정
	return "http://%s:80" % host.lower()


def hash_id(addr: str) -> str:
	"""MD5 hex 32자."""
	return hashlib.md5(hash_source(addr).encode("utf-8")).hexdigest()


def table_name(version: str) -> str:
	slug = re.sub(r"[^0-9a-zA-Z]+", "_", (version or "").strip()).strip("_").lower()
	if not slug:
		raise ValueError("버전 이름에 쓸 수 있는 문자가 없습니다: %r" % version)
	return "release_" + slug


def modify_time() -> str:
	"""PostgreSQL timestamptz 로 넣을 문자열. 예: 2026-08-05 15:30:00+09:00"""
	stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
	return stamp[:-2] + ":" + stamp[-2:]


# ---------------------------------------------------------------- 대상 수집

def category_codes(conn: sqlite3.Connection) -> Dict[str, Tuple[int, str]]:
	rows = conn.execute("SELECT category, code, ranky_category FROM category_codes").fetchall()
	return {r["category"]: (r["code"], r["ranky_category"]) for r in rows}


# 검수 시점에 박제된 라벨(source='review')은 원래 분류 방식으로 되돌려 표시한다.
# 안 그러면 검수했다는 이유만으로 전부 '수동'이 되어 근거 추적이 무의미해진다.
_METHOD_SQL = """COALESCE(
	CASE WHEN m.domain IS NOT NULL AND m.source != 'review' THEN 'manual' END,
	CASE WHEN m.source = 'review' THEN m.origin_method END,
	cl.method)"""


def reviewed_rows(conn: sqlite3.Connection) -> List[sqlite3.Row]:
	"""검수완료(reviewed)로 표시한 사이트. 제외(excluded)는 담지 않는다."""
	return conn.execute(
		"""
		SELECT s.domain,
		       COALESCE(m.title, s.title)            AS site_name,
		       COALESCE(m.categories, cl.categories) AS categories,
		       cl.primary_category,
		       %s                                    AS method,
		       cl.confidence,
		       s.rank, s.source_id, s.source_rank,
		       rv.updated_at                         AS reviewed_at
		FROM review_status rv
		JOIN sites s ON s.domain = rv.domain
		LEFT JOIN classification cl ON cl.domain = s.domain
		LEFT JOIN manual_labels m  ON m.domain  = s.domain
		WHERE rv.status = 'reviewed'
		ORDER BY s.rank
		""" % _METHOD_SQL
	).fetchall()


def _resolve_category(row: sqlite3.Row, codes: Dict[str, Tuple[int, str]]) -> Tuple[str, bool]:
	"""(카테고리명, 미분류였는지) 를 돌려준다.

	분류가 없으면 '기타' 로 넣는다 (사용자 확인 완료).
	"""
	cats = db.json_loads(row["categories"], []) or []
	primary = cats[0] if cats else row["primary_category"]
	if not primary or primary == "none" or primary not in codes:
		return db.OTHER_CATEGORY, True
	return primary, False


def build_rows(conn: sqlite3.Connection, skip_uncategorized: bool = False) -> Dict[str, Any]:
	"""릴리즈에 넣을 행을 만든다. DB 는 읽기만 한다."""
	codes = category_codes(conn)
	if db.OTHER_CATEGORY not in codes:
		raise RuntimeError(
			"category_codes 에 '%s' 가 없습니다. schema.sql 시드를 확인하세요." % db.OTHER_CATEGORY)

	stamp = modify_time()
	candidates: List[Dict[str, Any]] = []
	uncategorized = 0

	for row in reviewed_rows(conn):
		category, was_uncategorized = _resolve_category(row, codes)
		if was_uncategorized:
			uncategorized += 1
			if skip_uncategorized:
				continue
		code, ranky = codes[category]
		addr = url_address(row["domain"])
		if not addr:
			continue
		candidates.append({
			"hash_id": hash_id(addr),
			"url_address": addr,
			"site_name": row["site_name"],
			"category": code,
			"ranky_category": ranky,
			"block": PCF_BLOCK,
			"modify_time": stamp,
			"flag": PCF_FLAG,
			# --- 아래는 _map 테이블용. PCFILTER 로 나가지 않는다 ---
			"_domain": row["domain"],
			"_primary_category": category,
			"_categories": row["categories"],
			"_method": row["method"] or "unclassified",
			"_confidence": row["confidence"],
			"_source_id": row["source_id"],
			"_reviewed_at": row["reviewed_at"],
			"_rank": row["source_rank"] if row["source_rank"] is not None else row["rank"],
		})

	rows, collisions = dedupe(candidates)
	return {
		"rows": rows,
		"collisions": collisions,
		"candidates": len(candidates),
		"uncategorized": uncategorized,
		"skipped_uncategorized": uncategorized if skip_uncategorized else 0,
	}


def dedupe(candidates: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
	"""hash_id 중복을 제거한다.

	url_address 는 `www.` 를 남기는데 hash_id 는 떼기 때문에 `www.a.com` 과 `a.com`
	이 같은 해시를 만든다. hash_id 에 UNIQUE 가 걸려 있어 반드시 하나만 남겨야 한다.

	우선순위: ① www. 없는 쪽 (해시 입력과 형태가 일치) ② 순위가 상위 ③ 도메인 사전순
	"""
	def priority(item: Dict[str, Any]) -> Tuple[int, float, str]:
		has_www = 1 if item["url_address"].startswith("www.") else 0
		rank = item["_rank"] if item["_rank"] is not None else float("inf")
		return (has_www, rank, item["_domain"])

	groups: Dict[str, List[Dict[str, Any]]] = {}
	for item in candidates:
		groups.setdefault(item["hash_id"], []).append(item)

	winners: List[Dict[str, Any]] = []
	collisions: List[Dict[str, Any]] = []
	for h, items in groups.items():
		if len(items) == 1:
			winners.append(items[0])
			continue
		ordered = sorted(items, key=priority)
		winners.append(ordered[0])
		collisions.append({
			"hash_id": h,
			"kept": ordered[0]["_domain"],
			"dropped": [i["_domain"] for i in ordered[1:]],
		})

	winners.sort(key=lambda i: (i["_rank"] if i["_rank"] is not None else float("inf"),
	                            i["_domain"]))
	return winners, collisions


# ---------------------------------------------------------------- 릴리즈 생성

_CREATE_RELEASE = """
CREATE TABLE {t} (
	pno            INTEGER PRIMARY KEY AUTOINCREMENT,
	hash_id        TEXT    NOT NULL UNIQUE,
	url_address    TEXT    NOT NULL,
	site_name      TEXT,
	category       INTEGER,
	ranky_category TEXT,
	block          INTEGER NOT NULL DEFAULT 3,
	modify_time    TEXT    NOT NULL DEFAULT (datetime('now')),
	flag           TEXT    NOT NULL DEFAULT '0'
);
CREATE TABLE {t}_map (
	pno              INTEGER PRIMARY KEY,
	domain           TEXT NOT NULL,
	primary_category TEXT,
	categories       TEXT,
	method           TEXT,
	confidence       REAL,
	source_id        INTEGER,
	reviewed_at      TEXT
);
CREATE INDEX idx_{t}_map_domain ON {t}_map(domain);
"""


def create(version: str, note: Optional[str] = None,
           skip_uncategorized: bool = False, replace: bool = False) -> Dict[str, Any]:
	"""검수완료분을 버전 스냅샷으로 고정한다."""
	table = table_name(version)
	conn = db.connect()
	try:
		db.init_schema(conn)

		existing = conn.execute(
			"SELECT * FROM releases WHERE version = ?", (version,)).fetchone()
		if existing and not replace:
			raise ValueError(
				"릴리즈 '%s' 가 이미 있습니다 (%s행, %s). 덮어쓰려면 --replace 를 붙이세요."
				% (version, format(existing["row_count"], ","), existing["fixed_at"] or "draft"))

		built = build_rows(conn, skip_uncategorized=skip_uncategorized)
		rows = built["rows"]
		if not rows:
			raise ValueError("릴리즈에 넣을 검수완료 사이트가 없습니다.")

		if existing:
			conn.executescript(
				"DROP TABLE IF EXISTS {t}_map; DROP TABLE IF EXISTS {t};".format(t=table))
			conn.execute("DELETE FROM releases WHERE version = ?", (version,))

		conn.executescript(_CREATE_RELEASE.format(t=table))

		conn.executemany(
			"INSERT INTO {t} ({cols}) VALUES ({ph})".format(
				t=table, cols=", ".join(PCF_COLUMNS), ph=", ".join("?" * len(PCF_COLUMNS))),
			[tuple(r[c] for c in PCF_COLUMNS) for r in rows],
		)

		# pno 는 삽입 순서대로 1..N 이다. 같은 순서로 _map 을 채운다
		pnos = [r[0] for r in conn.execute(
			"SELECT pno FROM {t} ORDER BY pno".format(t=table))]
		conn.executemany(
			"""INSERT INTO {t}_map
			   (pno, domain, primary_category, categories, method, confidence,
			    source_id, reviewed_at)
			   VALUES (?,?,?,?,?,?,?,?)""".format(t=table),
			[(pno, r["_domain"], r["_primary_category"], r["_categories"], r["_method"],
			  r["_confidence"], r["_source_id"], r["_reviewed_at"])
			 for pno, r in zip(pnos, rows)],
		)

		prev = conn.execute(
			"SELECT version FROM releases ORDER BY id DESC LIMIT 1").fetchone()
		stamp = db.now()
		conn.execute(
			"""INSERT INTO releases
			   (version, table_name, status, row_count, based_on, created_at, fixed_at, note)
			   VALUES (?,?,'fixed',?,?,?,?,?)""",
			(version, table, len(rows), prev["version"] if prev else None, stamp, stamp, note),
		)
		conn.commit()

		built["version"] = version
		built["table"] = table
		built["count"] = len(rows)
		return built
	finally:
		conn.close()


def list_releases() -> List[sqlite3.Row]:
	conn = db.connect()
	try:
		db.init_schema(conn)
		return conn.execute("SELECT * FROM releases ORDER BY id").fetchall()
	finally:
		conn.close()


def _release_row(conn: sqlite3.Connection, version: str) -> sqlite3.Row:
	row = conn.execute("SELECT * FROM releases WHERE version = ?", (version,)).fetchone()
	if row is None:
		raise ValueError("그런 릴리즈가 없습니다: %s" % version)
	return row


def delete(version: str, with_files: bool = False) -> Dict[str, Any]:
	"""릴리즈를 지운다. 되돌릴 수 없다.

	카탈로그에 등록된 테이블만 지운다. `releases.table_name` 을 거치지 않은 이름은
	받지 않는다. 내보낸 파일은 기본적으로 두고, with_files=True 일 때만 지운다.
	검수 데이터(review_status·manual_labels)는 절대 건드리지 않는다.
	"""
	conn = db.connect()
	try:
		db.init_schema(conn)
		rel = _release_row(conn, version)
		table = rel["table_name"]

		# 카탈로그를 통해 얻은 이름인지 한 번 더 확인한다. 동적 DROP 이라 안전장치를 둔다
		if not re.fullmatch(r"release_[0-9a-z_]+", table or ""):
			raise ValueError("릴리즈 테이블 이름이 이상합니다: %r" % table)

		count = rel["row_count"]
		conn.executescript(
			"DROP TABLE IF EXISTS {t}_map; DROP TABLE IF EXISTS {t};".format(t=table))
		conn.execute("DELETE FROM releases WHERE version = ?", (version,))
		conn.commit()

		out_dir = os.path.join(RELEASE_DIR, version)
		removed_files = []
		if os.path.isdir(out_dir):
			if with_files:
				for name in sorted(os.listdir(out_dir)):
					path = os.path.join(out_dir, name)
					if os.path.isfile(path):
						os.remove(path)
						removed_files.append(name)
				try:
					os.rmdir(out_dir)
				except OSError:
					pass  # 우리가 만들지 않은 파일이 남아 있으면 폴더는 둔다
			else:
				removed_files = None  # 파일은 그대로 뒀다는 표시

		return {"version": version, "table": table, "count": count,
		        "out_dir": out_dir if os.path.isdir(out_dir) else None,
		        "removed_files": removed_files}
	finally:
		conn.close()


# ---------------------------------------------------------------- 내보내기

def _sql_literal(value: Any) -> str:
	if value is None:
		return "NULL"
	if isinstance(value, (int, float)):
		return str(value)
	return "'" + str(value).replace("'", "''") + "'"


def export(version: str) -> Dict[str, Any]:
	"""CSV 와 INSERT 문을 만든다. 둘 다 pno 를 넣지 않는다."""
	conn = db.connect()
	try:
		db.init_schema(conn)
		rel = _release_row(conn, version)
		table = rel["table_name"]
		rows = conn.execute(
			"SELECT {cols} FROM {t} ORDER BY pno".format(
				cols=", ".join(PCF_COLUMNS), t=table)).fetchall()

		out_dir = os.path.join(RELEASE_DIR, version)
		os.makedirs(out_dir, exist_ok=True)
		csv_path = os.path.join(out_dir, "default_website_t.csv")
		sql_path = os.path.join(out_dir, "default_website_t.sql")

		# CSV — Excel 호환을 위해 UTF-8 BOM + CRLF
		with open(csv_path, "w", encoding="utf-8-sig", newline="") as fp:
			writer = csv.writer(fp, lineterminator="\r\n")
			writer.writerow(PCF_COLUMNS)
			for r in rows:
				writer.writerow([r[c] for c in PCF_COLUMNS])

		# INSERT 문 — SQL_CHUNK 행마다 문장을 끊는다
		with open(sql_path, "w", encoding="utf-8") as fp:
			fp.write("-- %s 릴리즈 %s (%d행)\n" % (PCF_TABLE, version, len(rows)))
			fp.write("-- 생성: %s\n" % db.now())
			fp.write("-- pno 는 대상 DB 에서 채운다 (컬럼에 넣지 않음)\n\n")
			head = "INSERT INTO %s\n\t(%s)\nVALUES\n" % (PCF_TABLE, ", ".join(PCF_COLUMNS))
			for start in range(0, len(rows), SQL_CHUNK):
				chunk = rows[start:start + SQL_CHUNK]
				fp.write(head)
				values = [
					"\t(" + ", ".join(_sql_literal(r[c]) for c in PCF_COLUMNS) + ")"
					for r in chunk
				]
				fp.write(",\n".join(values))
				fp.write(";\n\n")

		return {"version": version, "count": len(rows),
		        "csv": csv_path, "sql": sql_path, "out_dir": out_dir}
	finally:
		conn.close()
