"""국내에서 차단된 사이트 판별.

불법·유해 사이트에 접속하면 ISP가 방송통신심의위원회 안내 페이지로 돌려보낸다.
그러면 **차단 안내문이 수집되어** 실제 사이트와 무관한 한국어 페이지가 저장된다.
이걸 그냥 두면 성인·불법 사이트가 "한국어 서비스"로 판정되어 결과에 섞인다.

수집 자체는 실패가 아니므로 `fetch_status = 'blocked'` 로 따로 표시해
판정·분류·산출물에서 빼되, 목록은 남겨 나중에 확인할 수 있게 한다.
"""
from __future__ import annotations

import sqlite3
from typing import Dict, Optional
from urllib.parse import urlparse

from . import db

# 차단 안내를 띄우는 호스트. 도착 URL로 판정하는 편이 제목 문구보다 확실하다.
BLOCK_HOSTS = (
	"warning.or.kr",
	"kcopa.or.kr",
	"kcsc.or.kr",
)

# 도착 URL이 없을 때만 쓰는 보조 신호 (제목은 바뀔 수 있어 우선순위가 낮다)
BLOCK_TITLE_HINTS = (
	"불법·유해정보사이트에 대한 차단 안내",
	"불법ㆍ유해정보사이트에 대한 차단 안내",
)

# SQL에서 같은 판정을 하기 위한 조건. 테이블 별칭은 s 로 고정한다.
SQL_CONDITION = "(" + " OR ".join(
	"s.final_url LIKE '%%%s%%'" % h for h in BLOCK_HOSTS
) + " OR s.title IN (%s))" % ", ".join("'%s'" % t for t in BLOCK_TITLE_HINTS)


def is_blocked(final_url: Optional[str], title: Optional[str] = None) -> bool:
	if final_url:
		host = (urlparse(final_url).hostname or "").lower()
		if any(host == h or host.endswith("." + h) for h in BLOCK_HOSTS):
			return True
	return bool(title) and title.strip() in BLOCK_TITLE_HINTS


def mark_existing(conn: Optional[sqlite3.Connection] = None) -> Dict[str, int]:
	"""이미 수집된 데이터에서 차단 페이지를 찾아 표시한다.

	사람이 지정한 검수 상태·수동 라벨은 건드리지 않는다.
	한국어 판정은 되돌려야 검수 목록에서 빠지므로 `is_korean = 0` 으로 갱신한다
	(행을 지우지 않고 근거만 'blocked' 로 남긴다).
	"""
	own = conn is None
	conn = conn or db.connect()
	try:
		marked = conn.execute(
			"UPDATE sites SET fetch_status = 'blocked' "
			"WHERE fetch_status != 'blocked' AND " + SQL_CONDITION.replace("s.", "")
		).rowcount

		# 사람이 지정했거나 검수로 박제한 판정(reasons='manual')은 건드리지 않는다.
		# 여기서 뒤집으면 검수한 사이트가 목록에서 사라진다.
		unjudged = conn.execute(
			"""UPDATE korean SET is_korean = 0, score = 0, reasons = 'blocked', judged_at = ?
			   WHERE is_korean = 1 AND reasons != 'manual' AND domain IN (
			       SELECT domain FROM sites WHERE fetch_status = 'blocked')""",
			(db.now(),),
		).rowcount

		uncls = conn.execute(
			"""UPDATE classification
			   SET categories = '[]', primary_category = 'none', method = 'blocked',
			       confidence = 0, evidence = '국내 차단 사이트', classified_at = ?
			   WHERE method NOT IN ('manual', 'llm', 'blocked') AND domain IN (
			       SELECT domain FROM sites WHERE fetch_status = 'blocked')""",
			(db.now(),),
		).rowcount

		conn.commit()
		return {"marked": marked, "korean_reverted": unjudged, "classification_cleared": uncls}
	finally:
		if own:
			conn.close()
