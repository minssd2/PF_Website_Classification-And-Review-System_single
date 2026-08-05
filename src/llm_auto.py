"""배치 자동 처리 — `claude` CLI(headless)를 불러 판정까지 한 번에 돌린다.

수동 흐름(배치 내보내기 → 세션에 붙여넣기 → 반영)을 그대로 자동화한 것이다.
배치 파일 내용을 그대로 표준입력으로 넘기므로 CLI에 파일 접근 권한을 주지 않아도 되고,
결과 JSON만 받아 기존 `llm-import` 경로로 반영한다.

전제: `claude` CLI가 설치되어 있고 로그인되어 있어야 한다.
    claude          # 한 번 실행해 로그인 상태 확인
    claude -p "안녕"  # headless 호출이 되는지 확인
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Dict, List, Optional

from . import db, llm_batch

# 배치 파일 뒤에 붙여 출력 형식을 못박는다. 파일에도 스키마가 적혀 있지만,
# headless 응답은 사람이 고칠 수 없으므로 한 번 더 분명히 지시한다.
OUTPUT_INSTRUCTION = """

---

# 출력 지시 (중요)

위 사이트들을 분류한 결과를 **JSON 배열만** 출력하세요.
설명·머리말·코드펜스 없이 `[` 로 시작해 `]` 로 끝나야 합니다.

[{"domain": "example.com", "categories": ["쇼핑"], "confidence": 0.9, "evidence": "판단 근거 한 줄"}]

- 위에 나열된 사이트를 **빠짐없이** 포함하세요. 순서는 그대로 두세요.
- 해당 카테고리가 없으면 `["none"]` 으로 두세요. 억지로 끼워맞추지 마세요.
- 카테고리 이름은 제시된 10개를 **정확히 그대로** 쓰세요.
"""

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)


class AuthError(RuntimeError):
	"""claude CLI 로그인이 안 된 상태."""


def cli_available() -> Optional[str]:
	return shutil.which("claude")


def check_cli(timeout: int = 60) -> Dict:
	"""CLI가 실제로 응답하는지 짧게 확인한다."""
	path = cli_available()
	if not path:
		return {"ok": False, "reason": "claude CLI를 찾을 수 없습니다."}
	try:
		proc = subprocess.run(
			["claude", "-p", "--output-format", "text"],
			input="ping 이라고만 답하세요.",
			capture_output=True, text=True, timeout=timeout,
		)
	except subprocess.TimeoutExpired:
		return {"ok": False, "reason": "CLI 응답이 %d초 안에 오지 않았습니다." % timeout}
	if proc.returncode != 0:
		return {"ok": False, "reason": (proc.stderr or proc.stdout).strip()[:300]}
	return {"ok": True, "path": path, "reply": proc.stdout.strip()[:80]}


def extract_json_array(text: str) -> List[Dict]:
	"""응답에서 JSON 배열을 꺼낸다. 코드펜스나 앞뒤 설명이 붙어도 견딘다."""
	text = (text or "").strip()
	if not text:
		raise ValueError("빈 응답")

	m = _JSON_BLOCK.search(text)
	if m:
		return json.loads(m.group(1))

	start = text.find("[")
	end = text.rfind("]")
	if start < 0 or end <= start:
		raise ValueError("JSON 배열을 찾지 못했습니다: %s" % text[:200])
	return json.loads(text[start:end + 1])


def _ask_claude(prompt: str, timeout: int) -> str:
	try:
		proc = subprocess.run(
			["claude", "-p", "--output-format", "text"],
			input=prompt, capture_output=True, text=True, timeout=timeout,
		)
	except subprocess.TimeoutExpired:
		raise RuntimeError("claude 호출이 %d초를 넘겨 중단했습니다." % timeout)

	if proc.returncode != 0:
		err = (proc.stderr or proc.stdout).strip()
		if "authenticate" in err.lower() or "oauth" in err.lower():
			raise AuthError(err[:300])
		raise RuntimeError("claude 호출 실패(코드 %d): %s" % (proc.returncode, err[:300]))
	return proc.stdout


def process_batch(batch_id: str, timeout: int = 600) -> Dict:
	"""배치 하나를 CLI로 판정하고 결과 파일을 쓴다."""
	request_path = os.path.join(llm_batch.REQUEST_DIR, "%s.md" % batch_id)
	result_path = os.path.join(llm_batch.RESULT_DIR, "%s.json" % batch_id)
	if not os.path.exists(request_path):
		raise FileNotFoundError("배치 파일이 없습니다: %s" % request_path)

	with open(request_path, "r", encoding="utf-8") as fp:
		prompt = fp.read() + OUTPUT_INSTRUCTION

	answer = _ask_claude(prompt, timeout)
	parsed = extract_json_array(answer)

	os.makedirs(llm_batch.RESULT_DIR, exist_ok=True)
	with open(result_path, "w", encoding="utf-8") as fp:
		json.dump(parsed, fp, ensure_ascii=False, indent=1)
	return {"batch_id": batch_id, "result_path": result_path, "count": len(parsed)}


def run_auto(rounds: int = 1, size: Optional[int] = None, timeout: int = 600,
             quiet: bool = False, stop_check=None) -> Dict:
	"""배치 생성 → CLI 판정 → 반영 을 rounds 번 반복한다 (rounds=0 이면 대상이 없어질 때까지)."""
	if not cli_available():
		raise RuntimeError(
			"claude CLI를 찾을 수 없습니다. Claude Code를 설치하고 로그인한 뒤 다시 시도하세요.")

	done = 0
	applied = 0
	started = time.time()
	conn = db.connect()
	job_id = db.start_job(conn, "llm-auto", rounds if rounds > 0 else 0)

	try:
		while rounds <= 0 or done < rounds:
			if stop_check is not None and stop_check():
				break

			batch_id = llm_batch.run_export(size=size, auto_prompt=False)
			if not batch_id:
				if not quiet:
					print("판정할 대상이 없습니다.")
				break

			if not quiet:
				print("[%d] %s 판정 중..." % (done + 1, batch_id), end="", flush=True)

			try:
				result = process_batch(batch_id, timeout)
			except Exception:
				# 판정에 실패하면 배치를 되돌린다. 그대로 두면 거기 실린 도메인이
				# "이미 나간 건"으로 묶여 다음 배치에서도 빠져 버린다.
				undo = llm_batch.cancel_batch(batch_id)
				if not quiet:
					print(" 실패 — 배치를 되돌렸습니다 (요청 복구 %d / 큐 해제 %d)"
					      % (undo["restored"], undo["dropped"]))
				raise

			imported = llm_batch.run_import(result["result_path"])
			applied += imported["applied"]
			done += 1

			if not quiet:
				print(" %d건 반영 (누적 %d건)" % (imported["applied"], applied))
			db.update_job(conn, job_id, processed=done, ok_count=applied,
			              message="%d배치 / %d건 반영" % (done, applied))

		elapsed = time.time() - started
		db.update_job(conn, job_id, status="done", processed=done, ok_count=applied,
		              message="%d배치 %d건 반영 (%.0f초)" % (done, applied, elapsed))
		return {"batches": done, "applied": applied, "elapsed": round(elapsed)}
	except Exception as exc:
		db.update_job(conn, job_id, status="error", message=str(exc)[:200])
		raise
	finally:
		conn.close()
