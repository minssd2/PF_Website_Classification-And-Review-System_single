"""규칙 기반 카테고리 분류 — category_rules 테이블을 읽어 점수를 매긴다.

도메인 패턴은 단순 부분일치를 쓰지 않는다. 'ai' 가 'mail' 에 걸리는 식의 오탐을
막기 위해 기본은 도메인 라벨 토큰과의 정확 일치이고, 부분일치가 필요하면
패턴에 '.' 이나 '*' 를 넣어 명시한다.
"""
from __future__ import annotations

import fnmatch
import re
import sqlite3
import sys
from typing import Dict, List, Optional, Tuple

from . import db

TOKEN_SPLIT = re.compile(r"[^a-z0-9가-힣]+")
BATCH = 2000


class RuleSet:
	"""DB의 규칙을 매칭하기 좋은 형태로 미리 정리해 둔 묶음."""

	def __init__(self, rows: List[sqlite3.Row]):
		self.domain: Dict[str, List[Tuple[str, float]]] = {}
		self.keyword: Dict[str, List[Tuple[str, float]]] = {}
		self.exclude: Dict[str, List[Tuple[str, float]]] = {}
		for r in rows:
			bucket = getattr(self, r["rule_type"], None)
			if bucket is None:
				continue
			bucket.setdefault(r["category"], []).append((r["pattern"], float(r["weight"])))

	@property
	def categories(self) -> List[str]:
		names = set(self.domain) | set(self.keyword) | set(self.exclude)
		return [c for c in db.CATEGORIES if c in names]


def load_rules(conn: sqlite3.Connection) -> RuleSet:
	"""활성화된 규칙 전체를 읽는다.

	카테고리 하나만 골라 분류하면 나머지 카테고리 점수가 0이 되어 기존 분류를
	지워버리므로, 분류는 항상 전체 규칙으로 수행한다.
	"""
	return RuleSet(conn.execute(
		"SELECT category, rule_type, pattern, weight FROM category_rules WHERE enabled = 1"
	).fetchall())


def domain_matches(domain: str, pattern: str) -> bool:
	domain = domain.lower()
	pattern = pattern.lower()
	if "*" in pattern or "?" in pattern:
		return fnmatch.fnmatch(domain, pattern)
	if "." in pattern:
		return pattern in domain
	return pattern in TOKEN_SPLIT.split(domain)


def _haystack(row: Dict) -> str:
	parts = [row.get("title"), row.get("description"), row.get("text_sample")]
	return " ".join(p for p in parts if p).lower()


def score_site(row: Dict, rules: RuleSet) -> Dict[str, float]:
	"""카테고리별 점수를 계산한다."""
	domain = (row.get("domain") or "").lower()
	text = _haystack(row)
	scores: Dict[str, float] = {}

	for category, patterns in rules.domain.items():
		hit = sum(w for p, w in patterns if domain_matches(domain, p))
		if hit:
			scores[category] = scores.get(category, 0.0) + hit

	if text:
		for category, patterns in rules.keyword.items():
			hit = sum(w for p, w in patterns if p.lower() in text)
			if hit:
				scores[category] = scores.get(category, 0.0) + hit

		for category, patterns in rules.exclude.items():
			if category in scores and any(p.lower() in text for p, _ in patterns):
				scores[category] = 0.0

	return {c: s for c, s in scores.items() if s > 0}


def decide(scores: Dict[str, float], cfg: Dict) -> Dict:
	"""점수를 최종 분류 결과로 환원한다."""
	if not scores:
		return {"categories": [], "primary": "none", "method": "unclassified",
		        "confidence": 0.0, "evidence": "규칙 매칭 없음"}

	ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
	top, top_score = ranked[0]
	second_score = ranked[1][1] if len(ranked) > 1 else 0.0
	hits = [c for c, s in ranked if s >= cfg["min_score"]]

	if not hits:
		return {"categories": [], "primary": "none", "method": "unclassified",
		        "confidence": 0.0,
		        "evidence": "최고 점수 %.1f < 기준 %.1f (%s)" % (top_score, cfg["min_score"], top)}

	confidence = min(1.0, top_score / (cfg["min_score"] * 2))
	if top_score - second_score < cfg["margin"]:
		confidence *= 0.7  # 1·2위가 붙어 있으면 확신을 낮춰 LLM 판정으로 넘긴다

	evidence = ", ".join("%s %.1f" % (c, s) for c, s in ranked[:3])
	return {"categories": hits, "primary": top, "method": "rule",
	        "confidence": round(confidence, 3), "evidence": evidence}


def _load_cfg(conn: sqlite3.Connection) -> Dict:
	s = db.get_settings(conn)
	return {
		"min_score": float(s.get("classify.min_score", 2.0)),
		"margin": float(s.get("classify.margin", 1.0)),
	}


def _target_sql(force: bool) -> str:
	"""한국어로 판정된 사이트 중 아직 분류되지 않은 것들."""
	extra = "" if force else "AND c.domain IS NULL"
	return """
		SELECT s.domain, s.title, s.description, s.text_sample
		FROM sites s
		JOIN korean k ON k.domain = s.domain AND k.is_korean = 1
		LEFT JOIN classification c ON c.domain = s.domain
		LEFT JOIN manual_labels m ON m.domain = s.domain
		WHERE m.domain IS NULL {extra}
		ORDER BY s.rank
	""".format(extra=extra)


def run_classify(force: bool = False, progress_cb=None) -> Dict[str, int]:
	conn = db.connect()
	try:
		db.init_schema(conn)
		cfg = _load_cfg(conn)
		rules = load_rules(conn)
		if not rules.categories:
			print("활성화된 규칙이 없습니다.")
			return {"total": 0, "classified": 0}

		sql = _target_sql(force)
		total = conn.execute("SELECT COUNT(*) FROM (%s)" % sql).fetchone()[0]
		if total == 0:
			print("분류할 대상이 없습니다. (다시 하려면 --force)")
			return {"total": 0, "classified": 0}

		job_id = db.start_job(conn, "classify-rule", total)
		print("규칙 분류 대상 {:,}건".format(total))

		reader = db.connect(readonly=True)
		cursor = reader.execute(sql)

		processed = 0
		classified = 0
		stamp = db.now()

		while True:
			rows = cursor.fetchmany(BATCH)
			if not rows:
				break
			buf: List[Tuple] = []
			for row in rows:
				data = dict(row)
				result = decide(score_site(data, rules), cfg)
				if result["method"] == "rule":
					classified += 1
				buf.append((
					data["domain"], db.json_dumps(result["categories"]), result["primary"],
					result["method"], result["confidence"], result["evidence"], stamp,
				))
			conn.executemany(
				"""INSERT INTO classification
				   (domain, categories, primary_category, method, confidence, evidence, classified_at)
				   VALUES (?,?,?,?,?,?,?)
				   ON CONFLICT(domain) DO UPDATE SET
				     categories=excluded.categories, primary_category=excluded.primary_category,
				     method=excluded.method, confidence=excluded.confidence,
				     evidence=excluded.evidence, classified_at=excluded.classified_at
				   WHERE classification.method NOT IN ('manual', 'llm')""",
				buf,
			)
			conn.commit()
			processed += len(buf)
			db.update_job(conn, job_id, processed=processed, ok_count=classified)
			if progress_cb:
				progress_cb(processed, total)
			sys.stdout.write("\r  {:,}/{:,}  규칙 확정 {:,}건".format(processed, total, classified))
			sys.stdout.flush()

		reader.close()
		db.update_job(conn, job_id, status="done", processed=processed,
		              ok_count=classified, message="규칙 확정 %d건" % classified)
		print("\n완료: {:,}건 처리, 규칙 확정 {:,}건, 미확정 {:,}건".format(
			processed, classified, processed - classified))
		return {"total": processed, "classified": classified}
	finally:
		conn.close()


def classify_one(conn: sqlite3.Connection, domain: str) -> Optional[Dict]:
	"""도메인 하나만 분류해 저장한다 (검수 화면에서 사이트를 추가할 때 쓴다)."""
	row = conn.execute(
		"SELECT domain, title, description, text_sample FROM sites WHERE domain = ?",
		(domain,),
	).fetchone()
	if row is None:
		return None

	result = decide(score_site(dict(row), load_rules(conn)), _load_cfg(conn))
	conn.execute(
		"""INSERT INTO classification
		   (domain, categories, primary_category, method, confidence, evidence, classified_at)
		   VALUES (?,?,?,?,?,?,?)
		   ON CONFLICT(domain) DO UPDATE SET
		     categories=excluded.categories, primary_category=excluded.primary_category,
		     method=excluded.method, confidence=excluded.confidence,
		     evidence=excluded.evidence, classified_at=excluded.classified_at
		   WHERE classification.method NOT IN ('manual', 'llm')""",
		(domain, db.json_dumps(result["categories"]), result["primary"], result["method"],
		 result["confidence"], result["evidence"], db.now()),
	)
	conn.commit()
	return result


def _snippet(text: str, needle: str, span: int = 45) -> str:
	"""키워드가 걸린 자리를 앞뒤 문맥과 함께 잘라 낸다 (검수 화면에서 근거로 보여준다)."""
	i = text.find(needle)
	if i < 0:
		return ""
	start = max(0, i - span)
	end = min(len(text), i + len(needle) + span)
	out = text[start:end].replace("\n", " ").strip()
	return ("…" if start else "") + out + ("…" if end < len(text) else "")


def explain(conn: sqlite3.Connection, domain: str) -> Optional[Dict]:
	"""이 도메인이 왜 그렇게 분류됐는지 규칙 단위로 되짚는다.

	`classification.evidence` 는 카테고리별 합계만 담고 있어서 어떤 규칙이 걸렸는지
	알 수 없다. 저장해 두는 대신 현재 규칙으로 그때그때 다시 채점한다.
	규칙을 고치면 설명도 같이 바뀌어야 하기 때문이다.
	"""
	row = conn.execute(
		"""SELECT s.domain, s.title, s.description, s.text_sample
		   FROM sites s WHERE s.domain = ?""",
		(domain,),
	).fetchone()
	if row is None:
		return None

	data = dict(row)
	rules = load_rules(conn)
	cfg = _load_cfg(conn)
	text = _haystack(data)
	dom = (data["domain"] or "").lower()

	matched: List[Dict] = []
	for category, patterns in rules.domain.items():
		for p, w in patterns:
			if domain_matches(dom, p):
				kind = "와일드카드" if ("*" in p or "?" in p) else ("부분일치" if "." in p else "토큰 일치")
				matched.append({"rule_type": "domain", "category": category, "pattern": p,
				                "weight": w, "where": kind, "snippet": dom})
	for category, patterns in rules.keyword.items():
		for p, w in patterns:
			if p.lower() in text:
				matched.append({"rule_type": "keyword", "category": category, "pattern": p,
				                "weight": w, "where": _where_hit(data, p),
				                "snippet": _snippet(text, p.lower())})
	excluded: List[Dict] = []
	scores = score_site(data, rules)
	for category, patterns in rules.exclude.items():
		for p, w in patterns:
			if p.lower() in text:
				excluded.append({"rule_type": "exclude", "category": category, "pattern": p,
				                 "where": _where_hit(data, p), "snippet": _snippet(text, p.lower())})

	matched.sort(key=lambda m: (-m["weight"], m["category"]))
	result = decide(scores, cfg)
	ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
	return {
		"domain": dom,
		"matched": matched,
		"excluded": excluded,
		"scores": [{"category": c, "score": s} for c, s in ranked],
		"min_score": cfg["min_score"],
		"margin": cfg["margin"],
		"result": result,
		"has_text": bool(text),
	}


def _where_hit(data: Dict, pattern: str) -> str:
	"""키워드가 제목·설명·본문 중 어디에 걸렸는지."""
	p = pattern.lower()
	for field, label in (("title", "제목"), ("description", "설명"), ("text_sample", "본문")):
		if p in (data.get(field) or "").lower():
			return label
	return "본문"


def preview_rule(conn: sqlite3.Connection, rule_type: str, pattern: str,
                 limit: int = 20) -> Dict:
	"""규칙을 저장하기 전에 몇 건이 매칭되는지 확인한다 (웹 /categories 미리보기)."""
	if rule_type in ("keyword", "exclude"):
		# 실제 분류와 같은 범위(title+description+본문)를 LIKE로 훑는다
		like = "%" + pattern.replace("%", r"\%").replace("_", r"\_") + "%"
		base = """
			FROM sites s JOIN korean k ON k.domain = s.domain AND k.is_korean = 1
			WHERE (s.title LIKE ? ESCAPE '\\' OR s.description LIKE ? ESCAPE '\\'
			       OR s.text_sample LIKE ? ESCAPE '\\')
		"""
		count = conn.execute("SELECT COUNT(*) " + base, (like, like, like)).fetchone()[0]
		rows = conn.execute(
			"SELECT s.domain, s.title " + base + " ORDER BY s.rank LIMIT ?",
			(like, like, like, limit),
		).fetchall()
		return {"count": count, "samples": [{"domain": r["domain"], "title": r["title"]} for r in rows]}

	# domain 규칙은 토큰 매칭이라 SQL로 표현할 수 없어 파이썬에서 훑는다
	rows = conn.execute(
		"""SELECT s.domain, s.title FROM sites s
		   JOIN korean k ON k.domain = s.domain AND k.is_korean = 1
		   ORDER BY s.rank"""
	).fetchall()

	matched: List[Dict] = []
	count = 0
	for r in rows:
		if domain_matches(r["domain"], pattern):
			count += 1
			if len(matched) < limit:
				matched.append({"domain": r["domain"], "title": r["title"]})
	return {"count": count, "samples": matched}
