"""초기 데이터 적재 — tranco CSV → sites, 카테고리 규칙 YAML → category_rules."""
from __future__ import annotations

import csv
import os
import sqlite3
import sys
from typing import Dict, Iterator, List, Tuple

import yaml

from . import db

CSV_PATH = os.path.join(db.PROJECT_ROOT, "top-1m.csv")
CATEGORY_SEED = os.path.join(db.PROJECT_ROOT, "seed", "categories.seed.yaml")

BATCH = 20000


def _rows(path: str) -> Iterator[Tuple[int, str]]:
	with open(path, "r", encoding="utf-8", newline="") as fp:
		for row in csv.reader(fp):
			if len(row) < 2:
				continue
			try:
				rank = int(row[0])
			except ValueError:
				continue  # 헤더 행
			domain = row[1].strip().lower()
			if domain:
				yield rank, domain


def load_sites(conn: sqlite3.Connection, csv_path: str = CSV_PATH) -> int:
	"""CSV를 sites에 적재한다. 이미 있는 도메인은 건드리지 않는다(멱등)."""
	if not os.path.exists(csv_path):
		raise FileNotFoundError("tranco CSV를 찾을 수 없습니다: %s" % csv_path)

	inserted = 0
	buf: List[Tuple[int, str]] = []
	for rank, domain in _rows(csv_path):
		buf.append((rank, domain))
		if len(buf) >= BATCH:
			inserted += _flush(conn, buf)
			buf.clear()
			sys.stdout.write("\r  적재 중... {:,}건".format(inserted))
			sys.stdout.flush()
	if buf:
		inserted += _flush(conn, buf)
	sys.stdout.write("\r  적재 완료: {:,}건 신규\n".format(inserted))
	return inserted


def _flush(conn: sqlite3.Connection, buf: List[Tuple[int, str]]) -> int:
	cur = conn.executemany(
		"INSERT OR IGNORE INTO sites(rank, domain) VALUES (?, ?)", buf
	)
	conn.commit()
	return cur.rowcount if cur.rowcount > 0 else 0


def load_category_rules(conn: sqlite3.Connection, path: str = CATEGORY_SEED) -> int:
	"""YAML 규칙을 category_rules에 주입한다. 기존 규칙은 덮어쓰지 않는다."""
	if not os.path.exists(path):
		print("  규칙 시드 파일 없음, 건너뜀: %s" % path)
		return 0

	with open(path, "r", encoding="utf-8") as fp:
		data: Dict = yaml.safe_load(fp) or {}

	added = 0
	for category, groups in data.items():
		if category not in db.CATEGORIES:
			print("  경고: 정의되지 않은 카테고리 '%s' — 건너뜀" % category)
			continue
		for rule_type, items in (groups or {}).items():
			if rule_type not in ("domain", "keyword", "exclude"):
				print("  경고: 알 수 없는 rule_type '%s' — 건너뜀" % rule_type)
				continue
			for item in items or []:
				pattern = str(item["pattern"]).strip()
				weight = float(item.get("weight", 1.0))
				if not pattern:
					continue
				cur = conn.execute(
					"""INSERT OR IGNORE INTO category_rules
					   (category, rule_type, pattern, weight, enabled, updated_at)
					   VALUES (?,?,?,?,1,?)""",
					(category, rule_type, pattern, weight, db.now()),
				)
				added += cur.rowcount
	conn.commit()
	return added


def run_init(csv_path: str = CSV_PATH) -> None:
	conn = db.connect()
	try:
		db.init_schema(conn)
		print("[1/3] 스키마 준비 완료")

		n_settings = db.seed_settings(conn)
		print("[2/3] 기본 설정 %d개 주입 (기존 값은 유지)" % n_settings)

		n_rules = load_category_rules(conn)
		print("      카테고리 규칙 %d개 주입" % n_rules)

		print("[3/3] tranco CSV 적재")
		load_sites(conn, csv_path)

		total = conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
		print("\n총 사이트: {:,}건".format(total))
	finally:
		conn.close()
