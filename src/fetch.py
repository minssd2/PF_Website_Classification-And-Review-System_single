"""비동기 크롤러 — 도메인 홈페이지를 받아 판정용 메타데이터를 수집한다.

중단/재개가 전제라 처리 결과를 200건마다 커밋하고, Ctrl+C를 받으면 진행 중인
요청만 마무리한 뒤 안전하게 빠져나온다. 다시 실행하면 pending 상태인 행부터 잇는다.
"""
from __future__ import annotations

import asyncio
import signal
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

import httpx

from . import blocked, db, extract

USER_AGENT = (
	"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
	"(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
	"User-Agent": USER_AGENT,
	"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
	# 다국어 사이트가 한국어 페이지를 내주도록 유도한다
	"Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
	"Accept-Encoding": "gzip, deflate",
}

COMMIT_EVERY = 200

# 재시도해도 소용없는 오류 — dead 로 확정하고 큐에서 뺀다
_DEAD_MARKERS = (
	"getaddrinfo failed",
	"name or service not known",
	"nodename nor servname",
	"no address associated",
	"temporary failure in name resolution",
)


class Result:
	__slots__ = ("domain", "status", "http_status", "final_url", "charset",
	             "title", "description", "html_lang", "og_locale", "text_sample", "error")

	def __init__(self, domain: str, status: str, **kw):
		self.domain = domain
		self.status = status
		self.http_status = kw.get("http_status")
		self.final_url = kw.get("final_url")
		self.charset = kw.get("charset")
		self.title = kw.get("title")
		self.description = kw.get("description")
		self.html_lang = kw.get("html_lang")
		self.og_locale = kw.get("og_locale")
		self.text_sample = kw.get("text_sample")
		self.error = kw.get("error")


def _classify_error(exc: Exception) -> Tuple[str, str]:
	"""예외를 (status, 메시지)로 바꾼다. status는 failed(재시도 가능) 또는 dead."""
	msg = "%s: %s" % (type(exc).__name__, str(exc)[:200])
	lowered = str(exc).lower()
	if isinstance(exc, httpx.ConnectError) and any(m in lowered for m in _DEAD_MARKERS):
		return "dead", msg
	if isinstance(exc, httpx.UnsupportedProtocol):
		return "dead", msg
	return "failed", msg


def _decode_and_parse(raw: bytes, content_type: str, sample_len: int) -> Tuple[Dict, Dict]:
	"""워커 스레드에서 도는 CPU 작업 묶음 (인코딩 판별 + HTML 파싱)."""
	decoded = extract.decode_body(raw, content_type)
	parsed = extract.parse(decoded["text"], sample_len)
	return decoded, parsed


_POOL: Optional[ThreadPoolExecutor] = None


def _parser_pool() -> ThreadPoolExecutor:
	"""파싱 전용 스레드풀.

	기본 executor를 쓰면 웹 UI의 재계산 작업(detect-korean 등)과 같은 풀을 나눠 쓰게 되어
	서로 밀어낸다. 크기를 작게 잡는 이유는 어차피 GIL 때문에 실제 병렬도가 낮고,
	스레드를 늘려봐야 전환 비용만 커지기 때문이다.
	"""
	global _POOL
	if _POOL is None:
		_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="parse")
	return _POOL


def shutdown_pool() -> None:
	global _POOL
	if _POOL is not None:
		_POOL.shutdown(wait=False)
		_POOL = None


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
	buf = bytearray()
	async for chunk in response.aiter_bytes():
		buf.extend(chunk)
		if len(buf) >= max_bytes:
			break
	return bytes(buf)


async def _fetch_one(
	client: httpx.AsyncClient, domain: str, cfg: Dict
) -> Result:
	last_exc: Optional[Exception] = None

	for scheme in ("https", "http"):
		url = "%s://%s" % (scheme, domain)
		try:
			async with client.stream("GET", url, headers=HEADERS) as resp:
				content_type = resp.headers.get("content-type", "")
				http_status = resp.status_code
				final_url = str(resp.url)

				# HTML이 아니면 본문을 읽지 않는다 (이미지/PDF/바이너리)
				if content_type and "html" not in content_type.lower() and "xml" not in content_type.lower():
					return Result(domain, "ok", http_status=http_status,
					              final_url=final_url, error="non-html: %s" % content_type[:60])

				raw = await _read_capped(resp, cfg["max_bytes"])

			# 디코딩·파싱은 CPU 작업이라 이벤트 루프에서 직접 돌리면 안 된다.
			# 같은 프로세스에서 웹 UI가 함께 뜰 수 있고, 그 경우 응답이 수 초씩 밀린다.
			loop = asyncio.get_running_loop()
			decoded, parsed = await loop.run_in_executor(
				_parser_pool(), _decode_and_parse, raw, content_type, cfg["text_sample_len"]
			)

			# 5xx는 일시 장애일 수 있어 재시도 대상으로 남긴다
			status = "failed" if http_status >= 500 else "ok"
			# 국내 차단 안내 페이지로 넘어간 경우 — 실제 사이트 내용이 아니므로 따로 표시
			if blocked.is_blocked(final_url, parsed.get("title")):
				status = "blocked"
			return Result(
				domain, status, http_status=http_status, final_url=final_url,
				charset=decoded["charset"], **parsed
			)

		except Exception as exc:  # noqa: BLE001 — 네트워크 오류 전반을 상태로 환원
			last_exc = exc
			# https 실패 시에만 http로 재시도. 타임아웃이면 http도 가망이 없어 중단
			if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout)):
				break
			continue

	status, msg = _classify_error(last_exc) if last_exc else ("failed", "unknown")
	return Result(domain, status, error=msg)


def _persist(conn: sqlite3.Connection, results: List[Result]) -> Tuple[int, int]:
	ok = sum(1 for r in results if r.status == "ok")
	fail = len(results) - ok
	conn.executemany(
		"""UPDATE sites SET
			fetch_status = ?, http_status = ?, final_url = ?, charset = ?,
			title = ?, description = ?, html_lang = ?, og_locale = ?,
			text_sample = ?, error = ?, fetched_at = ?,
			attempt_count = attempt_count + 1
		   WHERE domain = ?""",
		[
			(r.status, r.http_status, r.final_url, r.charset, r.title, r.description,
			 r.html_lang, r.og_locale, r.text_sample, r.error, db.now(), r.domain)
			for r in results
		],
	)
	conn.commit()
	return ok, fail


def _select_targets(conn: sqlite3.Connection, mode: str, limit: int, max_attempts: int) -> List[str]:
	if mode == "retry":
		sql = ("SELECT domain FROM sites WHERE fetch_status = 'failed' "
		       "AND attempt_count < ? ORDER BY rank")
		params: List = [max_attempts]
	else:
		sql = "SELECT domain FROM sites WHERE fetch_status = 'pending' ORDER BY rank"
		params = []
	if limit > 0:
		sql += " LIMIT ?"
		params.append(limit)
	return [r["domain"] for r in conn.execute(sql, params).fetchall()]


async def _run(
	conn: sqlite3.Connection,
	targets: List[str],
	cfg: Dict,
	job_id: int,
	stop: Optional[asyncio.Event] = None,
	install_signal: bool = True,
	quiet: bool = False,
) -> Dict[str, int]:
	total = len(targets)
	queue: asyncio.Queue = asyncio.Queue()
	for domain in targets:
		queue.put_nowait(domain)

	stop = stop or asyncio.Event()
	interrupts = {"n": 0}
	loop = asyncio.get_running_loop()

	def _on_sigint() -> None:
		interrupts["n"] += 1
		if interrupts["n"] == 1:
			stop.set()
			print("\n중단 요청 — 진행 중인 요청을 마무리하고 저장합니다. (한 번 더 누르면 강제 종료)")
		else:
			raise KeyboardInterrupt

	# 웹 UI에서 돌 때는 서버의 SIGINT를 가로채면 안 되므로 등록하지 않는다
	if install_signal:
		try:
			loop.add_signal_handler(signal.SIGINT, _on_sigint)
		except (NotImplementedError, RuntimeError):
			install_signal = False

	pending: List[Result] = []
	counters = {"processed": 0, "ok": 0, "fail": 0}
	started = time.time()

	def flush(force: bool = False) -> None:
		if not pending or (len(pending) < COMMIT_EVERY and not force):
			return
		ok, fail = _persist(conn, pending)
		counters["ok"] += ok
		counters["fail"] += fail
		counters["processed"] += len(pending)
		pending.clear()
		elapsed = max(time.time() - started, 0.001)
		rate = counters["processed"] / elapsed
		db.update_job(
			conn, job_id, processed=counters["processed"],
			ok_count=counters["ok"], fail_count=counters["fail"],
			message="%.1f건/초" % rate,
		)
		if quiet:
			return
		remain = total - counters["processed"]
		eta = remain / rate if rate > 0 else 0
		sys.stdout.write(
			"\r  {:,}/{:,}  성공 {:,}  실패 {:,}  {:.1f}건/초  남은 예상 {:.0f}분   ".format(
				counters["processed"], total, counters["ok"], counters["fail"], rate, eta / 60
			)
		)
		sys.stdout.flush()

	timeout = httpx.Timeout(
		connect=cfg["connect_timeout"], read=cfg["read_timeout"],
		write=cfg["read_timeout"], pool=cfg["connect_timeout"],
	)
	limits = httpx.Limits(
		max_connections=cfg["concurrency"] + 20,
		max_keepalive_connections=0,  # 도메인이 매번 달라 keepalive 이득이 없다
	)

	async with httpx.AsyncClient(
		timeout=timeout, limits=limits, follow_redirects=True,
		max_redirects=5, verify=False, http2=False,
	) as client:

		async def worker() -> None:
			while not stop.is_set():
				try:
					domain = queue.get_nowait()
				except asyncio.QueueEmpty:
					return
				try:
					result = await _fetch_one(client, domain, cfg)
				except Exception as exc:  # noqa: BLE001
					status, msg = _classify_error(exc)
					result = Result(domain, status, error=msg)
				pending.append(result)
				flush()
				queue.task_done()

		workers = [asyncio.create_task(worker()) for _ in range(cfg["concurrency"])]
		try:
			await asyncio.gather(*workers)
		finally:
			flush(force=True)

	if install_signal:
		try:
			loop.remove_signal_handler(signal.SIGINT)
		except (NotImplementedError, RuntimeError):
			pass

	status = "stopped" if stop.is_set() else "done"
	summary = "%d건 처리 (성공 %d / 실패 %d)" % (
		counters["processed"], counters["ok"], counters["fail"])
	db.update_job(conn, job_id, status=status, message=summary)
	if not quiet:
		print("\n%s: %s" % ("중단됨" if stop.is_set() else "완료", summary))
	return {
		"processed": counters["processed"], "ok": counters["ok"],
		"failed": counters["fail"], "stopped": stop.is_set(), "total": total,
	}


async def run_fetch_async(
	limit: int = 50000,
	concurrency: Optional[int] = None,
	mode: str = "pending",
	stop: Optional[asyncio.Event] = None,
	install_signal: bool = True,
	quiet: bool = False,
) -> Dict:
	"""크롤링 본체. CLI와 웹 UI가 같은 경로를 쓴다.

	웹에서 호출할 때는 stop 이벤트를 넘겨 중지 버튼과 연결하고, 서버의 SIGINT를
	가로채지 않도록 install_signal=False 로 둔다.
	"""
	conn = db.connect()
	try:
		db.init_schema(conn)
		settings = db.get_settings(conn)
		cfg = {
			"concurrency": concurrency or int(settings.get("fetch.concurrency", 150)),
			"connect_timeout": float(settings.get("fetch.connect_timeout", 5)),
			"read_timeout": float(settings.get("fetch.read_timeout", 8)),
			"max_bytes": int(settings.get("fetch.max_bytes", 204800)),
			"text_sample_len": int(settings.get("fetch.text_sample_len", 500)),
		}
		max_attempts = int(settings.get("fetch.max_attempts", 3))

		targets = _select_targets(conn, mode, limit, max_attempts)
		if not targets:
			message = "처리할 대상이 없습니다. (mode=%s)" % mode
			if not quiet:
				print(message)
			return {"processed": 0, "ok": 0, "failed": 0, "stopped": False,
			        "total": 0, "message": message}

		if not quiet:
			print("대상 {:,}건, 동시 {}개로 시작합니다. Ctrl+C로 언제든 중단 가능합니다.".format(
				len(targets), cfg["concurrency"]))
		job_id = db.start_job(conn, "fetch" if mode == "pending" else "retry", len(targets))
		return await _run(conn, targets, cfg, job_id, stop, install_signal, quiet)
	finally:
		conn.close()
		shutdown_pool()


def run_fetch(limit: int = 50000, concurrency: Optional[int] = None, mode: str = "pending") -> None:
	asyncio.run(run_fetch_async(limit, concurrency, mode))


async def fetch_one_domain(domain: str) -> Dict:
	"""도메인 하나만 즉시 수집한다 (검수 화면에서 사이트를 추가할 때 쓴다).

	job_runs 에 기록하지 않는다. 몇 초짜리 단건 작업이라 진행률을 볼 일이 없고,
	진행 중인 대량 크롤링의 표시를 밀어내면 오히려 헷갈린다.
	"""
	conn = db.connect()
	try:
		settings = db.get_settings(conn)
		cfg = {
			"connect_timeout": float(settings.get("fetch.connect_timeout", 5)),
			"read_timeout": float(settings.get("fetch.read_timeout", 8)),
			"max_bytes": int(settings.get("fetch.max_bytes", 204800)),
			"text_sample_len": int(settings.get("fetch.text_sample_len", 500)),
		}
		timeout = httpx.Timeout(
			connect=cfg["connect_timeout"], read=cfg["read_timeout"],
			write=cfg["read_timeout"], pool=cfg["connect_timeout"],
		)
		async with httpx.AsyncClient(
			timeout=timeout, follow_redirects=True, max_redirects=5,
			verify=False, http2=False,
		) as client:
			result = await _fetch_one(client, domain, cfg)
		_persist(conn, [result])
		return {"status": result.status, "http_status": result.http_status,
		        "title": result.title, "final_url": result.final_url,
		        "error": result.error}
	finally:
		conn.close()
