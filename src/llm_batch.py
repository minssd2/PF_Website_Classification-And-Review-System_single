"""Claude Code 세션 판정 핸드오프 — 배치 요청 파일 생성 / 결과 파일 반영.

API를 호출하지 않는다. 스크립트가 세션에 질문을 넣을 수는 없으므로 파일을 사이에 둔다.

	1) run.py llm-export            → llm/requests/batch_NNNN.md 생성
	2) 콘솔의 Claude Code 세션에서 그 파일을 읽고 분류 → llm/results/batch_NNNN.json 저장
	3) run.py llm-import <결과파일>  → DB 반영

배치 상태가 DB에 남으므로 어느 단계에서 멈춰도 이어서 진행할 수 있다.
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Dict, List, Optional, Tuple

from . import db

LLM_DIR = os.path.join(db.PROJECT_ROOT, "llm")
REQUEST_DIR = os.path.join(LLM_DIR, "requests")
RESULT_DIR = os.path.join(LLM_DIR, "results")

CATEGORY_GUIDE = """\
- 웹메일: 이메일 송수신 서비스 (메일함, 웹메일 로그인)
- 쇼핑: 상품을 사고파는 커머스 (오픈마켓, 브랜드몰, 가격비교)
- 증권: 주식·금융투자 (증권사, 시세, 코인 거래소 포함)
- 취업: 채용·구인구직 (채용공고, 이력서, 알바)
- 게임: 게임 서비스·게임 정보 (게임사 공식, 게임 커뮤니티/공략)
- 엔터테인먼트: 영상·음악·웹툰·웹소설·영화 예매 등 콘텐츠 소비
- 뉴스: 언론사·뉴스 매체
- SNS: 소셜네트워크·커뮤니티·블로그 (사용자끼리 소통이 핵심)
- 웹하드: 파일 저장·공유·다운로드 서비스 (클라우드 스토리지 포함)
- 생성형AI: 생성형 AI 서비스 (챗봇, 이미지·글 생성, 모델 허브)"""


def _next_batch_id(conn: sqlite3.Connection) -> str:
	row = conn.execute("SELECT batch_id FROM llm_batches ORDER BY batch_id DESC LIMIT 1").fetchone()
	if row is None:
		return "batch_0001"
	try:
		n = int(str(row["batch_id"]).split("_")[-1]) + 1
	except ValueError:
		n = conn.execute("SELECT COUNT(*) FROM llm_batches").fetchone()[0] + 1
	return "batch_%04d" % n


def select_pending(conn: sqlite3.Connection, size: int) -> List[sqlite3.Row]:
	"""LLM 판정이 필요한 도메인.

	두 갈래를 합쳐서 고른다.
	  ① 검수 화면에서 사람이 직접 요청한 것 (`llm_queue.batch_id IS NULL`) — 먼저 나간다
	  ② 규칙으로 확정되지 않아 자동으로 대상이 되는 것

	이미 배치에 실려 나간 건(`batch_id` 가 채워진 행)은 제외한다.
	"""
	threshold = float(db.get_setting(conn, "classify.llm_conf_threshold", 0.6))
	return conn.execute(
		"""
		SELECT s.domain, s.rank, s.title, s.description, s.text_sample,
		       c.method, c.confidence, c.evidence, c.primary_category,
		       (q.domain IS NOT NULL) AS requested
		FROM sites s
		LEFT JOIN korean k ON k.domain = s.domain
		LEFT JOIN classification c ON c.domain = s.domain
		LEFT JOIN manual_labels m ON m.domain = s.domain
		LEFT JOIN llm_queue q ON q.domain = s.domain
		WHERE q.batch_id IS NULL
		  AND (
		    -- 사람이 직접 요청한 건 한국어 여부·수동 라벨과 무관하게 내보낸다
		    q.domain IS NOT NULL
		    -- 자동 선정은 한국어로 판정됐고 사람 손을 타지 않은 것 중에서만
		    OR (m.domain IS NULL AND k.is_korean = 1
		        AND (c.domain IS NULL
		             OR c.method = 'unclassified'
		             OR (c.method = 'rule' AND c.confidence < ?)))
		  )
		ORDER BY requested DESC, s.rank
		LIMIT ?
		""",
		(threshold, size),
	).fetchall()


def request_llm(conn: sqlite3.Connection, domains: List[str]) -> Dict[str, int]:
	"""검수 화면에서 고른 사이트를 LLM 판정 대기열에 넣는다.

	batch_id 를 비워 두면 다음 `llm-export` 가 우선 집어 간다.
	"""
	stamp = db.now()
	added = skipped = 0
	for domain in domains:
		if not conn.execute("SELECT 1 FROM sites WHERE domain = ?", (domain,)).fetchone():
			skipped += 1
			continue
		cur = conn.execute(
			"INSERT OR IGNORE INTO llm_queue(domain, batch_id, queued_at, manual) "
			"VALUES (?, NULL, ?, 1)",
			(domain, stamp),
		)
		added += cur.rowcount
	conn.commit()
	return {"added": added, "skipped": skipped}


def cancel_batch(batch_id: str, remove_file: bool = True) -> Dict[str, int]:
	"""내보낸 배치를 없던 일로 되돌린다.

	판정이 실패하면 배치가 그대로 남아, 거기 실린 도메인들이 "이미 나간 건"으로 묶여
	영영 다시 처리되지 않는다. 그래서 실패 시 이 함수로 큐를 풀어 준다.
	사람이 요청했던 건은 요청 상태(batch_id=NULL)로 되돌리고, 자동 선정된 건은 지운다.
	"""
	conn = db.connect()
	try:
		restored = conn.execute(
			"UPDATE llm_queue SET batch_id = NULL WHERE batch_id = ? AND manual = 1",
			(batch_id,),
		).rowcount
		dropped = conn.execute(
			"DELETE FROM llm_queue WHERE batch_id = ? AND manual = 0", (batch_id,)
		).rowcount
		conn.execute("DELETE FROM llm_batches WHERE batch_id = ?", (batch_id,))
		conn.commit()
	finally:
		conn.close()

	if remove_file:
		path = os.path.join(REQUEST_DIR, "%s.md" % batch_id)
		if os.path.exists(path):
			os.remove(path)
	return {"restored": restored, "dropped": dropped}


def unrequest_llm(conn: sqlite3.Connection, domain: str) -> int:
	"""아직 배치로 나가지 않은 요청만 취소한다."""
	cur = conn.execute(
		"DELETE FROM llm_queue WHERE domain = ? AND batch_id IS NULL", (domain,)
	)
	conn.commit()
	return cur.rowcount


def _render_request(batch_id: str, rows: List[sqlite3.Row], excerpt_len: int) -> str:
	lines: List[str] = []
	lines.append("# 분류 요청 %s (%d건)" % (batch_id, len(rows)))
	lines.append("")
	lines.append("아래 사이트들을 카테고리로 분류해 주세요.")
	lines.append("")
	lines.append("## 카테고리 정의")
	lines.append(CATEGORY_GUIDE)
	lines.append("")
	lines.append("## 판정 규칙")
	lines.append("- 해당하는 카테고리가 없으면 `[\"none\"]` 으로 둡니다. 억지로 끼워맞추지 마세요.")
	lines.append("- 여러 카테고리에 해당하면 모두 적되, 가장 대표적인 것을 맨 앞에 둡니다.")
	lines.append("- 정보가 부족해 판단이 어려우면 `[\"none\"]` + confidence 를 낮게 줍니다.")
	lines.append("- 위 10개 이름을 **정확히 그대로** 사용합니다. (SNS, 생성형AI 등 표기 주의)")
	lines.append("")
	lines.append("## 출력")
	lines.append("`llm/results/%s.json` 파일에 아래 스키마로 저장해 주세요." % batch_id)
	lines.append("```json")
	lines.append('[{"domain": "example.com", "categories": ["쇼핑"], "confidence": 0.9, '
	             '"evidence": "판단 근거 한 줄"}]')
	lines.append("```")
	lines.append("")
	lines.append("---")
	lines.append("")

	for i, r in enumerate(rows, 1):
		mark = " ★" if ("requested" in r.keys() and r["requested"]) else ""
		lines.append("## %d. %s%s" % (i, r["domain"], mark))
		if mark:
			lines.append("- (검수자가 직접 분석을 요청한 사이트)")
		lines.append("- rank: %s" % r["rank"])
		lines.append("- title: %s" % (r["title"] or "(없음)"))
		lines.append("- description: %s" % (r["description"] or "(없음)"))
		text = (r["text_sample"] or "").strip()
		lines.append("- 본문: %s" % (text[:excerpt_len] if text else "(없음)"))
		if r["method"] == "rule":
			lines.append("- 참고(규칙 추정): %s / %s" % (r["primary_category"], r["evidence"]))
		lines.append("")

	return "\n".join(lines)


def run_export(size: Optional[int] = None, auto_prompt: bool = True) -> Optional[str]:
	conn = db.connect()
	try:
		db.init_schema(conn)
		os.makedirs(REQUEST_DIR, exist_ok=True)
		os.makedirs(RESULT_DIR, exist_ok=True)

		size = size or int(db.get_setting(conn, "llm.batch_size", 40))
		excerpt_len = int(db.get_setting(conn, "llm.excerpt_len", 300))

		rows = select_pending(conn, size)
		if not rows:
			remaining = conn.execute("SELECT COUNT(*) FROM llm_queue").fetchone()[0]
			if remaining:
				print("새로 내보낼 대상이 없습니다. 아직 반영되지 않은 배치가 %d건 있습니다." % remaining)
				pending_batches(conn)
			else:
				print("LLM 판정이 필요한 사이트가 없습니다.")
			return None

		batch_id = _next_batch_id(conn)
		request_path = os.path.join(REQUEST_DIR, "%s.md" % batch_id)
		result_path = os.path.join(RESULT_DIR, "%s.json" % batch_id)

		with open(request_path, "w", encoding="utf-8") as fp:
			fp.write(_render_request(batch_id, rows, excerpt_len))

		stamp = db.now()
		conn.execute(
			"""INSERT INTO llm_batches(batch_id, status, size, request_path, result_path, exported_at)
			   VALUES (?, 'exported', ?, ?, ?, ?)""",
			(batch_id, len(rows), request_path, result_path, stamp),
		)
		# REPLACE 를 쓰면 사람이 요청했다는 표시(manual)가 지워지므로 UPSERT 로 갱신한다
		conn.executemany(
			"""INSERT INTO llm_queue(domain, batch_id, queued_at, manual) VALUES (?,?,?,0)
			   ON CONFLICT(domain) DO UPDATE SET
			     batch_id = excluded.batch_id, queued_at = excluded.queued_at""",
			[(r["domain"], batch_id, stamp) for r in rows],
		)
		conn.commit()

		rel_request = os.path.relpath(request_path, db.PROJECT_ROOT)
		rel_result = os.path.relpath(result_path, db.PROJECT_ROOT)
		print("배치 %s 생성: %d건 → %s" % (batch_id, len(rows), rel_request))
		if auto_prompt:
			print("")
			print("─" * 66)
			print("아래 문장을 콘솔의 Claude Code 세션에 붙여넣으세요:")
			print("")
			print("  %s 를 읽고 지시대로 분류해서 %s 로 저장해줘" % (rel_request, rel_result))
			print("")
			print("완료되면:  python run.py llm-import %s" % rel_result)
			print("─" * 66)
		return batch_id
	finally:
		conn.close()


def _normalize_categories(raw, valid: set) -> Tuple[List[str], List[str]]:
	"""유효한 카테고리만 남기고, 무시된 값을 함께 돌려준다."""
	if isinstance(raw, str):
		raw = [raw]
	if not isinstance(raw, list):
		return [], ["형식 오류: %r" % (raw,)]
	kept: List[str] = []
	dropped: List[str] = []
	for item in raw:
		name = str(item).strip()
		if name in valid:
			if name not in kept:
				kept.append(name)
		elif name.lower() in ("none", "없음", ""):
			continue
		else:
			dropped.append(name)
	return kept, dropped


def run_import(result_path: str) -> Dict:
	if not os.path.isabs(result_path):
		result_path = os.path.join(db.PROJECT_ROOT, result_path)
	if not os.path.exists(result_path):
		raise FileNotFoundError("결과 파일이 없습니다: %s" % result_path)

	with open(result_path, "r", encoding="utf-8") as fp:
		payload = json.load(fp)
	if isinstance(payload, dict):
		payload = payload.get("results") or payload.get("data") or []
	if not isinstance(payload, list):
		raise ValueError("결과 파일은 JSON 배열이어야 합니다.")

	conn = db.connect()
	try:
		db.init_schema(conn)
		valid = set(db.CATEGORIES)
		batch_id = os.path.splitext(os.path.basename(result_path))[0]

		queued = {
			r["domain"] for r in conn.execute(
				"SELECT domain FROM llm_queue WHERE batch_id = ?", (batch_id,)
			).fetchall()
		}

		stamp = db.now()
		applied: List[Tuple] = []
		warnings: List[str] = []
		seen: set = set()

		for entry in payload:
			if not isinstance(entry, dict):
				warnings.append("항목 형식 오류: %r" % (entry,))
				continue
			domain = str(entry.get("domain", "")).strip().lower()
			if not domain:
				warnings.append("domain 누락: %r" % (entry,))
				continue
			seen.add(domain)

			cats, dropped = _normalize_categories(entry.get("categories"), valid)
			if dropped:
				warnings.append("%s: 알 수 없는 카테고리 무시 %s" % (domain, dropped))

			try:
				confidence = float(entry.get("confidence", 0.8))
			except (TypeError, ValueError):
				confidence = 0.8
			evidence = str(entry.get("evidence", ""))[:500]
			primary = cats[0] if cats else "none"

			applied.append((domain, db.json_dumps(cats), primary, "llm",
			                confidence, evidence, stamp))

		if applied:
			conn.executemany(
				"""INSERT INTO classification
				   (domain, categories, primary_category, method, confidence, evidence, classified_at)
				   VALUES (?,?,?,?,?,?,?)
				   ON CONFLICT(domain) DO UPDATE SET
				     categories=excluded.categories, primary_category=excluded.primary_category,
				     method=excluded.method, confidence=excluded.confidence,
				     evidence=excluded.evidence, classified_at=excluded.classified_at
				   WHERE classification.method != 'manual'""",
				applied,
			)
			# 반영된 도메인만 큐에서 뺀다 — 빠진 건은 다음 배치에 다시 실린다
			conn.executemany(
				"DELETE FROM llm_queue WHERE domain = ?", [(d[0],) for d in applied]
			)

		missing = sorted(queued - seen)
		if missing:
			warnings.append("결과에 빠진 도메인 %d건은 큐에 남겨 다음 배치로 넘깁니다." % len(missing))

		conn.execute(
			"UPDATE llm_batches SET status = ?, imported_at = ?, result_path = ? WHERE batch_id = ?",
			("imported" if not missing else "partial", stamp, result_path, batch_id),
		)
		conn.commit()

		print("배치 %s 반영: %d건" % (batch_id, len(applied)))
		for w in warnings[:20]:
			print("  경고: %s" % w)
		if len(warnings) > 20:
			print("  ... 경고 %d건 더" % (len(warnings) - 20))
		return {"applied": len(applied), "warnings": warnings, "missing": missing}
	finally:
		conn.close()


def pending_batches(conn: Optional[sqlite3.Connection] = None) -> List[Dict]:
	own = conn is None
	conn = conn or db.connect()
	try:
		rows = conn.execute(
			"""SELECT batch_id, status, size, request_path, result_path, exported_at
			   FROM llm_batches WHERE status != 'imported' ORDER BY batch_id"""
		).fetchall()
		items = []
		for r in rows:
			item = dict(r)
			item["result_exists"] = os.path.exists(r["result_path"] or "")
			items.append(item)
			if own:
				print("  %s (%s, %d건) 결과파일 %s" % (
					r["batch_id"], r["status"], r["size"],
					"있음" if item["result_exists"] else "대기 중"))
		return items
	finally:
		if own:
			conn.close()
