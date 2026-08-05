"""관리 웹 UI — 진행 상황 확인, 작업 실행, 기준 정보 수정, 사이트 검수.

로컬 전용(127.0.0.1)이며 인증이 없다. 크롤링을 포함한 모든 작업을 여기서 실행할 수
있고, 오래 걸리는 작업은 백그라운드 태스크로 돌린 뒤 job_runs로 진행률을 보여준다.
한 번에 하나만 실행한다 — 작업들이 같은 테이블을 쓰기 때문에 겹치면 서로 방해한다.
"""
from __future__ import annotations

import asyncio
import os
import sys
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import classify_rule, db, korean, llm_batch  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="웹사이트 분류 관리")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# CSS 를 고쳐도 브라우저가 예전 것을 쓰는 일이 있어 파일 수정 시각을 쿼리로 붙인다.
# 서버를 다시 띄우면 값이 바뀌므로 캐시가 자동으로 무효화된다.
try:
	ASSET_VERSION = str(int(os.path.getmtime(os.path.join(BASE_DIR, "static", "style.css"))))
except OSError:
	ASSET_VERSION = "0"
templates.env.globals["asset_version"] = ASSET_VERSION


@contextmanager
def db_conn():
	"""요청 하나가 쓰는 커넥션. 끝나면 확실히 닫는다."""
	c = db.connect()
	try:
		yield c
	finally:
		c.close()


# ---------------------------------------------------------------- 페이지

@app.get("/", response_class=HTMLResponse)
def page_dashboard(request: Request):
	return templates.TemplateResponse(
		"dashboard.html",
		{"request": request, "nav": "dashboard", "categories": db.CATEGORIES},
	)


@app.get("/categories", response_class=HTMLResponse)
def page_categories(request: Request):
	return templates.TemplateResponse(
		"categories.html",
		{"request": request, "nav": "categories", "categories": db.CATEGORIES},
	)


@app.get("/settings", response_class=HTMLResponse)
def page_settings(request: Request):
	return templates.TemplateResponse("settings.html", {"request": request, "nav": "settings"})


@app.get("/sites", response_class=HTMLResponse)
def page_sites(request: Request):
	return templates.TemplateResponse(
		"sites.html",
		{"request": request, "nav": "sites", "categories": db.CATEGORIES},
	)


# ---------------------------------------------------------------- 진행 상황

@app.get("/api/stats")
def api_stats():
	with db_conn() as c:
		fetch_counts = db.counts_by(c, "sites", "fetch_status")
		total = sum(fetch_counts.values())
		judged = c.execute("SELECT COUNT(*) FROM korean").fetchone()[0]
		korean_count = c.execute("SELECT COUNT(*) FROM korean WHERE is_korean=1").fetchone()[0]
		methods = db.counts_by(c, "classification", "method")

		cats = c.execute(
			"""SELECT primary_category AS cat, COUNT(*) AS c FROM classification
			   WHERE primary_category NOT IN ('none') AND primary_category IS NOT NULL
			   GROUP BY primary_category"""
		).fetchall()
		by_category = {r["cat"]: r["c"] for r in cats}

		jobs = [dict(r) for r in c.execute(
			"SELECT * FROM job_runs ORDER BY id DESC LIMIT 5").fetchall()]

		review_counts = db.counts_by(c, "review_status", "status")
		review = {
			"reviewed": review_counts.get("reviewed", 0),
			"excluded": review_counts.get("excluded", 0),
			# 미검수는 한국어로 판정된 사이트 기준으로 센다 (검수 대상이 그것뿐이다)
			"pending": c.execute(
				"""SELECT COUNT(*) FROM korean k
				   LEFT JOIN review_status rv ON rv.domain = k.domain
				   WHERE k.is_korean = 1 AND rv.domain IS NULL"""
			).fetchone()[0],
		}

		llm = {
			"queued": c.execute("SELECT COUNT(*) FROM llm_queue").fetchone()[0],
			"exported": c.execute("SELECT COUNT(*) FROM llm_batches").fetchone()[0],
			"imported": c.execute(
				"SELECT COUNT(*) FROM llm_batches WHERE status='imported'").fetchone()[0],
			"pending_batches": llm_batch.pending_batches(c),
		}

		return {
			"fetch": {
				"total": total,
				"ok": fetch_counts.get("ok", 0),
				"failed": fetch_counts.get("failed", 0),
				"dead": fetch_counts.get("dead", 0),
				"blocked": fetch_counts.get("blocked", 0),
				"pending": fetch_counts.get("pending", 0),
			},
			"korean": {"judged": judged, "korean": korean_count},
			"classification": {
				"rule": methods.get("rule", 0),
				"llm": methods.get("llm", 0),
				"manual": methods.get("manual", 0),
				"unclassified": methods.get("unclassified", 0),
			},
			"by_category": {c_: by_category.get(c_, 0) for c_ in db.CATEGORIES},
			"review": review,
			"jobs": jobs,
			"llm": llm,
			"current": _job_state(c),
		}


# ---------------------------------------------------------------- 설정

class SettingUpdate(BaseModel):
	values: Dict[str, str]


@app.get("/api/settings")
def api_get_settings():
	with db_conn() as c:
		rows = c.execute(
			"SELECT key, value, value_type, description FROM settings ORDER BY key").fetchall()
		return {"settings": [dict(r) for r in rows]}


@app.post("/api/settings")
def api_set_settings(payload: SettingUpdate):
	with db_conn() as c:
		known = {r["key"] for r in c.execute("SELECT key FROM settings").fetchall()}
		updated = 0
		for key, value in payload.values.items():
			if key not in known:
				continue
			c.execute("UPDATE settings SET value = ? WHERE key = ?", (str(value), key))
			updated += 1
		c.commit()
		return {"updated": updated}


# ---------------------------------------------------------------- 카테고리 규칙

class RuleIn(BaseModel):
	category: str
	rule_type: str
	pattern: str
	weight: float = 1.0


class RuleToggle(BaseModel):
	id: int
	enabled: Optional[bool] = None
	weight: Optional[float] = None


@app.get("/api/rules")
def api_rules(category: Optional[str] = None):
	with db_conn() as c:
		sql = ("SELECT id, category, rule_type, pattern, weight, enabled, updated_at "
		       "FROM category_rules")
		params: List[Any] = []
		if category:
			sql += " WHERE category = ?"
			params.append(category)
		sql += " ORDER BY category, rule_type, pattern"
		return {"rules": [dict(r) for r in c.execute(sql, params).fetchall()]}


@app.post("/api/rules")
def api_add_rule(rule: RuleIn):
	if rule.category not in db.CATEGORIES:
		raise HTTPException(400, "알 수 없는 카테고리입니다: %s" % rule.category)
	if rule.rule_type not in ("domain", "keyword", "exclude"):
		raise HTTPException(400, "rule_type은 domain/keyword/exclude 중 하나여야 합니다.")
	pattern = rule.pattern.strip()
	if not pattern:
		raise HTTPException(400, "패턴이 비어 있습니다.")

	with db_conn() as c:
		cur = c.execute(
			"""INSERT OR IGNORE INTO category_rules
			   (category, rule_type, pattern, weight, enabled, updated_at)
			   VALUES (?,?,?,?,1,?)""",
			(rule.category, rule.rule_type, pattern, rule.weight, db.now()),
		)
		c.commit()
		if cur.rowcount == 0:
			raise HTTPException(409, "이미 등록된 규칙입니다.")
		return {"id": cur.lastrowid}


@app.patch("/api/rules")
def api_update_rule(payload: RuleToggle):
	with db_conn() as c:
		fields: List[str] = []
		params: List[Any] = []
		if payload.enabled is not None:
			fields.append("enabled = ?")
			params.append(1 if payload.enabled else 0)
		if payload.weight is not None:
			fields.append("weight = ?")
			params.append(payload.weight)
		if not fields:
			raise HTTPException(400, "변경할 항목이 없습니다.")
		fields.append("updated_at = ?")
		params.extend([db.now(), payload.id])
		c.execute("UPDATE category_rules SET %s WHERE id = ?" % ", ".join(fields), params)
		c.commit()
		return {"ok": True}


@app.delete("/api/rules/{rule_id}")
def api_delete_rule(rule_id: int):
	with db_conn() as c:
		c.execute("DELETE FROM category_rules WHERE id = ?", (rule_id,))
		c.commit()
		return {"ok": True}


class PreviewIn(BaseModel):
	rule_type: str
	pattern: str


@app.post("/api/rules/preview")
def api_preview_rule(payload: PreviewIn):
	if not payload.pattern.strip():
		raise HTTPException(400, "패턴이 비어 있습니다.")
	with db_conn() as c:
		return classify_rule.preview_rule(c, payload.rule_type, payload.pattern.strip())


# ---------------------------------------------------------------- 사이트 조회

# 최종 URL이 원 도메인 그대로인지 판정하는 조건. www 차이는 같은 것으로 본다.
_SAME_HOST_SQL = """(
	s.final_url LIKE 'http://' || s.domain || '/%'
	OR s.final_url LIKE 'https://' || s.domain || '/%'
	OR s.final_url LIKE 'http://www.' || s.domain || '/%'
	OR s.final_url LIKE 'https://www.' || s.domain || '/%'
	OR s.final_url IN ('http://' || s.domain, 'https://' || s.domain,
	                   'http://www.' || s.domain, 'https://www.' || s.domain)
)"""


def _redirect_info(domain: str, final_url: Optional[str]) -> Optional[Dict[str, str]]:
	"""접속해 보니 다른 주소였는지 알려준다.

	검수할 때 중요한 정보다. googlevideo.com 이 google.com 으로 넘어가는 것처럼
	원 도메인과 무관한 페이지를 보고 판단하게 되는 경우가 실제로 46%나 된다.
	"""
	if not final_url:
		return None
	host = (urlparse(final_url).hostname or "").lower()
	if host.startswith("www."):
		host = host[4:]
	if not host or host == domain:
		return None
	if host.endswith("." + domain):
		return {"host": host, "kind": "sub"}       # m.example.com
	if domain.endswith("." + host):
		return {"host": host, "kind": "parent"}
	return {"host": host, "kind": "external"}      # 아예 다른 도메인

@app.get("/api/sites")
def api_sites(
	q: Optional[str] = None,
	korean: Optional[str] = None,
	category: Optional[str] = None,
	method: Optional[str] = None,
	review: Optional[str] = None,
	redirect: Optional[str] = None,
	blocked: Optional[str] = None,
	rank_max: Optional[int] = None,
	limit: int = Query(50, le=500),
	offset: int = 0,
):
	with db_conn() as c:
		where: List[str] = []
		params: List[Any] = []

		if q:
			where.append("(s.domain LIKE ? OR COALESCE(m.title, s.title) LIKE ?)")
			params.extend(["%" + q + "%", "%" + q + "%"])
		if korean == "1":
			where.append("k.is_korean = 1")
		elif korean == "0":
			where.append("(k.is_korean = 0 OR k.domain IS NULL)")
		if category:
			where.append("(COALESCE(m.categories, cl.categories) LIKE ?)")
			params.append('%"' + category + '"%')
		if method:
			# 검수로 박제된 라벨(source='review')은 원래 분류 방식으로 걸러야 한다.
			# 안 그러면 검수만 했다는 이유로 전부 '수동'이 되어 미분류 필터가 비어버린다.
			where.append("""COALESCE(
				CASE WHEN m.domain IS NOT NULL AND m.source != 'review' THEN 'manual' END,
				CASE WHEN m.source = 'review' THEN m.origin_method END,
				cl.method) = ?""")
			params.append(method)
		if review == "pending":
			where.append("rv.domain IS NULL")
		elif review in db.REVIEW_STATUSES:
			where.append("rv.status = ?")
			params.append(review)
		if redirect == "yes":
			where.append("(s.final_url IS NOT NULL AND NOT %s)" % _SAME_HOST_SQL)
		elif redirect == "no":
			where.append("(s.final_url IS NULL OR %s)" % _SAME_HOST_SQL)
		if blocked == "1":
			where.append("s.fetch_status = 'blocked'")
		elif blocked == "0":
			where.append("s.fetch_status != 'blocked'")
		if rank_max:
			where.append("s.rank <= ?")
			params.append(rank_max)

		clause = ("WHERE " + " AND ".join(where)) if where else ""
		base = """
			FROM sites s
			LEFT JOIN korean k ON k.domain = s.domain
			LEFT JOIN classification cl ON cl.domain = s.domain
			LEFT JOIN manual_labels m ON m.domain = s.domain
			LEFT JOIN review_status rv ON rv.domain = s.domain
			LEFT JOIN llm_queue q ON q.domain = s.domain
			%s
		""" % clause

		total = c.execute("SELECT COUNT(*) " + base, params).fetchone()[0]
		rows = c.execute(
			"""SELECT s.rank, s.domain, s.title AS crawled_title, s.description,
			          s.fetch_status, s.http_status, s.charset, s.html_lang, s.final_url,
			          k.is_korean, k.score AS korean_score, k.reasons AS korean_reasons,
			          cl.categories, cl.primary_category, cl.method, cl.confidence, cl.evidence,
			          m.categories AS manual_categories, m.is_korean AS manual_korean,
			          m.title AS manual_title, m.note,
			          m.source AS label_source, m.origin_method,
			          rv.status AS review_status, rv.reason AS review_reason,
			          rv.updated_at AS reviewed_at,
			          q.domain IS NOT NULL AS llm_requested, q.batch_id AS llm_batch_id
			   """ + base + " ORDER BY s.rank LIMIT ? OFFSET ?",
			params + [limit, offset],
		).fetchall()

		items = []
		for r in rows:
			item = dict(r)
			item["categories"] = db.json_loads(r["categories"], []) or []
			item["manual_categories"] = db.json_loads(r["manual_categories"], None)
			if item["manual_categories"] is not None:
				# 검수로 자동 박제한 건은 원래 분류 방식을 그대로 보여준다.
				# 검수했다는 이유만으로 전부 '수동'이 되면 분류방식 필터가 무의미해진다.
				item["method"] = ("manual" if r["label_source"] != "review"
				                  else (r["origin_method"] or "unclassified"))
				item["categories"] = item["manual_categories"]
			# 사람이 고친 제목이 있으면 그것을 보여준다
			item["title"] = r["manual_title"] or r["crawled_title"]
			# 검수할 때 그 시점 제목을 그대로 박제한 건은 '수정됨'이 아니다.
			# 저장된 제목이 수집한 제목과 실제로 다를 때만 배지를 단다.
			item["title_edited"] = bool(r["manual_title"]) and r["manual_title"] != r["crawled_title"]
			item["redirect"] = _redirect_info(r["domain"], r["final_url"])
			items.append(item)

		# 이동 주소가 이미 목록에 있는지 한 번에 확인한다 (행마다 조회하면 느리다)
		hosts = {i["redirect"]["host"] for i in items if i["redirect"]}
		if hosts:
			placeholders = ",".join("?" * len(hosts))
			known = {
				row[0] for row in c.execute(
					"SELECT domain FROM sites WHERE domain IN (%s)" % placeholders,
					list(hosts),
				).fetchall()
			}
			for i in items:
				if i["redirect"]:
					i["redirect"]["exists"] = i["redirect"]["host"] in known
		return {"total": total, "items": items}


class AddSiteIn(BaseModel):
	domain: str
	rank: Optional[int] = None
	source_domain: Optional[str] = None  # 어느 사이트의 이동 주소인지 (rank 상속용)


@app.post("/api/sites/add")
async def api_add_site(payload: AddSiteIn):
	"""리다이렉트 도착지를 목록에 추가하고 바로 수집·판정·분류까지 끝낸다.

	추가만 하면 크롤링 전이라 검수 목록에 나타나지 않는다. 단건이라 몇 초면 끝나므로
	한 번에 처리해서 버튼 한 번으로 결과를 볼 수 있게 한다.
	"""
	from src import fetch, korean

	domain = payload.domain.strip().lower().rstrip(".")
	if not domain or "/" in domain or " " in domain:
		raise HTTPException(400, "도메인 형식이 아닙니다: %s" % payload.domain)

	with db_conn() as c:
		if c.execute("SELECT 1 FROM sites WHERE domain = ?", (domain,)).fetchone():
			raise HTTPException(409, "이미 목록에 있는 도메인입니다: %s" % domain)

		rank = payload.rank
		if rank is None and payload.source_domain:
			row = c.execute(
				"SELECT rank FROM sites WHERE domain = ?", (payload.source_domain,)
			).fetchone()
			rank = row["rank"] if row else None

		c.execute(
			"INSERT INTO sites(rank, domain, fetch_status) VALUES (?,?,'pending')",
			(rank, domain),
		)
		c.commit()

	fetched = await fetch.fetch_one_domain(domain)

	with db_conn() as c:
		judged = korean.judge_one(c, domain)
		classified = classify_rule.classify_one(c, domain)

	return {
		"domain": domain, "rank": rank, "fetch": fetched,
		"korean": judged, "classification": classified,
	}


class LabelIn(BaseModel):
	domain: str
	categories: Optional[List[str]] = None
	is_korean: Optional[bool] = None
	title: Optional[str] = None
	note: Optional[str] = None


@app.post("/api/labels")
def api_set_label(payload: LabelIn):
	cats = payload.categories or []
	unknown = [x for x in cats if x not in db.CATEGORIES]
	if unknown:
		raise HTTPException(400, "알 수 없는 카테고리: %s" % unknown)

	with db_conn() as c:
		exists = c.execute(
			"SELECT 1 FROM sites WHERE domain = ?", (payload.domain,)).fetchone()
		if not exists:
			raise HTTPException(404, "등록되지 않은 도메인입니다.")

		title = (payload.title or "").strip() or None
		c.execute(
			"""INSERT INTO manual_labels
			     (domain, is_korean, categories, title, note, updated_at, source)
			   VALUES (?,?,?,?,?,?,'edit')
			   ON CONFLICT(domain) DO UPDATE SET
			     is_korean=excluded.is_korean, categories=excluded.categories,
			     title=excluded.title, note=excluded.note,
			     updated_at=excluded.updated_at, source='edit'""",
			(payload.domain,
			 None if payload.is_korean is None else int(payload.is_korean),
			 db.json_dumps(cats), title, payload.note, db.now()),
		)
		# 수동 라벨은 재분류가 덮어쓰지 않도록 classification에도 반영해 둔다
		c.execute(
			"""INSERT INTO classification
			   (domain, categories, primary_category, method, confidence, evidence, classified_at)
			   VALUES (?,?,?,'manual',1.0,'사람이 직접 지정',?)
			   ON CONFLICT(domain) DO UPDATE SET
			     categories=excluded.categories, primary_category=excluded.primary_category,
			     method='manual', confidence=1.0, evidence=excluded.evidence,
			     classified_at=excluded.classified_at""",
			(payload.domain, db.json_dumps(cats),
			 cats[0] if cats else "none", db.now()),
		)
		if payload.is_korean is not None:
			c.execute(
				"""INSERT INTO korean(domain, is_korean, score, reasons, judged_at)
				   VALUES (?,?,999,'manual',?)
				   ON CONFLICT(domain) DO UPDATE SET
				     is_korean=excluded.is_korean, score=999,
				     reasons='manual', judged_at=excluded.judged_at""",
				(payload.domain, int(payload.is_korean), db.now()),
			)
		c.commit()
		return {"ok": True}


@app.get("/api/explain/{domain}")
def api_explain(domain: str):
	"""이 사이트가 왜 이렇게 판정·분류됐는지 규칙 단위로 되짚는다.

	저장된 `evidence` 는 카테고리별 합계뿐이라 어떤 규칙이 걸렸는지 알 수 없다.
	현재 규칙으로 그때그때 다시 채점해 돌려준다.
	"""
	domain = domain.strip().lower()
	with db_conn() as c:
		rules = classify_rule.explain(c, domain)
		if rules is None:
			raise HTTPException(404, "등록되지 않은 도메인입니다.")

		stored = c.execute(
			"""SELECT cl.categories, cl.primary_category, cl.method, cl.confidence,
			          cl.evidence, cl.classified_at,
			          m.source AS label_source, m.origin_method, m.updated_at AS labeled_at,
			          rv.status AS review_status, rv.updated_at AS reviewed_at,
			          s.fetch_status, q.batch_id AS llm_batch_id
			   FROM sites s
			   LEFT JOIN classification cl ON cl.domain = s.domain
			   LEFT JOIN manual_labels m ON m.domain = s.domain
			   LEFT JOIN review_status rv ON rv.domain = s.domain
			   LEFT JOIN llm_queue q ON q.domain = s.domain
			   WHERE s.domain = ?""",
			(domain,),
		).fetchone()

		return {
			"domain": domain,
			"korean": korean.explain(c, domain),
			"rules": rules,
			"stored": {**dict(stored),
			           "categories": db.json_loads(stored["categories"], []) or []},
		}


class LlmRequestIn(BaseModel):
	domains: List[str]


@app.post("/api/llm/request")
def api_request_llm(payload: LlmRequestIn):
	"""검수 화면에서 고른 사이트를 LLM 판정 대기열에 넣는다.

	여기서는 대기열에 넣기만 한다. 실제 판정은 기존 흐름 그대로
	`배치 내보내기` → Claude Code 세션 → `반영` 을 거친다.
	"""
	if not payload.domains:
		return {"added": 0, "skipped": 0}
	with db_conn() as c:
		result = llm_batch.request_llm(c, [d.strip().lower() for d in payload.domains])
		result["queued_total"] = c.execute(
			"SELECT COUNT(*) FROM llm_queue WHERE batch_id IS NULL").fetchone()[0]
		return result


@app.delete("/api/llm/request/{domain}")
def api_unrequest_llm(domain: str):
	with db_conn() as c:
		removed = llm_batch.unrequest_llm(c, domain.strip().lower())
		if removed == 0:
			raise HTTPException(409, "이미 배치로 내보낸 요청이라 취소할 수 없습니다.")
		return {"ok": True}


class ReviewIn(BaseModel):
	domain: str
	status: Optional[str] = None  # reviewed | excluded, None이면 미검수로 되돌린다
	reason: Optional[str] = None


def _freeze_reviewed(c, domains: List[str]) -> int:
	"""검수한 사이트의 제목·카테고리·한국어 여부를 그 시점 값으로 박제한다.

	검수 결과가 나중에 LLM 판정이나 규칙 재분류, 재수집 때문에 바뀌는 사고가 있었다.
	보호 장치가 `method = 'manual'` 하나뿐인데 제외 버튼은 라벨을 만들지 않아
	그대로 뚫렸다. 검수 시점에 라벨을 만들어 두면 기존 보호 장치가 전부 적용된다.

	이미 사람이 수정 화면에서 고친 건(`source = 'edit'`)은 건드리지 않는다.
    제목만 비어 있으면 그 자리만 채운다 — 재수집돼도 화면 제목이 바뀌지 않게.
	"""
	frozen = 0
	stamp = db.now()
	for domain in domains:
		row = c.execute(
			"""SELECT s.title AS crawled_title, k.is_korean,
			          cl.categories, cl.method,
			          m.domain AS has_label, m.source, m.title AS manual_title
			   FROM sites s
			   LEFT JOIN korean k ON k.domain = s.domain
			   LEFT JOIN classification cl ON cl.domain = s.domain
			   LEFT JOIN manual_labels m ON m.domain = s.domain
			   WHERE s.domain = ?""",
			(domain,),
		).fetchone()
		if row is None:
			continue

		if row["has_label"] and row["source"] == "edit":
			# 사람이 고친 라벨은 그대로 두되, 제목을 안 고쳤으면 그 시점 제목을 채워 둔다
			if row["manual_title"] is None and row["crawled_title"]:
				c.execute(
					"UPDATE manual_labels SET title = ?, updated_at = ? WHERE domain = ?",
					(row["crawled_title"], stamp, domain),
				)
				frozen += 1
			continue
		if row["has_label"]:
			continue  # 이미 박제된 건

		cats = db.json_loads(row["categories"], []) or []
		c.execute(
			"""INSERT INTO manual_labels
			     (domain, is_korean, categories, title, note, updated_at, source, origin_method)
			   VALUES (?,?,?,?,NULL,?,'review',?)""",
			(domain, row["is_korean"], db.json_dumps(cats), row["crawled_title"],
			 stamp, row["method"] or "unclassified"),
		)
		# 기존 보호 장치(method='manual')를 그대로 타도록 분류에도 박아 둔다
		c.execute(
			"""INSERT INTO classification
			   (domain, categories, primary_category, method, confidence, evidence, classified_at)
			   VALUES (?,?,?,'manual',1.0,?,?)
			   ON CONFLICT(domain) DO UPDATE SET
			     method='manual', confidence=1.0,
			     evidence=excluded.evidence, classified_at=excluded.classified_at""",
			(domain, db.json_dumps(cats), cats[0] if cats else "none",
			 "검수 확정 (원래: %s)" % (row["method"] or "미분류"), stamp),
		)
		# 한국어 판정도 재판정에 밀리지 않게 고정한다
		if row["is_korean"] is not None:
			c.execute(
				"""INSERT INTO korean(domain, is_korean, score, reasons, judged_at)
				   VALUES (?,?,999,'manual',?)
				   ON CONFLICT(domain) DO UPDATE SET
				     is_korean=excluded.is_korean, score=999,
				     reasons='manual', judged_at=excluded.judged_at""",
				(domain, row["is_korean"], stamp),
			)
		frozen += 1
	return frozen


def _unfreeze_reviewed(c, domains: List[str]) -> None:
	"""검수를 해제하면 자동으로 박제한 것만 되돌린다 (사람이 고친 라벨은 남긴다)."""
	for domain in domains:
		row = c.execute(
			"SELECT source FROM manual_labels WHERE domain = ?", (domain,)).fetchone()
		if row is None or row["source"] != "review":
			continue
		c.execute("DELETE FROM manual_labels WHERE domain = ?", (domain,))
		c.execute("DELETE FROM classification WHERE domain = ? AND method = 'manual'", (domain,))
		c.execute("DELETE FROM korean WHERE domain = ? AND reasons = 'manual'", (domain,))


@app.post("/api/review")
def api_set_review(payload: ReviewIn):
	if payload.status is not None and payload.status not in db.REVIEW_STATUSES:
		raise HTTPException(400, "status는 %s 중 하나이거나 비어 있어야 합니다."
		                    % ", ".join(db.REVIEW_STATUSES))
	with db_conn() as c:
		if not c.execute("SELECT 1 FROM sites WHERE domain = ?", (payload.domain,)).fetchone():
			raise HTTPException(404, "등록되지 않은 도메인입니다.")

		if payload.status is None:
			c.execute("DELETE FROM review_status WHERE domain = ?", (payload.domain,))
			_unfreeze_reviewed(c, [payload.domain])
		else:
			c.execute(
				"""INSERT INTO review_status(domain, status, reason, updated_at)
				   VALUES (?,?,?,?)
				   ON CONFLICT(domain) DO UPDATE SET
				     status=excluded.status, reason=excluded.reason,
				     updated_at=excluded.updated_at""",
				(payload.domain, payload.status, payload.reason, db.now()),
			)
			_freeze_reviewed(c, [payload.domain])
		c.commit()
		return {"ok": True, "status": payload.status}


class ReviewBulkIn(BaseModel):
	domains: List[str]
	status: Optional[str] = None


@app.post("/api/review/bulk")
def api_set_review_bulk(payload: ReviewBulkIn):
	"""현재 목록을 한 번에 처리한다 (검수 화면의 '이 페이지 전체' 버튼)."""
	if payload.status is not None and payload.status not in db.REVIEW_STATUSES:
		raise HTTPException(400, "알 수 없는 status 입니다.")
	if not payload.domains:
		return {"updated": 0}

	with db_conn() as c:
		if payload.status is None:
			c.executemany("DELETE FROM review_status WHERE domain = ?",
			              [(d,) for d in payload.domains])
			_unfreeze_reviewed(c, payload.domains)
		else:
			stamp = db.now()
			c.executemany(
				"""INSERT INTO review_status(domain, status, reason, updated_at)
				   VALUES (?,?,NULL,?)
				   ON CONFLICT(domain) DO UPDATE SET
				     status=excluded.status, updated_at=excluded.updated_at""",
				[(d, payload.status, stamp) for d in payload.domains],
			)
			_freeze_reviewed(c, payload.domains)
		c.commit()
		return {"updated": len(payload.domains), "status": payload.status}


@app.delete("/api/labels/{domain}")
def api_delete_label(domain: str):
	with db_conn() as c:
		c.execute("DELETE FROM manual_labels WHERE domain = ?", (domain,))
		c.execute("DELETE FROM classification WHERE domain = ? AND method = 'manual'", (domain,))
		# 사람이 지정한 한국어 여부도 함께 지운다. 남겨두면 재판정해도
		# score=999 인 manual 기록이 계속 이겨서 원래 판정으로 돌아가지 않는다.
		c.execute("DELETE FROM korean WHERE domain = ? AND reasons = 'manual'", (domain,))
		c.commit()
		return {"ok": True}


# ---------------------------------------------------------------- 백그라운드 작업

# 한 번에 하나만 돌린다. 작업들이 같은 테이블을 쓰기 때문에 겹치면 서로 방해한다.
_current: Dict[str, Any] = {
	"name": None, "label": None, "started_at": None, "finished_at": None,
	"stop": None, "task": None, "result": None, "error": None, "stoppable": False,
}

JOB_LABELS = {
	"fetch": "크롤링",
	"retry": "실패분 재시도",
	"detect-korean": "한국어 판정",
	"classify-rule": "규칙 분류",
	"compact": "본문 정리",
	"export": "산출물 생성",
	"export-reviewed": "검수완료분 산출물 생성",
	"llm-auto": "LLM 자동 판정",
	"llm-import": "배치 결과 반영",
}


# job_runs 의 running 행이 이 시간 넘게 갱신되지 않으면 죽은 프로세스로 본다.
# 크롤러는 200건마다 갱신하므로 정상이면 수 초~수십 초 간격이다.
STALE_AFTER_SEC = 90


def _busy() -> bool:
	task = _current.get("task")
	return task is not None and not task.done()


def _external_job(c=None) -> Optional[Dict]:
	"""이 웹 프로세스 밖(콘솔 등)에서 돌고 있는 작업.

	진행 상황은 job_runs 에 남으므로, 어디서 실행했든 화면에 똑같이 보여야 한다.
	커넥션을 받으면 그것을 쓰고, 없으면 잠깐 열었다 닫는다.
	"""
	if c is None:
		with db_conn() as own:
			return _external_job(own)

	row = c.execute(
		"SELECT * FROM job_runs WHERE status = 'running' ORDER BY id DESC LIMIT 1"
	).fetchone()
	if row is None:
		return None
	try:
		updated = datetime.strptime(row["updated_at"], "%Y-%m-%d %H:%M:%S")
	except (TypeError, ValueError):
		return None
	if (datetime.now() - updated).total_seconds() > STALE_AFTER_SEC:
		return None  # 강제 종료돼 running 으로 남은 찌꺼기
	return dict(row)


async def _wrap(name: str, factory, stop: asyncio.Event) -> None:
	try:
		_current["result"] = await factory(stop)
		_current["error"] = None
	except asyncio.CancelledError:
		_current["error"] = "취소되었습니다."
		raise
	except Exception as exc:  # noqa: BLE001 — 웹에 그대로 보여주기 위해 문자열로 환원
		_current["error"] = "%s: %s" % (type(exc).__name__, exc)
		_current["result"] = None
	finally:
		_current["finished_at"] = db.now()


def _start(name: str, factory, stoppable: bool = False) -> Dict:
	"""백그라운드 작업을 시작하고 즉시 돌아온다. 진행률은 job_runs로 본다.

	`asyncio.create_task` 를 쓰므로 **반드시 async 라우트에서 호출해야 한다.**
	동기(`def`) 라우트는 FastAPI가 스레드풀에서 실행하기 때문에 실행 중인 이벤트
	루프가 없어 RuntimeError가 난다.
	"""
	if _busy():
		raise HTTPException(409, "%s 작업이 실행 중입니다. 끝난 뒤 다시 시도하세요."
		                    % JOB_LABELS.get(_current["name"], _current["name"]))
	ext = _external_job()
	if ext:
		raise HTTPException(409, "콘솔에서 %s 작업이 실행 중입니다 (%s/%s). 끝난 뒤 다시 시도하세요."
		                    % (JOB_LABELS.get(ext["job_name"], ext["job_name"]),
		                       ext["processed"], ext["total"]))
	stop = asyncio.Event()
	_current.update({
		"name": name, "label": JOB_LABELS.get(name, name), "started_at": db.now(),
		"finished_at": None, "stop": stop, "result": None, "error": None,
		"stoppable": stoppable,
	})
	_current["task"] = asyncio.create_task(_wrap(name, factory, stop))
	return {"started": name, "label": _current["label"]}


def _in_executor(func, *args):
	"""동기 함수를 백그라운드 작업으로 감싼다 (중지는 지원하지 않음)."""
	async def factory(stop: asyncio.Event):
		loop = asyncio.get_running_loop()
		return await loop.run_in_executor(None, lambda: func(*args))
	return factory


def _job_state(c=None) -> Dict:
	state = {
		"name": _current["name"],
		"label": _current["label"],
		"running": _busy(),
		"stoppable": _current["stoppable"],
		"external": False,
		"started_at": _current["started_at"],
		"finished_at": _current["finished_at"],
		"result": _current["result"],
		"error": _current["error"],
	}
	if state["running"]:
		return state

	# 이 프로세스가 한가하면 콘솔에서 도는 작업이 있는지 본다
	ext = _external_job(c)
	if ext:
		state.update({
			"name": ext["job_name"],
			"label": "%s (콘솔)" % JOB_LABELS.get(ext["job_name"], ext["job_name"]),
			"running": True,
			"stoppable": False,  # 다른 프로세스라 웹에서 세울 수 없다
			"external": True,
			"started_at": ext["started_at"],
			"finished_at": None,
			"result": None,
			"error": None,
		})
	return state


@app.post("/api/jobs/fetch")
async def api_job_fetch(
	mode: str = "pending",
	limit: int = 50000,
	concurrency: Optional[int] = None,
):
	from src import fetch

	if mode not in ("pending", "retry"):
		raise HTTPException(400, "mode는 pending 또는 retry 여야 합니다.")

	async def factory(stop: asyncio.Event):
		return await fetch.run_fetch_async(
			limit=limit, concurrency=concurrency, mode=mode,
			stop=stop, install_signal=False, quiet=True,
		)

	return _start("fetch" if mode == "pending" else "retry", factory, stoppable=True)


@app.post("/api/jobs/stop")
async def api_job_stop():
	if not _busy():
		return {"ok": True, "message": "실행 중인 작업이 없습니다."}
	if not _current["stoppable"]:
		raise HTTPException(400, "%s 작업은 중지할 수 없습니다. 곧 끝납니다."
		                    % (_current["label"] or ""))
	_current["stop"].set()
	return {"ok": True, "message": "중지 요청됨 — 진행 중인 요청을 마무리하고 저장합니다."}


@app.post("/api/jobs/detect-korean")
async def api_job_detect_korean(force: bool = False):
	from src import korean
	return _start("detect-korean", _in_executor(korean.run_detect, force))


@app.post("/api/jobs/classify-rule")
async def api_job_classify_rule(force: bool = False):
	return _start("classify-rule", _in_executor(classify_rule.run_classify, force))


@app.post("/api/jobs/compact")
async def api_job_compact():
	from src import korean
	return _start("compact", _in_executor(korean.compact))


@app.post("/api/jobs/export")
async def api_job_export(reviewed_only: bool = False):
	from src import export
	name = "export-reviewed" if reviewed_only else "export"
	return _start(name, _in_executor(export.run_export, reviewed_only))


@app.post("/api/jobs/llm-auto")
async def api_job_llm_auto(rounds: int = 1, size: Optional[int] = None):
	"""배치 생성 → claude CLI 판정 → 반영 을 자동 반복한다."""
	from src import llm_auto

	if not llm_auto.cli_available():
		raise HTTPException(400, "claude CLI를 찾을 수 없습니다. Claude Code 설치 후 로그인하세요.")

	async def factory(stop: asyncio.Event):
		loop = asyncio.get_running_loop()
		return await loop.run_in_executor(
			None,
			lambda: llm_auto.run_auto(rounds=rounds, size=size, quiet=True,
			                          stop_check=stop.is_set),
		)

	return _start("llm-auto", factory, stoppable=True)


@app.get("/api/llm/cli-status")
def api_llm_cli_status():
	from src import llm_auto
	return {"available": bool(llm_auto.cli_available())}


@app.post("/api/jobs/llm-export")
def api_job_llm_export(size: Optional[int] = None):
	"""파일만 만들고 즉시 끝나므로 백그라운드로 돌리지 않는다."""
	if _busy() or _external_job():
		raise HTTPException(409, "다른 작업이 실행 중입니다.")
	batch_id = llm_batch.run_export(size=size, auto_prompt=False)
	if not batch_id:
		return {"batch_id": None, "message": "내보낼 대상이 없습니다."}
	return {
		"batch_id": batch_id,
		"request_path": os.path.join("llm", "requests", "%s.md" % batch_id),
		"result_path": os.path.join("llm", "results", "%s.json" % batch_id),
	}


class ImportIn(BaseModel):
	result_path: str


@app.post("/api/jobs/llm-import")
async def api_job_llm_import(payload: ImportIn):
	path = payload.result_path
	full = path if os.path.isabs(path) else os.path.join(db.PROJECT_ROOT, path)
	if not os.path.exists(full):
		raise HTTPException(404, "결과 파일이 아직 없습니다: %s" % path)
	return _start("llm-import", _in_executor(llm_batch.run_import, full))


@app.get("/api/jobs/current")
def api_job_current():
	return _job_state()


@app.get("/api/jobs")
def api_jobs():
	with db_conn() as c:
		rows = c.execute("SELECT * FROM job_runs ORDER BY id DESC LIMIT 20").fetchall()
		return {"jobs": [dict(r) for r in rows]}


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
	return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
