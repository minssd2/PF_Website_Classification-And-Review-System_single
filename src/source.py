"""소스데이터 — 업로드 CSV 원본을 소스별 물리 테이블로 관리하고 작업데이터로 전달한다.

업로드 1건마다 테이블 하나를 만든다(`src_0001_tranco_top1m`). CSV 마다 컬럼 구성이
다르므로 원본 행 전체를 JSON 으로 함께 남긴다. 설계 근거는 DATA_TIERS.md.

테이블 이름을 다루는 코드는 이 모듈에만 둔다. 흩어지면 관리가 무너진다.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import db

BATCH = 20000

# 헤더 1행에서 역할을 추정할 때 쓰는 후보. 대소문자·공백은 무시한다
DOMAIN_HEADERS = ("domain", "domains", "url", "urls", "site", "sites", "host",
                  "hostname", "address", "url_address", "website",
                  "도메인", "주소", "사이트", "url주소")
RANK_HEADERS = ("rank", "ranking", "no", "num", "order", "index", "순위", "번호")


# ---------------------------------------------------------------- 헤더 해석

def _key(value: str) -> str:
	return re.sub(r"[\s_\-]+", "", (value or "").strip().lower())


def detect_columns(header: Sequence[str]) -> Dict[str, Optional[int]]:
	"""헤더 1행에서 domain·rank 컬럼 위치를 추정한다. 못 찾으면 None."""
	found: Dict[str, Optional[int]] = {"domain": None, "rank": None}
	keys = [_key(h) for h in header]
	for idx, k in enumerate(keys):
		if found["domain"] is None and k in DOMAIN_HEADERS:
			found["domain"] = idx
		if found["rank"] is None and k in RANK_HEADERS:
			found["rank"] = idx
	return found


def sniff(csv_path: str, has_header: bool = True) -> Dict[str, Any]:
	"""파일을 열어 헤더와 추정 결과, 미리보기 3행을 돌려준다. DB 는 건드리지 않는다."""
	with open(csv_path, "r", encoding="utf-8-sig", newline="") as fp:
		reader = csv.reader(fp)
		rows = []
		for row in reader:
			rows.append(row)
			if len(rows) >= 4:
				break
	if not rows:
		raise ValueError("빈 CSV 입니다: %s" % csv_path)

	if has_header:
		header = rows[0]
		sample = rows[1:4]
		detected = detect_columns(header)
	else:
		header = ["col%d" % i for i in range(len(rows[0]))]
		sample = rows[:3]
		detected = {"domain": None, "rank": None}
	return {"header": header, "sample": sample, "detected": detected,
	        "columns": len(rows[0])}


# ---------------------------------------------------------------- 테이블 이름

def _slug(name: str) -> str:
	"""테이블 이름에 쓸 조각. 한글 이름은 여기서 거의 다 깎이므로 fallback 을 둔다."""
	slug = re.sub(r"[^0-9a-zA-Z]+", "_", (name or "")).strip("_").lower()
	if not re.search(r"[a-z]", slug):
		return "data"  # 알파벳이 하나도 안 남으면 src_0002_2 같은 이름이 된다
	return slug[:40].strip("_")


def table_name(source_id: int, name: str) -> str:
	return "src_%04d_%s" % (source_id, _slug(name))


def _assert_known_table(conn: sqlite3.Connection, table: str) -> None:
	"""카탈로그에 등록된 테이블인지 확인한다. 동적 SQL 에 임의 이름이 끼지 않게."""
	row = conn.execute(
		"SELECT 1 FROM source_datasets WHERE table_name = ?", (table,)).fetchone()
	if row is None:
		raise ValueError("카탈로그에 없는 소스 테이블입니다: %s" % table)


# ---------------------------------------------------------------- 업로드

def add(csv_path: str, name: str, has_header: bool = True,
        domain_col: Optional[int] = None, rank_col: Optional[int] = None,
        note: Optional[str] = None, quiet: bool = False) -> Dict[str, Any]:
	"""CSV 를 소스데이터로 적재한다. 작업데이터는 건드리지 않는다."""
	if not os.path.exists(csv_path):
		raise FileNotFoundError("CSV 를 찾을 수 없습니다: %s" % csv_path)

	info = sniff(csv_path, has_header=has_header)
	header = info["header"]
	if domain_col is None:
		domain_col = info["detected"]["domain"]
	if rank_col is None:
		rank_col = info["detected"]["rank"]

	if domain_col is None:
		raise ValueError(
			"도메인 컬럼을 찾지 못했습니다. --domain-col 로 열 번호(0부터)를 지정하세요.\n"
			"  헤더: %s" % ", ".join("%d:%s" % (i, h) for i, h in enumerate(header)))
	if domain_col >= info["columns"]:
		raise ValueError("--domain-col %d 은 이 CSV 의 열 수(%d)를 넘습니다."
		                 % (domain_col, info["columns"]))

	conn = db.connect()
	try:
		db.init_schema(conn)
		if conn.execute("SELECT 1 FROM source_datasets WHERE name = ?", (name,)).fetchone():
			raise ValueError("같은 이름의 소스가 이미 있습니다: %s" % name)

		column_map = {"domain": domain_col, "rank": rank_col,
		              "has_header": has_header, "header": header}
		cur = conn.execute(
			"""INSERT INTO source_datasets
			   (name, table_name, original_filename, column_map, row_count,
			    pushed_count, uploaded_at, note)
			   VALUES (?,'',?,?,0,0,?,?)""",
			(name, os.path.basename(csv_path), db.json_dumps(column_map), db.now(), note),
		)
		source_id = int(cur.lastrowid)
		table = table_name(source_id, name)
		conn.execute("UPDATE source_datasets SET table_name = ? WHERE id = ?",
		             (table, source_id))
		conn.executescript(
			"""CREATE TABLE {t} (
				id          INTEGER PRIMARY KEY AUTOINCREMENT,
				domain      TEXT,
				source_rank INTEGER,
				raw         TEXT
			);
			CREATE INDEX idx_{t}_domain ON {t}(domain);""".format(t=table))
		conn.commit()

		loaded, skipped = _load_rows(conn, table, csv_path, has_header,
		                             domain_col, rank_col, header, quiet)
		conn.execute("UPDATE source_datasets SET row_count = ? WHERE id = ?",
		             (loaded, source_id))
		conn.commit()

		distinct = conn.execute(
			"SELECT COUNT(DISTINCT domain) FROM %s" % table).fetchone()[0]
		return {"id": source_id, "name": name, "table": table,
		        "loaded": loaded, "skipped": skipped, "distinct": distinct,
		        "domain_col": domain_col, "rank_col": rank_col}
	finally:
		conn.close()


def _load_rows(conn: sqlite3.Connection, table: str, csv_path: str, has_header: bool,
               domain_col: int, rank_col: Optional[int], header: Sequence[str],
               quiet: bool) -> Tuple[int, int]:
	loaded = 0
	skipped = 0
	buf: List[Tuple] = []

	def flush():
		conn.executemany(
			"INSERT INTO %s(domain, source_rank, raw) VALUES (?,?,?)" % table, buf)
		conn.commit()

	with open(csv_path, "r", encoding="utf-8-sig", newline="") as fp:
		reader = csv.reader(fp)
		for lineno, row in enumerate(reader):
			if has_header and lineno == 0:
				continue
			if not row or domain_col >= len(row):
				skipped += 1
				continue
			domain = db.normalize_domain(row[domain_col])
			if not domain:
				skipped += 1
				continue

			rank = None
			if rank_col is not None and rank_col < len(row):
				try:
					rank = int(str(row[rank_col]).strip())
				except (TypeError, ValueError):
					rank = None

			# 원본 행을 그대로 남긴다. 예상 못 한 컬럼도 잃지 않는다
			if has_header:
				raw = dict(zip(header, row))
			else:
				raw = row
			buf.append((domain, rank, json.dumps(raw, ensure_ascii=False)))
			loaded += 1

			if len(buf) >= BATCH:
				flush()
				buf.clear()
				if not quiet:
					sys.stdout.write("\r  적재 중... {:,}건".format(loaded))
					sys.stdout.flush()
	if buf:
		flush()
	if not quiet:
		sys.stdout.write("\r  적재 완료: {:,}건\n".format(loaded))
	return loaded, skipped


# ---------------------------------------------------------------- 조회

def list_sources() -> List[sqlite3.Row]:
	conn = db.connect()
	try:
		db.init_schema(conn)
		return conn.execute("SELECT * FROM source_datasets ORDER BY id").fetchall()
	finally:
		conn.close()


def _catalog(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row:
	row = conn.execute("SELECT * FROM source_datasets WHERE id = ?", (source_id,)).fetchone()
	if row is None:
		raise ValueError("그런 소스가 없습니다: %s" % source_id)
	return row


def preview(source_id: int, limit: int = 10) -> Dict[str, Any]:
	conn = db.connect()
	try:
		db.init_schema(conn)
		src = _catalog(conn, source_id)
		_assert_known_table(conn, src["table_name"])
		rows = conn.execute(
			"SELECT domain, source_rank, raw FROM %s ORDER BY id LIMIT ?"
			% src["table_name"], (limit,)).fetchall()
		return {"source": src, "rows": rows}
	finally:
		conn.close()


# ---------------------------------------------------------------- 전달

def plan(conn: sqlite3.Connection, table: str) -> Dict[str, int]:
	"""전달하면 어떻게 되는지 집계한다. 쓰기는 하지 않는다."""
	def one(sql: str) -> int:
		return conn.execute(sql.format(t=table)).fetchone()[0]

	return {
		"distinct": one("SELECT COUNT(DISTINCT domain) FROM {t} WHERE domain != ''"),
		"new": one("""SELECT COUNT(DISTINCT s.domain) FROM {t} s
		              WHERE s.domain != ''
		                AND NOT EXISTS (SELECT 1 FROM sites x WHERE x.domain = s.domain)
		                AND NOT EXISTS (SELECT 1 FROM review_status r WHERE r.domain = s.domain)"""),
		"reviewed": one("""SELECT COUNT(DISTINCT s.domain) FROM {t} s
		                   JOIN review_status r ON r.domain = s.domain
		                   WHERE r.status = 'reviewed'"""),
		"excluded": one("""SELECT COUNT(DISTINCT s.domain) FROM {t} s
		                   JOIN review_status r ON r.domain = s.domain
		                   WHERE r.status = 'excluded'"""),
		"existing": one("""SELECT COUNT(DISTINCT s.domain) FROM {t} s
		                   JOIN sites x ON x.domain = s.domain
		                   LEFT JOIN review_status r ON r.domain = s.domain
		                   WHERE r.domain IS NULL"""),
		"linkable": one("""SELECT COUNT(DISTINCT s.domain) FROM {t} s
		                   JOIN sites x ON x.domain = s.domain
		                   WHERE x.source_id IS NULL"""),
	}


def push(source_id: int, dry_run: bool = False) -> Dict[str, Any]:
	"""소스데이터를 작업데이터로 전달한다.

	이미 검수완료·제외한 도메인은 자동으로 빠진다. 작업데이터에 이미 있는데 출처가
	비어 있는 행은 이 소스로 소급 연결한다(기존 100만 행이 여기 해당한다).
	"""
	conn = db.connect()
	try:
		db.init_schema(conn)
		src = _catalog(conn, source_id)
		table = src["table_name"]
		_assert_known_table(conn, table)

		result = plan(conn, table)
		result.update({"id": source_id, "name": src["name"], "table": table,
		               "dry_run": dry_run, "linked": 0, "added": 0})
		if dry_run:
			return result

		stamp = db.now()
		# 신규 — 검수 이력이 있는 도메인은 sites 에 없더라도 다시 넣지 않는다
		cur = conn.execute(
			"""INSERT INTO sites(domain, rank, source_id, source_rank, added_at, fetch_status)
			   SELECT s.domain, MIN(s.source_rank), ?, MIN(s.source_rank), ?, 'pending'
			   FROM {t} s
			   WHERE s.domain != ''
			     AND NOT EXISTS (SELECT 1 FROM sites x WHERE x.domain = s.domain)
			     AND NOT EXISTS (SELECT 1 FROM review_status r WHERE r.domain = s.domain)
			   GROUP BY s.domain""".format(t=table),
			(source_id, stamp),
		)
		result["added"] = cur.rowcount

		# 소급 연결 — 이미 작업데이터에 있는데 출처가 비어 있는 행
		cur = conn.execute(
			"""UPDATE sites SET
			     source_id = ?,
			     source_rank = COALESCE(
			       (SELECT MIN(s.source_rank) FROM {t} s WHERE s.domain = sites.domain),
			       source_rank)
			   WHERE source_id IS NULL
			     AND EXISTS (SELECT 1 FROM {t} s WHERE s.domain = sites.domain)""".format(t=table),
			(source_id,),
		)
		result["linked"] = cur.rowcount

		conn.execute(
			"""UPDATE source_datasets
			   SET pushed_count = pushed_count + ?, last_pushed_at = ?
			   WHERE id = ?""",
			(result["added"], stamp, source_id),
		)
		conn.commit()
		return result
	finally:
		conn.close()
