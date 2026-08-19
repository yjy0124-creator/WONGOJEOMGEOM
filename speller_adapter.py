"""본문 맞춤법 검사를 전문 맞춤법 검사기 API에 맡기는 선택적 어댑터.

speller-api(https://github.com/jhaemin/speller-api, 부산대 맞춤법 검사기 기반)의
공개 인스턴스를 기본으로 호출한다. 자체 호스팅한 인스턴스를 쓰려면
`SPELLER_API_URL` 환경변수로 주소를 바꿀 수 있다.

호출이 실패·타임아웃하거나 응답이 예상 형식과 다르면 `check_text`는 예외를 던지지
않고 None을 반환한다 — 호출한 쪽(`curriculum_audit.py`)은 이 경우 기존 로컬 규칙
검사 결과만 사용한다. 첫 호출이 실패하면(서비스 다운·네트워크 차단 등) 같은 프로세스
안에서는 이후 호출을 곧바로 건너뛰어, 문장마다 타임아웃을 반복해 분석이 느려지는 것을
막는다.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

SPELLER_API_URL = os.environ.get("SPELLER_API_URL", "https://speller.town")
REQUEST_TIMEOUT_SECONDS = 5


class _SpellerAdapter:
    def __init__(self) -> None:
        self._unavailable = False

    def check_text(self, text: str) -> list[dict[str, Any]] | None:
        if self._unavailable or not text or not text.strip():
            return None
        try:
            body = json.dumps({"text": text}).encode("utf-8")
            request = urllib.request.Request(
                SPELLER_API_URL, data=body, method="POST",
                headers={"content-type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                data = json.loads(response.read().decode("utf-8"))
            suggestions = data.get("suggestions")
            if not isinstance(suggestions, list):
                return None
            result = []
            for item in suggestions:
                if not isinstance(item, dict):
                    continue
                start, end = item.get("start"), item.get("end")
                if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start <= end <= len(text):
                    continue
                candidates = [c for c in item.get("candidates") or [] if isinstance(c, str)]
                result.append({
                    "start": start, "end": end,
                    "text": item.get("text", ""),
                    "description": item.get("description", ""),
                    "candidates": candidates,
                })
            return result
        except (urllib.error.URLError, TimeoutError, ValueError, UnicodeDecodeError):
            self._unavailable = True
            return None
        except Exception:
            self._unavailable = True
            return None


adapter = _SpellerAdapter()
