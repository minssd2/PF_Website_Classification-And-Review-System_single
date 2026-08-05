"""한국어 서비스 판별 — 수집된 메타데이터만으로 점수를 매긴다.

네트워크를 쓰지 않으므로 가중치를 바꿔가며 몇 분 안에 몇 번이든 재실행할 수 있다.
가중치·임계값은 DB settings에서 읽으므로 웹 UI에서 고친 값이 그대로 반영된다.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from typing import Dict, List, Optional, Tuple

from . import db

HANGUL = re.compile(r"[가-힣]")
LETTERS = re.compile(r"[^\W\d_]", re.UNICODE)

KR_TLD_SUFFIXES = (".kr", ".한국")
KOREAN_CHARSETS = ("euc-kr", "cp949", "ks_c_5601-1987", "johab", "ksc5601")

BATCH = 5000


def hangul_ratio(text: Optional[str]) -> float:
	"""글자 중 한글 음절이 차지하는 비율. 숫자·기호는 분모에서 제외한다."""
	if not text:
		return 0.0
	letters = LETTERS.findall(text)
	if not letters:
		return 0.0
	hangul = sum(1 for ch in letters if "가" <= ch <= "힣")
	return hangul / len(letters)


def judge(row: Dict, cfg: Dict) -> Tuple[int, float, str]:
	"""(is_korean, score, reasons)를 돌려준다."""
	score = 0.0
	reasons: List[str] = []

	domain = (row.get("domain") or "").lower()
	if domain.endswith(KR_TLD_SUFFIXES):
		score += cfg["tld_kr"]
		reasons.append("tld_kr")

	lang = (row.get("html_lang") or "").lower()
	locale = (row.get("og_locale") or "").lower()
	if lang.startswith("ko") or locale.startswith("ko"):
		score += cfg["lang_ko"]
		reasons.append("lang_ko")

	charset = (row.get("charset") or "").lower()
	if charset in KOREAN_CHARSETS:
		score += cfg["charset_kr"]
		reasons.append("charset_kr")

	ratio = hangul_ratio(row.get("text_sample"))
	if ratio >= cfg["ratio_high"]:
		score += cfg["hangul_high"]
		reasons.append("hangul_%d%%" % round(ratio * 100))
	elif ratio >= cfg["ratio_low"]:
		score += cfg["hangul_low"]
		reasons.append("hangul_low_%d%%" % round(ratio * 100))

	meta = " ".join(filter(None, [row.get("title"), row.get("description")]))
	if meta and HANGUL.search(meta):
		score += cfg["meta_hangul"]
		reasons.append("meta_hangul")

	is_korean = 1 if score >= cfg["threshold"] else 0
	return is_korean, score, ",".join(reasons)


REASON_LABELS = {
	"tld_kr": (".kr 도메인", "tld_kr"),
	"lang_ko": ("html lang / og:locale 이 ko", "lang_ko"),
	"charset_kr": ("euc-kr 계열 charset", "charset_kr"),
	"meta_hangul": ("제목·설명에 한글", "meta_hangul"),
}


def explain(conn: sqlite3.Connection, domain: str) -> Optional[Dict]:
	"""한국어 판정 점수를 항목별로 되짚는다 (검수 화면의 판정 근거)."""
	row = conn.execute(
		"""SELECT s.domain, s.html_lang, s.og_locale, s.charset, s.title,
		          s.description, s.text_sample, k.is_korean, k.score, k.reasons
		   FROM sites s LEFT JOIN korean k ON k.domain = s.domain
		   WHERE s.domain = ?""",
		(domain,),
	).fetchone()
	if row is None:
		return None

	cfg = _load_cfg(conn)
	data = dict(row)
	_, live_score, live_reasons = judge(data, cfg)

	items = []
	for token in (live_reasons or "").split(","):
		if not token:
			continue
		if token.startswith("hangul_low_"):
			items.append({"label": "본문 한글 %s (%d%% 이상)" % (token[11:], round(cfg["ratio_low"] * 100)),
			              "token": token, "score": cfg["hangul_low"]})
		elif token.startswith("hangul_"):
			items.append({"label": "본문 한글 %s (%d%% 이상)" % (token[7:], round(cfg["ratio_high"] * 100)),
			              "token": token, "score": cfg["hangul_high"]})
		else:
			label, key = REASON_LABELS.get(token, (token, token))
			items.append({"label": label, "token": token, "score": cfg.get(key, 0)})

	return {
		"items": items,
		"score": live_score,
		"threshold": cfg["threshold"],
		"is_korean": bool(live_score >= cfg["threshold"]),
		"stored_is_korean": None if row["is_korean"] is None else bool(row["is_korean"]),
		"stored_score": row["score"],
		"stored_reasons": row["reasons"],
	}


def _load_cfg(conn: sqlite3.Connection) -> Dict:
	s = db.get_settings(conn)
	return {
		"threshold": float(s.get("korean.threshold", 100)),
		"tld_kr": float(s.get("korean.score.tld_kr", 100)),
		"lang_ko": float(s.get("korean.score.lang_ko", 80)),
		"hangul_high": float(s.get("korean.score.hangul_high", 80)),
		"charset_kr": float(s.get("korean.score.charset_kr", 60)),
		"meta_hangul": float(s.get("korean.score.meta_hangul", 40)),
		"hangul_low": float(s.get("korean.score.hangul_low", 30)),
		"ratio_high": float(s.get("korean.hangul_ratio_high", 0.10)),
		"ratio_low": float(s.get("korean.hangul_ratio_low", 0.03)),
	}


def _target_sql(force: bool) -> str:
	"""판정 대상은 크롤링 시도가 끝난 사이트뿐이다.

	.kr 도메인은 접속에 실패해도 TLD 점수만으로 한국어로 잡히지만, 아직 크롤링
	전(pending)인 것까지 판정해 버리면 나중에 본문을 받아도 재판정되지 않는다.
	"""
	# 'blocked' 는 뺀다. 국내 차단 안내 페이지가 수집된 것이라 내용이 한국어여도
	# 그 사이트가 한국어 서비스라는 뜻이 아니다.
	base = """
		SELECT s.domain, s.html_lang, s.og_locale, s.charset, s.title,
		       s.description, s.text_sample
		FROM sites s
		{join}
		WHERE s.fetch_status IN ('ok', 'failed', 'dead')
		  {extra}
		ORDER BY s.rank
	"""
	# 사람이 지정했거나 검수로 박제한 판정(reasons='manual')은 --force 여도 건드리지 않는다.
	# 재판정으로 is_korean 이 0이 되면 검수한 사이트가 목록에서 통째로 사라진다.
	if force:
		return base.format(
			join="LEFT JOIN korean k ON k.domain = s.domain",
			extra="AND (k.reasons IS NULL OR k.reasons != 'manual')",
		)
	return base.format(
		join="LEFT JOIN korean k ON k.domain = s.domain",
		extra="AND k.domain IS NULL",
	)


def run_detect(force: bool = False, progress_cb=None) -> Dict[str, int]:
	conn = db.connect()
	try:
		db.init_schema(conn)
		cfg = _load_cfg(conn)

		total = conn.execute(
			"SELECT COUNT(*) FROM (%s)" % _target_sql(force)
		).fetchone()[0]
		if total == 0:
			print("판정할 대상이 없습니다. (이미 판정 완료 — 다시 하려면 --force)")
			return {"total": 0, "korean": 0}

		job_id = db.start_job(conn, "detect-korean", total)
		print("한국어 판정 대상 {:,}건".format(total))

		# 판정 중 UPDATE가 커서에 영향을 주지 않도록 별도 커넥션으로 읽는다
		reader = db.connect(readonly=True)
		cursor = reader.execute(_target_sql(force))

		processed = 0
		korean_count = 0
		buf: List[Tuple] = []
		stamp = db.now()

		while True:
			rows = cursor.fetchmany(BATCH)
			if not rows:
				break
			for row in rows:
				data = dict(row)
				is_korean, score, reasons = judge(data, cfg)
				korean_count += is_korean
				buf.append((data["domain"], is_korean, score, reasons, stamp))
			conn.executemany(
				"""INSERT INTO korean(domain, is_korean, score, reasons, judged_at)
				   VALUES (?,?,?,?,?)
				   ON CONFLICT(domain) DO UPDATE SET
				     is_korean=excluded.is_korean, score=excluded.score,
				     reasons=excluded.reasons, judged_at=excluded.judged_at""",
				buf,
			)
			conn.commit()
			processed += len(buf)
			buf.clear()
			db.update_job(conn, job_id, processed=processed, ok_count=korean_count)
			if progress_cb:
				progress_cb(processed, total)
			sys.stdout.write("\r  {:,}/{:,}  한국어 {:,}건".format(processed, total, korean_count))
			sys.stdout.flush()

		reader.close()
		db.update_job(conn, job_id, status="done", processed=processed,
		              ok_count=korean_count, message="한국어 %d건" % korean_count)
		print("\n완료: {:,}건 판정, 한국어 {:,}건".format(processed, korean_count))
		return {"total": processed, "korean": korean_count}
	finally:
		conn.close()


def judge_one(conn: sqlite3.Connection, domain: str) -> Optional[Dict]:
	"""도메인 하나만 판정해 저장한다 (검수 화면에서 사이트를 추가할 때 쓴다)."""
	row = conn.execute(
		"""SELECT domain, html_lang, og_locale, charset, title, description, text_sample
		   FROM sites WHERE domain = ?""",
		(domain,),
	).fetchone()
	if row is None:
		return None

	cfg = _load_cfg(conn)
	is_korean, score, reasons = judge(dict(row), cfg)
	conn.execute(
		"""INSERT INTO korean(domain, is_korean, score, reasons, judged_at)
		   VALUES (?,?,?,?,?)
		   ON CONFLICT(domain) DO UPDATE SET
		     is_korean=excluded.is_korean, score=excluded.score,
		     reasons=excluded.reasons, judged_at=excluded.judged_at""",
		(domain, is_korean, score, reasons, db.now()),
	)
	conn.commit()
	return {"is_korean": bool(is_korean), "score": score, "reasons": reasons}


def compact(conn: Optional[sqlite3.Connection] = None) -> int:
	"""한국어가 아닌 사이트의 본문 발췌를 비워 DB 용량을 줄인다."""
	own = conn is None
	conn = conn or db.connect()
	try:
		cur = conn.execute(
			"""UPDATE sites SET text_sample = NULL
			   WHERE text_sample IS NOT NULL AND domain IN (
			       SELECT domain FROM korean WHERE is_korean = 0)"""
		)
		conn.commit()
		freed = cur.rowcount
		conn.execute("VACUUM")
		return freed
	finally:
		if own:
			conn.close()
