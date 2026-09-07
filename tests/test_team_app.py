import tempfile
import unittest
from pathlib import Path

from team_app import TeamApplication, TeamStore, _safe_name


class TeamAppTests(unittest.TestCase):
    def test_reference_versions_and_textbooks_are_persistent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = TeamStore(root)
            first = root / "first.pdf"
            second = root / "second.pdf"
            book = root / "book.pdf"
            for path in (first, second, book):
                path.write_bytes(b"%PDF-1.4\n")
            store.add_reference("curriculum", "first.pdf", first, "a" * 64, 9, "2015", "음악")
            store.add_reference("curriculum", "second.pdf", second, "b" * 64, 9, "2022", "음악")
            store.add_reference("textbook", "book.pdf", book, "c" * 64, 9, "2015", "음악")
            active = store.references()
            self.assertEqual(len([item for item in active if item["kind"] == "curriculum"]), 1)
            self.assertEqual(store.active_reference("curriculum")["original_name"], "second.pdf")
            self.assertEqual(len([item for item in active if item["kind"] == "textbook"]), 1)

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
            uploaded = store.uploads / "references" / "curriculum" / "copy.pdf"
            uploaded.parent.mkdir(parents=True, exist_ok=True)
            uploaded.write_bytes(original.read_bytes())
            store.add_reference("curriculum", "original.pdf", uploaded, "e" * 64, 9, "", "")
            store.create_job("original.pdf", original, "f" * 64, 9)
            store.update_job(store.jobs()[0]["id"], status="completed")
            store.reset_all()
            self.assertTrue(original.exists())
            self.assertFalse(uploaded.exists())
            self.assertFalse(store.references())
            self.assertFalse(store.jobs())


if __name__ == "__main__":
    unittest.main()
