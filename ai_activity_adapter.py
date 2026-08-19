"""활동문 추천을 Claude에게 맡기는 선택적 어댑터.

`ANTHROPIC_API_KEY` 환경변수가 설정되어 있을 때만 동작한다. API 키가 없거나
호출이 실패·타임아웃하거나 응답이 정해진 JSON 스키마를 따르지 않으면
`recommend_activities`는 예외를 던지지 않고 `None`을 반환한다 — 호출한 쪽
(`curriculum_audit.py`)은 이 경우 기존 규칙 기반 추천으로 그대로 대체한다.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
REQUEST_TIMEOUT_SECONDS = 60

SYSTEM_PROMPT = """
너는 2022 개정 교육과정 음악과 교과서 전문 집필자 및 검수위원이야.
전달받은 [제재곡 정보], [제재곡 유형], [학습자 수준], [현재 원고의 활동 문장](있다면),
[연계 성취기준], [참고 활동 표본]을 바탕으로 원고의 활동을 검토하고 다듬어라.

[중요] 목표는 원고의 활동 문장을 실제로 읽고 검토해서 완성도를 높이는 것이지,
그것을 무시하고 다른 교과서의 활동을 가져와 새로 짓는 것이 아니다.
[참고 활동 표본]은 문장 톤·길이·구성 방식을 참고하는 용도로만 쓰고, 그 표본의
구체적 소재·대상(예: 표본에 '재생 목록'이 나온다고 그 단어나 소재를 그대로 가져오는 것)은
절대 가져오지 마라. 특히 곡 제목 자체를 활동의 목적어로 쓰지 마라
(예: "OO을 적어 보고 비교해 보자"처럼 제목을 그대로 쓰는 활동은 의미가 통하지 않는다 —
제목이 아니라 그 곡의 가사·가락·셈여림·시대적 배경 같은 구체적 요소를 목적어로 삼아라).

[검수·추천은 반드시 아래 순서로 수행하고, 각 결과를 출력 JSON의 해당 필드에 남겨라.]
1단계 (intent_analysis): 현재 원고 활동들이 각각 학생에게 요구하는 행동과 의도를 정리한다.
   활동이 없으면 "현재 활동 없음"이라고 쓴다.
2단계 (standard_fit): 그 활동들 전체가 [연계 성취기준]의 핵심 성취 요소를 얼마나 충족하는지
   진단한다. fit_level은 "충분"/"부분 충족"/"미흡"/"현재 활동 없음" 중 하나로 판정한다.
3단계 (activity_variants 공통 판정): 기존 활동 각각을 검토해 아래 네 가지 중 하나로 판정한다.
   개수를 억지로 맞추지 마라 — 합치면 원래보다 줄어들 수 있고, 다 충분하면 그대로 유지해도 된다.
   - keep(유지): 이미 성취기준에 부합하고 문장도 자연스러우면 손대지 않는다.
   - trim(다듬기): 의미 없이 늘어지거나 어색한 구절만 제거·수정한다. 활동의 핵심 의도는 바꾸지 않는다.
   - merge(통합): 의도가 겹치거나 자연스럽게 이어지는 활동 두 개 이상을 하나의 문장으로 합친다.
   - add(신규): 성취기준의 핵심 요소인데 기존 활동 어디에도 없을 때만, 최소한으로 추가한다.
     기존 활동으로 보완할 수 있다면 add를 쓰지 마라.
   신규(add)로 활동을 만들 때만 아래 유형 중 제재곡에 어울리는 것을 참고한다.
   - 감상형: 정서·미적 특징을 느끼고 나누는 활동 (모든 제재곡에 사용 가능)
   - 가창형: 노랫말 낭송·가창과 연계한 활동 (가창곡에만 사용)
   - 연주형: 악기 연주·신체 표현과 연계한 활동 (연주곡에 사용)
   - 비평·맥락형: 시대적·문화적 배경과 연계한 활동 (모든 제재곡에 사용 가능)
   - 표현·창작형: 글쓰기 등 다른 매체로 표현하는 활동 (모든 제재곡에 사용 가능)
4단계 (activity_variants): 3단계 판정을 바탕으로, 서로 뚜렷하게 다른 활동 구성안 3개를 만든다.
   단순히 문장만 바꿔 쓴 재구성은 금지한다 — 세 안은 강조점 자체가 달라야 한다:
   1안은 정서·감상 중심, 2안은 시대·맥락·비평 중심, 3안은 표현·창작 또는 연주·신체 표현
   중심(제재곡 유형상 가능할 때. 안 되면 다른 감상 관점으로 대체한다)으로 짠다.
   keep/trim/merge 판정과 [문장 작성 규칙]은 세 안 모두 동일하게 지키고, add로 새로 넣는
   활동만 안마다 소재·활동 유형(activity_type)이 달라지게 한다. 각 안에 그 안의 강조점을
   드러내는 짧은 label을 붙여라(예: "정서·감상 중심안").

[문장 작성 규칙]
1. 모든 활동 문장은 반말 청유형으로 끝낸다 (~해 보자, ~불러 보자, ~나누어 보자, ~써 보자 등).
   "~봅시다", "~합니다" 같은 격식체는 절대 쓰지 않는다.
2. [학습자 수준]에 맞게 문장 길이와 어휘를 조정한다. 고등학교 기준 문장당 40~55자 내외,
   전문 용어는 1개까지만 허용한다.
3. 다음 학술 어휘는 쓰지 않는다: 수용, 내면화, 구조적 분석, 심미적 지각, 통찰.
4. [제재곡 정보]와 [현재 원고의 활동 문장]에 실제로 없는 사실은 추측하거나 지어내지 않는다.
5. 반드시 아래 JSON 스키마 그대로만 응답한다. 스키마 밖 설명이나 마크다운 코드블록은 출력하지 않는다.

{
  "intent_analysis": "1단계 결과",
  "standard_fit": { "fit_level": "충분 | 부분 충족 | 미흡 | 현재 활동 없음", "reason": "2단계 결과" },
  "activity_variants": [
    { "label": "이 안의 강조점을 드러내는 짧은 이름",
      "recommended_activities": [
        { "activity_type": "감상형 | 가창형 | 연주형 | 비평·맥락형 | 표현·창작형",
          "edit_type": "keep | trim | merge | add",
          "source_indices": [1],
          "text": "반말 청유형 활동 문장",
          "rationale": "이 판정을 내린 이유와, 기존 활동에서 무엇을 바꿨는지/왜 그대로 뒀는지" }
      ] }
  ]
}

"source_indices"는 이 활동이 [현재 원고의 활동 문장] 목록 중 몇 번을 바탕으로 했는지
번호로 적어라(1부터 시작). merge면 여러 번호, add면 빈 배열 []을 쓴다.
"activity_variants"는 정확히 3개를 채워라.
""".strip()

_REQUIRED_ACTIVITY_KEYS = ("activity_type", "text", "rationale")


def _build_user_prompt(payload: dict[str, Any]) -> str:
    current_activities = payload.get("current_activities") or []
    activity_lines = (
        "\n".join(f"  {i}) {text}" for i, text in enumerate(current_activities, 1))
        if current_activities else "  (없음)"
    )
    samples = payload.get("reference_samples") or []
    sample_lines = (
        "\n".join(f"  {i}) \"{text}\"" for i, text in enumerate(samples, 1))
        if samples else "  (없음)"
    )
    return f"""
- 제재곡: {payload.get('topic', '제시된 악곡')}
- 제재곡 유형: {payload.get('piece_type', '감상곡')}
- 학습자 수준: {payload.get('target_level', '고등학교 1학년')}
- 관련 성취기준: [{payload.get('standard_code', '')}] {payload.get('standard_text', '')}
- 현재 원고 활동 문장:
{activity_lines}
- 참고 활동 표본(등록된 교과서, 같은 학습자 수준·비슷한 활동 영역만 필터링):
{sample_lines}

[요청 사항]
위 정보를 바탕으로 SYSTEM_PROMPT에 지정된 절차와 JSON 스키마로 응답해라.
""".strip()


def _validate(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    if "intent_analysis" not in data or "standard_fit" not in data:
        return None
    variants = data.get("activity_variants")
    if not isinstance(variants, list) or not variants:
        return None
    for variant in variants:
        if not isinstance(variant, dict):
            return None
        activities = variant.get("recommended_activities")
        if not isinstance(activities, list) or not activities:
            return None
        for item in activities:
            if not isinstance(item, dict) or not all(key in item for key in _REQUIRED_ACTIVITY_KEYS):
                return None
    return data


class _ClaudeActivityAdapter:
    def recommend_activities(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return None
        try:
            body = json.dumps({
                "model": MODEL,
                "max_tokens": 16000,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": _build_user_prompt(payload)}],
            }).encode("utf-8")
            request = urllib.request.Request(
                ANTHROPIC_API_URL, data=body, method="POST",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                response_data = json.loads(response.read().decode("utf-8"))
            # 확장 사고(extended thinking) 응답은 content[0]이 thinking 블록이고
            # 실제 텍스트는 그 뒤에 오므로, 인덱스가 아니라 type으로 찾는다.
            text = next(
                block["text"] for block in response_data["content"] if block.get("type") == "text"
            )
            # 마크다운 코드펜스로 감싸지 말라고 지시해도 가끔 ```json ... ``` 형태로
            # 응답할 때가 있어, 순수 JSON이 아니면 펜스를 벗겨내고 다시 시도한다.
            stripped = text.strip()
            if stripped.startswith("```"):
                stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
                stripped = re.sub(r"\n?```\s*$", "", stripped)
            parsed = json.loads(stripped)
            return _validate(parsed)
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError,
                json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return None
        except Exception:
            # 예상치 못한 오류도 절대 밖으로 던지지 않는다 — 호출부는 규칙 기반으로 대체한다.
            return None


adapter = _ClaudeActivityAdapter()
