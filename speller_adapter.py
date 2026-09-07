"""본문 맞춤법 검사를 부산대학교 인공지능연구실과 (주)나라인포테크가 공동 개발한
'바른 한글' 맞춤법 검사기에 맡기는 선택적 어댑터.

원 서비스(speller.cs.pusan.ac.kr)는 여러 미러로 옮겨 다녀왔고, 현재는
https://hleecaster.github.io/posts/speller_cs_pusan_ac_kr/ 에 정리된 방식대로
나라의말씀 미러(nara-speller.co.kr)에 폼 데이터를 보내고, 응답 HTML에 박힌
`data = [...]` 스크립트 변수를 파싱해 오류 목록을 얻는다. 자체 미러를 쓰려면
`SPELLER_API_URL` 환경변수로 주소를 바꿀 수 있다.

호출이 실패·타임아웃하거나 응답이 예상 형식과 다르면 `check_text`는 예외를 던지지
않고 None을 반환한다 — 호출한 쪽(`curriculum_audit.py`)은 이 경우 기존 로컬 규칙
검사 결과만 사용한다. 첫 호출이 실패하면(서비스 다운·차단 등) 같은 프로세스
안에서는 이후 호출을 곧바로 건너뛰어, 문장마다 타임아웃을 반복해 분석이 느려지는 것을
막는다.
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

SPELLER_API_URL = os.environ.get("SPELLER_API_URL", "https://nara-speller.co.kr/old_speller/results")
REQUEST_TIMEOUT_SECONDS = 5
MAX_WORD_SEGMENTS = 290  # 검사기의 300어절 처리 한도에 여유를 둔 값
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _clean_description(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(raw))
    return re.sub(r"\s+", " ", text).strip()


class _SpellerAdapter:
    def __init__(self) -> None:
        self._unavailable = False

    def check_text(self, text: str) -> list[dict[str, Any]] | None:
        if self._unavailable or not text or not text.strip():
            return None
        if len(text.split()) > MAX_WORD_SEGMENTS:
            return None
        try:
            body = urllib.parse.urlencode({"text1": text}).encode("utf-8")
            request = urllib.request.Request(
                SPELLER_API_URL, data=body, method="POST",
                headers={
                    "content-type": "application/x-www-form-urlencoded",
                    "user-agent": USER_AGENT,
                },
            )
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                response_html = response.read().decode("utf-8", errors="replace")
            payload = response_html.split("data = [", 1)[-1].split("];", 1)[0]
            data = json.loads(payload)
        except (urllib.error.URLError, TimeoutError, ValueError, UnicodeDecodeError, IndexError):
            self._unavailable = True
            return None
        except Exception:
            self._unavailable = True
            return None
        err_info = data.get("errInfo") if isinstance(data, dict) else None
        if not isinstance(err_info, list):
            return None
        result = []
        for item in err_info:
            if not isinstance(item, dict):
                continue
            start, end = item.get("start"), item.get("end")
            if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start <= end <= len(text):
                continue
            candidates = [
                html.unescape(c) for c in str(item.get("candWord", "")).split("|") if c
            ]
            result.append({
                "start": start, "end": end,
                "text": html.unescape(item.get("orgStr", "")),
                "description": _clean_description(str(item.get("help", ""))),
                "candidates": candidates,
            })
        return result


adapter = _SpellerAdapter()
