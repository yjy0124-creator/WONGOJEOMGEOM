import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from team_app import (
    CURRICULUM_TAXONOMY,
    TeamApplication,
    TeamStore,
    _curriculum_file_path,
    _curriculum_sub_subjects,
    _curriculum_taxonomy_status,
    _safe_name,
)


class TeamAppTests(unittest.TestCase):
    def test_textbook_references_all_stay_active(self):
        # 교육과정은 더 이상 화면에서 업로드받지 않고 curricula/ 폴더의 고정 파일을
        # 쓰므로, add_reference의 '한 종류당 하나만 활성' 자동 교체 동작은 이제
        # 실제로 쓰이는 유일한 종류인 textbook에는 적용되지 않는다(여러 개 등록 가능).
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = TeamStore(root)
            first = root / "first.pdf"
            second = root / "second.pdf"
            for path in (first, second):
                path.write_bytes(b"%PDF-1.4\n")
            store.add_reference("textbook", "first.pdf", first, "a" * 64, 9, "2015", "음악")
            store.add_reference("textbook", "second.pdf", second, "b" * 64, 9, "2022", "음악")
            active = store.references()
            self.assertEqual(len([item for item in active if item["kind"] == "textbook"]), 2)

    def test_curriculum_file_path_uses_sub_subject_when_present(self):
        root = Path("team_data")
        self.assertEqual(
            _curriculum_file_path(root, "고등학교", "음악", "음악 감상과 비평"),
            root / "curricula" / "고등학교" / "음악" / "음악 감상과 비평.pdf",
        )
        self.assertEqual(
            _curriculum_file_path(root, "중학교", "한문"),
            root / "curricula" / "중학교" / "한문" / "한문.pdf",
        )

    def test_curriculum_file_path_falls_back_to_bundled_copy_when_frozen(self):
        # exe로 배포할 때는 team_data/curricula 폴더를 따로 넘기지 않아도 되도록,
        # 등록된 사본이 data_root에 없으면 exe에 동봉된 사본으로 대체한다 — 단
        # PyInstaller로 묶여 실행 중일 때만(개발 환경에서는 그대로 미등록 취급).
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "team_data"
            bundle_root = Path(temporary) / "bundle"
            bundled_file = bundle_root / "team_data" / "curricula" / "초등학교" / "음악" / "음악.pdf"
            bundled_file.parent.mkdir(parents=True, exist_ok=True)
            bundled_file.write_bytes(b"%PDF-1.4\n")

            unfrozen = _curriculum_file_path(data_root, "초등학교", "음악")
            self.assertFalse(unfrozen.is_file())

            with patch.object(sys, "frozen", True, create=True), \
                 patch("curriculum_audit._resource_path", lambda name: bundle_root / name):
                frozen = _curriculum_file_path(data_root, "초등학교", "음악")
            self.assertEqual(frozen, bundled_file)

    def test_curriculum_sub_subjects_only_for_configured_high_school_subjects(self):
        self.assertEqual(_curriculum_sub_subjects("고등학교", "음악"), ["음악", "음악 감상과 비평"])
        self.assertIsNone(_curriculum_sub_subjects("고등학교", "한문"))
        self.assertIsNone(_curriculum_sub_subjects("중학교", "음악"))

    def test_curriculum_taxonomy_status_reports_missing_and_registered_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registered = _curriculum_file_path(root, "고등학교", "음악", "음악")
            registered.parent.mkdir(parents=True, exist_ok=True)
            registered.write_bytes(b"%PDF-1.4\n")
            status = _curriculum_taxonomy_status(root)
            music_entry = status["고등학교"]["음악"]
            available_by_name = {item["name"]: item["available"] for item in music_entry["sub_subjects"]}
            self.assertTrue(available_by_name["음악"])
            self.assertFalse(available_by_name["음악 감상과 비평"])
            self.assertFalse(status["초등학교"]["음악"]["available"])
            self.assertEqual(set(status.keys()), set(CURRICULUM_TAXONOMY.keys()))

    def test_jobs_keep_status_and_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = TeamStore(root)
            source = root / "manuscript.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            job_id = store.create_job(
                "manuscript.pdf", source, "d" * 64, 9,
                work_titles="소녀와, 두 바퀴로 가는 자동차",
            )
            self.assertEqual(store.job(job_id)["status"], "queued")
            self.assertEqual(store.job(job_id)["work_titles"], "소녀와, 두 바퀴로 가는 자동차")
            store.update_job(job_id, status="completed", result_path="result.html")
            self.assertEqual(store.job(job_id)["result_path"], "result.html")

    def test_job_progress_updates_without_touching_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = TeamStore(root)
            source = root / "manuscript.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            job_id = store.create_job("manuscript.pdf", source, "e" * 64, 9)
            store.update_job(job_id, status="running")
            store.update_job_progress(job_id, 3, 10)
            job = store.job(job_id)
            self.assertEqual(job["status"], "running")
            self.assertEqual(job["current_page"], 3)
            self.assertEqual(job["total_page"], 10)

    def test_cancel_before_start_marks_job_cancelled_without_running_audit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            application = TeamApplication(root)
            source = root / "manuscript.pdf"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"%PDF-1.4\n")
            job_id = application.store.create_job("manuscript.pdf", source, "a" * 64, 9)
            application.cancel(job_id)
            # 취소 요청 직후에는 실제 작업 스레드가 아직 확인하지 못했더라도 화면에는
            # 바로 "취소 중"으로 보여야 사용자가 반응이 없다고 느끼지 않는다.
            self.assertEqual(application.store.job(job_id)["status"], "cancelling")
            # 실제 curriculum_audit.audit_manuscript를 부르면 등록된 교육과정 PDF가 없어
            # 실패하지만, 취소 요청이 먼저 걸려 있으면 그 전에 취소 처리로 끝나야 한다.
            application._run_audit(job_id)
            job = application.store.job(job_id)
            self.assertEqual(job["status"], "cancelled")
            self.assertFalse(application._is_cancel_requested(job_id))

    def test_retry_copies_manuscript_and_queues_new_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            application = TeamApplication(root)
            source = application.store.uploads / "manuscripts" / "old-token" / "manuscript.pdf"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"%PDF-1.4\n")
            job_id = application.store.create_job(
                "manuscript.pdf", source, "d" * 64, source.stat().st_size,
                target_level="고등학교 1학년", work_titles="곡명",
                school_level="고등학교", subject="음악", sub_subject="음악",
            )
            application.cancel(job_id)
            application._run_audit(job_id)
            self.assertEqual(application.store.job(job_id)["status"], "cancelled")

            # 실제 분석 스레드를 띄우지 않고 retry()가 새 job을 큐에 넣는지만 확인한다.
            submitted = []
            application.submit = submitted.append
            new_job_id = application.retry(job_id)
            self.assertEqual(submitted, [new_job_id])
            self.assertNotEqual(new_job_id, job_id)
            new_job = application.store.job(new_job_id)
            self.assertEqual(new_job["status"], "queued")
            self.assertEqual(new_job["original_name"], "manuscript.pdf")
            self.assertEqual(new_job["work_titles"], "곡명")
            new_path = Path(new_job["stored_path"])
            self.assertNotEqual(new_path, source)
            self.assertTrue(new_path.is_file())

    def test_retry_rejects_jobs_that_are_not_cancelled(self):
        with tempfile.TemporaryDirectory() as temporary:
            application = TeamApplication(Path(temporary) / "data")
            source = application.store.root / "manuscript.pdf"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"%PDF-1.4\n")
            job_id = application.store.create_job("manuscript.pdf", source, "e" * 64, 9)
            with self.assertRaises(ValueError):
                application.retry(job_id)

    def test_check_cancel_raises_once_cancel_is_requested(self):
        from team_app import _JobCancelled

        with tempfile.TemporaryDirectory() as temporary:
            application = TeamApplication(Path(temporary) / "data")
            source = application.store.root / "manuscript.pdf"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"%PDF-1.4\n")
            job_id = application.store.create_job("manuscript.pdf", source, "b" * 64, 9)
            application.store.update_job(job_id, status="running")
            application._check_cancel(job_id, 1, 10)
            self.assertEqual(application.store.job(job_id)["current_page"], 1)
            application.cancel(job_id)
            with self.assertRaises(_JobCancelled):
                application._check_cancel(job_id, 2, 10)

    def test_cancel_rejects_missing_or_finished_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            application = TeamApplication(Path(temporary) / "data")
            with self.assertRaises(ValueError):
                application.cancel("0" * 32)
            source = application.store.root / "manuscript.pdf"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"%PDF-1.4\n")
            job_id = application.store.create_job("manuscript.pdf", source, "c" * 64, 9)
            application.store.update_job(job_id, status="completed", result_path="result.html")
            with self.assertRaises(ValueError):
                application.cancel(job_id)

    def test_safe_name_removes_directories(self):
        self.assertEqual(_safe_name("../../원고 최종.pdf"), "원고_최종.pdf")

    def test_storage_usage_counts_all_uploaded_pdf_copies(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = TeamStore(Path(temporary))
            first = store.uploads / "references" / "first.pdf"
            second = store.uploads / "manuscripts" / "job" / "second.pdf"
            first.parent.mkdir(parents=True, exist_ok=True)
            second.parent.mkdir(parents=True, exist_ok=True)
            first.write_bytes(b"a" * 100)
            second.write_bytes(b"b" * 300)
            usage = store.storage_usage()
            self.assertEqual(usage["used_bytes"], 400)
            self.assertEqual(usage["remaining_bytes"], usage["capacity_bytes"] - 400)

    def test_delete_reference_removes_only_selected_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = TeamStore(Path(temporary) / "data")
            first = store.uploads / "references" / "textbook" / "first.pdf"
            second = store.uploads / "references" / "textbook" / "second.pdf"
            first.parent.mkdir(parents=True, exist_ok=True)
            first.write_bytes(b"%PDF-1.4\nfirst")
            second.write_bytes(b"%PDF-1.4\nsecond")
            first_item = store.add_reference("textbook", "first.pdf", first, "1" * 64, first.stat().st_size, "", "")
            store.add_reference("textbook", "second.pdf", second, "2" * 64, second.stat().st_size, "", "")
            removed = store.delete_reference(first_item["id"])
            self.assertEqual(removed["original_name"], "first.pdf")
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())
            self.assertEqual([item["original_name"] for item in store.references()], ["second.pdf"])

    def test_reset_removes_app_copies_and_history_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = TeamStore(root / "data")
            original = root / "original.pdf"
            original.write_bytes(b"%PDF-1.4\n")
            uploaded = store.uploads / "references" / "textbook" / "copy.pdf"
            uploaded.parent.mkdir(parents=True, exist_ok=True)
            uploaded.write_bytes(original.read_bytes())
            store.add_reference("textbook", "original.pdf", uploaded, "e" * 64, 9, "", "")
            store.create_job("original.pdf", original, "f" * 64, 9)
            store.update_job(store.jobs()[0]["id"], status="completed")
            store.reset_all()
            self.assertTrue(original.exists())
            self.assertFalse(uploaded.exists())
            self.assertFalse(store.references())
            self.assertFalse(store.jobs())


if __name__ == "__main__":
    unittest.main()
