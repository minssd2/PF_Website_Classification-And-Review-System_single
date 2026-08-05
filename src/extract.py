"""HTML 파싱 — 인코딩 판별 후 title/description/lang/og:locale/본문 발췌를 뽑는다."""
from __future__ import annotations

import re
from typing import Dict, Optional

from selectolax.lexbor import LexborHTMLParser

_META_CHARSET = re.compile(
	rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.IGNORECASE
)
_WS = re.compile(r"\s+")

# 한국 사이트에서 흔한 EUC-KR 계열 별칭을 파이썬 코덱명으로 정규화
_CHARSET_ALIAS = {
	"euckr": "euc-kr",
	"euc_kr": "euc-kr",
	"ksc5601": "euc-kr",
	"ks_c_5601-1987": "cp949",
	"ms949": "cp949",
	"x-windows-949": "cp949",
	"utf8": "utf-8",
}

KOREAN_CHARSETS = ("euc-kr", "cp949", "ks_c_5601-1987", "johab")


def normalize_charset(name: Optional[str]) -> Optional[str]:
	if not name:
		return None
	key = name.strip().strip('"\'').lower()
	return _CHARSET_ALIAS.get(key.replace("-", "").replace("_", ""), _CHARSET_ALIAS.get(key, key))


def charset_from_header(content_type: Optional[str]) -> Optional[str]:
	if not content_type or "charset=" not in content_type.lower():
		return None
	part = content_type.lower().split("charset=", 1)[1]
	return normalize_charset(part.split(";")[0])


def charset_from_meta(raw: bytes) -> Optional[str]:
	m = _META_CHARSET.search(raw[:4096])
	if not m:
		return None
	try:
		return normalize_charset(m.group(1).decode("ascii", "ignore"))
	except Exception:
		return None


def decode_body(raw: bytes, content_type: Optional[str]) -> Dict[str, Optional[str]]:
	"""응답 바이트를 문자열로 디코딩하고, 사용한 charset을 함께 돌려준다."""
	charset = charset_from_header(content_type) or charset_from_meta(raw)

	if charset:
		try:
			return {"text": raw.decode(charset, "replace"), "charset": charset}
		except (LookupError, UnicodeDecodeError):
			pass

	# 선언이 없거나 잘못된 경우: UTF-8 우선, 실패하면 자동 감지
	try:
		return {"text": raw.decode("utf-8"), "charset": "utf-8"}
	except UnicodeDecodeError:
		pass

	try:
		from charset_normalizer import from_bytes

		# 앞부분만 보고 인코딩을 정한다. 200KB 전체를 분석하면 이 함수가
		# 크롤링에서 가장 비싼 CPU 작업이 되는데, 판별 정확도는 별로 오르지 않는다.
		best = from_bytes(raw[:32768]).best()
		if best is not None:
			charset = normalize_charset(best.encoding)
			return {"text": raw.decode(charset, "replace"), "charset": charset}
	except Exception:
		pass

	return {"text": raw.decode("utf-8", "replace"), "charset": None}


def _attr(node, name: str) -> Optional[str]:
	if node is None:
		return None
	value = node.attributes.get(name)
	return value.strip() if value else None


def parse(html: str, sample_len: int = 500) -> Dict[str, Optional[str]]:
	"""HTML에서 판정에 쓰는 필드만 추출한다."""
	result: Dict[str, Optional[str]] = {
		"title": None,
		"description": None,
		"html_lang": None,
		"og_locale": None,
		"text_sample": None,
	}
	if not html:
		return result

	try:
		tree = LexborHTMLParser(html)
	except Exception:
		return result

	if tree.root is not None:
		html_node = tree.css_first("html")
		result["html_lang"] = _attr(html_node, "lang")

	title_node = tree.css_first("title")
	if title_node is not None:
		result["title"] = _clean(title_node.text())

	for selector, key in (
		('meta[name="description"]', "description"),
		('meta[property="og:description"]', "description"),
		('meta[property="og:locale"]', "og_locale"),
		('meta[http-equiv="content-language"]', "html_lang"),
	):
		node = tree.css_first(selector)
		if node is not None and not result[key]:
			value = _attr(node, "content")
			if value:
				result[key] = _clean(value)

	# 본문: script/style/noscript 제거 후 텍스트
	for tag in ("script", "style", "noscript", "template", "svg"):
		for node in tree.css(tag):
			node.decompose()

	body = tree.css_first("body") or tree.root
	if body is not None:
		text = _clean(body.text(separator=" "))
		if text:
			result["text_sample"] = text[:sample_len]

	return result


def _clean(value: Optional[str]) -> Optional[str]:
	if not value:
		return None
	return _WS.sub(" ", value).strip() or None
