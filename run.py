#!/usr/bin/env python3
"""웹사이트 분류 파이프라인 CLI.

모든 단계는 중단/재개가 가능하다. 같은 명령을 다시 실행하면 아직 처리되지 않은
행부터 이어서 진행한다.

	python run.py init
	python run.py fetch --limit 50000
	python run.py detect-korean
	python run.py classify-rule
	python run.py llm-export
	python run.py llm-import llm/results/batch_0001.json
	python run.py export
	python run.py web
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import db  # noqa: E402


def cmd_init(args: argparse.Namespace) -> None:
	from src import seed
	seed.run_init()


def cmd_fetch(args: argparse.Namespace) -> None:
	from src import fetch
	fetch.run_fetch(limit=args.limit, concurrency=args.concurrency, mode="pending")


def cmd_retry(args: argparse.Namespace) -> None:
	from src import fetch
	fetch.run_fetch(limit=args.limit, concurrency=args.concurrency, mode="retry")


def cmd_detect_korean(args: argparse.Namespace) -> None:
	from src import korean
	korean.run_detect(force=args.force)


def cmd_compact(args: argparse.Namespace) -> None:
	from src import korean
	freed = korean.compact()
	print("본문 발췌 %d건 정리 후 VACUUM 완료" % freed)


def cmd_classify_rule(args: argparse.Namespace) -> None:
	from src import classify_rule
	classify_rule.run_classify(force=args.force)


def cmd_llm_export(args: argparse.Namespace) -> None:
	from src import llm_batch
	llm_batch.run_export(size=args.size, auto_prompt=not args.no_prompt)


def cmd_llm_import(args: argparse.Namespace) -> None:
	from src import llm_batch
	llm_batch.run_import(args.path)


def cmd_llm_auto(args: argparse.Namespace) -> None:
	from src import llm_auto

	if args.check:
		result = llm_auto.check_cli()
		if result["ok"]:
			print("claude CLI 정상 (%s)" % result["path"])
			print("  응답: %s" % result["reply"])
		else:
			print("claude CLI를 쓸 수 없습니다: %s" % result["reason"])
			print("  터미널에서 `claude` 를 실행해 로그인 상태를 확인하세요.")
		return

	try:
		summary = llm_auto.run_auto(rounds=args.rounds, size=args.size, timeout=args.timeout)
		print("완료: %d배치 %d건 반영 (%d초)" % (
			summary["batches"], summary["applied"], summary["elapsed"]))
	except llm_auto.AuthError as exc:
		print("claude CLI 로그인이 필요합니다: %s" % exc)
		print("  터미널에서 `claude` 를 실행해 로그인한 뒤 다시 시도하세요.")
		sys.exit(1)


def cmd_llm_status(args: argparse.Namespace) -> None:
	from src import llm_batch
	conn = db.connect()
	try:
		queued = conn.execute("SELECT COUNT(*) FROM llm_queue").fetchone()[0]
		done = conn.execute(
			"SELECT COUNT(*) FROM llm_batches WHERE status = 'imported'").fetchone()[0]
		print("반영 완료 배치: %d개 / 대기 중인 도메인: %d건" % (done, queued))
	finally:
		conn.close()
	llm_batch.pending_batches()


def cmd_mark_blocked(args: argparse.Namespace) -> None:
	from src import blocked
	r = blocked.mark_existing()
	print("국내 차단 사이트 정리")
	print("  차단 표시  : {:,}건".format(r["marked"]))
	print("  한국어 해제: {:,}건".format(r["korean_reverted"]))
	print("  분류 정리  : {:,}건".format(r["classification_cleared"]))


def cmd_export(args: argparse.Namespace) -> None:
	from src import export
	export.run_export(reviewed_only=args.reviewed_only)


def cmd_status(args: argparse.Namespace) -> None:
	conn = db.connect()
	try:
		db.init_schema(conn)
		fetch_counts = db.counts_by(conn, "sites", "fetch_status")
		total = sum(fetch_counts.values())
		if total == 0:
			print("아직 데이터가 없습니다. 먼저 `python run.py init` 을 실행하세요.")
			return

		done = total - fetch_counts.get("pending", 0)
		print("■ 크롤링   {:,}/{:,} ({:.1f}%)".format(done, total, done / total * 100))
		for key in ("ok", "failed", "dead", "blocked", "pending"):
			print("    {:<8} {:>10,}".format(key, fetch_counts.get(key, 0)))

		judged = conn.execute("SELECT COUNT(*) FROM korean").fetchone()[0]
		kr = conn.execute("SELECT COUNT(*) FROM korean WHERE is_korean = 1").fetchone()[0]
		print("■ 한국어   판정 {:,}건 중 한국어 {:,}건".format(judged, kr))

		methods = db.counts_by(conn, "classification", "method")
		print("■ 분류     규칙 {:,} / 세션 {:,} / 수동 {:,} / 미분류 {:,}".format(
			methods.get("rule", 0), methods.get("llm", 0),
			methods.get("manual", 0), methods.get("unclassified", 0)))

		rows = conn.execute(
			"""SELECT primary_category, COUNT(*) c FROM classification
			   WHERE primary_category IS NOT NULL AND primary_category != 'none'
			   GROUP BY primary_category ORDER BY c DESC"""
		).fetchall()
		if rows:
			print("■ 카테고리")
			for r in rows:
				print("    {:<12} {:>8,}".format(r["primary_category"], r["c"]))

		review = db.counts_by(conn, "review_status", "status")
		if review:
			pending = conn.execute(
				"""SELECT COUNT(*) FROM korean k
				   LEFT JOIN review_status rv ON rv.domain = k.domain
				   WHERE k.is_korean = 1 AND rv.domain IS NULL"""
			).fetchone()[0]
			print("■ 검수     완료 {:,} / 제외 {:,} / 미검수 {:,}".format(
				review.get("reviewed", 0), review.get("excluded", 0), pending))

		queued = conn.execute("SELECT COUNT(*) FROM llm_queue").fetchone()[0]
		if queued:
			print("■ LLM 대기 {:,}건 (llm-status 로 배치 확인)".format(queued))

		job = conn.execute(
			"SELECT * FROM job_runs ORDER BY id DESC LIMIT 1").fetchone()
		if job:
			print("■ 최근 작업 {} [{}] {:,}/{:,} — {}".format(
				job["job_name"], job["status"], job["processed"], job["total"],
				job["message"] or ""))
	finally:
		conn.close()


def cmd_web(args: argparse.Namespace) -> None:
	import uvicorn
	from web.app import app

	conn = db.connect()
	try:
		db.init_schema(conn)
		db.seed_settings(conn)
		db.finish_stale_jobs(conn)
	finally:
		conn.close()

	print("관리 UI → http://127.0.0.1:%d" % args.port)
	uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


def main() -> None:
	parser = argparse.ArgumentParser(
		prog="run.py", description="tranco 100만 사이트 한국어/카테고리 분류 파이프라인",
		formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__,
	)
	sub = parser.add_subparsers(dest="command", required=True)

	p = sub.add_parser("init", help="DB 생성 + CSV 적재 + 규칙 시드")
	p.set_defaults(func=cmd_init)

	p = sub.add_parser("fetch", help="크롤링 (중단/재개 가능)")
	p.add_argument("--limit", type=int, default=50000, help="이번 실행에서 처리할 건수 (0=전체)")
	p.add_argument("--concurrency", type=int, default=None, help="동시 요청 수 (기본: 설정값)")
	p.set_defaults(func=cmd_fetch)

	p = sub.add_parser("retry", help="실패한 도메인 재시도")
	p.add_argument("--limit", type=int, default=50000)
	p.add_argument("--concurrency", type=int, default=None)
	p.set_defaults(func=cmd_retry)

	p = sub.add_parser("detect-korean", help="한국어 판정 (네트워크 불필요)")
	p.add_argument("--force", action="store_true", help="이미 판정된 것도 다시 계산")
	p.set_defaults(func=cmd_detect_korean)

	p = sub.add_parser("compact", help="비한국어 사이트 본문 발췌 삭제 + VACUUM")
	p.set_defaults(func=cmd_compact)

	p = sub.add_parser("classify-rule", help="규칙 기반 분류 (네트워크 불필요)")
	p.add_argument("--force", action="store_true", help="이미 분류된 것도 다시 계산")
	p.set_defaults(func=cmd_classify_rule)

	p = sub.add_parser("llm-export", help="세션 판정용 배치 파일 생성")
	p.add_argument("--size", type=int, default=None, help="배치당 건수 (기본: 설정값)")
	p.add_argument("--no-prompt", action="store_true", help="안내 문구 출력 생략")
	p.set_defaults(func=cmd_llm_export)

	p = sub.add_parser("llm-import", help="세션이 작성한 결과 JSON 반영")
	p.add_argument("path", help="결과 파일 경로 (예: llm/results/batch_0001.json)")
	p.set_defaults(func=cmd_llm_import)

	p = sub.add_parser("llm-auto", help="배치 생성→claude CLI 판정→반영 을 자동 반복")
	p.add_argument("--rounds", type=int, default=1, help="반복할 배치 수 (0=대상이 없어질 때까지)")
	p.add_argument("--size", type=int, default=None, help="배치당 건수 (기본: 설정값)")
	p.add_argument("--timeout", type=int, default=600, help="배치 하나당 최대 대기 초")
	p.add_argument("--check", action="store_true", help="claude CLI가 쓸 수 있는지만 확인")
	p.set_defaults(func=cmd_llm_auto)

	p = sub.add_parser("llm-status", help="배치 진행 상황")
	p.set_defaults(func=cmd_llm_status)

	p = sub.add_parser("mark-blocked", help="국내 차단 안내 페이지가 수집된 사이트를 blocked 로 표시")
	p.set_defaults(func=cmd_mark_blocked)

	p = sub.add_parser("export", help="최종 CSV/요약 생성")
	p.add_argument("--reviewed-only", action="store_true",
	               help="검수완료로 표시한 사이트만 out/reviewed/ 에 생성")
	p.set_defaults(func=cmd_export)

	p = sub.add_parser("status", help="전체 진행 상황 요약")
	p.set_defaults(func=cmd_status)

	p = sub.add_parser("web", help="관리 웹 UI 실행")
	p.add_argument("--port", type=int, default=8000)
	p.set_defaults(func=cmd_web)

	args = parser.parse_args()
	args.func(args)


if __name__ == "__main__":
	try:
		main()
	except KeyboardInterrupt:
		print("\n중단되었습니다.")
		sys.exit(130)
