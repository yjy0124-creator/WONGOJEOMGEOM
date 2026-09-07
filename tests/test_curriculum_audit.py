import unittest
from collections import Counter

from curriculum_audit import (
    _activity_semantic_similarity,
    _adapt_activity_to_topic,
    _cap_examples_by_genre,
    _completion_score,
    _compare_activity_similarity,
    _compare_textbook_similarity,
    _content_overlap_basis,
    _check_repertoire_overlap,
    _evaluate_activity_curriculum,
    _evaluate_activity_level,
    _fingerprint,
    _infer_genre,
    _infer_piece_type,
    _review_body_text,
    _recommendations,
    _recommend_layout,
    _select_reference_samples,
    _select_typed_reference_activities,
    _simplify_activity,
    compare_audits,
    detect_manuscript_components,
    extract_curriculum_standards,
    match_curriculum,
    similarity,
)


class CurriculumAuditTests(unittest.TestCase):
    def test_detects_goal_and_numbered_activities(self):
        pages = [
            "대취타와 취타 · 두 악곡의 음악적 특징을 비교하여 설명할 수 있다.\n"
            "1. 두 악곡을 감상하고 특징을 비교해 보자.\n"
            "2. 장단을 연주해 보자."
        ]
        result = detect_manuscript_components(pages)
        self.assertFalse(result["achievement_standards"]["included"])
        self.assertEqual(result["learning_goals"]["count"], 1)
        self.assertEqual(result["activities"]["count"], 2)

    def test_detects_conjugated_learning_goal_after_label(self):
        result = detect_manuscript_components([
            "학습 목표 : 다양한 뮤지컬 넘버를 감상한 후 미적 특성을 반영하여 뮤지컬 넘버를 부를 수 있다."
        ])
        self.assertEqual(result["learning_goals"]["count"], 1)
        self.assertTrue(result["learning_goals"]["items"][0]["text"].startswith("다양한 뮤지컬"))

    def test_labeled_goal_and_wrapped_activities_do_not_absorb_body(self):
        result = detect_manuscript_components([
            "학습 목표 : 다양한 오페라에 대해 알아보고 대표\n"
            "적인 오페라 아리아를 감상한 후 음악에 내재된 미적 특성을 반영하여 아리아를 부를 수 있다.\n"
            "집시 카르멘과 병사 돈 호세의 사랑과 갈등을 화려한 리듬에서 느낄 수 있다.\n"
            "(감상) [활동 1] 하바네라 리듬을 치면서 제재곡을 불러 보자.\n"
            "(감상) [활동 2] ‘카르멘’의 대표 등장인물들이 부르는 아리아를 감상해 보고, 모둠별로 인물의 성격과 음\n"
            "색의 특징을 비교하여 설명해 보자"
        ])
        self.assertEqual(result["learning_goals"]["count"], 1)
        self.assertNotIn("돈 호세", result["learning_goals"]["items"][0]["text"])
        self.assertEqual(result["activities"]["count"], 2)
        self.assertIn("음색의 특징", result["activities"]["items"][1]["text"])

    def test_detects_unnumbered_activity_instructions(self):
        pages = [
            "느린 빠르기의 발라드를 감상하면서 연상되는 단어를 이야기해보자.\n"
            "가사를 읽고 떠오르는 대상에게 편지 형식으로 작성해 보자."
        ]
        result = detect_manuscript_components(pages)
        self.assertEqual(result["activities"]["count"], 2)
        self.assertTrue(all(item["method"] == "지시문 종결형 감지" for item in result["activities"]["items"]))

    def test_extracts_wrapped_curriculum_standard(self):
        pages = [
            "나. 성취기준\n"
            "[12감비01-01] 음악 요소와 악곡 구성의 원리를 이해하며 음악을 듣고 특징을 비교·분석하\n"
            "여 설명한다.\n"
            "[12감비01-02] 다양한 시대의 음악 변화를 파악한다.\n"
            "(가) 성취기준 해설"
        ]
        standards = extract_curriculum_standards(pages)
        self.assertEqual(len(standards), 2)
        self.assertIn("설명한다", standards[0]["text"])

    def test_extracts_standard_explanation_and_application_considerations(self):
        pages = [
            "(1) 감상과 반응\n[12감비01-01] 음악의 특징을 비교한다.\n"
            "(가) 성취기준 해설\n• [12감비01-01] 느낌과 견해를 설명하도록 설정하였다.\n"
            "• (나) 성취기준 적용 시 고려 사항\n• 음악 요소를 분석적으로 다룬다.\n"
            "• 감상과 반응을 연계한다.\n(2) 비평과 활용\n"
            "[12감비02-01] 다양한 관점에서 음악을 비평한다.\n3. 교수⋅학습 및 평가"
        ]
        standards = extract_curriculum_standards(pages)
        self.assertIn("느낌과 견해", standards[0]["explanation"])
        self.assertEqual(len(standards[0]["application_considerations"]), 2)

    def test_matching_prefers_comparison_standard(self):
        components = detect_manuscript_components([
            "두 악곡을 감상하고 음악적 특징의 공통점과 차이점을 비교하여 설명할 수 있다.\n"
            "1. 두 악곡의 특징을 비교해 보자."
        ])
        standards = [
            {"code": "12감비01-01", "text": "음악을 듣고 특징을 비교·분석하여 설명한다.", "page": 4},
            {"code": "12감비01-04", "text": "자신의 음악적 취향을 발견하고 감상 경험을 공유한다.", "page": 4},
        ]
        result = match_curriculum(components, [""], standards)
        self.assertEqual(result["top_matches"][0]["code"], "12감비01-01")
        self.assertGreater(result["top_matches"][0]["score"], result["top_matches"][1]["score"])
        self.assertEqual(result["status"], "applicable")

    def test_similarity_is_bounded(self):
        score = similarity("음악의 특징을 비교하여 설명한다", "음악 특징을 비교 분석하여 설명한다")
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 1)

    def test_missing_standard_recommends_matched_curriculum_text_verbatim(self):
        components = detect_manuscript_components([
            "대취타와 취타 · 두 악곡의 특징을 비교하여 설명할 수 있다.\n"
            "1. 두 악곡을 비교해 보자."
        ])
        alignment = {
            "top_matches": [{
                "code": "12감비01-01", "text": "음악의 특징을 비교·분석하여 설명한다.",
                "page": 4, "score": .7, "matched_keywords": ["특징", "비교"],
            }]
        }
        result = _recommendations(components, alignment, ["대취타와 취타"])
        # 성취기준 문구는 새로 짓지 않고 교육과정 원문을 그대로 추천한다.
        self.assertIn("12감비01-01", result["achievement_standard"]["suggestion"])
        self.assertIn("음악의 특징을 비교·분석하여 설명한다", result["achievement_standard"]["suggestion"])
        self.assertIn("수정하지 않고", result["curriculum_policy"])
        self.assertIn("공통점과 차이점", result["learning_goal"]["suggestion"])
        self.assertTrue(result["learning_goal"]["curriculum_basis"])

    def test_recommended_goal_has_no_decorative_special_characters(self):
        components = detect_manuscript_components(["뮤지컬 넘버를 감상해 보자."])
        alignment = {"top_matches": [{
            "code": "12감비02-01", "text": "사회⋅문화적 의미와 음악적 특징을 비평한다.",
            "page": 1, "score": .5, "matched_keywords": ["문화"],
        }]}
        result = _recommendations(components, alignment, ["뮤지컬 넘버"])
        self.assertNotRegex(result["learning_goal"]["suggestion"], r"[·⋅∙\[\]()]")

    def test_activities_recommend_nothing_without_reference_examples(self):
        # 등록된 참고 활동 표본이 없으면, 무관한 활동을 지어내지 않고 정직하게 빈 목록을 낸다.
        components = detect_manuscript_components(["1. 곡을 감상하고 특징을 비교해 보자."])
        alignment = {"top_matches": [{
            "code": "12감비01-01", "text": "음악의 특징을 비교하여 설명한다.",
            "page": 1, "score": .5, "matched_keywords": ["특징"],
        }]}
        result = _recommendations(components, alignment, ["제재곡"])
        self.assertEqual(result["activities"], [])

    def test_activities_recommend_typed_suggestions_from_reference_examples(self):
        components = detect_manuscript_components(["1. 곡을 감상하고 특징을 비교해 보자."])
        alignment = {"top_matches": [{
            "code": "12감비01-01", "text": "음악의 특징을 비교하여 설명한다.",
            "page": 1, "score": .5, "matched_keywords": ["특징"],
        }]}
        reference = {"activity_readability": {"examples": [
            {"file": "교과서.pdf", "page": 1, "text": "곡을 감상하고 느낌을 나누어 보자."},
            {"file": "교과서.pdf", "page": 2, "text": "시대적 배경을 살펴보고 생각을 써 보자."},
        ]}}
        result = _recommendations(components, alignment, ["새로운 제재곡"], reference=reference)
        self.assertTrue(result["activities"])
        for item in result["activities"]:
            self.assertIn("activity_type", item)
            self.assertIn("새로운 제재곡", item["suggestion"])

    def test_activity_rewrite_uses_natural_textbook_flow(self):
        first = _simplify_activity(
            "기타 음색이 주는 분위기를 파악하며 감상하고 포크송의 특징을 조사해보자."
        )
        second = _simplify_activity(
            "악곡의 원곡을 감상해 보고, 포크송과 청년 문화와의 관련성을 토론해 보자."
        )
        self.assertEqual(first, "포크 송의 특징을 알아보고, 기타 음색에 집중하며 감상해 보자.")
        self.assertEqual(second, "원곡과 비교 감상하고, 포크 송과 청년 문화의 관련성을 모둠별로 조사하여 토의해 보자.")

    def test_activity_similarity_groups_related_instruments(self):
        score, basis = _activity_semantic_similarity(
            "기타 음색에 집중하며 감상해 보자.",
            "우쿨렐레의 음색을 들으며 특징을 말해 보자.",
        )
        self.assertIn("현악기", basis["shared_instrument_families"])
        self.assertGreaterEqual(score, .32)
        _, wind_basis = _activity_semantic_similarity(
            "단소의 음색을 들어 보자.", "소금의 음색을 감상해 보자."
        )
        self.assertIn("관악기", wind_basis["shared_instrument_families"])
        _, boundary_basis = _activity_semantic_similarity(
            "기타 음색의 특징을 감상해 보자.", "음악적 특징을 설명해 보자."
        )
        self.assertNotIn("징", boundary_basis["shared_instruments"])
        self.assertNotIn("타악기", boundary_basis["shared_instrument_families"])

    def test_body_review_includes_activity_and_improves_explanation(self):
        pages = [
            "두 악곡의 특징을 비교하여 설명할 수 있다.\n"
            "이 곡은 악기편성을 비교하는 데 알맞은 교향곡이다.\n"
            "1. 두 악곡의 악기편성을 비교해 보자."
        ]
        components = detect_manuscript_components(pages)
        result = _review_body_text(pages, components)
        items = result["pages"][1]
        # 활동 문장도 맞춤법 검사 대상에 포함되어 본문 설명 문장과 함께 2건이 나온다.
        self.assertEqual(len(items), 2)
        explanation = next(x for x in items if "교향곡" in x["current_text"])
        activity = next(x for x in items if x["current_text"].startswith("1."))
        self.assertIn("악기 편성", explanation["suggested_text"])
        self.assertIn("악기 편성", activity["suggested_text"])
        self.assertNotIn("비교해 보자", items[0]["current_text"])
        # '교향곡'은 편수자료 공식 용어집에 등재된 표준 용어라 인식돼야 한다.
        self.assertTrue(any(term["term"] == "교향곡" for term in explanation["terminology"]))

    def test_completion_score_uses_declared_weights(self):
        page = ("[12감비01-01] 음악 특징 비교\n학습 목표: 두 악곡을 비교하여 설명할 수 있다.\n"
                "1. 두 악곡을 비교해 보자.\n이 곡은 서로 다른 악기와 선율을 비교하는 제재이다.\n"
                "악보와 작곡가 사진 출처를 표시한다.")
        components = detect_manuscript_components([page])
        score = _completion_score(
            components, {"top_score": .65},
            [{"images": [{"bbox": [0, 0, 300, 120]}]}], [page],
        )
        self.assertEqual(score["percentage"], 100)
        self.assertFalse(score["to_reach_100"])

    def test_activity_similarity_ignores_generic_boja_ending(self):
        components = detect_manuscript_components(["1. 오페라 카르멘의 아리아를 감상하고 인물의 음색을 비교해 보자."])
        reference = {"file_count": 1, "chunk_count": 1, "chunks": [{
            "file": "교과서.pdf", "page": 3, "text": "민요의 장단을 악기로 연주해 보자."
        }]}
        result = _compare_activity_similarity([""], components, reference)
        # 핵심어가 3개 미만 겹치므로 화면에 표시되지 않는다.
        self.assertEqual(result["pages"][1], [])

    def test_activity_level_marks_long_abstract_instruction(self):
        component = {
            "items": [{"page": 1, "number": 1,
                       "text": "1. 사회·문화적 시대적 맥락과 음악 요소를 다양한 관점에서 비교·분석하고 구체적인 음악적 근거와 함께 설명해 보자."}]
        }
        reference = {"activity_readability": {
            "sample_count": 20, "average_length": 30, "p75_length": 45,
        }}
        result = _evaluate_activity_level(component, reference)
        self.assertNotEqual(result["items"][0]["status"], "적절")
        self.assertTrue(result["items"][0]["recommended_text"])

    def test_activity_level_changes_with_selected_learner_level(self):
        component = {"items": [{"page": 1, "number": 1,
                                 "text": "곡을 듣고 떠오르는 느낌을 적은 뒤 친구들과 이야기해 보자."}]}
        reference = {"activity_readability": {
            "sample_count": 10, "average_length": 30, "p75_length": 40,
            "examples": [{"file": "교과서.pdf", "page": 1, "text": "곡을 듣고 느낌을 이야기해 보자."}],
        }}
        elementary = _evaluate_activity_level(component, reference, "초등학교 저학년")
        high = _evaluate_activity_level(component, reference, "고등학교 1학년")
        self.assertNotEqual(elementary["items"][0]["status"], high["items"][0]["status"])
        self.assertEqual(high["grade"], "고등학교 1학년")

    def test_body_review_adds_comma_to_long_connective_clause(self):
        sentence = "우리나라에서는 70년대 서양의 새로운 장르로 받아들여졌고 팝송을 커버하며 자연스럽게 포크송이 정착하였다."
        components = detect_manuscript_components([sentence])
        result = _review_body_text([sentence], components)
        suggestion = result["pages"][1][0]["suggested_text"]
        self.assertIn("받아들여졌고, 팝송", suggestion)

    def test_activity_curriculum_alignment_uses_original_standard(self):
        activities = {"items": [{"page": 1, "number": 1,
                                  "text": "1. 두 음악의 특징을 비교하여 설명해 보자."}]}
        goals = {"items": [{"text": "두 음악의 특징을 비교하여 설명할 수 있다."}]}
        standards = [{"code": "12감비01-01",
                      "text": "음악을 듣고 특징을 비교·분석하여 설명한다.", "page": 4}]
        result = _evaluate_activity_curriculum(activities, goals, standards)
        self.assertEqual(result["items"][0]["status"], "알맞음")
        self.assertEqual(result["items"][0]["matched_standard"]["text"], standards[0]["text"])

    def test_textbook_similarity_finds_matching_body_sentence(self):
        pages = ["이 곡은 서정적인 가사와 선율이 돋보이는 작품이다."]
        components = detect_manuscript_components(pages)
        reference = {"file_count": 1, "chunk_count": 1, "chunks": [{
            "file": "교과서.pdf", "page": 10,
            "text": "이 곡은 서정적인 가사와 선율이 돋보이는 작품이다.",
        }]}
        result = _compare_textbook_similarity(pages, components, reference)
        self.assertEqual(result["pages"][1][0]["status"], "매우 유사")
        self.assertEqual(result["summary"]["similar_count"], 1)

    def test_textbook_similarity_requires_two_semantic_elements(self):
        pages = ["이 곡은 기타 음색이 주는 분위기가 인상적인 작품이다."]
        components = detect_manuscript_components(pages)
        reference = {"file_count": 1, "chunk_count": 1, "chunks": [{
            "file": "교과서.pdf", "page": 4,
            "text": "다른 시대의 작품에서 기타 연주를 살펴본다.",
        }]}
        result = _compare_textbook_similarity(pages, components, reference)
        self.assertEqual(result["pages"][1], [])

    def test_content_overlap_groups_equivalent_music_actions(self):
        basis = _content_overlap_basis(
            "기타 음색이 주는 분위기를 파악하여 감상하고 포크송의 특징을 조사한다.",
            "우쿨렐레 연주를 듣고 음색을 파악하여 곡의 특징을 말한다.",
        )
        self.assertIn("감상·듣기", basis["shared_meanings"])
        self.assertIn("특징 확인·설명", basis["shared_meanings"])

    def test_textbook_similarity_requires_three_shared_keywords(self):
        pages = ["기타 음색이 주는 분위기를 파악하여 감상하고 포크송의 특징을 조사한다."]
        components = detect_manuscript_components(pages)
        reference = {"file_count": 1, "chunk_count": 1, "chunks": [{
            "file": "교과서.pdf", "page": 5,
            "text": "우쿨렐레 연주를 듣고 음색을 파악하여 곡의 특징을 말한다.",
        }]}
        result = _compare_textbook_similarity(pages, components, reference)
        # 겹치는 핵심어가 '특징을' 한 개뿐이라 3개 기준에 못 미쳐 표시하지 않는다.
        self.assertEqual(result["pages"][1], [])

    def test_textbook_similarity_shows_three_or_more_shared_keywords(self):
        pages = ["기타 음색이 주는 분위기를 파악하여 감상하고 포크송의 특징을 조사한다."]
        components = detect_manuscript_components(pages)
        reference = {"file_count": 1, "chunk_count": 1, "chunks": [{
            "file": "교과서.pdf", "page": 5,
            "text": "기타 음색이 주는 분위기를 파악하여 감상하고 포크송의 특징을 말한다.",
        }]}
        result = _compare_textbook_similarity(pages, components, reference)
        item = result["pages"][1][0]
        self.assertGreaterEqual(len(item["shared_keywords"]), 3)

    def test_layout_recommendation_changes_by_page_content(self):
        components = detect_manuscript_components([
            "1. 악곡을 감상해 보자.\n2. 특징을 비교해 보자."
        ])
        reference = {"file_count": 5, "sampled_pages": 30,
                     "patterns": {"activity_table": 18}, "examples": []}
        result = _recommend_layout(1, "활동", components, 0, reference)
        self.assertEqual(result["type"], "activity_sequence")
        self.assertGreaterEqual(len(result["reasons"]), 2)

    def test_repertoire_overlap_extracts_quoted_song_title(self):
        result = _check_repertoire_overlap(
            "뮤지컬 위키드의 ‘중력을 벗어나’를 감상한다.", [], None,
            [{"file": "기존.pdf", "page": 12,
              "text": "중력을 벗어나를 노래하며 인물의 마음을 살펴보자."}],
        )
        self.assertTrue(result["checked"])
        matched = next(item for item in result["items"] if item["keyword"] == "중력을 벗어나")
        self.assertEqual(matched["status"], "같은 곡 있음")

    def test_repertoire_overlap_does_not_display_missing_titles(self):
        result = _check_repertoire_overlap("뮤지컬 ‘없는 작품’을 감상한다.", [], None, [])
        self.assertEqual(result["items"], [])

    def test_repertoire_overlap_uses_declared_titles_and_ignores_genre(self):
        result = _check_repertoire_overlap(
            "발라드 곡을 감상한다.", [], None,
            [
                {"file": "기존.pdf", "page": 1, "text": "대표적인 발라드의 특징"},
                {"file": "기존.pdf", "page": 2, "text": "소녀와"},
                {"file": "기존.pdf", "page": 3, "text": "두 바퀴로 가는 자동차"},
            ],
            ["소녀와", "두 바퀴로 가는 자동차"],
        )
        self.assertEqual([item["keyword"] for item in result["items"]], ["소녀와", "두 바퀴로 가는 자동차"])
        self.assertNotIn("발라드", [item["keyword"] for item in result["items"]])

    def test_short_work_title_does_not_match_longer_title_or_group_name(self):
        result = _check_repertoire_overlap(
            "곡명 소녀", [], None,
            [
                {"file": "기존.pdf", "page": 1, "text": "‘아마빛 머리의 소녀’를 감상해 보자."},
                {"file": "기존.pdf", "page": 2, "text": "소녀시대의 노래를 감상한다."},
                {"file": "기존.pdf", "page": 3, "text": "‘소녀’를 노래해 보자."},
            ],
            ["소녀"],
        )
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual([match["page"] for match in result["items"][0]["matches"]], [3])

    def test_layout_recommends_two_page_spread_and_checks_score(self):
        components = detect_manuscript_components(["악보를 보며 선율을 연주해 보자."])
        context = [
            {"page": 1, "text": "악보를 보며 선율을 연주해 보자.",
             "components": components, "images": [{"bbox": [10, 10, 400, 180]}]},
            {"page": 2, "text": "삽화를 보고 느낌을 이야기해 보자.",
             "components": components, "images": [{"bbox": [20, 20, 300, 220]}]},
        ]
        result = _recommend_layout(1, context[0]["text"], components,
                                   context[0]["images"], {"examples": []}, context)
        self.assertEqual(result["spread"]["page_count"], 2)
        self.assertTrue(result["spread"]["pages"][0]["score_check"]["detected"])
        self.assertNotIn("alternatives", result)

    def test_adapt_activity_to_topic_replaces_clear_leading_subject(self):
        result = _adapt_activity_to_topic(
            "당시의 정치나 경제 상황을 표현한 곡을 찾아서 감상해 보고 음악과 정치, 경제와의 연관성에 대하여 토론해 보자.",
            "임이 오시는지",
        )
        self.assertEqual(
            result,
            "임이 오시는지를 표현한 곡을 찾아서 감상해 보고 음악과 정치, 경제와의 연관성에 대하여 토론해 보자.",
        )

    def test_adapt_activity_to_topic_skips_ambiguous_compound_subject(self):
        # '이날치'의 '이'를 조사로 착각해 '레 미제라블이 날치의…' 같은 문장이 나오면 안 된다.
        result = _adapt_activity_to_topic(
            "4 제재곡과 밴드 이날치의 ‘범 내려온다’를 감상하고, 두 악곡의 특징과 느낌을 서로 비교해 보자.",
            "레 미제라블",
        )
        self.assertIsNone(result)

    def test_fingerprint_is_stable_and_content_sensitive(self):
        first = _fingerprint("body_text", "두 악곡의 특징을 비교한다.")
        again = _fingerprint("body_text", "두 악곡의   특징을 비교한다.")
        different_text = _fingerprint("body_text", "다른 문장이다.")
        different_section = _fingerprint("textbook_similarity", "두 악곡의 특징을 비교한다.")
        self.assertEqual(first, again)
        self.assertNotEqual(first, different_text)
        self.assertNotEqual(first, different_section)

    def test_body_review_marks_suppressed_fingerprint_as_false_positive(self):
        pages = ["두 악곡의 음악적 특징을 비교하여 설명한다."]
        components = detect_manuscript_components(pages)
        fingerprint = _fingerprint("body_text", pages[0])
        result = _review_body_text(pages, components, {fingerprint})
        item = result["pages"][1][0]
        self.assertEqual(item["fingerprint"], fingerprint)
        self.assertTrue(item["is_false_positive"])

    def test_compare_audits_classifies_resolved_new_and_unchanged(self):
        def make_result(sentences, percentage, page_count=1):
            return {
                "manuscript": {"filename": "원고.pdf", "page_count": page_count},
                "completion": {"percentage": percentage},
                "page_audits": [{
                    "body_text_review": {"items": [
                        {"current_text": text, "fingerprint": _fingerprint("body_text", text)}
                        for text in sentences
                    ]},
                    "textbook_similarity": {"items": []},
                    "activity_textbook_similarity": {"items": []},
                }],
            }

        previous = make_result(["해결될 문장이다.", "계속 남는 문장이다."], 60)
        current = make_result(["계속 남는 문장이다.", "새로 생긴 문장이다."], 75)
        diff = compare_audits(previous, current)
        body = diff["sections"]["body_text_review"]
        self.assertEqual([item["text"] for item in body["resolved"]], ["해결될 문장이다."])
        self.assertEqual([item["text"] for item in body["new"]], ["새로 생긴 문장이다."])
        self.assertEqual(body["unchanged_count"], 1)
        self.assertEqual(diff["completion_before"], 60)
        self.assertEqual(diff["completion_after"], 75)
        self.assertEqual(diff["completion_delta"], 15)

    def test_select_reference_samples_ranks_by_relevance(self):
        examples = [
            {"file": "a.pdf", "page": 1, "text": "특징을 비교하여 설명해 보자."},
            {"file": "b.pdf", "page": 2, "text": "오늘 날씨가 좋다."},
            {"file": "c.pdf", "page": 3, "text": "음악 요소를 비교·분석하여 특징을 설명해 보자."},
        ]
        samples = _select_reference_samples(
            examples, "12감비01-01",
            "음악 요소와 악곡 구성의 원리를 이해하며 특징을 비교·분석하여 설명한다.", limit=2,
        )
        self.assertEqual(len(samples), 2)
        self.assertIn("음악 요소를 비교·분석하여 특징을 설명해 보자.", samples)

    def test_infer_piece_type_detects_vocal_and_instrumental(self):
        self.assertEqual(
            _infer_piece_type(["이 곡은 가사가 애틋한 가곡으로 노래 부르는 활동이 중심이다."]), "가창곡"
        )
        self.assertEqual(
            _infer_piece_type(["이 곡은 악기로 연주하며 리듬을 치는 관악기 활동이 중심이다."]), "연주곡"
        )
        self.assertEqual(_infer_piece_type(["이 곡은 감상하며 느낌을 나눈다."]), "감상곡")

    def test_infer_genre_detects_opera_musical_pop_instrumental_and_traditional(self):
        self.assertEqual(_infer_genre("오페라 아리아를 감상해 보자."), "오페라")
        self.assertEqual(_infer_genre("뮤지컬 넘버를 따라 불러 보자."), "뮤지컬")
        self.assertEqual(_infer_genre("이 가요는 발라드 장르에 속한다."), "가요·대중음악")
        self.assertEqual(_infer_genre("이 교향곡은 관현악 편성이 돋보인다."), "클래식 기악곡")
        self.assertEqual(_infer_genre("판소리 한 대목을 듣고 장단을 쳐 보자."), "국악")
        self.assertEqual(_infer_genre("슈베르트의 대표적인 리트를 감상해 보자."), "예술가곡")

    def test_infer_genre_prioritizes_traditional_signal_over_art_song_label(self):
        # '가곡'은 서양 예술가곡과 국악 전통 성악(정가)을 모두 가리킬 수 있어,
        # 판소리 같은 국악 신호가 함께 있으면 국악으로 분류되어야 한다.
        self.assertEqual(
            _infer_genre("전통 성악곡인 가곡, 가사, 시조 중 가곡을 판소리와 비교해 보자."), "국악"
        )

    def test_infer_genre_returns_none_for_neutral_text(self):
        self.assertIsNone(_infer_genre("이 곡을 감상하고 느낌을 나누어 보자."))

    def test_cap_examples_by_genre_prevents_dominant_genre_from_crowding_out_others(self):
        bucket_counts = Counter()
        opera_candidates = [{"file": f"오페라{i}.pdf", "page": 1, "text": f"오페라 활동 {i}"} for i in range(20)]
        traditional_candidates = [{"file": f"국악{i}.pdf", "page": 1, "text": f"국악 활동 {i}"} for i in range(20)]
        accepted = []
        accepted.extend(_cap_examples_by_genre(opera_candidates, "오페라", bucket_counts, 15))
        accepted.extend(_cap_examples_by_genre(traditional_candidates, "국악", bucket_counts, 15))
        self.assertEqual(len(accepted), 30)
        self.assertEqual(sum(1 for item in accepted if item["genre"] == "오페라"), 15)
        self.assertEqual(sum(1 for item in accepted if item["genre"] == "국악"), 15)

    def test_cap_examples_by_genre_buckets_none_as_general(self):
        bucket_counts = Counter()
        candidates = [{"file": "x.pdf", "page": 1, "text": f"활동 {i}"} for i in range(3)]
        accepted = _cap_examples_by_genre(candidates, None, bucket_counts, 15)
        self.assertEqual(len(accepted), 3)
        self.assertEqual(bucket_counts["일반"], 3)
        self.assertTrue(all(item["genre"] is None for item in accepted))

    def test_select_typed_reference_activities_reorders_matching_genre_first(self):
        high_score = {"file": "a.pdf", "page": 1, "text": "음악의 특징을 비교하며 감상해보자.", "genre": None}
        low_score_matching = {"file": "b.pdf", "page": 2, "text": "이 곡을 감상하고 느낌을 나누어 보자.", "genre": "국악"}
        examples = [high_score, low_score_matching]
        standard_text = "음악의 특징을 비교하여 설명한다."
        without_genre = _select_typed_reference_activities(examples, "감상형", "12감비01-01", standard_text)
        self.assertEqual(without_genre[0]["file"], "a.pdf")
        with_genre = _select_typed_reference_activities(
            examples, "감상형", "12감비01-01", standard_text, genre="국악"
        )
        self.assertEqual(with_genre[0]["file"], "b.pdf")

    def test_select_typed_reference_activities_handles_missing_genre_key(self):
        example_no_genre_key = {"file": "c.pdf", "page": 3, "text": "이 곡을 감상하고 느낌을 나누어 보자."}
        result = _select_typed_reference_activities(
            [example_no_genre_key], "감상형", "12감비01-01", "음악의 특징을 비교하여 설명한다.", genre="국악"
        )
        self.assertEqual(result, [example_no_genre_key])

    def test_recommendations_mentions_genre_when_reference_matches(self):
        components = detect_manuscript_components(["1. 곡을 감상하고 특징을 비교해 보자."])
        alignment = {"top_matches": [{
            "code": "12감비01-01", "text": "음악의 특징을 비교하여 설명한다.",
            "page": 1, "score": .5, "matched_keywords": ["특징"],
        }]}
        reference = {"activity_readability": {"examples": [
            {"file": "국악교과서.pdf", "page": 3, "text": "판소리를 감상하고 느낌을 나누어 보자.", "genre": "국악"},
            {"file": "오페라교과서.pdf", "page": 5, "text": "오페라 아리아를 감상하고 느낌을 나누어 보자.", "genre": "오페라"},
        ]}}
        pages = ["새로운 제재곡", "이 곡은 국악 판소리 갈래에 속한다. 장단에 맞춰 불러 보자."]
        result = _recommendations(components, alignment, pages, reference=reference)
        self.assertTrue(result["activities"])
        matching = [item for item in result["activities"] if item["reference_example"]["genre"] == "국악"]
        self.assertTrue(matching)
        self.assertIn("국악", matching[0]["reason"])


class _FakeActivitiesAdapter:
    def __init__(self, result=None, raise_error=False):
        self.result = result
        self.raise_error = raise_error

    def recommend_activities(self, payload):
        if self.raise_error:
            raise RuntimeError("boom")
        return self.result


class ActivitiesAdapterTests(unittest.TestCase):
    def setUp(self):
        self.components = detect_manuscript_components(["1. 곡을 감상하고 특징을 비교해 보자."])
        self.alignment = {"top_matches": [{
            "code": "12감비01-01", "text": "음악의 특징을 비교하여 설명한다.",
            "page": 1, "score": .5, "matched_keywords": ["특징"],
        }]}
        self.ai_response = {
            "intent_analysis": "학생이 곡을 감상하고 비교한다.",
            "standard_fit": {"fit_level": "부분 충족", "reason": "비교는 있으나 설명이 약함"},
            "activity_variants": [
                {"label": "감상 중심안", "recommended_activities": [
                    {"activity_type": "감상형", "text": "곡을 감상하며 느낌을 나누어 보자.", "rationale": "정서 파악 보완"},
                    {"activity_type": "비평·맥락형", "text": "시대적 배경을 살펴보고 생각을 써 보자.", "rationale": "맥락 이해 보완"},
                ]},
            ],
        }

    def test_uses_ai_adapter_result_when_valid(self):
        adapter = _FakeActivitiesAdapter(result=self.ai_response)
        result = _recommendations(self.components, self.alignment, ["제재곡"], activities_adapter=adapter)
        self.assertEqual(result["generation_method"], "AI(Claude) 기반 기존 활동 검토·재구성 (3안)")
        self.assertEqual(len(result["activity_variants"]), 1)
        self.assertEqual(result["activities"], result["activity_variants"][0]["activities"])
        self.assertEqual(len(result["activities"]), 2)
        self.assertEqual(result["activities"][0]["suggestion"], "곡을 감상하며 느낌을 나누어 보자.")
        self.assertEqual(result["activities"][0]["activity_type"], "감상형")
        self.assertEqual(result["ai_activity_review"]["intent_analysis"], self.ai_response["intent_analysis"])

    def test_ai_adapter_result_maps_source_indices_to_current_text(self):
        response = dict(self.ai_response)
        response["activity_variants"] = [{"label": "다듬기 중심안", "recommended_activities": [
            {"activity_type": "감상형", "edit_type": "trim", "source_indices": [1],
             "text": "곡을 감상해 보자.", "rationale": "군더더기 구절만 제거"},
            {"activity_type": "표현·창작형", "edit_type": "add", "source_indices": [],
             "text": "감상 후 느낀 점을 글로 표현해 보자.", "rationale": "표현 활동 보완"},
        ]}]
        adapter = _FakeActivitiesAdapter(result=response)
        components = detect_manuscript_components(["1. 곡을 감상하고 특징을 비교해 보자.\n2. 시대적 배경을 알아보자."])
        result = _recommendations(components, self.alignment, ["제재곡"], activities_adapter=adapter)
        self.assertEqual(result["activities"][0]["current_text"], "1. 곡을 감상하고 특징을 비교해 보자.")
        self.assertTrue(result["activities"][0]["reason"].startswith("[다듬기]"))
        self.assertIsNone(result["activities"][1]["current_text"])
        self.assertTrue(result["activities"][1]["reason"].startswith("[신규]"))

    def test_ai_adapter_result_keeps_multiple_variants_independent(self):
        response = dict(self.ai_response)
        response["activity_variants"] = [
            {"label": "감상 중심안", "recommended_activities": [
                {"activity_type": "감상형", "text": "곡을 감상하며 느낌을 나누어 보자.", "rationale": "정서 파악 보완"},
            ]},
            {"label": "비평 중심안", "recommended_activities": [
                {"activity_type": "비평·맥락형", "text": "시대적 배경을 살펴보고 생각을 써 보자.", "rationale": "맥락 이해 보완"},
            ]},
        ]
        adapter = _FakeActivitiesAdapter(result=response)
        result = _recommendations(self.components, self.alignment, ["제재곡"], activities_adapter=adapter)
        self.assertEqual(len(result["activity_variants"]), 2)
        self.assertEqual(result["activity_variants"][0]["label"], "감상 중심안")
        self.assertEqual(result["activity_variants"][1]["label"], "비평 중심안")
        self.assertEqual(result["activities"], result["activity_variants"][0]["activities"])
        self.assertNotEqual(
            result["activity_variants"][0]["activities"][0]["suggestion"],
            result["activity_variants"][1]["activities"][0]["suggestion"],
        )

    def test_falls_back_to_rule_based_when_adapter_raises(self):
        adapter = _FakeActivitiesAdapter(raise_error=True)
        result = _recommendations(self.components, self.alignment, ["제재곡"], activities_adapter=adapter)
        self.assertNotEqual(result.get("generation_method"), "AI(Claude) 기반 기존 활동 검토·재구성")
        self.assertNotIn("ai_activity_review", result)

    def test_falls_back_to_rule_based_when_adapter_returns_none(self):
        adapter = _FakeActivitiesAdapter(result=None)
        result = _recommendations(self.components, self.alignment, ["제재곡"], activities_adapter=adapter)
        self.assertNotEqual(result.get("generation_method"), "AI(Claude) 기반 기존 활동 검토·재구성")
        self.assertNotIn("ai_activity_review", result)


if __name__ == "__main__":
    unittest.main()
