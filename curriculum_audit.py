"""원고의 필수 구성과 2022 개정 교육과정 연계 여부를 점검한다."""

from __future__ import annotations

import functools
import hashlib
import html
import importlib
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import speller_adapter


STANDARD_CODE = re.compile(r"\[(12감비\d{2}-\d{2})\]")
STOPWORDS = {
    "음악", "통해", "대한", "위해", "한다", "있다", "있는", "하며", "하고",
    "다양한", "학생", "학습", "활동", "관한", "따라", "대한", "것을", "에서",
}
GOAL_ACTIONS = ("설명", "비교", "분석", "표현", "연주", "부르", "감상", "이해", "파악", "활용", "비평", "토의", "발표", "작성")
STANDARD_SIGNATURES = {
    "12감비01-01": ("음악 요소", "악곡 구성", "특징", "비교", "분석", "설명", "음색", "선율", "장단", "악기"),
    "12감비01-02": ("시대", "지역", "문화", "공동체", "변화", "발전", "양상"),
    "12감비01-03": ("미적", "느낌", "수용", "공감", "표현", "감상", "연상", "분위기", "감정", "발표", "작성", "편지"),
    "12감비01-04": ("생활", "취향", "감상 경험", "공유", "존중"),
    "12감비02-01": ("사회", "문화", "시대적", "맥락", "관점", "비평"),
    "12감비02-02": ("사회", "문화", "산업", "역할", "필요성", "토의", "토론", "관련성"),
    "12감비02-03": ("생활", "가치", "영향력", "분야", "연계", "활용"),
    "12감비02-04": ("감상자", "비평자", "관점", "비평", "향유", "태도"),
}

# 교육과정 원문은 비교 근거로만 사용하며 수정·재작성하지 않는다.
CURRICULUM_TEXT_POLICY = "교육과정의 성취기준 문구는 수정하지 않고 원문 그대로 참조합니다."

MUSIC_EDITING_TERMS_SOURCE = "2022 개정 교육과정 교과서 편수자료 Ⅱ - 음악 일반·서양 음악 용어"


def _resource_path(name: str) -> Path:
    """PyInstaller로 묶었을 때(_MEIPASS)와 스크립트로 바로 실행할 때 모두 동작하는 리소스 경로."""
    base = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent)
    return base / name


@functools.lru_cache(maxsize=1)
def _official_music_terms() -> tuple[str, ...]:
    """교육부 편수자료(Ⅱ)의 음악 일반·서양 음악 용어집에서 뽑은 공식 표준 용어.

    PDF 표를 추출하는 과정에서 한자·외국어·비고 칸이 이따금 옆 칸과 섞여 신뢰도가
    낮아, 검사에는 정확도가 높은 '용어' 칼럼만 쓴다. 리소스를 못 찾으면(개발 환경
    설정 오류 등) 빈 목록을 반환해 이 검사만 조용히 건너뛴다.
    """
    try:
        raw = json.loads(_resource_path("music_editing_terms.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    terms = {str(item.get("term", "")).strip() for item in raw if item.get("term")}
    return tuple(sorted(terms, key=lambda value: (-len(value), value)))


KNOWN_WORK_TITLES = ("대취타", "취타", "만파정식지곡")

TARGET_LEVELS = {
    "초등학교 저학년": {"length_limit": 22, "max_steps": 1, "max_terms": 0},
    "초등학교 고학년": {"length_limit": 30, "max_steps": 2, "max_terms": 0},
    "중학교": {"length_limit": 38, "max_steps": 2, "max_terms": 1},
    "고등학교 1학년": {"length_limit": None, "max_steps": 3, "max_terms": 1},
    "고등학교 2·3학년": {"length_limit": None, "max_steps": 4, "max_terms": 2},
}


# 세로 A4 기준 포인트 크기(72dpi). 가로 방향 페이지가 '세로 A4 두 쪽이 이어붙은 스프레드'인지,
# 낱장 자체가 가로로 제작된 학습지인지 구분하는 기준으로 쓴다.
_A4_PORTRAIT_WIDTH_PT = 595.0
_A4_PORTRAIT_HEIGHT_PT = 842.0


def _is_two_page_spread(width: float, height: float) -> bool:
    """가로 방향이면서 너비가 세로 A4 한 쪽의 약 2배인 경우만 '두 쪽 이어붙임'으로 판단한다.

    가로 폭이 한 장 크기(예: 가로 A4 학습지)에 그치면 낱장 그대로 두고,
    한 쪽 너비의 2배에 가까울 때만 두 쪽으로 나눠 센다.
    """
    if width <= height:
        return False
    return width >= _A4_PORTRAIT_WIDTH_PT * 1.7 and height <= _A4_PORTRAIT_HEIGHT_PT * 1.15


def _fingerprint(section: str, *parts: str) -> str:
    """지적사항 하나를 안정적으로 식별하는 지문. 오탐 표시와 재분석 비교(diff)에 함께 쓴다."""
    normalized = "|".join(re.sub(r"\s+", "", str(part)) for part in (section, *parts))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_pages(path: Path) -> list[str]:
    try:
        import pdfplumber  # type: ignore
    except ImportError as exc:
        raise RuntimeError("pdfplumber가 필요합니다. requirements.txt를 설치하세요.") from exc
    pages = []
    with pdfplumber.open(str(path)) as document:
        for page in document.pages:
            # 다단 원고에서 서로 다른 열의 같은 높이 문장이 합쳐지지 않도록
            # PDF 내부의 텍스트 흐름(콘텐츠 스트림) 순서를 우선한다.
            if _is_two_page_spread(page.width, page.height):
                # 세로 A4 두 쪽이 나란히 이어붙어 가로로 올라온 원고이므로 좌우로 나눠 각각 한 쪽으로 셈한다.
                half = page.width / 2
                left = page.crop((0, 0, half, page.height))
                right = page.crop((half, 0, page.width, page.height))
                pages.append((left.extract_text(use_text_flow=True) or "").replace("\x00", " "))
                pages.append((right.extract_text(use_text_flow=True) or "").replace("\x00", " "))
            else:
                pages.append((page.extract_text(use_text_flow=True) or "").replace("\x00", " "))
    return pages


def _clean_text(value: str) -> str:
    value = value.replace("\u22c5", "·").replace("∙", "·").replace("ㆍ", "·")
    value = re.sub(r"(?<=[가-힣])\s*\n\s*(?=[가-힣])", " ", value)
    value = re.sub(r"[ \t]+", " ", value)
    return value.strip()


def _unique(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for item in items:
        key = re.sub(r"\s+", "", item["text"])
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def detect_manuscript_components(pages: list[str]) -> dict[str, Any]:
    explicit_standards: list[dict[str, Any]] = []
    learning_goals: list[dict[str, Any]] = []
    activities: list[dict[str, Any]] = []

    for page_no, raw in enumerate(pages, 1):
        text = _clean_text(raw)
        for match in STANDARD_CODE.finditer(text):
            start = match.start()
            excerpt = text[start:start + 260].split("\n", 1)[0].strip()
            explicit_standards.append({"page": page_no, "code": match.group(1), "text": excerpt})

        # 명시된 '학습 목표' 표제를 최우선으로 사용한다. 본문의 설명 문장도 흔히
        # '수 있다'로 끝나므로, 표제가 있으면 같은 페이지의 다른 후보는 목표로 보지 않는다.
        labeled_goals = list(re.finditer(
            r"학습\s*목표\s*[:：]\s*(.{8,260}?(?:수 있다|수 있도록 한다))\s*[.!?]", text
        ))
        if labeled_goals:
            for match in labeled_goals:
                candidate = re.sub(r"\s+", " ", match.group(1)).strip(" ·-–—")
                learning_goals.append({"page": page_no, "text": candidate + ".", "method": "학습 목표 표제 감지"})
        else:
            # 표제가 없는 원고는 페이지 상단의 학생 행동 문장만 제한적으로 허용한다.
            top_text = " ".join(line.strip() for line in raw.splitlines()[:6])
            top_text = _clean_text(top_text)
            for match in re.finditer(r"([^.!?]{8,220}?(?:수 있다|수 있도록 한다))\s*[.!?]", top_text):
                candidate = re.sub(r"\s+", " ", match.group(1)).strip(" ·-–—")
                if any(action in candidate for action in GOAL_ACTIONS):
                    learning_goals.append({"page": page_no, "text": candidate + ".", "method": "페이지 상단 행동 문장 감지"})

        # [활동 n] 표제부터 '보자'까지를 한 덩어리로 읽어 줄바꿈·다단 때문에
        # 끝부분만 활동으로 잘리는 문제를 막는다.
        captured_activity_keys: set[str] = set()
        for match in re.finditer(r"(?:\([^)]{1,12}\)\s*)?\[활동\s*(\d{1,2})\]\s*(.{6,360}?(?:해\s*보자|보자))\s*[.!?]?", text):
            number = int(match.group(1))
            body = re.sub(r"\s+", " ", match.group(2)).strip()
            body = body.replace("음 색", "음색")
            item_text = f"{number}. {body}"
            activities.append({"page": page_no, "number": number, "text": item_text, "method": "활동 표제 블록 감지"})
            captured_activity_keys.add(re.sub(r"\s+", "", body))

        # 표제가 없는 활동은 번호 또는 학생 행동 지시형 어미로 보완한다.
        page_activity_number = 0
        for line in raw.splitlines():
            normalized = re.sub(r"\s+", " ", line).strip()
            match = re.match(r"^(\d{1,2})[.)]\s*(.+)", normalized)
            if match:
                number = int(match.group(1))
                body = match.group(2).strip()
                method = "번호 활동 감지"
            else:
                body = normalized
                if not re.search(r"(?:해\s*보자|보자)\s*[.!?]?\s*$", body):
                    continue
                page_activity_number += 1
                number = page_activity_number
                method = "지시문 종결형 감지"
            if len(body) >= 6 and re.search(r"보자|해 보|적어|설명|비교|토의|토론|감상|연주|작성|발표|조사|이야기", body):
                if any(key and (key in re.sub(r"\s+", "", body) or re.sub(r"\s+", "", body) in key)
                       for key in captured_activity_keys):
                    continue
                page_activity_number = max(page_activity_number, number)
                activities.append({
                    "page": page_no, "number": number,
                    "text": f"{number}. {body}", "method": method,
                })

    explicit_standards = _unique(explicit_standards)
    learning_goals = _unique(learning_goals)
    activities = _unique(activities)
    return {
        "achievement_standards": {
            "included": bool(explicit_standards), "count": len(explicit_standards),
            "items": explicit_standards,
            "note": "성취기준 코드 또는 명시 문구를 기준으로 검사했습니다.",
        },
        "learning_goals": {
            "included": bool(learning_goals), "count": len(learning_goals), "items": learning_goals,
            "note": "'학습 목표' 표제가 있으면 그 문장만 사용하고, 표제가 없을 때만 페이지 상단의 학생 행동 문장을 제한적으로 검사했습니다.",
        },
        "activities": {
            "included": bool(activities), "count": len(activities), "items": activities,
            "note": "[활동 n] 표제부터 지시형 종결어미까지 블록으로 읽고, 표제가 없는 경우에만 번호·지시형 문장을 보완 검사했습니다.",
        },
    }


def _body_sentences(pages: list[str], components: dict[str, Any]) -> list[dict[str, Any]]:
    """목표·활동·표제·대화문을 제외하고 설명형 본문 문장만 고른다."""
    excluded = []
    for key in ("achievement_standards", "learning_goals", "activities"):
        for item in components[key]["items"]:
            value = re.sub(r"[^가-힣0-9]", "", item["text"])
            excluded.append(re.sub(r"^\d+", "", value))
    results = []
    for page_no, raw in enumerate(pages, 1):
        joined = re.sub(r"\s*\n\s*", " ", raw.replace("\x00", " "))
        # <이 곡은>처럼 꺾쇠괄호로 표시한 설명 태그는 마침표 없이 앞 캡션·표제에 바로 붙어 있는
        # 경우가 많아, 앞 내용과 섞이지 않도록 태그 앞에서 문장을 끊는다.
        joined = re.sub(r"(?<=[^\s.!?])\s+(?=<[가-힣A-Za-z0-9 ]{1,12}>)", ". ", joined)
        for match in re.finditer(r"[^.!?]+[.!?]", joined):
            sentence = re.sub(r"\s+", " ", match.group(0)).strip(" ·-–—")
            sentence = re.sub(r"^(\S{2,10})\s+\1는\s+", r"\1는 ", sentence)
            normalized = re.sub(r"[^가-힣0-9]", "", sentence)
            if not 18 <= len(sentence) <= 340:
                continue
            if any(value and value in normalized for value in excluded):
                continue
            if "할 수 있다" in sentence:
                continue
            if re.search(r"(?:집사|다\s*같이|용고)\s*:", sentence):
                tail = re.search(r"(?:대취타|취타)는\s+.+[가-힣]다[.!?]$", sentence)
                if not tail:
                    continue
                sentence = tail.group(0)
                normalized = re.sub(r"[^가-힣0-9]", "", sentence)
            if not re.search(r"[가-힣]다[.!?]$", sentence):
                continue
            results.append({"page": page_no, "text": sentence})
    return _unique(results)


def _review_body_text(pages: list[str], components: dict[str, Any],
                      suppressed_fingerprints: set[str] | None = None) -> dict[str, Any]:
    """설명형 본문과 활동 문장의 맞춤법·문법·용어 표기·문장 난이도를 보조 점검한다.

    활동 문장은 '~해 보자'처럼 청유형으로 끝나 본문 문장 판정 규칙(다.로 끝남)을
    통과하지 못하므로, 문장 선별과 무관하게 원문 그대로 검사 대상에 더한다.
    """
    suppressed_fingerprints = suppressed_fingerprints or set()
    page_items: dict[int, list[dict[str, Any]]] = {page_no: [] for page_no in range(1, len(pages) + 1)}
    official_terms = _official_music_terms()
    activity_sentences = [
        {"page": activity["page"], "text": activity["text"]}
        for activity in components["activities"]["items"]
    ]
    for item in _body_sentences(pages, components) + activity_sentences:
        current = item["text"]
        suggested = current
        issues = []

        replacements = (
            (r"\b다같이\b", "다 같이", "띄어쓰기", "‘다 같이’는 띄어 씁니다."),
            (r"징\s*을\s*한번", "징을 한 번", "띄어쓰기", "횟수를 나타내는 ‘한 번’은 띄어 씁니다."),
            (r"악기편성", "악기 편성", "띄어쓰기", "‘악기 편성’은 두 단어로 띄어 씁니다."),
            (r"대취\s+타", "대취타", "띄어쓰기", "PDF 줄바꿈으로 나뉜 ‘대취타’를 붙여 씁니다."),
            (r"들\s+고", "들고", "띄어쓰기", "PDF 줄바꿈으로 나뉜 말을 원래 표기로 복원합니다."),
            (r"악\s+기", "악기", "띄어쓰기", "PDF 줄바꿈으로 나뉜 말을 원래 표기로 복원합니다."),
            (r"징\s+을", "징을", "띄어쓰기", "PDF 줄바꿈으로 나뉜 조사 결합을 복원합니다."),
            (r"소리\s+를", "소리를", "띄어쓰기", "PDF 줄바꿈으로 나뉜 조사 결합을 복원합니다."),
            (r"말\s+이", "말이", "띄어쓰기", "PDF 줄바꿈으로 나뉜 조사 결합을 복원합니다."),
            (r"훤화금\s+\(", "훤화금(", "띄어쓰기", "낱말과 바로 뒤 한자 표기 사이의 불필요한 공백을 제거합니다."),
            (r"하라‘", "하라’", "문장 부호", "닫는따옴표의 방향을 바로잡습니다."),
            (r"한 가지\s+이다", "한 가지이다", "띄어쓰기", "서술격 조사 ‘이다’는 앞말에 붙여 씁니다."),
            (r"아\s+련한", "아련한", "띄어쓰기", "‘아련한’은 한 단어로 붙여 씁니다."),
            (r"표현\s+한", "표현한", "띄어쓰기", "관형형 어미가 붙은 ‘표현한’을 붙여 씁니다."),
            (r"곡\s+으로", "곡으로", "띄어쓰기", "명사와 조사 ‘으로’를 붙여 씁니다."),
            (r"장구[・ㆍ]꽹과리", "장구·꽹과리", "문장 부호", "가운뎃점 표기를 ‘·’로 통일합니다."),
        )
        for pattern, replacement, issue_type, reason in replacements:
            changed, count = re.subn(pattern, replacement, suggested)
            if count:
                suggested = changed
                issues.append({"type": issue_type, "reason": reason})

        if len(re.sub(r"\s+", "", suggested)) >= 40:
            changed, count = re.subn(
                r"([가-힣]{2,}(?:졌|되었|하였|했|였)고)\s+(?=[가-힣A-Za-z])",
                r"\1, ", suggested, count=1,
            )
            if count:
                suggested = changed
                issues.append({
                    "type": "문장 부호",
                    "reason": "긴 연결문에서 앞뒤 의미 단위를 분명히 하도록 쉼표를 넣었습니다.",
                })

        if "행진곡으로" in suggested and "사용된 행진곡이다" in suggested:
            suggested = suggested.replace("행진곡으로", "행진 음악으로", 1).replace(
                "사용된 행진곡이다", "사용되었다", 1
            )
            issues.append({"type": "문법·표현", "reason": "‘행진곡’이 한 문장에 반복되어 뜻을 유지하며 간결하게 다듬었습니다."})
        if "모두 7장의 악곡으로" in suggested:
            suggested = suggested.replace("모두 7장의 악곡으로", "모두 7장으로 이루어져 있으며", 1)
            issues.append({"type": "문법·표현", "reason": "악곡이 일곱 장으로 구성된다는 관계를 분명하게 표현했습니다."})
        split_patterns = (
            (r"연주하며,\s*(장구·꽹과리 등이 추가로 편성되기도 한다\.)", r"연주한다. \1"),
            (r"연주하며,\s*(‘[^’]+’이라고도 불린다\.)", r"연주한다. \1"),
        )
        for pattern, replacement in split_patterns:
            changed, count = re.subn(pattern, replacement, suggested)
            if count:
                suggested = changed
                issues.append({"type": "난이도", "reason": "정보가 많은 문장을 둘로 나누어 고등학교 1학년이 읽기 쉽게 했습니다."})

        compact_length = len(re.sub(r"\s+", "", current))
        clause_count = len(re.findall(r",|하며|하고|따라|맞춰", current)) + 1
        if (compact_length > 90 or clause_count >= 5) and not any(x["type"] == "난이도" for x in issues):
            issues.append({
                "type": "난이도",
                "reason": "문장이 길거나 한 문장에 정보가 많습니다. 뜻 단위로 두 문장으로 나누는 편이 좋습니다.",
            })

        speller_suggestions = speller_adapter.adapter.check_text(suggested)
        if speller_suggestions:
            shift = 0
            for item in sorted(speller_suggestions, key=lambda x: x["start"]):
                candidate = item["candidates"][0] if item["candidates"] else None
                reason = item.get("description") or "표준 표기와 다를 수 있습니다."
                if candidate:
                    reason += f" → 추천: {candidate}"
                issues.append({"type": "맞춤법(전문 검사기)", "reason": reason})
                if candidate:
                    start, end = item["start"] + shift, item["end"] + shift
                    if 0 <= start <= end <= len(suggested):
                        suggested = suggested[:start] + candidate + suggested[end:]
                        shift += len(candidate) - (end - start)

        terminology = []
        matched_terms: set[str] = set()
        for term in official_terms:
            if term in matched_terms or any(term in longer for longer in matched_terms):
                continue
            term_found = (
                bool(re.search(rf"(?<![가-힣]){re.escape(term)}(?![가-힣])", current))
                if len(term) == 1 else term in current
            )
            if not term_found:
                continue
            matched_terms.add(term)
            terminology.append({
                "term": term,
                "status": "표준 용어 확인",
                "reason": f"{MUSIC_EDITING_TERMS_SOURCE}에 등재된 표준 용어입니다.",
            })

        fingerprint = _fingerprint("body_text", current)
        page_items[item["page"]].append({
            "current_text": current,
            "suggested_text": suggested if suggested != current else None,
            "status": "수정 제안" if issues else "적절",
            "issues": issues,
            "terminology": terminology,
            "fingerprint": fingerprint,
            "is_false_positive": fingerprint in suppressed_fingerprints,
            "metrics": {"length": compact_length, "clauses": clause_count},
        })

    total = sum(len(items) for items in page_items.values())
    suggestions = sum(item["status"] == "수정 제안" for items in page_items.values() for item in items)
    return {
        "summary": {"sentence_count": total, "suggestion_count": suggestions},
        "pages": page_items,
        "policy": CURRICULUM_TEXT_POLICY,
        "basis": {
            "method": "로컬 편집 규칙 검사 + 전문 맞춤법 검사기(가능할 때)",
            "checks": ["PDF 줄바꿈 복원", "주요 띄어쓰기", "문장 호응·중복 표현", "긴 연결 문장", "기존 교과서 용어 표기", "전문 맞춤법 검사기 대조"],
            "limitation": "전문 맞춤법 검사기(비공식 공개 API) 호출이 실패하면 그 문장은 로컬 규칙 검사 결과만 반영됩니다.",
        },
        "note": "학습 목표를 제외한 설명형 본문과 활동 문장을 함께 검토했습니다. 명칭은 기존 교과서의 동일 표기 여부를 확인한 보조 결과입니다.",
    }


def extract_curriculum_standards(pages: list[str]) -> list[dict[str, Any]]:
    standards: list[dict[str, Any]] = []
    for page_no, raw in enumerate(pages, 1):
        lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
        current: dict[str, Any] | None = None
        for line in lines:
            match = re.match(r"^\[(12감비\d{2}-\d{2})\]\s*(.*)", line)
            if match:
                if current:
                    standards.append(current)
                current = {"code": match.group(1), "text": match.group(2), "page": page_no}
                continue
            if current:
                if (not line or line.startswith(("(가)", "(나)", "•", "[12감비", "(2)"))
                        or "성취기준 해설" in line or "성취기준 적용" in line):
                    standards.append(current)
                    current = None
                elif len(current["text"]) < 280:
                    # 교육과정 PDF는 한 단어가 줄 경계에서 분리되는 경우가 많다.
                    current["text"] += line
        if current:
            standards.append(current)
    deduplicated = {item["code"]: item for item in standards}
    context = _extract_curriculum_context(pages)
    for code, item in deduplicated.items():
        item.update(context.get(code, {}))
    return [deduplicated[code] for code in sorted(deduplicated)]


def _extract_curriculum_context(pages: list[str]) -> dict[str, dict[str, Any]]:
    """성취기준별 해설과 영역별 적용 시 고려 사항을 교육과정 원문에서 추출한다."""
    text = "\n".join(pages)
    result: dict[str, dict[str, Any]] = {}
    explanation_pattern = re.compile(
        r"•\s*\[(12감비\d{2}-\d{2})\]\s*(.*?)"
        r"(?=\n\s*•\s*\[12감비|\n\s*•?\s*\(나\)\s*성취기준 적용 시 고려 사항|\Z)",
        re.S,
    )
    for match in explanation_pattern.finditer(text):
        result.setdefault(match.group(1), {})["explanation"] = re.sub(
            r"\s+", " ", match.group(2)
        ).strip()

    domains = (
        ("01", r"\[12감비01-01\]", r"\[12감비02-01\]"),
        ("02", r"\[12감비02-01\]", r"\n3\.\s*교수"),
    )
    for domain, start_pattern, end_pattern in domains:
        section_match = re.search(start_pattern + r"(.*?)" + end_pattern, text, re.S)
        if not section_match:
            continue
        section = section_match.group(1)
        considerations_match = re.search(
            r"(?:•\s*)?\(나\)\s*성취기준 적용 시 고려 사항\s*(.*)", section, re.S
        )
        if not considerations_match:
            continue
        considerations = [
            re.sub(r"\s+", " ", item).strip()
            for item in re.split(r"\n\s*•\s*", considerations_match.group(1))
            if re.sub(r"\s+", " ", item).strip()
        ]
        for code in [key for key in result if key.startswith(f"12감비{domain}-")]:
            result[code]["application_considerations"] = considerations
    return result


def _normalize_for_similarity(text: str) -> str:
    return re.sub(r"[^가-힣0-9]", "", text)


def _ngrams(text: str, size: int = 3) -> Counter[str]:
    normalized = _normalize_for_similarity(text)
    return Counter(normalized[i:i + size] for i in range(max(0, len(normalized) - size + 1)))


def _cosine(first: Counter[str], second: Counter[str]) -> float:
    if not first or not second:
        return 0.0
    common = set(first) & set(second)
    numerator = sum(first[key] * second[key] for key in common)
    denominator = math.sqrt(sum(value * value for value in first.values())) * math.sqrt(
        sum(value * value for value in second.values())
    )
    return numerator / denominator if denominator else 0.0


def _tokens(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[가-힣]{2,}", text)
        if token not in STOPWORDS and len(token) >= 2
    }


ACTIVITY_GENERIC_TOKENS = {
    "보자", "해보자", "활동", "제재곡", "대표", "음악", "악곡", "노래",
    "특징", "내용", "모둠별로", "친구들과", "대하여", "알아보고", "감상하고",
}
GENRE_PATTERNS = {
    "클래식 기악곡": r"교향곡|협주곡|소나타|실내악|기악곡|관현악|피아노곡|바이올린곡",
    "오페라": r"오페라|아리아",
    "뮤지컬": r"뮤지컬|넘버",
    "예술가곡": r"가곡|리트|아트송",
    "가요·대중음악": r"가요|대중음악|팝송|팝 음악|록 음악|발라드|힙합",
    "국악": r"국악|민요|판소리|정악|정가|산조|농악|대취타|취타|풍물",
}
ACTION_PATTERNS = {
    "노래 부르기": r"부르|가창|노래하",
    "악기 연주하기": r"연주|악기로|장단을 치|리듬을 치|반주하",
    "감상하기": r"감상|들어 보|듣고|들으며",
    "비교하기": r"비교|같은 점|다른 점|공통점|차이점",
    "분석·설명하기": r"분석|설명|근거|특징.{0,8}(?:조사|파악|말하|말해)",
    "조사하기": r"조사|자료를 찾|검색",
    "표현·창작하기": r"표현|창작|만들|그리|동작",
    "토의·발표하기": r"토의|토론|발표|이야기",
}
INSTRUMENT_FAMILIES = {
    "현악기": ("기타", "우쿨렐레", "가야금", "거문고", "바이올린", "비올라", "첼로", "콘트라베이스", "해금", "아쟁"),
    "타악기": ("드럼", "카혼", "장구", "북", "소고", "꽹과리", "징", "탬버린", "실로폰", "마림바"),
    "관악기": ("단소", "소금", "대금", "피리", "태평소", "플루트", "클라리넷", "오보에", "바순", "트럼펫", "트롬본", "색소폰", "호른"),
    "건반악기": ("피아노", "오르간", "건반", "신시사이저"),
}


def _instrument_mentioned(text: str, instrument: str) -> bool:
    if len(instrument) >= 3:
        return instrument in text
    particles = r"(?:에서|으로|에게|을|를|이|가|은|는|과|와|의|로|에)?"
    return bool(re.search(rf"(?<![가-힣]){re.escape(instrument)}{particles}(?![가-힣])", text))


def _activity_semantics(text: str) -> dict[str, list[str]]:
    """문장 종결 표현을 제외하고 곡·장르·실제 수행 행동을 구조화한다."""
    works = []
    for value in re.findall(r"[‘'\"“](.{2,40}?)[’'\"”]", text):
        value = re.sub(r"\s+", " ", value).strip()
        if not re.fullmatch(r"카르멘|오페라|뮤지컬|활동\s*\d+", value, re.I):
            works.append(value)
    works.extend(title for title in KNOWN_WORK_TITLES if title in text)
    genres = [name for name, pattern in GENRE_PATTERNS.items() if re.search(pattern, text)]
    actions = [name for name, pattern in ACTION_PATTERNS.items() if re.search(pattern, text)]
    instruments = [
        instrument for values in INSTRUMENT_FAMILIES.values() for instrument in values
        if _instrument_mentioned(text, instrument)
    ]
    instrument_families = [
        family for family, values in INSTRUMENT_FAMILIES.items()
        if any(_instrument_mentioned(text, instrument) for instrument in values)
    ]
    keywords = sorted(
        token for token in _tokens(text)
        if token not in ACTIVITY_GENERIC_TOKENS
        and not any(re.search(pattern, token) for pattern in GENRE_PATTERNS.values())
        and not any(re.search(pattern, token) for pattern in ACTION_PATTERNS.values())
    )
    return {
        "works": list(dict.fromkeys(works)),
        "genres": genres,
        "actions": actions,
        "instruments": list(dict.fromkeys(instruments)),
        "instrument_families": instrument_families,
        "keywords": keywords,
    }


def _activity_semantic_similarity(first: str, second: str) -> tuple[float, dict[str, list[str]]]:
    left, right = _activity_semantics(first), _activity_semantics(second)
    shared_works = sorted(set(left["works"]) & set(right["works"]))
    shared_genres = sorted(set(left["genres"]) & set(right["genres"]))
    shared_actions = sorted(set(left["actions"]) & set(right["actions"]))
    shared_instruments = sorted(set(left["instruments"]) & set(right["instruments"]))
    shared_instrument_families = sorted(
        set(left["instrument_families"]) & set(right["instrument_families"])
    )
    shared_keywords = sorted(set(left["keywords"]) & set(right["keywords"]))[:8]
    central = bool(
        shared_works or shared_genres or shared_actions or shared_instrument_families
        or len(shared_keywords) >= 2
    )
    if not central:
        return 0.0, {"shared_works": [], "shared_genres": [], "shared_actions": [],
                     "shared_instruments": [], "shared_instrument_families": [], "shared_keywords": []}
    work_score = 1.0 if shared_works else 0.0
    genre_score = len(shared_genres) / max(1, min(len(left["genres"]), len(right["genres"])))
    action_score = len(shared_actions) / max(1, min(len(left["actions"]), len(right["actions"])))
    instrument_score = (
        1.0 if shared_instruments else
        len(shared_instrument_families) / max(
            1, min(len(left["instrument_families"]), len(right["instrument_families"]))
        )
    )
    keyword_score = len(shared_keywords) / max(2, min(len(left["keywords"]), len(right["keywords"])))
    score = min(1.0, work_score * .30 + genre_score * .10 + action_score * .15
                + instrument_score * .35 + keyword_score * .10)
    return round(score, 4), {
        "shared_works": shared_works, "shared_genres": shared_genres,
        "shared_actions": shared_actions, "shared_instruments": shared_instruments,
        "shared_instrument_families": shared_instrument_families,
        "shared_keywords": shared_keywords,
    }


def similarity(first: str, second: str) -> float:
    char_score = _cosine(_ngrams(first), _ngrams(second))
    first_tokens, second_tokens = _tokens(first), _tokens(second)
    token_score = len(first_tokens & second_tokens) / max(1, min(len(first_tokens), len(second_tokens)))
    return round(min(1.0, char_score * .72 + token_score * .28), 4)


def _similarity_explanation(first: str, second: str, score: float,
                            comparison_type: str) -> dict[str, Any]:
    shared_keywords = sorted(_tokens(first) & _tokens(second), key=lambda value: (-len(value), value))[:8]
    action_words = ("감상", "비교", "분석", "설명", "표현", "연주", "노래", "토론", "토의", "조사", "작성", "발표")
    shared_actions = [word for word in action_words if word in first and word in second]
    if score >= .72:
        verdict = "표현과 내용이 매우 유사함" if comparison_type == "content" else "활동 구조와 수행 행동이 매우 유사함"
        interpretation = "핵심어뿐 아니라 문장 표현과 배열도 많이 겹쳐 출처와 독자성을 확인해야 합니다."
    elif score >= .50:
        verdict = "유사함"
        interpretation = "핵심 내용과 표현 방식이 상당 부분 겹쳐 사람이 원문을 대조해야 합니다."
    elif score >= .32:
        verdict = "일부 요소만 유사함"
        interpretation = "일부 핵심어 또는 수행 행동만 겹치며 문장 전체가 유사하다는 뜻은 아닙니다."
    else:
        verdict = "유사하지 않음"
        interpretation = "의미 있게 겹치는 표현이 부족해 유사 문장으로 판단하지 않았습니다."
    return {
        "verdict": verdict,
        "interpretation": interpretation,
        "shared_keywords": shared_keywords,
        "shared_actions": shared_actions,
        "comparison_focus": "본문의 주제·핵심어·표현 순서" if comparison_type == "content" else "활동의 수행 동사·대상·과정",
    }


CONTENT_ACTION_PATTERNS = {
    "감상·듣기": r"감상|듣",
    "특징 확인·설명": r"조사|말해|말하|설명|파악|살펴|찾아|찾고",
    "비교·분석": r"비교|분석|공통점|차이점",
    "노래 부르기": r"부르|가창|노래하",
    "악기 연주하기": r"연주|악기로|리듬을 치|장단을 치",
    "표현·창작": r"표현|창작|만들|그리|동작",
}
CONTENT_GENERIC_TOKENS = STOPWORDS | {
    "감상하고", "감상하여", "감상해", "듣고", "들으며", "조사해보자", "말해보자",
    "파악하여", "설명해보자", "곡의", "주는", "해보자", "보자",
}


def _content_overlap_basis(first: str, second: str) -> dict[str, Any]:
    """정확한 핵심어와 같은 수행 범주를 합쳐 본문 비교 근거를 만든다."""
    shared_keywords = sorted(
        (_tokens(first) & _tokens(second)) - CONTENT_GENERIC_TOKENS,
        key=lambda value: (-len(value), value),
    )[:8]
    shared_meanings = []
    for label, pattern in CONTENT_ACTION_PATTERNS.items():
        if re.search(pattern, first) and re.search(pattern, second):
            if label == "특징 확인·설명":
                focus = r"특징|음색|분위기|선율|리듬|장단|가락|악기|가사"
                if not (re.search(focus, first) and re.search(focus, second)):
                    continue
            shared_meanings.append(label)
    evidence = [f"핵심어:{value}" for value in shared_keywords]
    evidence.extend(f"수행:{value}" for value in shared_meanings)
    return {
        "shared_keywords": shared_keywords,
        "shared_meanings": shared_meanings,
        "semantic_evidence": evidence,
        "semantic_count": len(evidence),
    }


def _signature_similarity(code: str, text: str) -> tuple[float, list[str]]:
    signatures = STANDARD_SIGNATURES.get(code, ())
    normalized = text.replace("·", " ")
    matched = [keyword for keyword in signatures if keyword in normalized]
    # 핵심 수행 동사나 개념 세 개가 확인되면 충분한 의미 근거로 본다.
    score = min(1.0, len(matched) / min(3, len(signatures))) if signatures else 0.0
    return score, matched


def match_curriculum(components: dict[str, Any], pages: list[str],
                     standards: list[dict[str, Any]]) -> dict[str, Any]:
    signals = []
    for key in ("learning_goals", "activities"):
        signals.extend(components[key]["items"])
    if not signals:
        # 목표·활동을 찾지 못한 경우 본문 문장으로 제한적으로 비교한다.
        for page_no, raw in enumerate(pages, 1):
            for sentence in re.split(r"(?<=[.!?])\s+", _clean_text(raw)):
                if 15 <= len(sentence) <= 220:
                    signals.append({"page": page_no, "text": sentence, "method": "본문 문장"})

    explicit_codes = {item["code"] for item in components["achievement_standards"]["items"]}
    matches = []
    for standard in standards:
        best_signal, best_score, best_keywords = None, 0.0, []
        for signal in signals:
            context_texts = [standard["text"]]
            if standard.get("explanation"):
                context_texts.append(standard["explanation"])
            context_texts.extend(standard.get("application_considerations", []))
            lexical_score = max(similarity(signal["text"], value) for value in context_texts)
            signature_score, keywords = _signature_similarity(standard["code"], signal["text"])
            score = max(lexical_score, signature_score * .65 + lexical_score * .35)
            if score > best_score:
                best_signal, best_score, best_keywords = signal, score, keywords
        if standard["code"] in explicit_codes:
            best_score = 1.0
        matches.append({
            **standard, "score": round(best_score, 4),
            "evidence": best_signal,
            "matched_keywords": best_keywords,
            "explicitly_declared": standard["code"] in explicit_codes,
        })
    matches.sort(key=lambda item: item["score"], reverse=True)
    top_score = matches[0]["score"] if matches else 0.0
    if top_score >= .42:
        status, label = "applicable", "해당"
    elif top_score >= .24:
        status, label = "partially_applicable", "부분 해당"
    elif top_score < .10 and signals:
        status, label = "not_applicable", "해당 없음"
    else:
        status, label = "review_required", "확인 필요"
    signal_count = len(signals)
    if status == "not_applicable":
        interpretation = (
            f"학습 목표·활동·본문에서 비교한 근거 문장 {signal_count}개가 있었으나, "
            f"가장 가까운 성취기준의 일치도가 {top_score * 100:.1f}%로 10% 미만이고 "
            "명시된 성취기준 코드도 없어 자동 판정상 '해당 없음'으로 분류했습니다. "
            "이는 교육적 비해당을 확정하는 판정이 아니라 원고에 연결 근거가 드러나지 않았다는 뜻입니다."
        )
    elif status == "review_required":
        interpretation = (
            f"비교 근거 문장 {signal_count}개와 성취기준의 일치도가 {top_score * 100:.1f}%로 "
            "자동 판정 임계 구간에 들지 않아 사람이 확인해야 합니다."
        )
    else:
        interpretation = (
            f"비교 근거 문장 {signal_count}개 중 가장 가까운 성취기준의 일치도가 "
            f"{top_score * 100:.1f}%여서 '{label}'으로 분류했습니다."
        )
    return {
        "status": status, "label": label, "top_score": top_score,
        "method": "성취기준 원문·해설·적용 시 고려 사항의 문자 유사도, 핵심 수행 동사·개념과 명시 코드 비교",
        "decision_basis": {
            "signal_count": signal_count,
            "thresholds": {"applicable": .42, "partially_applicable": .24, "not_applicable_below": .10},
            "interpretation": interpretation,
            "caution": "'해당 없음'은 교육과정상 연계 불가능을 뜻하지 않고, 추출된 원고 문장에 자동 연결 근거가 부족하다는 뜻입니다.",
        },
        "note": "판정 근거와 임계값을 공개한 보조 결과이며, 최종 성취기준 연결은 사람이 확인해야 합니다.",
        "top_matches": matches[:3],
    }


def _load_activities_adapter(module_name: str | None) -> Any:
    """활동문 추천용 어댑터를 분석 시작 시 한 번만 로딩한다.

    모듈이 없거나 import에 실패해도 예외를 던지지 않고 None을 반환해,
    호출부가 규칙 기반 추천으로 그대로 대체하도록 한다.
    """
    if not module_name:
        return None
    try:
        module = importlib.import_module(module_name)
        return getattr(module, "adapter", None)
    except Exception:
        return None


def _apply_ai_adapter(module_name: str | None, result: dict[str, Any]) -> dict[str, Any]:
    """어댑터가 curriculum_alignment 갱신용 audit(payload)를 지원할 때만 적용한다.

    같은 ai_module이 activities_adapter(recommend_activities)만 구현하고 audit는
    구현하지 않을 수도 있으므로, audit가 없으면 조용히 건너뛴다.
    """
    if not module_name:
        return result
    adapter = _load_activities_adapter(module_name)
    if adapter is None or not callable(getattr(adapter, "audit", None)):
        return result
    update = adapter.audit(json.loads(json.dumps(result, ensure_ascii=False)))
    if update:
        result["curriculum_alignment"] = update
        result["curriculum_alignment"]["method"] = f"AI 챗봇 어댑터: {module_name}"
    return result


def _render_pages_and_images(manuscript: Path, destination: Path, dpi: int = 144) -> list[dict[str, Any]]:
    """페이지 PNG와 이미지 영역을 생성하고 읽기 가능한 파일인지 검사한다.

    여기서 '읽음'은 이미지 영역을 정상적으로 렌더링·추출했다는 뜻이다.
    이미지가 무엇을 의미하는지 해석하는 기능은 AI 비전 어댑터의 범위다.
    """
    import pdfplumber  # type: ignore
    import pymupdf  # type: ignore
    from PIL import Image, ImageStat  # type: ignore

    pages_dir, images_dir = destination / "pages", destination / "images"
    pages_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)
    scale = dpi / 72
    pymupdf_document = pymupdf.open(str(manuscript))
    results: list[dict[str, Any]] = []
    page_no = 0
    with pdfplumber.open(str(manuscript)) as plumber_document:
        for index, plumber_page in enumerate(plumber_document.pages):
            pixmap = pymupdf_document[index].get_pixmap(
                matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False,
            )
            full_image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            # 세로 A4 두 쪽이 나란히 이어붙어 가로로 올라온 원고이므로 좌우로 나눠 각각 한 쪽으로 셈한다.
            is_spread = _is_two_page_spread(plumber_page.width, plumber_page.height)
            half_width_pts = plumber_page.width / 2
            half_width_px = full_image.width // 2
            slices = (
                [(0, full_image.width, 0.0)] if not is_spread else
                [(0, half_width_px, 0.0), (half_width_px, full_image.width, half_width_pts)]
            )
            for left_px, right_px, offset_pts in slices:
                page_no += 1
                image = full_image if not is_spread else full_image.crop((left_px, 0, right_px, full_image.height))
                page_path = pages_dir / f"page_{page_no:04d}.png"
                image.save(page_path)
                page_items = []
                for image_index, info in enumerate(plumber_page.images or [], 1):
                    bbox = [float(info["x0"]), float(info["top"]), float(info["x1"]), float(info["bottom"])]
                    if is_spread:
                        center_x = (bbox[0] + bbox[2]) / 2
                        if not (offset_pts <= center_x < offset_pts + half_width_pts):
                            continue
                        bbox = [bbox[0] - offset_pts, bbox[1], bbox[2] - offset_pts, bbox[3]]
                    pixel_box = (
                        max(0, round(bbox[0] * scale)), max(0, round(bbox[1] * scale)),
                        min(image.width, round(bbox[2] * scale)), min(image.height, round(bbox[3] * scale)),
                    )
                    asset_path = images_dir / f"page_{page_no:04d}_image_{image_index:03d}.png"
                    status, reason = "읽지 못함", "이미지 영역을 정상적으로 추출하지 못했습니다."
                    try:
                        if pixel_box[2] - pixel_box[0] >= 8 and pixel_box[3] - pixel_box[1] >= 8:
                            crop = image.crop(pixel_box)
                            crop.save(asset_path)
                            variation = max(ImageStat.Stat(crop.convert("L")).stddev or [0])
                            if crop.width >= 16 and crop.height >= 16 and variation >= 1:
                                status, reason = "읽음", "이미지 영역을 정상적으로 렌더링하고 추출했습니다."
                            else:
                                reason = "이미지가 너무 작거나 내용 대비가 없어 판독하기 어렵습니다."
                    except Exception as exc:
                        reason = f"이미지 추출 오류: {type(exc).__name__}"
                    page_items.append({
                        "page": page_no, "number": image_index, "bbox": [round(v, 3) for v in bbox],
                        "read_status": status, "reason": reason,
                        "asset_path": asset_path.relative_to(destination).as_posix() if asset_path.exists() else None,
                        "semantic_analysis": "미실행",
                    })
                results.append({
                    "page": page_no, "page_image": page_path.relative_to(destination).as_posix(),
                    "width": image.width, "height": image.height, "images": page_items,
                })
    pymupdf_document.close()
    return results


def _component_for_page(component: dict[str, Any], page_no: int) -> dict[str, Any]:
    items = [item for item in component["items"] if item["page"] == page_no]
    return {**component, "included": bool(items), "count": len(items),
            "document_count": component["count"], "items": items}


# 장르·범주만 나타내는 표제어. 실제 곡·작품 제목이 아니므로 주제 추론에서 제외한다.
_GENRE_ONLY_TERMS = {
    "음악", "악곡", "작품", "노래", "활동", "학습 목표", "특징", "내용", "으로", "이다",
    "발라드", "오페라", "뮤지컬", "국악", "가요", "민요", "클래식", "아리아", "넘버",
    "가곡", "예술가곡", "교향곡", "협주곡", "실내악",
}


def _infer_topic(pages: list[str], fallback: str | None = None) -> str:
    """이 페이지(들)의 텍스트에서 제재곡 주제를 추론한다.

    `fallback`은 같은 원고의 1쪽에서 이미 찾은 제목이다. 이 페이지 자체에서
    제목을 못 찾으면(예: 제목이 다시 표기되지 않는 이어지는 쪽), 페이지별로
    엉뚱한 문장을 새로 추측하는 대신 원고 전체의 제목을 그대로 재사용한다.
    이 페이지에 서로 다른 제목이 실제로 표기돼 있으면(원고 한 편에 곡이 여러
    개인 경우) 그 페이지 고유의 제목이 fallback보다 항상 우선한다.
    """
    if pages:
        # 장르명 같은 일반 표제보다 원고에 실제로 표기된 곡·작품 제목을 주제로 우선한다.
        work_titles = _extract_work_titles(pages[0])
        if work_titles:
            return work_titles[0]
    if fallback:
        return fallback
    blocked = re.compile(r"학습\s*목표|역량|사진\s*출처|https?://|작사|작곡|편곡|중략|<이 곡은>|\[활동")
    for line in pages[0].splitlines() if pages else []:
        if blocked.search(line):
            continue
        candidate = re.split(r"[·∙]", line, 1)[0].strip(" ◎◈◆·∙-–—")
        if (candidate not in _GENRE_ONLY_TERMS and 2 <= len(candidate) <= 60
                and not re.match(r"^\d", candidate)
                and not re.search(r"수 있다[.!]?$|해 보자[.!]?$", candidate)):
            return candidate
    return "제시된 악곡"


_TRADITIONAL_MUSIC_SIGNALS = (
    "국악", "판소리", "시나위", "산조", "가야금", "거문고", "대금", "해금", "아쟁",
    "장구", "꽹과리", "징", "태평소", "대취타", "취타", "정악", "민요", "장단",
)


def _josa(word: str, with_batchim: str, without_batchim: str) -> str:
    """마지막 글자의 받침 유무에 따라 '을/를', '이/가' 같은 조사를 고른다."""
    if not word:
        return without_batchim
    last = word[-1]
    if "가" <= last <= "힣":
        return with_batchim if (ord(last) - 0xAC00) % 28 else without_batchim
    return without_batchim


def _rhythm_term(pages: list[str]) -> str:
    """국악(전통 음악) 신호가 있는 원고에서만 '장단'을 쓰고, 그 외 장르는 '리듬'으로 표현한다."""
    text = "\n".join(pages)
    for term in _TRADITIONAL_MUSIC_SIGNALS:
        found = (
            bool(re.search(rf"(?<![가-힣]){re.escape(term)}(?![가-힣])", text))
            if len(term) == 1 else term in text
        )
        if found:
            return "장단"
    return "리듬"


def _plain_recommendation_text(text: str) -> str:
    """추천 문구에서는 장식용 특수문자를 제거하고 일반 문장부호만 남긴다."""
    text = text.replace("·", " ").replace("⋅", " ").replace("∙", " ")
    text = re.sub(r"[^0-9A-Za-z가-힣\s,.]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([,.])", r"\1", text)


def _activity_signature(text: str) -> str:
    text = re.sub(r"^\s*\d{1,2}[.)]\s*", "", text)
    text = re.sub(r"^\s*\[활동\s*\d+\]\s*", "", text)
    return _normalize_for_similarity(text)


def _activity_flow_profile(examples: list[dict[str, Any]]) -> dict[str, Any]:
    """기존 교과서 활동 표본에서 자주 쓰는 문장 전개 순서를 요약한다."""
    counts: Counter[str] = Counter()
    for example in examples:
        text = example["text"]
        clauses = re.split(r"[,，]|(?:한 뒤|후)", text, maxsplit=1)
        first, second = clauses[0], clauses[1] if len(clauses) > 1 else ""
        if re.search(r"알아보|살펴|조사|찾", first) and re.search(r"감상|듣|연주|부르", second):
            counts["개념 확인 → 음악 수행"] += 1
        if re.search(r"감상|듣|연주|부르", first) and re.search(r"비교|설명|토의|이야기|조사", second):
            counts["음악 수행 → 비교·표현"] += 1
        if "모둠" in text:
            counts["모둠 과정 명시"] += 1
    dominant = counts.most_common(1)[0][0] if counts else "대상 제시 → 수행 방법 → 결과"
    return {"dominant": dominant, "counts": dict(counts), "sample_count": len(examples)}


_PARTICLE_PAIRS = {
    "을": ("을", "를"), "를": ("을", "를"),
    "이": ("이", "가"), "가": ("이", "가"),
    "은": ("은", "는"), "는": ("은", "는"),
}


def _select_reference_activity(examples: list[dict[str, Any]], code: str,
                               standard_text: str) -> dict[str, Any] | None:
    """등록된 교과서의 실제 활동 표본 중 이 교육과정 기준과 가장 잘 맞는 문장을 고른다.

    활동 추천이 코드마다 고정 문구 하나로만 나오지 않도록, 매번 등록 자료에서
    가장 관련성 높은 실제 예시를 새로 골라 그 문장 구조를 따른다.
    """
    if not examples:
        return None
    signature_words = STANDARD_SIGNATURES.get(code, ())

    def score(example: dict[str, Any]) -> float:
        text = example["text"]
        keyword_hits = sum(1 for word in signature_words if word in text)
        return keyword_hits * 0.4 + similarity(text, standard_text)

    return max(examples, key=score)


def _select_reference_samples(examples: list[dict[str, Any]], code: str,
                              standard_text: str, limit: int = 3) -> list[str]:
    """AI 활동 추천의 참고 표본으로 쓸 실제 활동 문장 상위 N개를 고른다(가벼운 키워드 필터).

    벡터 검색 없이, 등록된 표본(장르별 상한 적용)을 성취기준 키워드·유사도 점수로만
    정렬해 상위 몇 개만 프롬프트에 넣는다.
    """
    if not examples:
        return []
    signature_words = STANDARD_SIGNATURES.get(code, ())

    def score(example: dict[str, Any]) -> float:
        text = example["text"]
        keyword_hits = sum(1 for word in signature_words if word in text)
        return keyword_hits * 0.4 + similarity(text, standard_text)

    ranked = sorted(examples, key=score, reverse=True)
    return [item["text"] for item in ranked[:limit]]


# 특이하고 좁은 신호(가창형·연주형)를 먼저 확인하고, 어디에도 안 걸리면 가장 흔한
# '감상형'으로 분류한다. 순서를 바꾸면 '감상'류 단어가 너무 자주 걸려 다른 유형을 덮어버린다.
_ACTIVITY_TYPE_PATTERNS = {
    "가창형": r"노래|부르|가창|낭송|가사",
    "연주형": r"연주|악기|리듬을 치|건반|타악기|현악기|관악기",
    "비평·맥락형": r"시대|배경|문화|사회|역사|맥락",
    "표현·창작형": r"표현|창작|그리|만들|써\s*보|작성",
    "감상형": r"감상|느낌|느끼|듣고|들으며",
}
_ACTIVITY_TYPE_PRIORITY = ("가창형", "연주형", "비평·맥락형", "표현·창작형", "감상형")
_PIECE_TYPE_ACTIVITY_MENU = {
    "가창곡": ("감상형", "가창형", "비평·맥락형"),
    "연주곡": ("감상형", "연주형", "비평·맥락형"),
    "감상곡": ("감상형", "비평·맥락형", "표현·창작형"),
}


def _classify_activity_type(text: str) -> str:
    """등록 교과서의 실제 활동 문장 하나를 5개 유형 중 하나로 분류한다."""
    for activity_type in _ACTIVITY_TYPE_PRIORITY:
        if re.search(_ACTIVITY_TYPE_PATTERNS[activity_type], text):
            return activity_type
    return "감상형"


def _select_typed_reference_activities(examples: list[dict[str, Any]], activity_type: str,
                                       code: str, standard_text: str,
                                       genre: str | None = None) -> list[dict[str, Any]]:
    """이 유형으로 분류된 실제 활동 표본을 성취기준 관련도 순으로 정렬해 반환한다.

    가장 관련도 높은 문장이 대상 치환에 실패할 수 있으므로(예: 고유명사가 섞인 문장),
    호출부가 순서대로 다음 후보를 시도할 수 있도록 목록 전체를 넘긴다.

    genre가 주어지면 같은 장르 표본을 앞으로 재정렬한다(하드 필터가 아닌 소프트
    재정렬). 호출부가 어차피 리스트 전체를 순서대로 시도하다 첫 성공에서 멈추므로,
    장르 표본이 없거나 전부 치환에 실패해도 항상 나머지 표본으로 폴백된다.
    """
    typed = [example for example in examples if _classify_activity_type(example["text"]) == activity_type]
    if not typed:
        return []
    signature_words = STANDARD_SIGNATURES.get(code, ())

    def score(example: dict[str, Any]) -> float:
        text = example["text"]
        keyword_hits = sum(1 for word in signature_words if word in text)
        return keyword_hits * 0.4 + similarity(text, standard_text)

    ranked = sorted(typed, key=score, reverse=True)
    if not genre:
        return ranked
    matched = [example for example in ranked if example.get("genre") == genre]
    rest = [example for example in ranked if example.get("genre") != genre]
    return matched + rest


_VOCAL_PIECE_SIGNALS = ("가사", "노랫말", "낭송", "가곡", "성악", "부르는", "불러 보", "노래 부르")
_INSTRUMENTAL_PIECE_SIGNALS = ("연주", "악기로", "리듬을 치", "건반", "타악기", "현악기", "관악기")


def _infer_piece_type(pages: list[str]) -> str:
    """제재곡이 가창곡/연주곡/감상곡 중 무엇에 가까운지 가볍게 추정한다.

    AI 어댑터에 참고용 힌트로만 전달하며, 최종 활동 유형 선택은 프롬프트 안에서
    다시 판단하도록 맡긴다.
    """
    text = "\n".join(pages)
    vocal_hits = sum(text.count(term) for term in _VOCAL_PIECE_SIGNALS)
    instrumental_hits = sum(text.count(term) for term in _INSTRUMENTAL_PIECE_SIGNALS)
    if vocal_hits > instrumental_hits:
        return "가창곡"
    if instrumental_hits > vocal_hits:
        return "연주곡"
    return "감상곡"


_GENRE_PRIORITY = ("국악", "오페라", "뮤지컬", "예술가곡", "클래식 기악곡", "가요·대중음악")


def _infer_genre(text: str) -> str | None:
    """제재곡의 장르(국악/오페라/뮤지컬/예술가곡/기악곡/대중가요)를 가볍게 추정한다.

    좁고 구체적인 신호를 넓은 신호보다 먼저 확인한다. '가곡'은 서양 예술가곡과 국악
    전통 성악(정가)을 모두 가리킬 수 있어 국악을 최우선으로 두되, 국악 신호 없이
    '가곡'만 단독으로 쓰인 경우는 예술가곡으로 분류되는 한계가 있다.
    """
    for genre in _GENRE_PRIORITY:
        if re.search(GENRE_PATTERNS[genre], text):
            return genre
    return None


def _adapt_activity_to_topic(example: str, topic: str) -> str | None:
    """실제 활동 예시의 대상(맨 앞 명사구)이 뚜렷할 때만 현재 원고 주제로 바꿔 낀다.

    문장 맨 앞 15자 안에서만 조사 경계를 찾고, 그 안에 따옴표·숫자·접속조사(와/과)가
    있으면 두 대상을 비교하는 문장이거나 고유명사가 섞인 것으로 보아 건드리지 않는다.
    (예: '제재곡과 밴드 이날치의 …'에서 '이날치'의 '이'를 조사로 잘못 아는 사고를 막는다.)
    바꾸고 남은 나머지 문장에 또 다른 따옴표 제목이 남아 있으면(예: '권민석이 연주하는
    바흐의 ‘모음곡 2번’을…'처럼 애초에 다른 곡 얘기인 문장), 그 문장 전체를 포기한다.
    """
    # PDF에서 뽑은 표본은 "1. 두 곡을..." 뿐 아니라 구두점 없는 "1 두 곡을..." 형태로도
    # 앞에 번호가 남아 있어, 마침표·괄호가 없어도 번호+공백이면 지운다.
    stripped = re.sub(r"^\s*\d{1,2}[.)]?\s+", "", example).strip()
    # PDF 줄바꿈 때문에 앞부분이 잘려 접속어로 시작하는 문장 조각은(예: '으로 이 두 갈래를…')
    # 원래 맥락 없이는 뜻이 통하지 않으므로 애초에 대상 치환을 시도하지 않는다.
    if re.match(r"^(그리고|그러나|하지만|그래서|따라서|즉|이에|또한|으로|게다가)(?:\s|$)", stripped):
        return None

    def _finish(remainder: str, particle: str) -> str | None:
        remainder = remainder.lstrip()
        if re.search(r"[‘’“”'\"]", remainder):
            return None
        if particle == "의":
            return f"{topic}의 {remainder}"
        with_batchim, without_batchim = _PARTICLE_PAIRS[particle]
        return f"{topic}{_josa(topic, with_batchim, without_batchim)} {remainder}"

    # 문장이 따옴표로 감싼 제목으로 시작하면(예: '학연화대처용무합설'의 구성과…),
    # 따옴표째로 안전하게 대상을 통째로 바꾼다.
    quoted_match = re.match(r"^[‘'\"“]([^’'\"”]{2,30})[’'\"”](을|를|이|가|은|는|의)(?=\s|$)", stripped)
    if quoted_match:
        return _finish(stripped[quoted_match.end():], quoted_match.group(2))
    window = stripped[:16]
    match = re.match(r"^((?:[가-힣]+\s+)*[가-힣]+?)(을|를|이|가|은|는|의)(?=\s|$)", window)
    if not match or re.search(r"[‘’“”'\"0-9]|와|과", match.group(1)):
        return None
    return _finish(stripped[match.end():], match.group(2))


def _extract_work_titles(text: str) -> list[str]:
    """원고에 명시된 곡·작품 제목 후보를 보수적으로 추출한다."""
    candidates: list[str] = []
    for match in re.finditer(r"[‘'\"“〈《「『](.{2,50}?)[’'\"”〉》」』]", text):
        context = text[max(0, match.start() - 45):match.start()]
        # '자연'과 연관된 가사'처럼 활동 캡션 속 예시어 목록의 분류명일 뿐인 따옴표는
        # 앞쪽에 '제재곡' 같은 단어가 있어도 제목이 아니다 — 곡 제목을 '~와 연관된'
        # 식으로 다시 지칭하는 경우는 없으므로 이 패턴만 보이면 후보에서 제외한다.
        trailing = text[match.end():match.end() + 10]
        if re.match(r"\s*(?:와|과)\s*(?:연관|관련)", trailing):
            continue
        value = match.group(1).strip()
        if re.search(r"뮤지컬|넘버|수록곡|제재곡|감상곡|노래|악곡|작품", context):
            candidates.append(value)
    # ‘뮤지컬 위키드의 중력을 벗어나’처럼 소유격으로 표기한 제목도 잡는다.
    candidates.extend(
        match.group(1).strip(" ‘'\"“”〈〉《》")
        for match in re.finditer(r"뮤지컬\s+[^\n,.!?]{1,30}?의\s+([^\n,.!?]{2,40})", text)
    )
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    for line in lines[:1]:
        heading = re.split(r"[·∙]", line, 1)[0].strip(" ◎◈◆·∙-–—")
        if 2 <= len(heading) <= 35 and not re.search(r"할 수 있다|해 보자|한다[.!]?|입니다", heading):
            parts = re.split(r"(?:와|과)\s+", heading)
            candidates.extend(parts if len(parts) == 2 and all(2 <= len(x) <= 15 for x in parts) else [heading])
    for title in KNOWN_WORK_TITLES:
        if title in text:
            candidates.append(title)
    result: list[str] = []
    for candidate in candidates:
        candidate = re.sub(r"\s+", " ", candidate).strip(" ◎◈◆·-–—:;")
        candidate = re.sub(r"(?:을|를|이|가|은|는)$", "", candidate).strip()
        if candidate not in _GENRE_ONLY_TERMS and 2 <= len(candidate) <= 40 and candidate not in result:
            result.append(candidate)
    return result[:10]


_AI_EDIT_LABELS = {"keep": "유지", "trim": "다듬기", "merge": "통합", "add": "신규"}


def _map_ai_activity_item(item: dict[str, Any], current_activities: list[str],
                          top: dict[str, Any]) -> dict[str, Any]:
    source_indices = [
        index for index in item.get("source_indices") or []
        if isinstance(index, int) and 1 <= index <= len(current_activities)
    ]
    source_texts = [current_activities[index - 1] for index in source_indices]
    label = _AI_EDIT_LABELS.get(item.get("edit_type"))
    reason = item.get("rationale", "")
    return {
        "activity_type": item.get("activity_type", ""),
        "current_text": " / ".join(source_texts) if source_texts else None,
        "reason": f"[{label}] {reason}" if label else reason,
        "suggestion": item.get("text", ""),
        "curriculum_basis": f"[{top['code']}] {top['text']}",
        "matched_keywords": top.get("matched_keywords", []),
        "reference_example": None,
    }


def _recommendations(components: dict[str, Any], alignment: dict[str, Any],
                     pages: list[str], target_level: str = "고등학교 1학년",
                     reference: dict[str, Any] | None = None,
                     activities_adapter: Any = None,
                     document_topic: str | None = None) -> dict[str, Any]:
    top = alignment["top_matches"][0] if alignment["top_matches"] else None
    topic = _infer_topic(pages, fallback=document_topic)
    rhythm_term = _rhythm_term(pages)
    topic_eul = topic + _josa(topic, "을", "를")
    topic_i = topic + _josa(topic, "이", "가")
    result: dict[str, Any] = {
        "achievement_standard": None, "learning_goal": None, "activities": [],
        "generation_method": "등록된 기존 교과서의 실제 활동 표본을 유형별로 골라 대상만 원고 주제로 재구성",
        "curriculum_policy": CURRICULUM_TEXT_POLICY,
    }
    if top and (not components["learning_goals"]["included"] or len(top.get("matched_keywords", [])) < 3):
        goal_templates = {
            "12감비01-01": f"{topic_eul} 감상하고, 악기 편성과 {rhythm_term}의 공통점과 차이점을 비교하여 설명할 수 있다.",
            "12감비01-02": f"{topic_eul} 시대·지역·문화적 맥락에서 감상하고 변화와 발전 양상을 설명할 수 있다.",
            "12감비01-03": f"{topic}의 미적 특성을 감상하고 느낀 점을 다양한 방법으로 표현할 수 있다.",
            "12감비01-04": f"{topic} 감상 경험을 공유하고 서로의 음악적 취향을 존중할 수 있다.",
            "12감비02-01": f"{topic}의 사회·문화·시대적 의미와 음악적 특징을 다양한 관점에서 비평할 수 있다.",
        }
        current_goals = [item["text"] for item in components["learning_goals"]["items"]]
        combined_goal = " ".join(current_goals)
        missing = [word for word in STANDARD_SIGNATURES.get(top["code"], ()) if word not in combined_goal]
        goal_examples = (reference or {}).get("learning_goal_examples", [])
        ranked_goal_examples = sorted(
            goal_examples,
            key=lambda item: max(similarity(item["text"], top["text"]), similarity(item["text"], topic)),
            reverse=True,
        )[:3]
        result["learning_goal"] = {
            "current_text": " / ".join(current_goals) if current_goals else "학습 목표 없음",
            "reason": (
                f"등록된 기존 교과서의 학습 목표 {len(goal_examples)}개에서 문장 구조와 수행 동사를 참고하고, "
                f"교육과정 핵심 개념 {', '.join(missing[:4]) or '일부'}를 원고 주제에 맞게 적용했습니다."
            ),
            "suggestion": _plain_recommendation_text(goal_templates.get(top["code"], top["text"])),
            "curriculum_basis": f"[{top['code']}] {top['text']}",
            "matched_keywords": top.get("matched_keywords", []),
            "missing_keywords": missing,
            "reference_examples": ranked_goal_examples,
            "generation_method": "기존 교과서 학습 목표의 문장 구조 + 교육과정 수행 동사 + 현재 원고 주제",
        }
    # 원고에 이미 활동이 있어도, 편집자가 난이도·흥미·성취기준 부합성을 다시 판단해
    # 상당 부분 새로 쓰는 경우가 대부분이므로 1:1로 다듬지 않고 유형별로 새로 제안한다.
    if top:
        current_activities = [item["text"] for item in components["activities"]["items"]]
        piece_type = _infer_piece_type(pages)
        genre = _infer_genre("\n".join(pages))
        ai_handled = False
        if activities_adapter and callable(getattr(activities_adapter, "recommend_activities", None)):
            ai_examples = (reference or {}).get("activity_readability", {}).get("examples", [])
            ai_payload = {
                "topic": topic,
                "piece_type": piece_type,
                "target_level": target_level,
                "standard_code": top["code"],
                "standard_text": top["text"],
                "current_activities": current_activities,
                "reference_samples": _select_reference_samples(ai_examples, top["code"], top["text"]),
            }
            ai_result = None
            try:
                ai_result = activities_adapter.recommend_activities(ai_payload)
            except Exception:
                ai_result = None
            if ai_result:
                variants_out = []
                for variant in ai_result.get("activity_variants", []):
                    activities_out = [
                        _map_ai_activity_item(item, current_activities, top)
                        for item in variant.get("recommended_activities", [])
                    ]
                    if not activities_out:
                        continue
                    variants_out.append({
                        "label": variant.get("label") or f"안 {len(variants_out) + 1}",
                        "activities": activities_out,
                    })
                if variants_out:
                    result["activity_variants"] = variants_out
                    result["activities"] = variants_out[0]["activities"]
                    result["generation_method"] = "AI(Claude) 기반 기존 활동 검토·재구성 (3안)"
                    result["ai_activity_review"] = {
                        "intent_analysis": ai_result.get("intent_analysis"),
                        "standard_fit": ai_result.get("standard_fit"),
                    }
                    ai_handled = True
        if ai_handled:
            return result
        # API 호출 없이, 등록된 교과서의 실제 활동 문장을 유형별로 골라 대상만 원고 주제로 바꾼다.
        examples = (reference or {}).get("activity_readability", {}).get("examples", [])
        activity_types = _PIECE_TYPE_ACTIVITY_MENU.get(piece_type, _PIECE_TYPE_ACTIVITY_MENU["감상곡"])
        result["activities"] = []
        for activity_type in activity_types:
            reference_activity = None
            adapted_activity = None
            for candidate in _select_typed_reference_activities(
                examples, activity_type, top["code"], top["text"], genre=genre,
            ):
                adapted_candidate = _adapt_activity_to_topic(candidate["text"], topic)
                if adapted_candidate:
                    reference_activity, adapted_activity = candidate, adapted_candidate
                    break
            if not adapted_activity:
                # 이 유형은 후보 전부가 대상 치환에 실패했다(예: 고유명사를 조사로 착각할 위험) —
                # 억지로 끼워 맞추지 않고 건너뛴다.
                continue
            genre_note = (
                f" 원고를 '{genre}' 갈래로 보고, 같은 갈래의 활동 표본을 우선 참고했습니다."
                if genre and reference_activity.get("genre") == genre else ""
            )
            result["activities"].append({
                "activity_type": activity_type,
                "current_text": None,
                "reason": (
                    f"등록된 기존 교과서의 '{activity_type}' 활동 표본 중 이 교육과정 기준과 가장 관련 있는 문장"
                    f"('{reference_activity['text']}', {reference_activity['file']} {reference_activity['page']}쪽)"
                    "의 구조를 가져와, 대상만 현재 원고 주제로 바꿨습니다."
                    f"{genre_note} 교육과정 원문을 활동 문장에 그대로 옮기지 않았습니다."
                ),
                "suggestion": _simplify_activity(adapted_activity, target_level),
                "curriculum_basis": f"[{top['code']}] {top['text']}",
                "matched_keywords": top.get("matched_keywords", []),
                "reference_example": reference_activity,
            })
        # 등록 자료에 해당 유형 예시가 없거나 대상 치환이 안전하지 않은 유형은 억지로 채우지 않아
        # 3개보다 적게(또는 0개) 나올 수 있다 — 무관한 활동을 지어내는 것보다 정직한 편을 택한다.
    return result


def _completion_score(components: dict[str, Any], alignment: dict[str, Any],
                      rendered_pages: list[dict[str, Any]], pages: list[str]) -> dict[str, Any]:
    weights = {
        "achievement_standard": 15, "learning_goal": 15, "activities": 15,
        "music_score": 20, "reference_photo": 15, "body_text": 20,
    }
    score_present = any(
        _detect_score_material(text, rendered_pages[index].get("images", [])).get("detected")
        for index, text in enumerate(pages) if index < len(rendered_pages)
    )
    photo_present = any(
        rendered_pages[index].get("images")
        and re.search(r"사진|삽화|그림|인물|작곡가|공연|출처", text)
        for index, text in enumerate(pages) if index < len(rendered_pages)
    )
    body_present = bool(_body_sentences(pages, components))
    present = {
        "achievement_standard": components["achievement_standards"]["included"],
        "learning_goal": components["learning_goals"]["included"],
        "activities": components["activities"]["included"],
        "music_score": score_present,
        "reference_photo": photo_present,
        "body_text": body_present,
    }
    earned = {key: maximum if present[key] else 0 for key, maximum in weights.items()}
    total = round(sum(earned.values()), 1)
    labels = {
        "achievement_standard": ("성취기준", "원고에 성취기준이 있습니다.", "원고에 성취기준을 찾지 못했습니다."),
        "learning_goal": ("학습 목표", "명시된 학습 목표가 있습니다.", "학습 목표를 찾지 못했습니다."),
        "activities": ("활동", "학생 수행 활동이 있습니다.", "학생 수행 활동을 찾지 못했습니다."),
        "music_score": ("악보", "악보 관련 문맥과 이미지 영역이 있습니다.", "악보가 확인되지 않았습니다."),
        "reference_photo": ("참고 사진·삽화", "사진·삽화 관련 문맥과 이미지 영역이 있습니다.", "참고 사진·삽화가 확인되지 않았습니다."),
        "body_text": ("본문 텍스트", "목표·활동과 구분되는 본문 설명문이 있습니다.", "목표·활동과 구분되는 본문 설명문을 찾지 못했습니다."),
    }
    details = [
        {"name": labels[key][0], "earned": earned[key], "maximum": weights[key],
         "reason": labels[key][1] if present[key] else labels[key][2]}
        for key in weights
    ]
    to_reach_100 = []
    for detail in details:
        missing = round(detail["maximum"] - detail["earned"], 1)
        if missing <= 0:
            continue
        actions = {
            "성취기준": "연계할 성취기준 코드와 문장을 원고의 단원 정보에 명시하고 사람이 최종 확인합니다.",
            "학습 목표": "성취기준과 연결되는 학생 행동 중심의 학습 목표를 추가합니다.",
            "활동": "학습 목표를 확인할 수 있는 학생 수행 활동을 추가합니다.",
            "악보": "제재곡 악보를 배치하고 악보 출처·마디 번호·연주 안내를 확인합니다.",
            "참고 사진·삽화": "본문 이해를 돕는 사진 또는 삽화를 출처와 함께 추가합니다.",
            "본문 텍스트": "제재의 배경·음악적 특징을 설명하는 본문 텍스트를 추가합니다.",
        }
        to_reach_100.append({
            "name": detail["name"], "missing_points": missing,
            "action": actions[detail["name"]],
            "reason": f"이 항목의 최대 {detail['maximum']}점 중 {detail['earned']}점만 반영되었습니다.",
        })
    return {
        "percentage": min(100, total),
        "breakdown": earned,
        "weights": weights,
        "details": details,
        "to_reach_100": to_reach_100,
        "explanation": f"현재 완성도는 {total:.1f}%입니다. 6개 평가 항목의 획득 점수를 합산했으며 미충족 항목은 자동으로 감점했습니다.",
        "note": "성취기준·학습 목표·활동·악보·참고 사진·본문 텍스트의 유무만 합산한 편집 구성 점검 지표이며 교육적 품질의 절대 평가가 아닙니다.",
    }


_ACTIVITY_EXAMPLE_GENRE_CAP = 15


def _cap_examples_by_genre(candidates: list[dict[str, Any]], genre: str | None,
                           bucket_counts: Counter[str], cap: int) -> list[dict[str, Any]]:
    """한 파일에서 뽑은 활동 후보에 장르를 태그하고, 장르별 상한까지만 채택한다.

    한 파일(단원)이 전체 상한을 독점해 다른 장르가 0개로 밀리는 문제를 막는다.
    """
    bucket = genre or "일반"
    accepted = []
    for candidate in candidates:
        if bucket_counts[bucket] >= cap:
            break
        accepted.append({**candidate, "genre": genre})
        bucket_counts[bucket] += 1
    return accepted


def _analyze_layout_references(textbook_dir: Path | None, cache_path: Path) -> dict[str, Any]:
    """등록된 기존 교과서 전 쪽의 구성 평균을 분석하고 캐시한다."""
    if not textbook_dir or not textbook_dir.exists():
        return {"file_count": 0, "sampled_pages": 0, "patterns": {}, "sources": [],
                "note": "참고 교과서 폴더가 없어 일반 A4 교재 원칙을 사용했습니다."}
    files = sorted(textbook_dir.rglob("*.pdf"))
    fingerprint = [{"path": str(path.resolve()), "size": path.stat().st_size,
                    "mtime": path.stat().st_mtime_ns} for path in files]
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("version") == 8 and cached.get("fingerprint") == fingerprint:
                return cached["analysis"]
        except (OSError, json.JSONDecodeError):
            pass
    import pymupdf  # type: ignore
    pattern_counts: Counter[str] = Counter()
    examples: list[dict[str, Any]] = []
    activity_lengths: list[int] = []
    activity_examples: list[dict[str, Any]] = []
    genre_bucket_counts: Counter[str] = Counter()
    sampled_pages = 0
    image_ratios: list[float] = []
    text_lengths: list[int] = []
    table_counts: list[int] = []
    pages_with_images = 0
    for path in files:
        try:
            file_page_texts: list[str] = []
            file_activity_candidates: list[dict[str, Any]] = []
            with pymupdf.open(str(path)) as document:
                for index, page in enumerate(document):
                    page_area = max(1.0, float(page.rect.width) * float(page.rect.height))
                    image_rects = []
                    for image in page.get_images(full=True):
                        try:
                            image_rects.extend(page.get_image_rects(image[0]))
                        except Exception:
                            continue
                    image_area = sum(max(0.0, rect.width) * max(0.0, rect.height)
                                     for rect in image_rects) / page_area
                    # PDF 선 객체의 묶음 수로 표 영역을 보수적으로 추정한다.
                    try:
                        drawing_count = len(page.get_drawings())
                    except Exception:
                        drawing_count = 0
                    table_count = min(3, drawing_count // 12)
                    page_text = page.get_text("text") or ""
                    text_length = len(page_text)
                    file_page_texts.append(page_text)
                    image_ratios.append(image_area)
                    text_lengths.append(text_length)
                    table_counts.append(table_count)
                    if image_rects:
                        pages_with_images += 1
                    for line in page_text.splitlines():
                        candidate = re.sub(r"[\x00-\x1f\x7f]", "", line)
                        candidate = re.sub(r"\s+", " ", candidate).strip()
                        if (8 <= len(candidate) <= 140
                                and (re.match(r"^\d{1,2}[.)]\s*", candidate)
                                     or re.search(r"(?:해|적어|설명|비교|토의|토론|감상|작성|발표).{0,12}보자", candidate))):
                            activity_lengths.append(len(re.sub(r"\s+", "", candidate)))
                            file_activity_candidates.append({"file": path.name, "page": index + 1, "text": candidate})
                    has_score_terms = bool(re.search(r"악보|보표|오선|음표|마디|채보", page_text))
                    if table_count >= 2:
                        pattern = "activity_table"
                    elif image_area >= .38 and has_score_terms:
                        pattern = "score_visual"
                    elif image_area >= .38:
                        pattern = "visual_centered"
                    elif text_length >= 900:
                        pattern = "text_explanation"
                    else:
                        pattern = "mixed_content"
                    pattern_counts[pattern] += 1
                    sampled_pages += 1
                    if len([e for e in examples if e["pattern"] == pattern]) < 3:
                        examples.append({"file": path.name, "page": index + 1, "pattern": pattern})
                file_genre = _infer_genre("\n".join(file_page_texts))
                activity_examples.extend(_cap_examples_by_genre(
                    file_activity_candidates, file_genre, genre_bucket_counts, _ACTIVITY_EXAMPLE_GENRE_CAP,
                ))
        except Exception:
            continue
    ordered_lengths = sorted(activity_lengths)
    average_length = round(sum(ordered_lengths) / len(ordered_lengths), 1) if ordered_lengths else 35.0
    p75_length = ordered_lengths[round((len(ordered_lengths) - 1) * .75)] if ordered_lengths else 50
    dominant_pattern = pattern_counts.most_common(1)[0][0] if pattern_counts else None
    analysis = {
        "file_count": len(files), "sampled_pages": sampled_pages,
        "patterns": dict(pattern_counts), "sources": [path.name for path in files],
        "average_metrics": {
            "image_area_percent": round(sum(image_ratios) / max(1, len(image_ratios)) * 100, 1),
            "text_characters": round(sum(text_lengths) / max(1, len(text_lengths)), 1),
            "tables_per_page": round(sum(table_counts) / max(1, len(table_counts)), 2),
            "pages_with_images_percent": round(pages_with_images / max(1, sampled_pages) * 100, 1),
            "dominant_pattern": dominant_pattern,
        },
        "examples": examples,
        "activity_readability": {
            "sample_count": len(ordered_lengths), "average_length": average_length,
            "p75_length": p75_length, "examples": activity_examples,
            "genre_counts": dict(genre_bucket_counts),
        },
        "note": "등록된 기존 교과서의 모든 쪽을 분석해 이미지·텍스트 평균과 PDF 선 객체 기반 표 영역 추정값을 계산했습니다. PDF가 바뀌지 않으면 저장된 분석값을 재사용합니다.",
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"version": 8, "fingerprint": fingerprint, "analysis": analysis},
                                     ensure_ascii=False, indent=2), encoding="utf-8")
    return analysis


def _simplify_activity(text: str, target_level: str = "고등학교 1학년") -> str:
    simplified = re.sub(r"^\d{1,2}[.)]\s*", "", text).strip()
    compact = re.sub(r"\s+", " ", simplified)
    # 교과서 활동에서 자연스러운 '개념 확인 → 집중 감상' 순서를 우선한다.
    if ("기타" in compact and "음색" in compact and "감상" in compact
            and re.search(r"포크\s*송", compact) and "특징" in compact
            and re.search(r"조사|알아보", compact)):
        return "포크 송의 특징을 알아보고, 기타 음색에 집중하며 감상해 보자."
    # 비교 대상과 사회·문화적 탐구 방법, 모둠 결과를 차례로 드러낸다.
    if ("원곡" in compact and re.search(r"포크\s*송", compact)
            and "청년 문화" in compact and re.search(r"토론|토의", compact)):
        return "원곡과 비교 감상하고, 포크 송과 청년 문화의 관련성을 모둠별로 조사하여 토의해 보자."
    replacements = {
        "음악 요소별로 비교·분석": "악기와 장단을 중심으로 같은 점과 다른 점을 비교",
        "음악 요소": "악기, 장단, 선율",
        "비교·분석": "같은 점과 다른 점을 비교",
        "구체적인 음악적 근거와 함께 설명": "듣고 찾은 내용을 한두 문장으로 설명",
        "악곡 구성 원리": "곡의 짜임",
        "가사를 낭독해보고 떠오르는 대상에게 곡에 대한 소개를 편지 형식으로 작성해 보자.":
            "가사를 소리 내어 읽고, 떠오르는 사람에게 이 곡을 소개하는 편지를 써 보자.",
        "기타 음색이 주는 분위기를 파악하며 감상하고 포크송의 특징을 조사해보자.":
            "포크 송의 특징을 알아보고, 기타 음색에 집중하며 감상해 보자.",
        "악곡의 원곡을 감상해 보고, 포크송과 청년 문화와의 관련성을 토론해 보자.":
            "원곡과 비교 감상하고, 포크 송과 청년 문화의 관련성을 모둠별로 조사하여 토의해 보자.",
    }
    for source, target in replacements.items():
        simplified = simplified.replace(source, target)
    if "장단을 학습하고, 음원에 맞춰" in simplified:
        simplified = simplified.replace("장단을 학습하고, 음원에 맞춰", "장단을 익힌 뒤, 음원에 맞춰")
    simplified = re.sub(r"(이야기|조사|감상|낭독)해보자", r"\1해 보자", simplified)
    simplified = simplified.replace("낭독해 보고", "소리 내어 읽고")
    simplified = re.sub(r"포크\s*송", "포크 송", simplified)
    if target_level.startswith("초등학교"):
        simplified = simplified.replace("파악", "찾아보기").replace("관련성", "어떤 관계가 있는지")
        simplified = simplified.replace("토론해 보자", "친구들과 이야기해 보자")
    elif target_level == "중학교":
        simplified = simplified.replace("관련성을 토론해 보자", "어떤 관련이 있는지 이야기해 보자")
    return simplified


def _evaluate_activity_level(component: dict[str, Any], reference: dict[str, Any],
                             target_level: str = "고등학교 1학년") -> dict[str, Any]:
    readability = reference.get("activity_readability", {})
    reference_average = float(readability.get("average_length", 35))
    reference_p75 = int(readability.get("p75_length", 50))
    profile = TARGET_LEVELS.get(target_level, TARGET_LEVELS["고등학교 1학년"])
    if profile["length_limit"] is None:
        length_limit = reference_p75 + (18 if target_level == "고등학교 2·3학년" else 10)
    else:
        length_limit = int(profile["length_limit"])
    technical_terms = (
        "비교·분석", "음악 요소", "악곡 구성", "사회·문화적", "시대적 맥락",
        "미적 특성", "음악적 근거", "관점", "비평", "내재적",
    )
    items = []
    for activity in component["items"]:
        text = activity["text"]
        compact_length = len(re.sub(r"\s+", "", re.sub(r"^\d{1,2}[.)]\s*", "", text)))
        found_terms = [term for term in technical_terms if term in text]
        steps = max(1, len(re.findall(r"(?:하고|하며|하여|한 뒤|맞춰|그리고|,)", text)) + 1)
        difficulty = 0
        reasons = []
        if compact_length > length_limit:
            difficulty += 1
            reasons.append(f"문장 길이 {compact_length}자가 {target_level} 권장 기준 {length_limit}자보다 깁니다.")
        else:
            reasons.append(f"문장 길이 {compact_length}자는 {target_level} 권장 기준 {length_limit}자 이내입니다.")
        if len(found_terms) > int(profile["max_terms"]):
            difficulty += 1
            reasons.append(f"전문·추상 용어가 {len(found_terms)}개 있어 권장 수 {profile['max_terms']}개를 넘습니다: " + ", ".join(found_terms))
        elif found_terms:
            reasons.append("설명이 필요한 용어가 포함되어 있습니다: " + ", ".join(found_terms))
        if steps > int(profile["max_steps"]):
            difficulty += 1
            reasons.append(f"수행 단계가 약 {steps}개로 {target_level} 권장 수 {profile['max_steps']}개보다 많습니다.")
        status = "적절" if difficulty == 0 else ("조금 어려움" if difficulty == 1 else "어려움")
        simplified = _simplify_activity(text, target_level)
        examples = readability.get("examples", [])
        closest_example = max(
            examples, key=lambda example: similarity(text, example["text"]), default=None
        )
        items.append({
            "page": activity["page"], "number": activity.get("number"), "text": text,
            "status": status, "reasons": reasons,
            "recommended_text": simplified if status != "적절" or simplified != re.sub(r"^\d{1,2}[.)]\s*", "", text).strip() else None,
            "reference_example": closest_example,
            "metrics": {"length": compact_length, "steps": steps, "technical_terms": found_terms},
        })
    difficult_count = sum(item["status"] != "적절" for item in items)
    return {
        "grade": target_level, "items": items,
        "overall": "적절" if not difficult_count else ("일부 보완 권장" if difficult_count < len(items) else "보완 권장"),
        "reference": {
            "sample_count": readability.get("sample_count", 0),
            "average_length": reference_average, "p75_length": reference_p75,
            "level_length_limit": length_limit,
            "level_max_steps": profile["max_steps"], "level_max_terms": profile["max_terms"],
        },
        "note": "기존 교과서 활동 문장 표본을 바탕으로 선택 학년의 문장 길이, 수행 단계 수, 전문·추상 용어 수를 적용한 보조 판정입니다.",
    }


def _evaluate_activity_curriculum(activities: dict[str, Any], learning_goals: dict[str, Any],
                                  standards: list[dict[str, Any]]) -> dict[str, Any]:
    """활동별로 교육과정 성취기준과 학습 목표의 연결 정도를 점검한다."""
    goals = [item["text"] for item in learning_goals.get("items", [])]
    items = []
    for activity in activities.get("items", []):
        components = {
            "achievement_standards": {"items": []},
            "learning_goals": {"items": []},
            "activities": {"items": [activity]},
        }
        alignment = match_curriculum(components, [activity["text"]], standards)
        match = alignment["top_matches"][0] if alignment["top_matches"] else None
        standard_score = float(match["score"]) if match else 0.0
        goal_score = max((similarity(activity["text"], goal) for goal in goals), default=0.0)
        if standard_score >= .42:
            status = "알맞음"
            reason = "활동의 수행 동사와 핵심 내용이 성취기준과 충분히 연결됩니다."
        elif standard_score >= .24 or goal_score >= .28:
            status = "부분 알맞음"
            reason = "학습 방향은 연결되지만 성취기준의 핵심 개념이나 결과물이 더 분명해야 합니다."
        else:
            status = "확인 필요"
            reason = "활동 문장만으로는 성취기준과의 직접적인 연결 근거가 충분하지 않습니다."
        items.append({
            "page": activity["page"], "number": activity.get("number"),
            "text": activity["text"], "status": status,
            "standard_score": round(standard_score, 4),
            "learning_goal_score": round(goal_score, 4),
            "matched_standard": ({
                "code": match["code"], "text": match["text"], "page": match["page"],
                "matched_keywords": match.get("matched_keywords", []),
            } if match else None),
            "reason": reason,
            "review_required": status != "알맞음",
        })
    review_count = sum(item["review_required"] for item in items)
    return {
        "items": items,
        "overall": "알맞음" if not review_count else ("일부 확인 필요" if review_count < len(items) else "확인 필요"),
        "note": "활동 문장과 학습 목표·교육과정 성취기준의 문자 유사도 및 핵심 수행 동사를 비교한 보조 판정입니다. 교육과정 원문은 수정하지 않습니다.",
    }


def _find_related_work(page_text: str, learning_goal_texts: list[str],
                       related_works: dict[str, str]) -> tuple[str, str] | None:
    """사용자가 등록한 관련 자료(리메이크·다른 버전 등)의 키워드가 이 페이지에 등장하는지 찾는다."""
    haystack = page_text + " " + " ".join(learning_goal_texts)
    for keyword, note in related_works.items():
        if keyword and keyword in haystack:
            return keyword, note
    return None


def _generate_activity_ideas(page_text: str, page_components: dict[str, Any],
                             standards: list[dict[str, Any]],
                             related_works: dict[str, str] | None,
                             target_level: str) -> dict[str, Any]:
    """등록된 관련 자료가 있고, 이 페이지가 아직 다루지 않은 '비교' 성취기준이 있으면 새 활동을 제안한다.

    리메이크·다른 버전처럼 원고 밖의 사실은 자동으로 알아낼 수 없으므로
    related_works에 사용자가 직접 등록한 항목이 있을 때만 동작한다. 현재 활동이
    이미 다른 성취기준(예: 미적 감상)에 알맞게 연결되어 있어도, 그 판정과 별개로
    이 페이지에서 아직 다루지 않은 '비교' 성취기준을 등록된 자료로 채울 수 있는지 확인한다.
    """
    items: list[dict[str, Any]] = []
    activities = page_components["activities"]["items"]
    if related_works and activities:
        learning_goal_texts = [item["text"] for item in page_components["learning_goals"]["items"]]
        found = _find_related_work(page_text, learning_goal_texts, related_works)
        if found and found[1]:
            keyword, note = found
            signals = learning_goal_texts + [activity["text"] for activity in activities]
            for standard in standards:
                if "비교" not in STANDARD_SIGNATURES.get(standard["code"], ()):
                    continue
                best_score = 0.0
                for signal in signals:
                    lexical_score = similarity(signal, standard["text"])
                    signature_score, _ = _signature_similarity(standard["code"], signal)
                    best_score = max(best_score, lexical_score, signature_score * .65 + lexical_score * .35)
                if best_score >= .24:
                    continue  # 이 페이지의 목표·활동이 이미 이 성취기준을 어느 정도 다루고 있음
                suggestion = _simplify_activity(
                    f"{keyword}를 감상하고, {note}과 비교하여 같은 점과 다른 점을 이야기해 보자.",
                    target_level,
                )
                items.append({
                    "page": activities[0]["page"], "number": activities[0].get("number"),
                    "current_text": activities[0]["text"], "suggestion": suggestion,
                    "curriculum_basis": f"[{standard['code']}] {standard['text']}",
                    "related_work": {"keyword": keyword, "note": note},
                    "reason": (
                        f"이 페이지의 활동이 아직 [{standard['code']}]의 '비교' 성취 요소를 다루지 않아, "
                        f"등록한 관련 자료({note})를 활용해 비교하는 활동으로 구체화했습니다."
                    ),
                })
    return {
        "items": items,
        "note": "사용자가 등록한 관련 자료(리메이크·다른 버전 등)가 있고, 이 페이지의 활동이 아직 다루지 않은 '비교' 성취기준이 있을 때만 생성되는 보조 제안입니다.",
    }


def _exact_work_title_match(title: str, text: str) -> bool:
    """짧은 제목이 더 긴 작품명·일반 단어 안에 포함된 경우를 제외한다."""
    normalized_title = _normalize_for_similarity(title)
    if not normalized_title:
        return False
    quoted_ranges = []
    for match in re.finditer(r"[‘'\"“〈《「『](.{1,80}?)[’'\"”〉》」』]", text):
        quoted_ranges.append((match.start(), match.end()))
        if _normalize_for_similarity(match.group(1)) == normalized_title:
            return True
    # 일치하지 않는 인용 제목 안의 부분 문자열은 검색 대상에서 제거한다.
    plain = list(text)
    for start, end in quoted_ranges:
        plain[start:end] = " " * (end - start)
    plain_text = "".join(plain)
    compact_line = _normalize_for_similarity(plain_text)
    if compact_line == normalized_title:
        return True
    spaced_title = r"\s*".join(re.escape(part) for part in re.split(r"\s+", title.strip()))
    particles = r"(?:을|를|이|가|은|는|의)?"
    title_pattern = rf"(?<![가-힣A-Za-z0-9]){spaced_title}{particles}(?![가-힣A-Za-z0-9])"
    # 인용 부호가 없으면 곡명 표지나 작사·작곡 정보와 직접 붙은 독립 제목만 인정한다.
    return bool(
        re.search(rf"(?:곡명|제재곡|감상곡|수록곡)\s*[:：]?\s*{title_pattern}", plain_text)
        or re.search(rf"{title_pattern}\s*(?:작사|작곡|노래|원곡)", plain_text)
        or (len(normalized_title) >= 4 and re.search(title_pattern, plain_text))
    )


def _check_repertoire_overlap(page_text: str, learning_goal_texts: list[str],
                              related_works: dict[str, str] | None,
                              textbook_chunks: list[dict[str, Any]],
                              declared_titles: list[str] | None = None) -> dict[str, Any]:
    """입력 또는 보수적으로 추출한 정확한 곡명만 기존 교과서에서 찾는다."""
    items: list[dict[str, Any]] = []
    keywords = [re.sub(r"\s+", " ", value).strip() for value in (declared_titles or []) if value.strip()]
    source_label = "메인 화면에서 입력한 곡명" if keywords else "원고에서 자동 추출한 곡명"
    if not keywords:
        keywords = _extract_work_titles(page_text)
    if related_works and not declared_titles:
        found = _find_related_work(page_text, learning_goal_texts, related_works)
        if found:
            keywords.insert(0, found[0])
    keywords = list(dict.fromkeys(keywords))
    for keyword in keywords:
        matches: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for chunk in textbook_chunks:
            exact = _exact_work_title_match(keyword, chunk["text"])
            if exact and (chunk["file"], chunk["page"]) not in seen:
                seen.add((chunk["file"], chunk["page"]))
                matches.append({
                    "file": chunk["file"], "page": chunk["page"], "text": chunk["text"],
                    "match_type": "정확한 곡명 일치", "score": 1.0,
                })
        # '같은 곡 없음' 후보는 곡명 오인식까지 길게 노출하므로 결과 화면에 싣지 않는다.
        if matches:
            items.append({
                "keyword": keyword,
                "source": source_label,
                "found_in_existing_textbooks": True,
                "status": "같은 곡 있음",
                "matches": matches[:5],
                "reason": f"기존 교과서에서 곡명 '{keyword}'이 정확히 표기된 쪽을 {len(matches)}건 찾았습니다.",
            })
    return {
        "items": items,
        "checked_title_count": len(keywords),
        "checked": bool(keywords),
        "note": "장르명과 유사 문장은 사용하지 않고, 입력하거나 원고에서 추출한 곡명이 정확히 일치할 때만 표시합니다.",
    }


def _compare_activity_similarity(pages: list[str], components: dict[str, Any],
                                 reference: dict[str, Any],
                                 suppressed_fingerprints: set[str] | None = None) -> dict[str, Any]:
    """곡·장르·수행 방식 중심으로 기존 교과서 활동을 비교한다."""
    suppressed_fingerprints = suppressed_fingerprints or set()
    chunks = reference.get("chunks", [])
    page_items: dict[int, list[dict[str, Any]]] = {page_no: [] for page_no in range(1, len(pages) + 1)}
    max_score = 0.0
    similar_count = 0
    for activity in components["activities"]["items"]:
        best = None
        best_score = 0.0
        best_basis: dict[str, list[str]] = {}
        for candidate in chunks:
            score, basis = _activity_semantic_similarity(activity["text"], candidate["text"])
            if score > best_score:
                best, best_score, best_basis = candidate, score, basis
        max_score = max(max_score, best_score)
        if best_score >= .72:
            status = "매우 유사"
            similar_count += 1
        elif best_score >= .50:
            status = "유사"
            similar_count += 1
        elif best_score >= .32:
            status = "부분 유사"
        else:
            status = "유사도 낮음"
        if best_score >= .72:
            verdict, interpretation = "핵심 활동이 매우 유사함", "같은 곡·장르·수행 방식이 다수 겹칩니다."
        elif best_score >= .50:
            verdict, interpretation = "핵심 활동이 유사함", "곡, 장르 또는 수행 방식의 중심 내용이 상당 부분 겹칩니다."
        elif best_score >= .32:
            verdict, interpretation = "일부 핵심 요소만 유사함", "일부 중심 요소만 같으며 문장 종결 표현은 판단에 사용하지 않았습니다."
        else:
            verdict, interpretation = "핵심 활동이 유사하지 않음", "같은 곡·장르·수행 방식의 결합이 충분히 겹치지 않습니다."
        # 전문 로직(같은 곡·장르·악기 계열)은 그대로 두되, 정확히 일치하는 핵심어가
        # 3개 이상일 때만 화면에 노출한다.
        if len(best_basis.get("shared_keywords", [])) < 3:
            continue
        match = ({"file": best["file"], "page": best["page"], "text": best["text"]}
                 if best and best_score >= .32 else None)
        fingerprint = _fingerprint(
            "activity_similarity", activity["text"],
            (match or {}).get("file", ""), str((match or {}).get("page", "")),
        )
        page_items[activity["page"]].append({
            "manuscript_text": activity["text"], "status": status,
            "score": round(best_score, 4),
            "match": match,
            "review_required": best_score >= .50,
            "verdict": verdict, "interpretation": interpretation,
            "comparison_focus": "같은 곡 · 같은 장르 · 같은 악기 계열 · 실제 수행 방식",
            **best_basis,
            "fingerprint": fingerprint,
            "is_false_positive": fingerprint in suppressed_fingerprints,
        })
    reviewed = len(components["activities"]["items"])
    return {
        "summary": {"reviewed_activities": reviewed, "similar_count": similar_count,
                    "maximum_score": round(max_score, 4)},
        "pages": page_items,
        "reference": {"file_count": reference.get("file_count", 0),
                      "chunk_count": reference.get("chunk_count", 0)},
        "note": "정확히 일치하는 핵심어가 3개 이상 겹치는 활동만 표시합니다. 같은 곡·장르·악기 계열 판정은 참고 정보로 함께 보여줍니다.",
    }


def _load_textbook_content(textbook_dir: Path | None, cache_path: Path) -> dict[str, Any]:
    """기존 교과서의 설명 문장을 추출해 내용 유사도 비교용으로 캐시한다."""
    files = sorted(textbook_dir.glob("*.pdf")) if textbook_dir and textbook_dir.exists() else []
    fingerprint = [
        {"name": path.name, "size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}
        for path in files
    ]
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("version") == 3 and cached.get("fingerprint") == fingerprint:
                return cached["analysis"]
        except (OSError, ValueError, KeyError):
            pass

    chunks = []
    title_chunks = []
    for path in files:
        try:
            import pymupdf  # type: ignore
            with pymupdf.open(str(path)) as document:
                pages = [(index + 1, page.get_text("text") or "") for index, page in enumerate(document)]
        except Exception:
            try:
                pages = list(enumerate(_extract_pages(path), 1))
            except Exception:
                continue
        for page_no, raw in pages:
            for line in raw.splitlines():
                title_line = re.sub(r"\s+", " ", line).strip(" ·-–—")
                if 2 <= len(title_line) <= 160 and re.search(r"[가-힣]", title_line):
                    title_chunks.append({"file": path.name, "page": page_no, "text": title_line})
            for candidate in re.split(r"(?<=[.!?])\s+|[\r\n]+", raw):
                sentence = re.sub(r"\s+", " ", candidate).strip(" ·-–—")
                if 20 <= len(sentence) <= 300 and re.search(r"[가-힣]", sentence):
                    chunks.append({"file": path.name, "page": page_no, "text": sentence})

    learning_goal_examples = []
    seen_goals: set[str] = set()
    for chunk in chunks:
        goal = re.sub(r"\s+", " ", chunk["text"]).strip()
        normalized_goal = _normalize_for_similarity(goal)
        if (18 <= len(goal) <= 180 and re.search(r"수 있다[.!]?$|수 있도록 한다[.!]?$", goal)
                and "?" not in goal and normalized_goal not in seen_goals):
            seen_goals.add(normalized_goal)
            learning_goal_examples.append({
                "file": chunk["file"], "page": chunk["page"], "text": goal,
            })

    analysis = {
        "file_count": len(files), "chunk_count": len(chunks), "chunks": chunks,
        "title_chunks": title_chunks,
        "learning_goal_examples": learning_goal_examples,
        "note": "기존 교과서의 텍스트층에서 추출한 설명 문장을 원고 본문과 비교합니다.",
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"version": 3, "fingerprint": fingerprint, "analysis": analysis}, ensure_ascii=False),
        encoding="utf-8",
    )
    return analysis


def _compare_textbook_similarity(pages: list[str], components: dict[str, Any],
                                 reference: dict[str, Any],
                                 suppressed_fingerprints: set[str] | None = None) -> dict[str, Any]:
    """정확히 일치하는 핵심어가 세 개 이상 겹치는 본문 문장만 표시한다."""
    suppressed_fingerprints = suppressed_fingerprints or set()
    chunks = reference.get("chunks", [])
    page_items: dict[int, list[dict[str, Any]]] = {page_no: [] for page_no in range(1, len(pages) + 1)}
    max_score = 0.0
    similar_count = 0
    source_sentences = _body_sentences(pages, components)
    for source in source_sentences:
        best = None
        best_score = 0.0
        best_basis: dict[str, Any] | None = None
        for candidate in chunks:
            basis = _content_overlap_basis(source["text"], candidate["text"])
            if len(basis["shared_keywords"]) < 3:
                continue
            lexical_score = similarity(source["text"], candidate["text"])
            semantic_score = min(.85, .18 + basis["semantic_count"] * .18)
            score = max(lexical_score, semantic_score)
            if score > best_score:
                best, best_score, best_basis = candidate, score, basis
        if not best or not best_basis:
            continue
        max_score = max(max_score, best_score)
        if best_score >= .72:
            status = "매우 유사"
            similar_count += 1
        elif best_score >= .50:
            status = "유사"
            similar_count += 1
        elif best_score >= .32:
            status = "부분 유사"
        else:
            status = "유사도 낮음"
        explanation = _similarity_explanation(
            source["text"], best["text"] if best else "", best_score, "content"
        )
        match = ({"file": best["file"], "page": best["page"], "text": best["text"]}
                 if best and best_score >= .32 else None)
        fingerprint = _fingerprint(
            "textbook_similarity", source["text"],
            (match or {}).get("file", ""), str((match or {}).get("page", "")),
        )
        page_items[source["page"]].append({
            "manuscript_text": source["text"], "status": status,
            "score": round(best_score, 4),
            "match": match,
            "review_required": best_score >= .50,
            **explanation,
            "shared_keywords": best_basis["shared_keywords"],
            "shared_meanings": best_basis["shared_meanings"],
            "semantic_evidence": best_basis["semantic_evidence"],
            "fingerprint": fingerprint,
            "is_false_positive": fingerprint in suppressed_fingerprints,
        })
    return {
        "summary": {"reviewed_sentences": len(source_sentences), "similar_count": similar_count,
                    "maximum_score": round(max_score, 4)},
        "pages": page_items,
        "reference": {"file_count": reference.get("file_count", 0),
                      "chunk_count": reference.get("chunk_count", 0)},
        "note": "정확히 일치하는 핵심어가 3개 이상 겹치는 문장만 표시합니다.",
    }


def _detect_score_material(page_text: str, images: int | list[dict[str, Any]]) -> dict[str, Any]:
    image_items = images if isinstance(images, list) else []
    image_count = len(image_items) if isinstance(images, list) else int(images)
    score_terms = sorted(set(re.findall(
        r"악보|보표|오선|음표|쉼표|마디|조표|박자표|계이름|채보|선율|장단|반주|노래 부르|연주",
        page_text,
    )))
    wide_images = 0
    for item in image_items:
        bbox = item.get("bbox") or []
        if len(bbox) == 4 and (bbox[2] - bbox[0]) >= 250 and (bbox[3] - bbox[1]) >= 80:
            wide_images += 1
    # '악보'처럼 실제 악보를 직접 가리키는 말이 있거나, 넓은 이미지와 음악 관련 용어가 함께 있을 때만
    # 악보가 실린 것으로 판단한다. '음표'·'마디'는 리듬을 설명하는 일반 문장에도 흔히 쓰여
    # 그 자체만으로는(넓은 이미지 없이는) 악보 존재의 근거로 보지 않는다.
    detected = bool(re.search(r"악보|보표|오선|채보", page_text)) or bool(wide_images and score_terms)
    possible = not detected and image_count > 0 and bool(re.search(r"곡|음악|감상|연주|노래", page_text))
    return {
        "detected": detected,
        "possible": possible,
        "status": "악보 있음" if detected else ("악보 가능성 있음·확인 필요" if possible else "악보 근거 없음"),
        "image_count": image_count,
        "wide_image_count": wide_images,
        "text_signals": score_terms,
        "basis": (
            "악보 관련 용어와 페이지의 이미지 영역을 함께 확인했습니다."
            if detected else
            "이미지 영역과 음악 관련 문맥은 있으나 악보라고 확정할 텍스트 근거가 부족합니다."
            if possible else
            "악보 관련 용어와 이미지 영역이 감지되지 않았습니다."
        ),
    }


def _recommend_layout(page_no: int, page_text: str, page_components: dict[str, Any],
                      image_count: int | list[dict[str, Any]], reference: dict[str, Any],
                      spread_context: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    activity_count = page_components["activities"]["count"]
    score_check = _detect_score_material(page_text, image_count)
    actual_image_count = score_check["image_count"]
    # '가능성 있음'만으로는 악보 중심 레이아웃을 강제하지 않는다. 음표·오선이 보이는
    # 실제 악보 이미지가 확인됐을 때(detected)만 악보용 넓은 영역을 배정한다.
    has_score_signal = score_check["detected"]
    if has_score_signal:
        layout_type, title = "score_centered", "악보·이미지 중심 A4 레이아웃"
        zones = [
            {"label": "단원 제목", "secondary": "학습 목표", "height": 10, "style": "header"},
            {"label": "악보(크기·마디 번호·연주 안내)", "height": 36, "style": "score"},
            {"label": "삽화·공연 이미지", "height": 18, "style": "media"},
            {"label": "감상·연주 활동", "height": 22, "style": "activity"},
            {"label": "핵심 정리", "height": 10, "style": "summary"},
        ]
        reasons = [
            f"{page_no}쪽에서 {score_check['status']}으로 판정했습니다. 이미지 영역은 {actual_image_count}개이며, 악보 신호는 {', '.join(score_check['text_signals']) or '텍스트 근거 부족'}입니다.",
            "악보는 가독성을 위해 가장 넓은 영역을 배정하고, 마디 번호·연주 순서·참고 음원을 악보 가까이에 둡니다.",
            "캡션과 출처는 교과서 뒷부분에 별도로 모아 싣는 정책에 맞춰 본문 쪽 배치에는 넣지 않습니다.",
        ]
    elif activity_count >= 2:
        layout_type, title = "activity_sequence", "활동 순서형 A4 레이아웃"
        zones = [
            {"label": "쪽 제목·활동 목표", "height": 10, "style": "header"},
            {"label": "활동 1 · 감상 및 자료 확인", "height": 20, "style": "activity"},
            {"label": "활동 2 · 비교·분석 표", "height": 28, "style": "table"},
            {"label": "활동 3 · 적용·연주", "height": 22, "style": "activity"},
            {"label": "자기 평가·정리", "height": 12, "style": "assessment"},
        ]
        reasons = [
            f"현재 {page_no}쪽에는 번호 활동이 {activity_count}개 있어 활동별 수행 공간을 분리하는 편이 읽기 순서가 명확합니다.",
            "비교 활동은 표 형태의 넓은 중앙 영역을 사용하고, 적용·연주 활동은 별도 하단 영역으로 분리하면 결과물을 작성하기 쉽습니다.",
            "기존 교과서 활동면에서 상단 지시문, 중앙 활동 영역, 하단 평가 박스를 반복하는 패턴을 참고했습니다.",
        ]
    else:
        layout_type, title = "mixed_content", "본문·활동 혼합형 A4 레이아웃"
        zones = [
            {"label": "쪽 제목·학습 목표", "height": 10, "style": "header"},
            {"label": "핵심 본문", "secondary": "보충 설명", "height": 32, "style": "split"},
            {"label": "자료·사례", "height": 25, "style": "media"},
            {"label": "확인 활동", "height": 20, "style": "activity"},
            {"label": "핵심 정리", "height": 10, "style": "summary"},
        ]
        reasons = [
            "본문과 확인 활동을 한 페이지에서 처리하되 내용 설명과 학생 수행 영역을 시각적으로 분리합니다.",
            "기존 교과서의 혼합형 페이지에서 본문, 자료, 활동 순으로 내려가는 읽기 흐름을 참고했습니다.",
        ]
    patterns = reference.get("patterns", {})
    averages = reference.get("average_metrics", {})
    reference_summary = (
        f"기존 교과서 {reference.get('file_count', 0)}개 파일의 전체 {reference.get('sampled_pages', 0)}쪽 평균: "
        f"이미지 면적 {averages.get('image_area_percent', 0)}%, 이미지 포함 쪽 {averages.get('pages_with_images_percent', 0)}%, "
        f"표 {averages.get('tables_per_page', 0)}개/쪽, 본문 {averages.get('text_characters', 0)}자/쪽. "
        + "구성 유형: " + ", ".join(f"{key} {value}쪽" for key, value in sorted(patterns.items()))
        if reference.get("sampled_pages") else reference.get("note", "참고 자료 없음")
    )
    relevant_pattern = "activity_table" if layout_type == "activity_sequence" else (
        "score_visual" if layout_type == "score_centered" else "mixed_content"
    )
    examples = [item for item in reference.get("examples", []) if item["pattern"] == relevant_pattern][:3]
    result = {
        "type": layout_type, "title": title, "zones": zones, "reasons": reasons,
        "reference_summary": reference_summary, "reference_examples": examples,
        "page_size": "A4 210×297mm", "margin": "상하 18mm, 좌우 18mm",
        "score_check": score_check,
        "note": "기존 교과서의 구성 원리를 참고하되 동일한 디자인을 복제하지 않고 원고 내용에 맞게 재구성한 추천안입니다.",
    }
    if spread_context and len(spread_context) > 1:
        # 실제 책이 두 쪽씩 펼쳐지는 순서(1·2쪽, 3·4쪽, ...)에 맞춰, 현재 쪽이 속한 펼침면만 본다.
        spread_start = ((page_no - 1) // 2) * 2 + 1
        window = [item for item in spread_context if spread_start <= item["page"] <= spread_start + 1]
        spread_pages = []
        for context in window:
            single = _recommend_layout(
                context["page"], context["text"], context["components"],
                context["images"], reference,
            )
            spread_pages.append({
                "page": context["page"], "title": single["title"],
                "zones": single["zones"], "score_check": single["score_check"],
            })
        score_pages = [str(item["page"]) for item in spread_pages if item["score_check"]["detected"]]
        identical_layout = len(spread_pages) > 1 and all(
            item["title"] == spread_pages[0]["title"] and item["zones"] == spread_pages[0]["zones"]
            for item in spread_pages[1:]
        )
        if identical_layout:
            page_numbers = "·".join(str(item["page"]) for item in spread_pages)
            spread_pages[0]["page_label"] = f"{page_numbers}쪽 공통"
            display_pages = [spread_pages[0]]
            spread_title = "공통 레이아웃 1안"
        else:
            for item in spread_pages:
                item["page_label"] = f"{item['page']}쪽"
            display_pages = spread_pages
            spread_title = f"{len(spread_pages)}쪽 펼침 추천"
        result["spread"] = {
            "page_count": len(display_pages), "source_page_count": len(spread_pages), "pages": display_pages,
            "title": spread_title,
            "score_distribution": (
                f"악보 감지 쪽: {', '.join(score_pages)}쪽. 악보가 길면 두 쪽에 나누되 마디 중간을 피하고 이어짐 표시를 넣습니다."
                if score_pages else
                "자동 감지된 악보는 없습니다. 삽화·사진 영역은 확보하되 악보 원본이 별도 제공되는지 편집자가 확인해야 합니다."
            ),
        }
    else:
        result["spread"] = {"page_count": 1, "pages": [{
            "page": page_no, "page_label": f"{page_no}쪽", "title": title, "zones": zones, "score_check": score_check,
        }], "title": "1쪽 추천", "score_distribution": score_check["basis"]}
    return result


def audit_manuscript(manuscript: Path, curriculum: Path, output_root: Path,
                     ai_module: str | None = None, textbook_dir: Path | None = None,
                     target_level: str = "고등학교 1학년",
                     related_works: dict[str, str] | None = None,
                     work_titles: list[str] | None = None,
                     suppressed_fingerprints: set[str] | None = None,
                     on_page_progress: Callable[[int, int], None] | None = None) -> Path:
    if target_level not in TARGET_LEVELS:
        raise ValueError(f"지원하지 않는 학습자 수준입니다: {target_level}")
    manuscript, curriculum = manuscript.resolve(), curriculum.resolve()
    # 쪽 단위 진행률(on_page_progress)은 원래 본문 쪽 검사 반복문에서만 불렸는데,
    # 그 앞의 PDF 렌더링·유사도 비교 같은 준비 단계가 실제로는 더 오래 걸릴 수 있어
    # 그 사이에 취소를 눌러도 반영되지 않았다. (0, 0)으로 같은 콜백을 호출해 취소
    # 확인 지점을 늘리되, total_page=0이라 진행률 표시줄에는 영향을 주지 않는다.
    checkpoint = (lambda: on_page_progress(0, 0)) if on_page_progress else (lambda: None)
    activities_adapter = _load_activities_adapter(ai_module)
    manuscript_pages = _extract_pages(manuscript)
    curriculum_pages = _extract_pages(curriculum)
    checkpoint()
    components = detect_manuscript_components(manuscript_pages)
    standards = extract_curriculum_standards(curriculum_pages)
    alignment = match_curriculum(components, manuscript_pages, standards)
    digest = _sha256(manuscript)
    document_id = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", manuscript.stem).strip("_") + "_" + digest[:8]
    destination = output_root.resolve() / document_id
    destination.mkdir(parents=True, exist_ok=True)
    rendered_pages = _render_pages_and_images(manuscript, destination)
    checkpoint()
    layout_reference = _analyze_layout_references(
        textbook_dir, output_root.resolve() / "layout_reference.json"
    )
    checkpoint()
    body_text_review = _review_body_text(
        manuscript_pages, components, suppressed_fingerprints
    )
    textbook_content = _load_textbook_content(
        textbook_dir, output_root.resolve() / "textbook_content_cache.json"
    )
    checkpoint()
    layout_reference["learning_goal_examples"] = textbook_content.get("learning_goal_examples", [])
    textbook_similarity = _compare_textbook_similarity(
        manuscript_pages, components, textbook_content, suppressed_fingerprints
    )
    checkpoint()
    activity_similarity = _compare_activity_similarity(
        manuscript_pages, components, textbook_content, suppressed_fingerprints
    )
    checkpoint()
    # 원고 1쪽에서 찾은 제목을 문서 전체의 기본 주제로 고정해, 제목이 다시 표기되지
    # 않는 이어지는 쪽에서 엉뚱한 문장을 주제로 잘못 추측하는 것을 막는다.
    document_topic = _infer_topic(manuscript_pages)
    recommendations = _recommendations(
        components, alignment, manuscript_pages, target_level, layout_reference, activities_adapter,
        document_topic,
    )
    document_repertoire_review = _check_repertoire_overlap(
        "\n".join(manuscript_pages),
        [item["text"] for item in components["learning_goals"]["items"]],
        related_works, textbook_content.get("title_chunks", textbook_content.get("chunks", [])),
        work_titles,
    )
    spread_context = []
    for context_page, context_text in enumerate(manuscript_pages, 1):
        spread_context.append({
            "page": context_page,
            "text": context_text,
            "components": {
                key: _component_for_page(value, context_page) for key, value in components.items()
            },
            "images": rendered_pages[context_page - 1]["images"],
        })
    page_audits = []
    for page_no, page_text in enumerate(manuscript_pages, 1):
        if on_page_progress:
            on_page_progress(page_no, len(manuscript_pages))
        page_components = {
            key: _component_for_page(value, page_no) for key, value in components.items()
        }
        image_items = rendered_pages[page_no - 1]["images"]
        page_alignment = match_curriculum(page_components, [page_text], standards)
        activity_curriculum_review = _evaluate_activity_curriculum(
            page_components["activities"], components["learning_goals"], standards
        )
        activity_idea_review = _generate_activity_ideas(
            page_text, page_components, standards, related_works, target_level
        )
        repertoire_review = document_repertoire_review
        layout_recommendation = _recommend_layout(
            page_no, page_text, page_components, image_items, layout_reference, spread_context
        )
        page_recommendation = _recommendations(
            page_components, page_alignment, [page_text], target_level, layout_reference, activities_adapter,
            document_topic,
        )
        page_recommendations = {
            "achievement_standard": page_recommendation["achievement_standard"],
            # 원고 한 편은 하나의 주제를 다루므로 학습 목표는 1쪽에서만 표시·추천하고
            # 같은 주제가 이어지는 다음 쪽에는 반복해서 넣지 않는다.
            "learning_goal": page_recommendation["learning_goal"] if page_no == 1 else None,
            "activities": page_recommendation["activities"],
            "activity_variants": page_recommendation.get("activity_variants"),
            "curriculum_policy": page_recommendation["curriculum_policy"],
            "generation_method": page_recommendation.get("generation_method"),
            "ai_activity_review": page_recommendation.get("ai_activity_review"),
        }
        page_audits.append({
            "page": page_no, "page_image": rendered_pages[page_no - 1]["page_image"],
            "components": page_components, "curriculum_alignment": page_alignment,
            "layout_recommendation": layout_recommendation,
            "activity_curriculum_review": activity_curriculum_review,
            "activity_idea_review": activity_idea_review,
            "repertoire_review": repertoire_review,
            "textbook_similarity": {
                "items": textbook_similarity["pages"].get(page_no, []),
                "reference": textbook_similarity["reference"],
                "note": textbook_similarity["note"],
            },
            "activity_textbook_similarity": {
                "items": activity_similarity["pages"].get(page_no, []),
                "reference": activity_similarity["reference"],
                "note": activity_similarity["note"],
            },
            "body_text_review": {
                "items": body_text_review["pages"].get(page_no, []),
                "policy": body_text_review["policy"],
                "basis": body_text_review["basis"],
                "note": body_text_review["note"],
            },
            "recommendations": page_recommendations,
            "process": [
                {"step": 1, "name": "PDF 텍스트 추출", "result": f"{len(page_text)}자 추출"},
                {"step": 2, "name": "필수 항목 감지", "result": f"성취기준 {page_components['achievement_standards']['count']} · 학습 목표 {page_components['learning_goals']['count']} · 활동 {page_components['activities']['count']}"},
                {"step": 3, "name": "페이지 구성 분석", "result": layout_recommendation["title"]},
                {"step": 4, "name": "교육과정 비교", "result": f"{page_alignment['label']} · 최고 {page_alignment['top_score'] * 100:.1f}%"},
            ],
        })
    completion = _completion_score(components, alignment, rendered_pages, manuscript_pages)
    result: dict[str, Any] = {
        "schema_version": "1.0.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "processing_settings": {"target_level": target_level, "work_titles": work_titles or []},
        "document_id": document_id,
        "manuscript": {"filename": manuscript.name, "sha256": digest, "page_count": len(manuscript_pages)},
        "curriculum": {
            "filename": curriculum.name, "sha256": _sha256(curriculum),
            "standard_count": len(standards), "standards": standards,
        },
        "required_components": components,
        "curriculum_alignment": alignment,
        "recommendations": recommendations,
        "body_text_review": {
            "summary": body_text_review["summary"],
            "policy": body_text_review["policy"],
            "basis": body_text_review["basis"],
            "note": body_text_review["note"],
        },
        "textbook_similarity": {
            "summary": textbook_similarity["summary"],
            "reference": textbook_similarity["reference"],
            "note": textbook_similarity["note"],
        },
        "repertoire_review": document_repertoire_review,
        "activity_textbook_similarity": {
            "summary": activity_similarity["summary"],
            "reference": activity_similarity["reference"],
            "note": activity_similarity["note"],
        },
        "layout_reference": layout_reference,
        "completion": completion,
        "page_audits": page_audits,
        "overall_review_required": (
            not all(components[key]["included"] for key in components)
            or alignment["status"] in {"review_required", "not_applicable", "partially_applicable"}
        ),
    }
    result = _apply_ai_adapter(ai_module, result)
    json_path = destination / "audit.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (destination / "audit.html").write_text(_render_html(result), encoding="utf-8")
    return json_path


def _render_html(result: dict[str, Any]) -> str:
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return AUDIT_HTML.replace("__AUDIT_DATA__", payload)


_DIFF_SECTIONS = {
    "body_text_review": ("본문 맞춤법 검사", "current_text"),
    "textbook_similarity": ("기존 교과서 본문 비교", "manuscript_text"),
    "activity_textbook_similarity": ("기존 교과서 활동 비교", "manuscript_text"),
}


def _section_fingerprint_map(result: dict[str, Any], section: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for page in result.get("page_audits", []):
        for item in page.get(section, {}).get("items", []):
            fingerprint = item.get("fingerprint")
            if fingerprint and fingerprint not in found:
                found[fingerprint] = item
    return found


def compare_audits(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """이전 분석과 현재 분석을 fingerprint 기준으로 비교해 해결됨·신규·유지 지적사항을 정리한다."""
    section_labels = {key: label for key, (label, _) in _DIFF_SECTIONS.items()}
    sections: dict[str, Any] = {}
    for section, (_, text_key) in _DIFF_SECTIONS.items():
        before = _section_fingerprint_map(previous, section)
        after = _section_fingerprint_map(current, section)
        before_keys, after_keys = set(before), set(after)
        sections[section] = {
            "resolved": [{"text": before[key].get(text_key, "")} for key in sorted(before_keys - after_keys)],
            "new": [{"text": after[key].get(text_key, "")} for key in sorted(after_keys - before_keys)],
            "unchanged_count": len(before_keys & after_keys),
        }
    completion_before = previous.get("completion", {}).get("percentage", 0)
    completion_after = current.get("completion", {}).get("percentage", 0)
    return {
        "previous_filename": previous.get("manuscript", {}).get("filename", ""),
        "current_filename": current.get("manuscript", {}).get("filename", ""),
        "completion_before": completion_before,
        "completion_after": completion_after,
        "completion_delta": round(completion_after - completion_before, 1),
        "page_count_before": previous.get("manuscript", {}).get("page_count", 0),
        "page_count_after": current.get("manuscript", {}).get("page_count", 0),
        "sections": sections,
        "section_labels": section_labels,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _render_diff_html(diff: dict[str, Any]) -> str:
    payload = json.dumps(diff, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return DIFF_HTML.replace("__DIFF_DATA__", payload)


def compare_manuscript_audits(previous_json: Path, current_json: Path, destination: Path) -> Path:
    """두 audit.json을 비교해 destination에 diff.html을 생성하고 그 경로를 반환한다."""
    previous = json.loads(Path(previous_json).read_text(encoding="utf-8"))
    current = json.loads(Path(current_json).read_text(encoding="utf-8"))
    diff = compare_audits(previous, current)
    diff_path = Path(destination) / "diff.html"
    diff_path.write_text(_render_diff_html(diff), encoding="utf-8")
    return diff_path


DIFF_HTML = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>원고 재분석 비교</title><style>
:root{--bg:#f5f3ee;--panel:#fff;--text:#171717;--muted:#6b6862;--line:#d8d3ca;--accent:#563fc9;--lime:#dfff70;--ok:#24684a;--warn:#b42318}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:"Noto Sans KR","Malgun Gothic",system-ui,sans-serif;font-size:15px}
.shell{max-width:900px;margin:auto;padding:34px 24px 72px}
h1{font-size:32px;letter-spacing:-.03em;margin:0 0 8px}
.sub{color:var(--muted);margin-bottom:24px}
.score-badges{display:flex;gap:14px;margin-bottom:28px;flex-wrap:wrap}
.badge{background:var(--panel);border:1px solid var(--text);border-radius:8px;padding:14px 18px}
.badge b{display:block;font-size:24px;margin-top:4px}
.diff-section{background:var(--panel);border:1px solid var(--text);border-radius:8px;padding:20px;margin-bottom:16px}
.diff-section h3{margin:0 0 12px}
.diff-col{margin-bottom:10px}
.diff-col ul{margin:6px 0 0;padding-left:20px;line-height:1.6}
.note{color:var(--muted);font-size:12px;margin-top:8px}
</style></head><body><main class="shell" id="app"></main>
<script id="diff-data" type="application/json">__DIFF_DATA__</script><script>
(()=>{const data=JSON.parse(document.getElementById('diff-data').textContent);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
const delta=data.completion_delta,deltaText=`${delta>=0?'+':''}${delta}%p`;
const list=items=>items.length?`<ul>${items.map(x=>`<li>${esc(x.text)}</li>`).join('')}</ul>`:'<div class="note">없음</div>';
const sections=Object.entries(data.section_labels).map(([key,label])=>{const s=data.sections[key];return `<section class="diff-section"><h3>${esc(label)}</h3><div class="diff-col"><b>해결됨 (${s.resolved.length}건)</b>${list(s.resolved)}</div><div class="diff-col"><b>새로 발견 (${s.new.length}건)</b>${list(s.new)}</div><div class="note">유지된 항목 ${s.unchanged_count}건</div></section>`}).join('');
document.getElementById('app').innerHTML=`<h1>원고 재분석 비교</h1><div class="sub">${esc(data.previous_filename)} → ${esc(data.current_filename)}</div><div class="score-badges"><div class="badge">완성도 변화<b>${data.completion_before}% → ${data.completion_after}% (${deltaText})</b></div><div class="badge">쪽수<b>${data.page_count_before}쪽 → ${data.page_count_after}쪽</b></div></div>${sections}`;
})();
</script></body></html>'''


AUDIT_HTML = r'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>원고 교육과정 페이지 점검</title>
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.css');
:root{
  --bg:#F5F6F8; --surface:#FFFFFF; --surface-2:#FBFBFC;
  --border:rgba(16,24,40,.07); --border-strong:rgba(16,24,40,.12);
  --text-1:#1A1D23; --text-2:#4B5160; --text-3:#8A8F9C;
  --blue:#3B66E0; --blue-bg:#EFF3FF; --blue-border:#C6D4FA;
  --red:#DC2626; --red-bg:#FFF1F1; --red-border:#FBCACA;
  --amber:#EA580C; --amber-bg:#FFF3EA; --amber-border:#FED7AA;
  --green:#16794C; --green-bg:#EEFBF3; --green-border:#C3ECD6;
  --cat-similarity:#0369A1; --cat-similarity-bg:#F0F9FF; --cat-similarity-border:#BAE6FD;
  --cat-achievement:#7C3AED; --cat-achievement-bg:#F5F3FF; --cat-achievement-border:#DDD6FE;
  --cat-activity:#0F766E; --cat-activity-bg:#F0FDFA; --cat-activity-border:#99F6E4;
  --cat-expression:#475569; --cat-expression-bg:#F1F5F9; --cat-expression-border:#CBD5E1;
  --selected-border:#8B93A3; --selected-bg:#F3F4F6;
  --shadow-sm:0 1px 2px rgba(16,24,40,.05);
  --shadow-md:0 1px 3px rgba(16,24,40,.07),0 1px 2px rgba(16,24,40,.04);
  --shadow-lg:0 6px 16px rgba(16,24,40,.09),0 2px 4px rgba(16,24,40,.05);
  --shadow-hover:0 10px 24px rgba(16,24,40,.12),0 3px 6px rgba(16,24,40,.06);
  --radius:14px; --radius-sm:8px;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--bg);
  font-family:'Pretendard',-apple-system,BlinkMacSystemFont,sans-serif;
  color:var(--text-1);-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums;
}
button{font-family:inherit}
.shell{max-width:1580px;margin:28px auto;background:linear-gradient(180deg,#FAFBFC 0%,#F5F6F8 140px);border:1px solid var(--border);border-radius:20px;box-shadow:var(--shadow-lg);overflow:hidden}
.topbar{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;padding:16px 28px;background:var(--surface);border-bottom:1px solid var(--border)}
.ybm-logo{height:18px;width:auto;display:block;grid-column:2;justify-self:center}
.env-pill{grid-column:3;justify-self:end;font-size:12.5px;color:var(--text-3);background:var(--surface-2);border:1px solid var(--border);padding:5px 12px;border-radius:999px;white-space:nowrap}
.header{padding:22px 28px 20px}
.breadcrumb{font-size:13.5px;color:var(--text-3);margin-bottom:6px}
h1{font-size:21.5px;font-weight:800;letter-spacing:-.02em;margin:0}

.summary-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;padding:0 28px 22px}
.metric-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:13px 18px;box-shadow:var(--shadow-md);position:relative}
.metric-label{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--text-2);font-weight:600;margin-bottom:7px}
.dot{width:7px;height:7px;border-radius:50%;flex:none}
.dot-blue{background:var(--blue)} .dot-red{background:var(--red)} .dot-neutral{background:var(--text-3)}
.metric-value{font-size:24.5px;font-weight:800;letter-spacing:-.02em;line-height:1.1;font-variant-numeric:tabular-nums}
.metric-sub{font-size:12.5px;color:var(--text-3);margin-top:5px;line-height:1.4}
.progress-track{height:5px;background:#EDEEF2;border-radius:999px;margin-top:7px;overflow:hidden}
.progress-fill{height:100%;border-radius:999px;background:linear-gradient(90deg,#6E97FF,#3B66E0)}
.metric-card.alert{background:linear-gradient(180deg,#FFF6F6 0%,#FFFFFF 55%);border:1px solid var(--red-border)}
.metric-card.alert::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:linear-gradient(180deg,#EF4444,#B91C1C);border-radius:var(--radius) 0 0 var(--radius)}
.metric-card.alert .metric-value{color:var(--red)}

.main-grid{display:grid;grid-template-columns:354px 1fr 400px;gap:16px;padding:0 28px 28px;align-items:start}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow-md);min-width:0;height:560px;display:flex;flex-direction:column;overflow:hidden;position:relative}
.scroll-fade{position:absolute;left:1px;right:1px;bottom:1px;height:28px;background:linear-gradient(180deg,rgba(255,255,255,0) 0%,var(--surface) 80%);pointer-events:none;opacity:0;transition:opacity .15s ease;border-radius:0 0 var(--radius) var(--radius)}
.scroll-fade.visible{opacity:1}
.panel-header{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--border)}
.panel-title{font-size:14.5px;font-weight:700}
.panel-title-sub{font-size:12.5px;color:var(--text-3);font-weight:500}

.left-panel-body{padding:14px;flex:1;min-height:0;display:flex;flex-direction:column}
.preview-thumb{border:1px solid var(--border);border-radius:10px;overflow:auto;box-shadow:var(--shadow-sm);background:#fff;display:flex;align-items:center;justify-content:center;position:relative;flex:1;min-height:0}
.preview-thumb img{width:100%;height:100%;object-fit:contain;display:block;transform-origin:center center}
.preview-nav{display:flex;align-items:center;justify-content:space-between;margin-top:10px;font-size:13.5px;color:var(--text-2)}
.nav-btn{width:30px;height:30px;border-radius:8px;border:1px solid var(--blue-border);background:var(--blue-bg);box-shadow:var(--shadow-sm);cursor:pointer;display:flex;align-items:center;justify-content:center;color:var(--blue);font-weight:700}
.nav-btn:hover:not(:disabled){background:var(--blue);border-color:var(--blue);color:#fff;box-shadow:var(--shadow-md)}
.nav-btn:disabled{opacity:.35;cursor:default}
.preview-zoom{display:flex;align-items:center;justify-content:center;gap:10px;margin-top:8px;font-size:12.5px;color:var(--text-2);font-weight:600}
.zoom-btn{width:26px;height:26px;border-radius:8px;border:1px solid var(--blue-border);background:var(--blue-bg);color:var(--blue);font-weight:700;font-size:15.5px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:var(--shadow-sm)}
.zoom-btn:hover:not(:disabled){background:var(--blue);border-color:var(--blue);color:#fff;box-shadow:var(--shadow-md)}
.zoom-btn:disabled{opacity:.35;cursor:default}
.file-meta{margin-top:14px}
.file-name{font-size:13.5px;font-weight:700}
.file-sub{font-size:12.5px;color:var(--text-3);margin-top:3px}

.checklist-toolbar{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;padding:14px 18px 12px;border-bottom:1px solid var(--border)}
.status-counts{display:flex;gap:14px;font-size:13.5px;flex-wrap:wrap}
.status-counts span{display:inline-flex;align-items:center;gap:6px;font-weight:600;color:var(--text-2)}
.status-counts .count-fix{color:var(--red)}
.status-counts .count-review{color:var(--amber)}
.status-counts .count-ok{color:var(--green)}
.variant-tabs{display:flex;gap:6px;flex-wrap:wrap;padding:12px 18px 0}
.variant-tabs:empty{display:none;padding:0}
.variant-tab{font-size:12.5px;font-weight:600;padding:6px 12px;border-radius:999px;border:1px solid var(--border-strong);background:var(--surface);color:var(--text-2);cursor:pointer}
.variant-tab.active{background:var(--blue);border-color:var(--blue);color:#fff}

.checklist-body{padding:8px 14px 14px;flex:1;min-height:0;overflow-y:auto;scrollbar-gutter:stable}
.checklist-body::-webkit-scrollbar{width:6px}
.checklist-body::-webkit-scrollbar-track{background:transparent}
.checklist-body::-webkit-scrollbar-thumb{background:var(--border-strong);border-radius:999px}
.checklist-body::-webkit-scrollbar-thumb:hover{background:var(--text-3)}
.section-label{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:700;color:var(--text-2);padding:12px 8px 8px}
.section-label .count{color:var(--text-3);font-weight:600}
.section-label.sl-fix{color:var(--red)}
.section-label.sl-review{color:var(--amber)}
.section-label.sl-ok{color:var(--green)}
.section-toggle{margin-left:auto;font-size:13px;color:var(--blue);font-weight:600;cursor:pointer}

.item-card{border:1px solid var(--border-strong);border-radius:10px;padding:10px 13px;margin-bottom:9px;background:var(--surface);cursor:pointer;transition:box-shadow .15s ease,border-color .15s ease,transform .1s ease;position:relative}
.item-card:hover{box-shadow:var(--shadow-md);transform:translateY(-1px)}
.item-card.selected{border:1.5px solid var(--selected-border);box-shadow:0 0 0 3px rgba(16,24,40,.08),var(--shadow-md)}
.item-card.fp-dim{opacity:.5}
.item-card.severity-high{border-left:3px solid var(--red);background:linear-gradient(180deg,var(--red-bg) 0%,var(--surface) 65%)}
.item-card.severity-med{border-left:3px solid var(--amber);background:linear-gradient(180deg,var(--amber-bg) 0%,var(--surface) 65%)}
.item-card.severity-pass{border-left:3px solid var(--green);background:linear-gradient(180deg,var(--green-bg) 0%,var(--surface) 65%)}
.item-card.selected.severity-high,.item-card.selected.severity-med,.item-card.selected.severity-pass{background:var(--selected-bg)}
.item-row1{display:flex;align-items:center;gap:8px;margin-bottom:3px}
.item-tag{font-size:12px;font-weight:700;padding:3px 9px;border-radius:999px;display:inline-flex;align-items:center;gap:5px;white-space:nowrap;flex:none}
.tag-similarity{background:var(--cat-similarity-bg);color:var(--cat-similarity);border:1px solid var(--cat-similarity-border)}
.tag-achievement{background:var(--cat-achievement-bg);color:var(--cat-achievement);border:1px solid var(--cat-achievement-border)}
.tag-activity{background:var(--cat-activity-bg);color:var(--cat-activity);border:1px solid var(--cat-activity-border)}
.tag-expression{background:var(--cat-expression-bg);color:var(--cat-expression);border:1px solid var(--cat-expression-border)}
.item-title{font-size:14.5px;font-weight:700;color:var(--text-1);letter-spacing:-.01em;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item-loc{font-size:12px;color:var(--text-3);font-weight:600;white-space:nowrap;flex:none}
.item-row2{display:flex;align-items:center;justify-content:space-between;gap:10px}
.item-desc{font-size:13px;color:var(--text-2);font-weight:400;letter-spacing:-.01em;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item-actions{flex:none}
.mini-link{font-size:13px;font-weight:600;color:var(--blue);background:none;border:none;padding:2px 0;cursor:pointer;display:inline-flex;align-items:center;gap:4px}
.mini-link:hover{text-decoration:underline}
.mini-link svg{transition:transform .15s ease}
.mini-link:hover svg{transform:translateX(2px)}
.collapsed-section{border:1px dashed var(--border-strong);border-radius:12px;padding:12px 16px;margin-bottom:10px;font-size:13.5px;color:var(--text-3);display:flex;align-items:center;justify-content:space-between;cursor:pointer}

.right-panel-body{padding:18px 20px 20px;flex:1;min-height:0;overflow-y:auto;scrollbar-gutter:stable}
.right-panel-body::-webkit-scrollbar{width:6px}
.right-panel-body::-webkit-scrollbar-track{background:transparent}
.right-panel-body::-webkit-scrollbar-thumb{background:var(--border-strong);border-radius:999px}
.right-panel-body::-webkit-scrollbar-thumb:hover{background:var(--text-3)}
.issue-status-pill{font-size:12.5px;font-weight:700;padding:4px 11px;border-radius:999px;background:var(--red-bg);color:var(--red);border:1px solid var(--red-border);white-space:nowrap}
.issue-status-pill.review{background:var(--amber-bg);color:var(--amber);border-color:var(--amber-border)}
.issue-status-pill.ok{background:var(--green-bg);color:var(--green);border-color:var(--green-border)}
.issue-title{font-size:17px;font-weight:800;letter-spacing:-.02em;margin:12px 0 16px;line-height:1.4}
.block{margin-bottom:18px}
.block-label{font-size:13px;font-weight:700;color:var(--text-2);margin-bottom:8px}
.block-text{font-size:14px;color:var(--text-1);line-height:1.6;font-weight:400;letter-spacing:-.01em}
.block-text ul{margin:8px 0 0;padding-left:18px}
.quote-box{background:var(--amber-bg);border:1px solid var(--amber-border);border-radius:10px;padding:12px 14px;font-size:13.5px;line-height:1.6;color:#5C4113;margin-top:8px;font-weight:400;letter-spacing:-.01em;position:relative}
.quote-box .quote-label{font-size:12px;font-weight:700;color:var(--amber);margin-bottom:4px;padding-right:70px}
mark{background:#FDE68A;padding:0 2px;border-radius:3px}
.recommend-box{background:var(--blue-bg);border:1px solid var(--blue-border);border-radius:10px;padding:12px 14px;font-size:14px;font-weight:600;color:#1E3A8A;line-height:1.6;display:flex;align-items:flex-start;justify-content:space-between;gap:10px}
.confidence-box{background:var(--green-bg);border:1px solid var(--green-border);border-radius:10px;padding:11px 14px;font-size:13px;color:#0F5132;line-height:1.55}
.divider{height:1px;background:var(--border);margin:18px 0}
.mini-btn{font-size:12.5px;font-weight:600;padding:5px 11px;border-radius:8px;background:#fff;border:1px solid var(--border-strong);color:var(--text-2);box-shadow:var(--shadow-sm);cursor:pointer;flex:none}
.mini-btn:hover{box-shadow:var(--shadow-md)}
.mini-btn.copied{background:var(--green);color:#fff;border-color:transparent}
.fp-btn{position:absolute;top:10px;right:10px;font-size:11px;padding:3px 8px;border-radius:7px;background:#fff;border:1px solid var(--amber-border);color:var(--amber);cursor:pointer;flex:none}
.empty-detail{padding:40px 20px;color:var(--text-3);font-size:13.5px;text-align:center}
.term-check{margin-top:8px;padding-top:8px;border-top:1px dashed var(--border-strong);font-size:12.5px;color:var(--text-2)}
.match{padding:10px 0;border-bottom:1px solid var(--border);font-size:13.5px;line-height:1.55;color:var(--text-1)}
.match:last-child{border:0}
.note{color:var(--text-3);font-size:13px;line-height:1.5}

@media(max-width:1200px){.main-grid{grid-template-columns:260px 1fr}.panel:last-child{grid-column:1/-1}}
@media(max-width:1100px){.summary-grid{grid-template-columns:repeat(2,1fr)}.main-grid{grid-template-columns:1fr}.shell{margin:0;border-radius:0}}
</style></head><body>
<div class="shell">
<div class="topbar">
  <img class="ybm-logo" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAJ4AAAAeCAYAAADdNaejAAAAGXRFWHRTb2Z0d2FyZQBBZG9iZSBJbWFnZVJlYWR5ccllPAAABK9JREFUeNrsnDFy4jAUQEUm/brdKt4b0G0JnhwgMOMqzZoTLJwAOIGTEwQaV5kJOcAOpNwqzgnirbZlT5DVtwXY31+2jB1bJP4zCmAL2f56+vr/S6Tz9/vlGyPk6+9fHXjl59f8pU9UmfM6M34ezq0V2gj45xFVj9ketOHyMmT314GkDtzniJ9fSM7P+N8rXixeZ8ta0VrOaryWwwG8yzjf5eWZA9TNqHPHzzs5bax5HaPt2ha8IvAZApw8+O5a+FrwmoDPUYAvz3q28snAqws+U6GNVhqS8wavDfC98IDjJgM+NwwWsuFb8mBiU6KNaiQKbuKykAZKx7XfR0FeIA203vP+o4F8cGMo3Ud+uBmrM8PHzhsGX08/LPIPB7z0YsqCTnjiZSWJmqfo80Z8R3YNB7Xvh+3fX68k3+ija2xCOA73m7TsGAjbMxMwMLbldXzl+4+Ac1MZDtsDXcx5W3ED8gPVm+Fj56wVDMQgDGDoQeGEyre9ydHWJurABwTBDqwxPw+dPURQ5EkUUCWlQ9w7BtdSvGdTtG9IZxXbYwg+LX08XaFzBBRGjpXOS+tkQbcmoKN8U1MjzUwVZie3yD2fhSa3OfE1gm7nD6qKe0TK5oGpuReGsLq6CB5k4A5Q1m1QBLxvDQAAsFs8sFhpplyD8HOGYkraEHA4BcAeEJbOF+0PiT7oa2H1oqCGoaAGXIFJ6NulB+Mbo1e6klEt7/wtjy4tMQV0a4TOZ3pJj1CwlYDQ9l4RPD3JyFdpP9TDPlCxPdDHK2FBbjTT0woNzOnR6ZQa4dMVOirCXhJ1lkjRRabaLpGu2Mai0IDDt0LTVe9o8NKW6kKnTMQ+uAD4xJTif0Lo6hAM3gtR56XCTl6j4lT0HH3J++PAQ/DNK1b68gSh+6J4LCv4WIvSJSAKTkIL6QSxKZ7JJaZZSDN1CH84P50C8MF2p09okYJUsBGPWqP3TgF4usIi9JmuiXJ1WRBWb0zMaItCPl4NAllrF/xIja3eEwILYHkOl+QOz2AQ39FV0pYqO3+YJXPhe2YNoEmRfZB1JpDDhX8On56L9tFKREB01lQUk4h6F9piBxF5vNDBkmpbAaNTSjtLNyqqi7qXzHbw6Wr5hky+NISVPcypYyX8I1hSOmWJlvCs1JovvUHDzzvWxFqtvvCBcm3PYtRieHIKmxRcS323tEQDOgoSie102gbkUZR4ncSxpjYJ6A3fYWTjhO6oxPS6QTCDy4FXbi5OBL912Qaa3CSw23qk78jOj3yLCHa8r4g6/RMKXkpJuy0qmgpUI76r2NRyIb7ji3VLlah5kLB4tjfebyWKdlObCr6S6jPN0JFeC55+4jC1NcdxCV9twdLbiyDJ/FMcM1IWUr4pVEWmNeoPnu1PkftpwSsvaukhyHHZ3i0BhMzSjk5IB1k/P9hZ4GkVPh5MAWV3TVTRRlVST4ADvz1Qy+7flLR2H9LHC6M+sa5bxjRPSrZRpQRMYX1RPPu/2Ptt4YDj/nrER/+jsHzYWgJst7nWQ9InJYOdMuKK3168G3iVQCf9VxZNp1Dqu96KpVMpZdrbKg6cZt2NI6fazYeErhWtp9oqgGmha2Uv/wUYACEAx8+d16uvAAAAAElFTkSuQmCC" alt="YBM 로고">
  <div class="env-pill">원고 <b id="page-total"></b>쪽 연속 보기 · 선택 <b id="page-now">1</b>쪽</div>
</div>
<div class="header">
  <div class="breadcrumb">교과서 원고 점검</div>
  <h1 id="audit-title"></h1>
</div>
<div class="summary-grid" id="metrics-bar"></div>
<div class="main-grid">
  <section class="panel">
    <div class="panel-header"><div class="panel-title">원고 미리보기</div><div class="panel-title-sub" id="page-sub"></div></div>
    <div class="left-panel-body">
      <div class="preview-thumb"><img id="page-image" src="" alt=""></div>
      <div class="preview-nav">
        <button type="button" class="nav-btn" id="page-prev">←</button>
        <span id="page-nav-text"></span>
        <button type="button" class="nav-btn" id="page-next">→</button>
      </div>
      <div class="preview-zoom">
        <button type="button" class="zoom-btn" id="zoom-out">−</button>
        <span id="zoom-level">100%</span>
        <button type="button" class="zoom-btn" id="zoom-in">+</button>
      </div>
      <div class="file-meta">
        <div class="file-name" id="page-filename"></div>
        <div class="file-sub" id="page-filesub"></div>
      </div>
    </div>
  </section>
  <section class="panel">
    <div class="checklist-toolbar">
      <div style="font-weight:700;font-size:14.5px">점검 항목 <span style="color:var(--text-3);font-weight:600" id="queue-total"></span></div>
      <div class="status-counts" id="status-counts"></div>
    </div>
    <div class="variant-tabs" id="variant-tabs"></div>
    <div class="checklist-body" id="checklist-body"></div>
  </section>
  <aside class="panel" id="detail-panel"></aside>
</div>
</div>
<script id="audit-data" type="application/json">__AUDIT_DATA__</script><script>
(()=>{const data=JSON.parse(document.getElementById('audit-data').textContent);
let page=1,selectedId=null,currentIssues=[],variantByPage={},reviewExpanded=false,okExpanded=false,previewZoom=1;
const $=id=>document.getElementById(id);
const cleanFileName=name=>String(name||'').replace(/^[0-9a-f]{8,16}_/i,'').replace(/_/g,' ');
const stripEmbeddedFileNames=s=>String(s??'').replace(/[0-9a-f]{8,16}_[^\s,'")]+?\.pdf/gi,m=>cleanFileName(m));
const esc=s=>stripEmbeddedFileNames(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
const bulletList=items=>`<ul>${items.filter(Boolean).map(t=>`<li>${t}</li>`).join('')}</ul>`;
const names={achievement_standards:'성취기준',learning_goals:'학습 목표',activities:'활동'};
const buckets={achievement_standards:'성취기준',learning_goals:'표현',activities:'활동 구성'};
const completionBuckets={'성취기준':'성취기준','학습 목표':'성취기준','활동':'활동 구성','악보':'활동 구성','참고 사진·삽화':'활동 구성','본문 텍스트':'표현'};
const tierLabel=t=>t==='fix'?'수정 필요':t==='review'?'검토 필요':'통과';
const catTagClass=c=>c==='유사도'?'tag-similarity':c==='성취기준'?'tag-achievement':c==='활동 구성'?'tag-activity':'tag-expression';
const tierSeverityClass=t=>t==='fix'?'severity-high':t==='review'?'severity-med':'severity-pass';

let fp=new Set();
async function loadFp(){try{const r=await fetch('/api/false-positives');const items=await r.json();(items||[]).forEach(x=>fp.add(x.fingerprint))}catch(e){}}
function fpBtn(section,text,fingerprint){if(!fingerprint)return '';const marked=fp.has(fingerprint);return `<button type="button" class="fp-btn" data-fp="${esc(fingerprint)}" data-section="${esc(section)}" data-text="${esc(text)}">${marked?'오탐 해제':'오탐'}</button>`}
document.addEventListener('click',async event=>{const btn=event.target.closest('.fp-btn');if(!btn)return;event.stopPropagation();const fingerprint=btn.dataset.fp,section=btn.dataset.section,text=btn.dataset.text;btn.disabled=true;try{if(fp.has(fingerprint)){await fetch(`/api/false-positives/${encodeURIComponent(fingerprint)}`,{method:'DELETE'});fp.delete(fingerprint)}else{await fetch('/api/false-positives',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fingerprint,section,manuscript_text:text})});fp.add(fingerprint)}render()}catch(e){}finally{btn.disabled=false}});
document.addEventListener('click',event=>{const btn=event.target.closest('.mini-btn[data-copy]');if(!btn)return;event.stopPropagation();const text=btn.dataset.copy||'';if(navigator.clipboard){navigator.clipboard.writeText(text).catch(()=>{})}const original=btn.textContent;btn.textContent='복사됨';btn.classList.add('copied');setTimeout(()=>{btn.textContent=original;btn.classList.remove('copied')},1400)});

function goToPage(n){if(n<1||n>data.manuscript.page_count)return;page=n;selectedId=null;reviewExpanded=false;okExpanded=false;previewZoom=1;render()}
function initPages(){$('page-prev').onclick=()=>goToPage(page-1);$('page-next').onclick=()=>goToPage(page+1)}
function applyZoom(){
  $('page-image').style.transform=`scale(${previewZoom})`;
  $('zoom-level').textContent=`${Math.round(previewZoom*100)}%`;
  $('zoom-out').disabled=previewZoom<=1;
  $('zoom-in').disabled=previewZoom>=3;
}
function initZoom(){
  $('zoom-in').onclick=()=>{previewZoom=Math.min(3,+(previewZoom+0.25).toFixed(2));applyZoom()};
  $('zoom-out').onclick=()=>{previewZoom=Math.max(1,+(previewZoom-0.25).toFixed(2));applyZoom()};
  applyZoom();
}
function renderPagePreview(p){
  $('page-image').src=encodeURI(p.page_image);
  $('page-image').alt=`원고 ${p.page}쪽`;
  $('page-nav-text').textContent=`${page} / ${data.manuscript.page_count}쪽`;
  $('page-prev').disabled=page<=1;
  $('page-next').disabled=page>=data.manuscript.page_count;
  $('page-filename').textContent=data.manuscript.filename;
  const chars=(p.process[0]||{}).result||'';
  const layout=(p.process[2]||{}).result||'';
  $('page-filesub').textContent=[chars,layout].filter(Boolean).join(' · ');
  applyZoom();
}

function componentIssue(key,item){
  const bucket=buckets[key];
  const list=item.items.length?`<ul>${item.items.map(x=>`<li>${esc(x.text)}</li>`).join('')}</ul>`:'';
  const countText=item.included?`현재 쪽 ${item.count}개${Number.isFinite(item.document_count)?` · 전체 원고 ${item.document_count}개`:''}`:`현재 쪽 미포함${Number.isFinite(item.document_count)?` · 전체 원고 ${item.document_count}개`:''}`;
  return {tier:item.included?'ok':'fix',cat:bucket,loc:`p.${page}`,
    title:item.included?`${names[key]} 확인됨`:`${names[key]}을(를) 찾지 못함`,
    summary:item.included?(item.items[0]?item.items[0].text:''):'이 페이지에서 찾지 못했습니다.',
    reasonLabel:item.included?'확인 내용':'왜 수정이 필요한가',
    reasonHtml:`${countText}${list}`,
    quotes:[],
    suggestLabel:'권장 조치',
    recommendHtml:item.included?'':`이 페이지에 ${names[key]}을(를) 명시하세요.`,
    confidenceHtml:'',
    basisHtml:esc(item.note),
    hasAction:!item.included,actionLabel:'수정안 보기'};
}

function bodyReviewIssues(p){
  const b=p.body_text_review,flagged=b.items.filter(x=>x.status!=='적절');
  const bodyTerms=x=>(x.terminology||[]).length?`<div class="term-check">명칭 표기 확인<ul>${x.terminology.map(t=>`<li><b>${esc(t.term)}</b> · ${esc(t.status)} — ${esc(t.reason)}</li>`).join('')}</ul></div>`:'';
  const basisText=`검사 항목: ${esc(b.basis.checks.join(' · '))}<br>${esc(b.basis.limitation)}<br>${esc(b.note)}<br><b>${esc(b.policy)}</b>`;
  if(!flagged.length){
    return [{tier:'ok',cat:'표현',loc:`p.${page} · 본문`,title:'본문 맞춤법 검사 완료',
      summary:`${b.items.length}문장 확인 · 수정 제안 없음`,
      reasonLabel:'확인 내용',reasonHtml:`이 페이지의 설명형 본문 문장 ${b.items.length}개를 검사했고, 수정이 필요한 문장은 없었습니다.`,
      quotes:[],suggestLabel:'판정 방식',recommendHtml:'',confidenceHtml:'',basisHtml:basisText,hasAction:false}];
  }
  return flagged.map((x,i)=>({tier:'fix',cat:'표현',loc:`p.${page} · 본문`,
    title:`본문 맞춤법 수정 제안 ${i+1}`,
    summary:x.issues.length?x.issues.map(i2=>i2.type).join(' · '):(x.suggested_text||x.current_text),
    dimmed:x.fingerprint?fp.has(x.fingerprint):false,
    reasonLabel:'왜 수정이 필요한가',
    reasonHtml:`${x.issues.length?`<ul>${x.issues.map(i2=>`<li><b>${esc(i2.type)}</b> · ${esc(i2.reason)}</li>`).join('')}</ul>`:''}${bodyTerms(x)}`,
    quotes:[{label:'현재 문장',html:esc(x.current_text),fp:fpBtn('body_text',x.current_text,x.fingerprint)}],
    suggestLabel:'권장 수정안',
    recommendHtml:x.suggested_text?esc(x.suggested_text):'',
    recommendCopy:x.suggested_text||null,
    confidenceHtml:'',basisHtml:basisText,
    hasAction:true,actionLabel:x.suggested_text?'수정안 보기':'자세히 보기'}));
}

function renderSimilarityQuotes(x){
  const quotes=[{label:x.kind==='본문'?'원고 문장':'원고 활동',html:esc(x.manuscript_text),fp:fpBtn(x.kind==='본문'?'textbook_similarity':'activity_similarity',x.manuscript_text,x.fingerprint)}];
  if(x.match)quotes.push({label:`참고 교과서 — ${esc(cleanFileName(x.match.file))} ${x.match.page}쪽`,html:esc(x.match.text)});
  return quotes;
}
function similarityIssues(p){
  const ts=p.textbook_similarity,as=p.activity_textbook_similarity;
  const combined=[...ts.items.map(x=>({...x,kind:'본문'})),...as.items.map(x=>({...x,kind:'활동'}))];
  const flagged=combined.filter(x=>x.review_required);
  if(!flagged.length){
    return [{tier:'ok',cat:'유사도',loc:`p.${page}`,title:'기존 교과서 비교 완료',
      summary:`${combined.length}건 비교 · 검토 필요 없음`,
      reasonLabel:'확인 내용',reasonHtml:`이 페이지의 본문·활동 ${combined.length}건을 기존 교과서와 비교했고, 핵심어 3개 이상 겹치는 항목은 없었습니다.`,
      quotes:[],suggestLabel:'판정 방식',recommendHtml:'',confidenceHtml:'',basisHtml:esc(ts.note),hasAction:false}];
  }
  const kindTotal={};flagged.forEach(x=>{kindTotal[x.kind]=(kindTotal[x.kind]||0)+1});
  const kindSeen={};
  return flagged.map(x=>{
    const sharedAll=[...(x.shared_keywords||[]),...(x.shared_meanings||[]),...(x.shared_works||[]),...(x.shared_genres||[]),...(x.shared_instruments||[]),...(x.shared_instrument_families||[]),...(x.shared_actions||[])];
    const highSeverity=x.score>=0.7;
    kindSeen[x.kind]=(kindSeen[x.kind]||0)+1;
    const idxLabel=kindTotal[x.kind]>1?`${kindSeen[x.kind]} · `:'';
    return {tier:highSeverity?'fix':'review',cat:'유사도',loc:`p.${page} · ${x.kind}`,
      title:`${x.kind} 유사도 ${idxLabel}${(x.score*100).toFixed(1)}% · ${x.verdict}`,
      summary:x.manuscript_text,
      dimmed:x.fingerprint?fp.has(x.fingerprint):false,
      reasonLabel:highSeverity?'왜 수정이 필요한가':'왜 검토가 필요한가',
      reasonHtml:esc(x.interpretation),
      quotes:renderSimilarityQuotes(x),
      suggestLabel:'권장 수정안',
      recommendHtml:sharedAll.length?`겹치는 표현(${esc(sharedAll.join('·'))})을 다른 어휘로 바꿔 쓰거나, 인용이라면 출처를 명시하세요.`:'',
      confidenceHtml:`공유 핵심어 ${sharedAll.length}개 일치 · 유사도 점수 ${(x.score*100).toFixed(1)}% — 실제 문자열 비교 결과입니다.`,
      basisHtml:esc(ts.note),
      hasAction:sharedAll.length>0,actionLabel:'수정안 보기'};
  });
}

function curriculumAlignmentIssue(p){
  const a=p.curriculum_alignment,basis=a.decision_basis||{};
  const matchesHtml=a.top_matches.map(m=>`<div class="match"><b>[${esc(m.code)}] ${esc(m.text)}</b><br>일치도 ${(m.score*100).toFixed(1)}% · 핵심어 ${esc((m.matched_keywords||[]).join(', ')||'없음')}<br><span class="note">원고 근거: ${esc((m.evidence||{}).text||'없음')}</span>${m.explanation?`<br><br><span class="note">성취기준 해설</span><br>${esc(m.explanation)}`:''}${(m.application_considerations||[]).length?`<br><br><span class="note">적용 시 고려 사항</span><ul>${m.application_considerations.slice(0,3).map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}</div>`).join('');
  return {tier:a.label==='해당'?'ok':'review',cat:'성취기준',loc:`p.${page}`,
    title:`교과 성취기준 연계 · ${a.label} (${(a.top_score*100).toFixed(1)}%)`,
    summary:basis.interpretation||a.note,
    reasonLabel:'통합 판정 근거',
    reasonHtml:`${esc(basis.interpretation||a.note)}<br><span class="note">${esc(basis.caution||'')}</span>${matchesHtml}`,
    quotes:[],suggestLabel:'권장 조치',recommendHtml:'',confidenceHtml:'',
    basisHtml:`성취기준 원문·해설·적용 시 고려 사항을 함께 읽어 하나의 결과로 표시합니다.<br>${esc(a.note)}`,
    hasAction:false};
}

function recommendIssues(p){
  const r=p.recommendations,list=[];
  const variants=r.activity_variants,vi=(variants&&variants.length>1)?(variantByPage[page]||0):0;
  const activeActivities=(variants&&variants.length>1)?variants[vi].activities:(r.activities||[]);
  if(r.ai_activity_review){
    const fit=r.ai_activity_review.standard_fit||{};
    list.push({tier:'review',cat:'표현',loc:`p.${page}`,title:'AI 검토 요약 확인',
      summary:fit.fit_level?`성취기준 부합도: ${fit.fit_level}`:'',
      reasonLabel:'확인 내용',
      reasonHtml:`의도 파악: ${esc(r.ai_activity_review.intent_analysis||'')}<br>성취기준 부합도: ${esc(fit.fit_level||'')} — ${esc(fit.reason||'')}`,
      quotes:[],suggestLabel:'권장 조치',recommendHtml:'',confidenceHtml:'',basisHtml:'AI(Claude) 기반 기존 활동 검토·재구성 결과입니다.',
      hasAction:false});
  }
  if(r.learning_goal){
    const g=r.learning_goal,unchanged=(g.suggestion||'').trim()===(g.current_text||'').trim();
    const kw=(g.matched_keywords||[]),ref=(g.reference_examples||[]);
    list.push({tier:unchanged?'ok':'review',cat:'표현',loc:`p.${page} · 학습 목표`,
      title:unchanged?'학습 목표 확인됨':'학습 목표 추천 문구 검토',
      summary:g.suggestion,
      reasonLabel:unchanged?'확인 내용':'왜 검토가 필요한가',
      reasonHtml:`현재 문구: ${esc(g.current_text||'없음')}`,
      quotes:[],
      suggestLabel:`권장 수정안${g.curriculum_basis?` · ${esc(g.curriculum_basis.match(/^\[[^\]]+\]/)?.[0]||'')}`:''}`,
      recommendHtml:esc(g.suggestion),recommendCopy:g.suggestion,
      confidenceHtml:(kw.length||ref.length)?`일치 키워드 ${kw.length}개(${esc(kw.join('·'))}) · 참고한 기존 교과서 학습 목표 ${ref.length}건`:'',
      basisHtml:bulletList([esc(g.reason),g.curriculum_basis?`교육과정 근거(원문): ${esc(g.curriculum_basis)}`:'',g.generation_method?`생성 방식: ${esc(g.generation_method)}`:'']),
      hasAction:!unchanged,actionLabel:'수정안 보기'});
  }
  activeActivities.forEach((x,i)=>{
    const unchanged=(x.suggestion||'').trim()===(x.current_text||'').trim()||/^\[유지\]/.test(x.reason||'');
    list.push({tier:unchanged?'ok':'review',cat:'활동 구성',loc:`p.${page} · 활동 ${i+1}`,activityIndex:i+1,
      title:unchanged?`활동 ${i+1} 확인됨`:`활동 ${i+1} 추천 문구 검토`,
      summary:x.suggestion,
      reasonLabel:unchanged?'확인 내용':'왜 검토가 필요한가',
      reasonHtml:`현재 문구: ${esc(x.current_text||'없음')}`,
      quotes:[],suggestLabel:'권장 수정안',
      recommendHtml:esc(x.suggestion),recommendCopy:x.suggestion,
      confidenceHtml:'',
      basisHtml:bulletList([esc(x.reason),x.curriculum_basis?`교육과정 근거(원문): ${esc(x.curriculum_basis)}`:'',x.reference_example?`참고한 교과서 활동 문장 흐름: ${esc(x.reference_example.text)} (${esc(cleanFileName(x.reference_example.file))} ${x.reference_example.page}쪽)`:'']),
      hasAction:!unchanged,actionLabel:'수정안 보기'});
  });
  return list;
}

function repertoireIssues(p){
  const rr=p.repertoire_review;
  return (rr.items||[]).map(x=>({tier:'review',cat:'유사도',loc:`p.${page}`,
    title:`기존 교과서에 같은 곡 있음 · ${x.keyword}`,
    summary:x.matches.length?`${x.matches.length}건 참고 교과서에서 확인`:'',
    reasonLabel:'왜 검토가 필요한가',
    reasonHtml:`'${esc(x.keyword)}'이(가) 이미 다른 교과서에도 실려 있어, 반복 활용 여부를 확인하는 것이 좋습니다.`,
    quotes:[],suggestLabel:'참고 교과서',
    recommendHtml:x.matches.length?x.matches.map(m=>`${esc(cleanFileName(m.file))} ${m.page}쪽`).join('<br>'):'',
    confidenceHtml:'',basisHtml:'등록된 참고 교과서에서 동일 핵심어를 검색한 결과입니다.',
    hasAction:x.matches.length>0,actionLabel:'자세히 보기'}));
}

function activityIdeaIssues(p){
  const ai=p.activity_idea_review;
  return (ai.items||[]).map(x=>({tier:'review',cat:'활동 구성',loc:`p.${page}`,
    title:'새로운 활동 아이디어 제안',summary:x.suggestion,
    reasonLabel:'왜 검토가 필요한가',reasonHtml:`현재 활동: ${esc(x.current_text)}`,
    quotes:[],suggestLabel:'권장 수정안',recommendHtml:esc(x.suggestion),recommendCopy:x.suggestion,
    confidenceHtml:'',
    basisHtml:bulletList([esc(x.reason),`등록된 관련 자료: ${esc(x.related_work.keyword)} — ${esc(x.related_work.note)}`,`교육과정 근거(원문): ${esc(x.curriculum_basis)}`]),
    hasAction:true,actionLabel:'수정안 보기'}));
}

function completionIssues(){
  const c=data.completion;
  const skip=new Set(['성취기준','학습 목표','활동','본문 텍스트']);
  return c.details.filter(d=>!skip.has(d.name)).map(d=>{
    const tier=d.earned>=d.maximum?'ok':(d.earned>0?'review':'fix');
    const toReach=(c.to_reach_100||[]).find(x=>x.name===d.name);
    return {tier,cat:completionBuckets[d.name]||'표현',loc:'문서 전체',title:`완성도 · ${d.name}`,summary:d.reason,
      reasonLabel:'확인 내용',reasonHtml:esc(d.reason),
      quotes:[],suggestLabel:'권장 조치',
      recommendHtml:toReach?esc(toReach.action):'',
      confidenceHtml:'',basisHtml:esc(c.note),
      hasAction:!!toReach,actionLabel:'수정안 보기'};
  });
}

function buildIssues(p){
  const list=[];
  ['achievement_standards','learning_goals','activities'].forEach(key=>list.push(componentIssue(key,p.components[key])));
  list.push(...bodyReviewIssues(p));
  list.push(...similarityIssues(p));
  list.push(curriculumAlignmentIssue(p));
  list.push(...recommendIssues(p));
  list.push(...repertoireIssues(p));
  list.push(...activityIdeaIssues(p));
  if(page===1)list.push(...completionIssues());
  return list.map((x,i)=>({id:'i'+i,dimmed:false,...x}));
}

function renderMetrics(p,issues){
  const c=data.completion;
  const actionCount=issues.filter(x=>x.tier==='fix'||x.tier==='review').length;
  const simItems=[...p.textbook_similarity.items,...p.activity_textbook_similarity.items];
  const maxSim=simItems.length?Math.max(...simItems.map(x=>x.score)):0;
  const simFlag=simItems.some(x=>x.review_required);
  $('metrics-bar').innerHTML=`
    <div class="metric-card"><div class="metric-label"><span class="dot dot-blue"></span>전체 완성도</div><div class="metric-value">${c.percentage}%</div><div class="progress-track"><div class="progress-fill" style="width:${c.percentage}%"></div></div><div class="metric-sub">${esc(c.explanation)}</div></div>
    <div class="metric-card"><div class="metric-label"><span class="dot dot-blue"></span>교과 성취기준 연계</div><div class="metric-value">${(p.curriculum_alignment.top_score*100).toFixed(1)}%</div><div class="metric-sub">${esc(p.curriculum_alignment.label)} · 이 페이지 기준</div></div>
    <div class="metric-card${simFlag?' alert':''}"><div class="metric-label"><span class="dot ${simFlag?'dot-red':'dot-blue'}"></span>교과서 유사도</div><div class="metric-value">${(maxSim*100).toFixed(1)}%${simFlag?' · 검토 필요':''}</div><div class="metric-sub">이 페이지 본문·활동 비교 중 최고값</div></div>
    <div class="metric-card"><div class="metric-label"><span class="dot dot-neutral"></span>조치 필요</div><div class="metric-value">${actionCount}건</div><div class="metric-sub">이 페이지 · 전체 ${issues.length}건 중</div></div>`;
}

function renderVariantTabs(p){
  const variants=p.recommendations.activity_variants;
  if(!variants||variants.length<=1){$('variant-tabs').innerHTML='';return}
  const vi=variantByPage[page]||0;
  $('variant-tabs').innerHTML=variants.map((v,i)=>`<button type="button" class="variant-tab${i===vi?' active':''}" data-vi="${i}">${esc(v.label||('안 '+(i+1)))}</button>`).join('');
  document.querySelectorAll('.variant-tab').forEach(btn=>btn.onclick=()=>{variantByPage[page]=Number(btn.dataset.vi);selectedId=null;render()});
}

function cardHtml(x){
  const badge=`${esc(x.cat)}${x.activityIndex?` · ${x.activityIndex}`:''}`;
  return `<div class="item-card ${tierSeverityClass(x.tier)}${x.id===selectedId?' selected':''}${x.dimmed?' fp-dim':''}" data-id="${esc(x.id)}">
    <div class="item-row1"><span class="item-tag ${catTagClass(x.cat)}">${badge}</span><span class="item-title">${esc(x.title)}</span><span class="item-loc">${esc(x.loc)}</span></div>
    <div class="item-row2"><span class="item-desc">${esc(x.summary||'')}</span>${x.hasAction?`<button type="button" class="mini-link">${esc(x.actionLabel||'수정안 보기')}<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg></button>`:''}</div>
  </div>`;
}

function renderChecklist(){
  const fix=currentIssues.filter(x=>x.tier==='fix');
  const review=currentIssues.filter(x=>x.tier==='review');
  const ok=currentIssues.filter(x=>x.tier==='ok');
  if(!currentIssues.some(x=>x.id===selectedId))selectedId=currentIssues[0]?currentIssues[0].id:null;

  $('queue-total').textContent=`${currentIssues.length}건`;
  $('status-counts').innerHTML=`<span class="count-fix"><span class="dot dot-red"></span>수정 필요 ${fix.length}</span><span class="count-review"><span class="dot" style="background:var(--amber)"></span>검토 필요 ${review.length}</span><span class="count-ok"><span class="dot" style="background:var(--green)"></span>통과 ${ok.length}</span>`;

  let html=`<div class="section-label sl-fix">수정 필요 <span class="count">· ${fix.length}건</span></div>`;
  html+=fix.length?fix.map(cardHtml).join(''):'<div class="note" style="padding:0 8px 8px">해당 없음</div>';

  html+=`<div class="section-label sl-review">검토 필요 <span class="count">· ${review.length}건</span></div>`;
  if(review.length){
    html+=cardHtml(review[0]);
    if(review.length>1){
      html+=`<div class="collapsed-section" data-toggle="review-rest"><span>${reviewExpanded?'검토 필요 항목 접기':`검토 필요 항목 ${review.length-1}건 더 보기`}</span><span class="section-toggle" data-toggle="review-rest">${reviewExpanded?'접기':'펼치기'}</span></div>`;
      html+=`<div data-group-body="review-rest" style="display:${reviewExpanded?'':'none'}">${review.slice(1).map(cardHtml).join('')}</div>`;
    }
  } else {
    html+='<div class="note" style="padding:0 8px 8px">해당 없음</div>';
  }

  if(ok.length){
    html+=`<div class="collapsed-section" data-toggle="ok-all"><span>${okExpanded?'통과 항목 접기':`통과 항목 ${ok.length}건`}</span><span class="section-toggle" data-toggle="ok-all">${okExpanded?'접기':'펼치기'}</span></div>`;
    html+=`<div data-group-body="ok-all" style="display:${okExpanded?'':'none'}">${ok.map(cardHtml).join('')}</div>`;
  }

  $('checklist-body').innerHTML=html;
  document.querySelectorAll('.item-card').forEach(el=>el.onclick=()=>{selectedId=el.dataset.id;renderChecklist()});
  document.querySelectorAll('[data-toggle]').forEach(el=>el.onclick=event=>{event.stopPropagation();const key=el.dataset.toggle;if(key==='review-rest')reviewExpanded=!reviewExpanded;else okExpanded=!okExpanded;renderChecklist()});
  renderDetail(currentIssues.find(x=>x.id===selectedId));
}

function renderDetail(issue){
  if(!issue){$('detail-panel').innerHTML='<div class="panel-header"><div class="panel-title">선택한 이슈</div></div><div class="empty-detail">표시할 이슈가 없습니다.</div>';return}
  const quotesHtml=(issue.quotes||[]).map(q=>`<div class="quote-box">${q.fp||''}<div class="quote-label">${esc(q.label)}</div>${q.html}</div>`).join('');
  const copyBtn=issue.recommendCopy?`<button type="button" class="mini-btn" data-copy="${esc(issue.recommendCopy)}">복사</button>`:'';
  const recommendBlock=issue.recommendHtml?`<div class="block"><div class="block-label">${esc(issue.suggestLabel||'권장 수정안')}</div><div class="recommend-box"><span>${issue.recommendHtml}</span>${copyBtn}</div></div>`:'';
  const confidenceBlock=issue.confidenceHtml?`<div class="block"><div class="block-label">AI 판정 신뢰도</div><div class="confidence-box">${issue.confidenceHtml}</div></div>`:'';
  $('detail-panel').innerHTML=`
    <div class="panel-header"><div class="panel-title">선택한 이슈</div><span class="issue-status-pill ${issue.tier}">${tierLabel(issue.tier)}</span></div>
    <div class="right-panel-body" id="right-panel-body">
      <div class="issue-title">${esc(issue.title)}</div>
      <div class="block"><div class="block-label">${esc(issue.reasonLabel)}</div><div class="block-text">${issue.reasonHtml}</div>${quotesHtml}</div>
      ${recommendBlock}
      ${confidenceBlock}
      <div class="divider"></div>
      <div class="block" style="margin-bottom:0"><div class="block-label">판정 근거</div><div class="block-text" style="color:var(--text-2)">${issue.basisHtml}</div></div>
    </div>
    <div class="scroll-fade" id="detail-scroll-fade"></div>`;
  const body=$('right-panel-body');
  const fade=$('detail-scroll-fade');
  const updateFade=()=>fade.classList.toggle('visible',body.scrollHeight-body.scrollTop-body.clientHeight>4);
  body.onscroll=updateFade;
  updateFade();
}

function render(){
  const p=data.page_audits[page-1];
  $('page-now').textContent=page;
  $('page-total').textContent=data.manuscript.page_count;
  $('page-sub').textContent=`p. ${page} / ${data.manuscript.page_count}`;
  renderPagePreview(p);
  $('audit-title').textContent=`${page}쪽 교육과정 점검`;
  renderVariantTabs(p);
  currentIssues=buildIssues(p);
  renderMetrics(p,currentIssues);
  renderChecklist();
}
initPages();initZoom();loadFp().then(render)})();
</script></body></html>'''


if __name__ == "__main__":
    import argparse
    command = argparse.ArgumentParser()
    command.add_argument("manuscript", type=Path)
    command.add_argument("--curriculum", type=Path, required=True)
    command.add_argument("--output", type=Path, default=Path("output/audit"))
    command.add_argument("--ai-module")
    command.add_argument("--textbooks", type=Path, default=Path("★타사 교과서"))
    command.add_argument("--target-level", choices=tuple(TARGET_LEVELS), default="고등학교 1학년")
    command.add_argument("--related-works", type=Path,
                         help="관련 자료(리메이크·다른 버전 등) 키워드→설명 JSON 파일")
    args = command.parse_args()
    related_works = (
        json.loads(args.related_works.read_text(encoding="utf-8")) if args.related_works else None
    )
    print(audit_manuscript(args.manuscript, args.curriculum, args.output,
                           args.ai_module, args.textbooks, args.target_level, related_works))
