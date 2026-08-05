"""최종 산출물 생성 — 한국어 사이트 목록, 분류 결과, 카테고리별 파일, 요약 리포트."""
from __future__ import annotations

import csv
import os
import sqlite3
from typing import Dict, List

from . import db

OUT_DIR = os.path.join(db.PROJECT_ROOT, "out")
# 검수완료분만 뽑을 때는 전체 산출물을 덮어쓰지 않도록 하위 폴더에 따로 쓴다
REVIEWED_DIR = os.path.join(OUT_DIR, "reviewed")


def _effective_rows(conn: sqlite3.Connection) -> List[sqlite3.Row]:
	"""수동 라벨을 우선 적용한 최종 분류 결과."""
	return conn.execute(
		"""
		SELECT s.rank, s.domain, COALESCE(m.title, s.title) AS title,
		       s.description, s.final_url,
		       k.score AS korean_score, k.reasons AS korean_reasons,
		       COALESCE(m.categories, c.categories) AS categories,
		       CASE WHEN m.categories IS NOT NULL THEN 'manual' ELSE c.method END AS method,
		       c.primary_category, c.confidence, c.evidence,
		       rv.status AS review_status, rv.reason AS review_reason
		FROM sites s
		JOIN korean k ON k.domain = s.domain AND k.is_korean = 1
		LEFT JOIN classification c ON c.domain = s.domain
		LEFT JOIN manual_labels m ON m.domain = s.domain
		LEFT JOIN review_status rv ON rv.domain = s.domain
		ORDER BY s.rank
		"""
	).fetchall()


def _write_csv(path: str, header: List[str], rows: List[List]) -> None:
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w", encoding="utf-8-sig", newline="") as fp:
		writer = csv.writer(fp)
		writer.writerow(header)
		writer.writerows(rows)


def run_export(reviewed_only: bool = False) -> Dict[str, int]:
	"""산출물을 만든다.

	reviewed_only=True 면 검수완료로 표시한 사이트만 골라 `out/reviewed/` 에 쓴다.
	전체 산출물(`out/`)은 그대로 두므로 두 결과를 나란히 볼 수 있다.
	"""
	out_dir = REVIEWED_DIR if reviewed_only else OUT_DIR
	by_category_dir = os.path.join(out_dir, "by_category")

	conn = db.connect()
	try:
		os.makedirs(out_dir, exist_ok=True)
		all_rows = _effective_rows(conn)

		# 검수에서 '제외'로 표시한 사이트는 결과물에서 뺀다 (목록은 따로 남긴다)
		excluded = [r for r in all_rows if r["review_status"] == "excluded"]
		if reviewed_only:
			rows = [r for r in all_rows if r["review_status"] == "reviewed"]
		else:
			rows = [r for r in all_rows if r["review_status"] != "excluded"]

		# 1) 한국어 사이트 목록
		_write_csv(
			os.path.join(out_dir, "korean_sites.csv"),
			["rank", "domain", "title", "korean_score", "korean_reasons",
			 "review_status", "final_url"],
			[[r["rank"], r["domain"], r["title"], r["korean_score"],
			  r["korean_reasons"], r["review_status"] or "", r["final_url"]] for r in rows],
		)

		# 1-1) 제외된 사이트 목록 (전체 산출물에만 넣는다)
		if not reviewed_only:
			_write_csv(
				os.path.join(out_dir, "excluded_sites.csv"),
				["rank", "domain", "title", "review_reason"],
				[[r["rank"], r["domain"], r["title"], r["review_reason"] or ""] for r in excluded],
			)

		# 2) 분류 결과 전체
		classified_rows = []
		by_category: Dict[str, List[List]] = {c: [] for c in db.CATEGORIES}
		for r in rows:
			cats = db.json_loads(r["categories"], []) or []
			primary = cats[0] if cats else (r["primary_category"] or "none")
			line = [r["rank"], r["domain"], r["title"], primary,
			        ";".join(cats), r["method"] or "unclassified",
			        r["confidence"], r["evidence"]]
			classified_rows.append(line)
			for cat in cats:
				if cat in by_category:
					by_category[cat].append(
						[r["rank"], r["domain"], r["title"], r["description"],
						 ";".join(cats), r["method"] or "unclassified", r["confidence"]]
					)

		_write_csv(
			os.path.join(out_dir, "classified_sites.csv"),
			["rank", "domain", "title", "primary_category", "categories",
			 "method", "confidence", "evidence"],
			classified_rows,
		)

		# 3) 카테고리별 파일
		os.makedirs(by_category_dir, exist_ok=True)
		for cat, lines in by_category.items():
			_write_csv(
				os.path.join(by_category_dir, "%s.csv" % cat),
				["rank", "domain", "title", "description", "categories", "method", "confidence"],
				lines,
			)

		# 4) 요약 리포트
		summary = _build_summary(conn, rows, by_category, len(excluded), reviewed_only)
		with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as fp:
			fp.write(summary)

		matched = sum(1 for line in classified_rows if line[4])
		label = "검수완료 사이트" if reviewed_only else "한국어 사이트"
		print("산출물 생성 완료 → %s" % os.path.relpath(out_dir, db.PROJECT_ROOT))
		if reviewed_only:
			print("  {} {:,}건, 카테고리 매칭 {:,}건".format(label, len(rows), matched))
		else:
			print("  {} {:,}건, 카테고리 매칭 {:,}건, 검수 제외 {:,}건".format(
				label, len(rows), matched, len(excluded)))
		return {"korean": len(rows), "matched": matched,
		        "excluded": 0 if reviewed_only else len(excluded),
		        "reviewed_only": reviewed_only}
	finally:
		conn.close()


def _build_summary(conn: sqlite3.Connection, rows, by_category: Dict[str, List],
                   excluded_count: int = 0, reviewed_only: bool = False) -> str:
	fetch = db.counts_by(conn, "sites", "fetch_status")
	total = sum(fetch.values())
	method = db.counts_by(conn, "classification", "method")
	llm_batches = conn.execute(
		"SELECT COUNT(*) FROM llm_batches WHERE status IN ('imported','partial')"
	).fetchone()[0]
	manual = conn.execute("SELECT COUNT(*) FROM manual_labels").fetchone()[0]

	lines: List[str] = []
	lines.append("# 웹사이트 분류 결과 요약%s" % (" (검수완료분)" if reviewed_only else ""))
	lines.append("")
	lines.append("생성 시각: %s" % db.now())
	if reviewed_only:
		lines.append("")
		lines.append("> **검수완료로 표시한 사이트만 담았다.** 미검수 사이트는 들어 있지 않다.")
		lines.append("> 전체 결과는 상위 폴더 `out/` 에 있다.")
	lines.append("")
	lines.append("## 크롤링")
	lines.append("")
	lines.append("| 상태 | 건수 |")
	lines.append("|---|---:|")
	for key in ("ok", "failed", "dead", "pending"):
		lines.append(f"| {key} | {fetch.get(key, 0):,} |")
	lines.append(f"| **합계** | **{total:,}** |")
	lines.append("")
	lines.append("## 한국어 판정")
	lines.append("")
	judged = conn.execute("SELECT COUNT(*) FROM korean").fetchone()[0]
	korean_total = conn.execute(
		"SELECT COUNT(*) FROM korean WHERE is_korean = 1").fetchone()[0]
	lines.append("- 판정 완료: {:,}건".format(judged))
	lines.append("- 한국어 서비스: {:,}건".format(korean_total))
	if not reviewed_only:
		lines.append("- 검수에서 제외: {:,}건 (excluded_sites.csv)".format(excluded_count))
	lines.append("- **결과에 포함: {:,}건**{}".format(
		len(rows), " (검수완료분만)" if reviewed_only else ""))
	lines.append("")
	lines.append("## 검수 현황")
	lines.append("")
	review = db.counts_by(conn, "review_status", "status")
	lines.append(f"- 검수완료: {review.get('reviewed', 0):,}건")
	lines.append(f"- 제외: {review.get('excluded', 0):,}건")
	lines.append(f"- 미검수: {max(korean_total - sum(review.values()), 0):,}건")
	lines.append("")
	lines.append("## 카테고리별 건수")
	lines.append("")
	lines.append("| 카테고리 | 건수 |")
	lines.append("|---|---:|")
	for cat in db.CATEGORIES:
		lines.append(f"| {cat} | {len(by_category.get(cat, [])):,} |")
	lines.append("")
	lines.append("## 분류 방식")
	lines.append("")
	for key, label in (("rule", "규칙"), ("llm", "세션 판정"),
	                   ("manual", "수동"), ("unclassified", "미분류")):
		lines.append(f"- {label}: {method.get(key, 0):,}건")
	lines.append("- 반영된 LLM 배치: %d개" % llm_batches)
	lines.append(f"- 수동 라벨: {manual:,}건")
	lines.append("")
	return "\n".join(lines)
