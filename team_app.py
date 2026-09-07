"""팀용 PDF 원고 점검 웹 애플리케이션.

기준 자료는 한 번 등록해 재사용하고 원고만 매번 업로드한다. 외부 패키지 없이
실행할 수 있도록 표준 라이브러리 HTTP 서버와 SQLite를 사용한다.
"""

from __future__ import annotations

import cgi
import functools
import hashlib
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import sys
import threading
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import vercel_blob_store as blob_store


def _dotenv_candidates() -> list[Path]:
    """`.env`를 찾을 후보 위치들.

    PyInstaller로 묶은 데스크톱 실행 파일은 실행 시 작업 디렉터리가 exe 위치와
    다를 수 있어(`team_app_desktop.py`), 현재 작업 디렉터리뿐 아니라 실행 파일 ·
    스크립트가 있는 디렉터리도 함께 찾는다.
    """
    candidates = [Path(".env")]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / ".env")
    else:
        candidates.append(Path(__file__).resolve().parent / ".env")
    return candidates


def _load_dotenv() -> None:
    """`.env` 파일의 KEY=VALUE 줄을 환경변수로 불러온다 (이미 설정된 값은 덮어쓰지 않음).

    ANTHROPIC_API_KEY 같은 비밀 값을 셸 환경변수 대신 gitignore된 로컬 파일로
    관리하기 위한 용도라 외부 패키지(dotenv) 없이 표준 라이브러리만 사용한다.
    """
    for path in _dotenv_candidates():
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_dotenv()


MAX_MANUSCRIPT_BYTES = 200 * 1024 * 1024
MAX_REFERENCE_REQUEST_BYTES = 800 * 1024 * 1024
MAX_STORED_UPLOAD_BYTES = 800 * 1024 * 1024
MAX_MANUSCRIPT_PAGES = 300
ALLOWED_KINDS = {"textbook"}
KIND_LABELS = {
    "textbook": "이전 개정 교과서",
}
TARGET_LEVEL_OPTIONS = (
    "초등학교 저학년", "초등학교 고학년", "중학교",
    "고등학교 1학년", "고등학교 2·3학년",
)

# 교육과정 PDF는 화면에서 업로드받지 않고, 학교급/과목(/세부과목)별로 미리 정해진
# 폴더 구조(team_data/curricula/...)에 등록해 둔 파일을 그대로 사용한다.
# 원고 분석 시 사용자가 이 트리에서 학교급→과목→세부과목 순으로 선택하면 그 값에
# 해당하는 파일을 찾아 성취기준 비교에 사용한다.
CURRICULUM_TAXONOMY: dict[str, dict[str, Any]] = {
    "초등학교": {
        "subjects": ["영어", "수학", "사회", "음악", "체육", "보건", "미술", "과학"],
    },
    "중학교": {
        "subjects": [
            "영어", "수학", "과학", "기술·가정", "정보", "체육", "음악", "한문",
            "보건", "진로와 선택", "개념 기반 탐구", "미디어와 민주시민",
        ],
    },
    "고등학교": {
        "subjects": ["영어", "수학", "정보", "체육", "음악", "한문", "보건"],
        "sub_subjects": {
            "영어": ["공통영어1", "공통영어2", "영어 I", "영어 Ⅱ", "영어 독해와 작문",
                    "심화 영어", "실생활 영어 회화", "세계 문화와 영어", "미디어 영어",
                    "심화 영어 독해와 작문"],
            "수학": ["공통수학1", "공통수학2", "대수", "미적분Ⅰ", "미적분Ⅱ", "확률과 통계", "기하"],
            "정보": ["정보", "인공지능 기초", "데이터 과학"],
            "체육": ["스포츠 과학", "체육1", "체육2", "운동과 건강", "스포츠 생활1", "스포츠 생활2"],
            "음악": ["음악", "음악 감상과 비평"],
        },
    },
}


def _curriculum_sub_subjects(school_level: str, subject: str) -> list[str] | None:
    config = CURRICULUM_TAXONOMY.get(school_level, {})
    return config.get("sub_subjects", {}).get(subject)


def _curriculum_file_path(data_root: Path, school_level: str, subject: str, sub_subject: str = "") -> Path:
    base = data_root / "curricula" / school_level / subject
    return base / f"{sub_subject or subject}.pdf"


def _curriculum_taxonomy_status(data_root: Path) -> dict[str, dict[str, Any]]:
    """학교급·과목·세부과목별로 PDF가 실제로 등록돼 있는지 스캔한다."""
    result: dict[str, dict[str, Any]] = {}
    for school_level, config in CURRICULUM_TAXONOMY.items():
        subjects_out: dict[str, Any] = {}
        for subject in config["subjects"]:
            sub_list = _curriculum_sub_subjects(school_level, subject)
            if sub_list:
                subjects_out[subject] = {"sub_subjects": [
                    {"name": name, "available": _curriculum_file_path(data_root, school_level, subject, name).is_file()}
                    for name in sub_list
                ]}
            else:
                subjects_out[subject] = {"available": _curriculum_file_path(data_root, school_level, subject).is_file()}
        result[school_level] = subjects_out
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(value: str) -> str:
    name = Path(value or "upload.pdf").name
    stem = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", name).strip("._")
    return stem[:160] or "upload.pdf"


def _json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class TeamStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.uploads = self.root / "uploads"
        self.results = self.root / "results"
        self.web_root = Path(__file__).resolve().parent / "team_web"
        self.db_path = self.root / "team.db"
        self.root.mkdir(parents=True, exist_ok=True)
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.results.mkdir(parents=True, exist_ok=True)
        blob_store.pull_if_missing(self.db_path, "team.db")
        self._init_db()

    def _blob_key(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def _push_db(self) -> None:
        blob_store.push(self.db_path, "team.db")

    def _delete_blob_tree(self, directory: Path) -> None:
        """로컬에서 지우기 전에, 그 안의 각 파일에 대응하는 Blob 객체도 지운다.

        Vercel Blob에는 '접두어 삭제' 없이 키 하나씩 지워야 하므로, 아직 로컬에
        파일이 남아있는 지금 트리를 순회해 실제로 존재하는 파일만 지운다.
        """
        if not directory.is_dir():
            return
        for file_path in directory.rglob("*"):
            if file_path.is_file():
                blob_store.delete(self._blob_key(file_path))

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with closing(self.connect()) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS reference_files (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    revision TEXT NOT NULL DEFAULT '',
                    subject TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reference_kind_active_idx
                    ON reference_files(kind, active, created_at);
                CREATE TABLE IF NOT EXISTS audit_jobs (
                    id TEXT PRIMARY KEY,
                    original_name TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    target_level TEXT NOT NULL DEFAULT '고등학교 1학년',
                    status TEXT NOT NULL,
                    result_path TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS audit_job_created_idx ON audit_jobs(created_at DESC);
                CREATE TABLE IF NOT EXISTS false_positives (
                    fingerprint TEXT PRIMARY KEY,
                    section TEXT NOT NULL,
                    manuscript_text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(audit_jobs)")}
            if "target_level" not in columns:
                db.execute(
                    "ALTER TABLE audit_jobs ADD COLUMN target_level TEXT NOT NULL DEFAULT '고등학교 1학년'"
                )
            if "work_titles" not in columns:
                db.execute("ALTER TABLE audit_jobs ADD COLUMN work_titles TEXT NOT NULL DEFAULT ''")
            if "diff_path" not in columns:
                db.execute("ALTER TABLE audit_jobs ADD COLUMN diff_path TEXT")
            if "school_level" not in columns:
                db.execute("ALTER TABLE audit_jobs ADD COLUMN school_level TEXT NOT NULL DEFAULT ''")
            if "subject" not in columns:
                db.execute("ALTER TABLE audit_jobs ADD COLUMN subject TEXT NOT NULL DEFAULT ''")
            if "sub_subject" not in columns:
                db.execute("ALTER TABLE audit_jobs ADD COLUMN sub_subject TEXT NOT NULL DEFAULT ''")
            if "current_page" not in columns:
                db.execute("ALTER TABLE audit_jobs ADD COLUMN current_page INTEGER NOT NULL DEFAULT 0")
            if "total_page" not in columns:
                db.execute("ALTER TABLE audit_jobs ADD COLUMN total_page INTEGER NOT NULL DEFAULT 0")

    def references(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT * FROM reference_files WHERE active=1 ORDER BY kind, created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def jobs(self, limit: int = 30) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT * FROM audit_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def storage_usage(self) -> dict[str, int | float]:
        used = 0
        for root, _dirs, files in os.walk(self.uploads, onerror=lambda exc: None):
            for name in files:
                try:
                    used += (Path(root) / name).stat().st_size
                except OSError:
                    continue
        remaining = max(0, MAX_STORED_UPLOAD_BYTES - used)
        percentage = round(min(100.0, used / MAX_STORED_UPLOAD_BYTES * 100), 1)
        return {
            "capacity_bytes": MAX_STORED_UPLOAD_BYTES,
            "used_bytes": used,
            "remaining_bytes": remaining,
            "percentage": percentage,
        }

    def job(self, job_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM audit_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def add_reference(self, kind: str, original_name: str, stored_path: Path,
                      sha256: str, size_bytes: int, revision: str, subject: str) -> dict[str, Any]:
        reference_id = uuid.uuid4().hex
        with closing(self.connect()) as db:
            existing = db.execute(
                "SELECT * FROM reference_files WHERE kind=? AND sha256=? AND active=1",
                (kind, sha256),
            ).fetchone()
            if existing:
                return {**dict(existing), "duplicate": True}
            db.execute(
                """INSERT INTO reference_files
                   (id,kind,original_name,stored_path,sha256,size_bytes,revision,subject,active,created_at)
                   VALUES (?,?,?,?,?,?,?,?,1,?)""",
                (reference_id, kind, original_name, str(stored_path), sha256, size_bytes,
                 revision.strip(), subject.strip(), _utc_now()),
            )
        self._push_db()
        return {"id": reference_id, "kind": kind, "original_name": original_name,
                "sha256": sha256, "size_bytes": size_bytes, "duplicate": False}

    def create_job(self, original_name: str, stored_path: Path, sha256: str,
                   size_bytes: int, target_level: str = "고등학교 1학년",
                   work_titles: str = "", school_level: str = "", subject: str = "",
                   sub_subject: str = "") -> str:
        job_id = uuid.uuid4().hex
        with closing(self.connect()) as db:
            db.execute(
                """INSERT INTO audit_jobs
                   (id,original_name,stored_path,sha256,size_bytes,target_level,work_titles,
                    school_level,subject,sub_subject,status,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,'queued',?)""",
                (job_id, original_name, str(stored_path), sha256, size_bytes,
                 target_level, work_titles, school_level, subject, sub_subject, _utc_now()),
            )
        self._push_db()
        return job_id

    def update_job(self, job_id: str, *, status: str, result_path: str | None = None,
                   error: str | None = None, diff_path: str | None = None) -> None:
        completed = _utc_now() if status in {"completed", "failed", "cancelled"} else None
        with closing(self.connect()) as db:
            db.execute(
                "UPDATE audit_jobs SET status=?,result_path=?,error=?,completed_at=?,diff_path=? WHERE id=?",
                (status, result_path, error, completed, diff_path, job_id),
            )
        self._push_db()

    def update_job_progress(self, job_id: str, current_page: int, total_page: int) -> None:
        """분석 도중 실시간 진행률만 갱신한다. 매 페이지마다 호출되므로 원격 저장소 동기화는 생략한다."""
        with closing(self.connect()) as db:
            db.execute(
                "UPDATE audit_jobs SET current_page=?,total_page=? WHERE id=?",
                (current_page, total_page, job_id),
            )

    def previous_completed_job(self, original_name: str, exclude_job_id: str) -> dict[str, Any] | None:
        """같은 파일명으로 먼저 완료된 가장 최근 분석(재분석 비교 대상)을 찾는다."""
        with closing(self.connect()) as db:
            row = db.execute(
                """SELECT * FROM audit_jobs
                   WHERE original_name=? AND status='completed' AND id!=?
                   ORDER BY created_at DESC LIMIT 1""",
                (original_name, exclude_job_id),
            ).fetchone()
        return dict(row) if row else None

    def false_positive_fingerprints(self) -> set[str]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT fingerprint FROM false_positives").fetchall()
        return {row["fingerprint"] for row in rows}

    def false_positives(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT * FROM false_positives ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_false_positive(self, fingerprint: str, section: str, manuscript_text: str) -> None:
        with closing(self.connect()) as db:
            db.execute(
                """INSERT INTO false_positives (fingerprint,section,manuscript_text,created_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(fingerprint) DO NOTHING""",
                (fingerprint, section, manuscript_text, _utc_now()),
            )
        self._push_db()

    def unmark_false_positive(self, fingerprint: str) -> None:
        with closing(self.connect()) as db:
            db.execute("DELETE FROM false_positives WHERE fingerprint=?", (fingerprint,))
        self._push_db()

    def delete_job(self, job_id: str) -> dict[str, Any]:
        """분석 이력 한 건과 업로드 사본·결과 파일을 삭제한다.

        같은 내용의 파일을 두 번 올리면 document_id(해시 기반)가 같아 결과 폴더를
        공유할 수 있으므로, 다른 이력이 같은 result_path를 쓰고 있으면 폴더는 남긴다.
        """
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("삭제할 이력 식별자가 올바르지 않습니다.")
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM audit_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("삭제할 분석 이력을 찾지 못했습니다.")
        item = dict(row)
        if item["status"] in ("queued", "running", "cancelling"):
            raise ValueError("분석 중인 원고는 삭제할 수 없습니다. 완료 후 다시 시도해 주세요.")

        manuscript_root = (self.uploads / "manuscripts").resolve()
        upload_dir = Path(item["stored_path"]).resolve().parent
        if upload_dir == manuscript_root or manuscript_root in upload_dir.parents:
            self._delete_blob_tree(upload_dir)
            shutil.rmtree(upload_dir, ignore_errors=True)

        if item.get("result_path"):
            with closing(self.connect()) as db:
                shared = db.execute(
                    "SELECT COUNT(*) FROM audit_jobs WHERE result_path=? AND id!=?",
                    (item["result_path"], job_id),
                ).fetchone()[0]
            if not shared:
                results_root = self.results.resolve()
                result_dir = Path(item["result_path"]).resolve().parent
                if result_dir == results_root or results_root in result_dir.parents:
                    self._delete_blob_tree(result_dir)
                    shutil.rmtree(result_dir, ignore_errors=True)

        with closing(self.connect()) as db:
            db.execute("DELETE FROM audit_jobs WHERE id=?", (job_id,))
        self._push_db()
        return item

    def active_reference(self, kind: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM reference_files WHERE kind=? AND active=1 ORDER BY created_at DESC LIMIT 1",
                (kind,),
            ).fetchone()
        return dict(row) if row else None

    def delete_reference(self, reference_id: str) -> dict[str, Any]:
        """등록한 기준 PDF 복사본 한 개와 해당 목록 항목만 삭제한다."""
        if not re.fullmatch(r"[0-9a-f]{32}", reference_id):
            raise ValueError("삭제할 자료 식별자가 올바르지 않습니다.")
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM reference_files WHERE id=? AND active=1", (reference_id,)
            ).fetchone()
        if not row:
            raise ValueError("삭제할 등록 자료를 찾지 못했습니다.")
        item = dict(row)
        target = Path(item["stored_path"]).resolve()
        reference_root = (self.uploads / "references").resolve()
        if target != reference_root and reference_root not in target.parents:
            raise ValueError("등록 자료 경로가 안전하지 않아 삭제하지 않았습니다.")
        blob_store.delete(self._blob_key(target))
        target.unlink(missing_ok=True)
        with closing(self.connect()) as db:
            db.execute("DELETE FROM reference_files WHERE id=?", (reference_id,))
        self._push_db()
        if item["kind"] == "textbook":
            self.textbook_directory()
        return item

    def textbook_directory(self) -> Path:
        directory = self.root / "active_textbooks"
        directory.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT * FROM reference_files WHERE kind='textbook' AND active=1 ORDER BY created_at"
            ).fetchall()
        expected = set()
        for row in rows:
            source = Path(row["stored_path"])
            target = directory / f"{row['sha256'][:10]}_{_safe_name(row['original_name'])}"
            expected.add(target.name)
            if not target.exists():
                blob_store.pull_if_missing(source, self._blob_key(source))
                if source.exists():
                    shutil.copy2(source, target)
        for path in directory.glob("*.pdf"):
            if path.name not in expected:
                path.unlink()
        return directory

    def reset_all(self) -> None:
        """업로드 복사본, 분석 결과와 목록을 초기화한다. 원본 파일은 건드리지 않는다."""
        with closing(self.connect()) as db:
            active = db.execute(
                "SELECT COUNT(*) FROM audit_jobs WHERE status IN ('queued','running','cancelling')"
            ).fetchone()[0]
        if active:
            raise ValueError("분석 중인 원고가 있어 초기화할 수 없습니다. 완료 후 다시 시도해 주세요.")
        for target in (self.uploads, self.results, self.root / "active_textbooks"):
            resolved = target.resolve()
            if resolved.parent != self.root:
                raise ValueError("초기화 대상 경로가 안전하지 않습니다.")
            if resolved.exists():
                self._delete_blob_tree(resolved)
                shutil.rmtree(resolved)
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.results.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.execute("DELETE FROM reference_files")
            db.execute("DELETE FROM audit_jobs")
        self._push_db()


def _copy_upload(field: cgi.FieldStorage, destination: Path, maximum: int) -> tuple[str, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with destination.open("wb") as target:
        while True:
            chunk = field.file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                target.close()
                destination.unlink(missing_ok=True)
                raise ValueError(f"파일 용량 제한 {maximum // (1024 * 1024)}MB를 초과했습니다.")
            digest.update(chunk)
            target.write(chunk)
    with destination.open("rb") as source:
        signature = source.read(5)
    if size < 5 or signature != b"%PDF-":
        destination.unlink(missing_ok=True)
        raise ValueError("정상적인 PDF 파일이 아닙니다.")
    return digest.hexdigest(), size


def _pdf_page_count(path: Path) -> int:
    from pypdf import PdfReader  # type: ignore
    return len(PdfReader(str(path)).pages)


class _JobCancelled(Exception):
    pass


class TeamApplication:
    def __init__(self, data_root: Path):
        self.store = TeamStore(data_root)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="manuscript-audit")
        self._cancel_requested: set[str] = set()
        self._cancel_lock = threading.Lock()

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.job(job_id)
        if not job:
            raise ValueError("취소할 분석 작업을 찾지 못했습니다.")
        if job["status"] not in ("queued", "running"):
            raise ValueError("이미 끝난 작업은 취소할 수 없습니다.")
        with self._cancel_lock:
            self._cancel_requested.add(job_id)
        # 실제 작업 스레드가 취소를 알아채기까지는 다음 진행률 확인 지점까지 걸리므로,
        # 화면에는 즉시 "취소 중"으로 보여 사용자가 반응이 없다고 느끼지 않게 한다.
        self.store.update_job(job_id, status="cancelling")
        return job

    def retry(self, job_id: str) -> str:
        job = self.store.job(job_id)
        if not job:
            raise ValueError("다시 시작할 분석 이력을 찾지 못했습니다.")
        if job["status"] != "cancelled":
            raise ValueError("취소된 분석만 다시 시작할 수 있습니다.")
        source = Path(job["stored_path"])
        blob_store.pull_if_missing(source, self.store._blob_key(source))
        if not source.is_file():
            raise ValueError("원본 원고 파일을 찾지 못해 다시 시작할 수 없습니다.")
        # 새 job이 원본과 다른 자기 자신만의 사본을 갖도록 새 토큰 폴더로 복사한다.
        # stored_path를 그대로 공유하면, 취소된 예전 이력을 삭제할 때 아직 실행 중인
        # 새 작업의 원고 파일까지 함께 지워질 수 있다.
        target = self.store.uploads / "manuscripts" / uuid.uuid4().hex / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        blob_store.push(target, self.store._blob_key(target))
        new_job_id = self.store.create_job(
            job["original_name"], target, job["sha256"], job["size_bytes"],
            job.get("target_level") or "고등학교 1학년", job.get("work_titles") or "",
            job.get("school_level") or "", job.get("subject") or "", job.get("sub_subject") or "",
        )
        self.submit(new_job_id)
        return new_job_id

    def _is_cancel_requested(self, job_id: str) -> bool:
        with self._cancel_lock:
            return job_id in self._cancel_requested

    def _clear_cancel_requested(self, job_id: str) -> None:
        with self._cancel_lock:
            self._cancel_requested.discard(job_id)

    def _check_cancel(self, job_id: str, current: int, total: int) -> None:
        if self._is_cancel_requested(job_id):
            raise _JobCancelled()
        self.store.update_job_progress(job_id, current, total)

    def submit(self, job_id: str) -> None:
        # Vercel 서버리스 함수는 응답 이후 백그라운드 스레드 지속을 보장하지 않으므로,
        # Vercel에서 실행 중(VERCEL 환경변수가 자동 주입됨)이면 동기적으로 끝까지 처리한다.
        # 응답 형식(202 + job_id)은 그대로라 프런트엔드 폴링 코드는 수정할 필요가 없다 —
        # 이미 완료 상태로 기록된 뒤 응답이 나가므로 첫 폴링에서 바로 결과를 보게 된다.
        if os.environ.get("VERCEL"):
            self._run_audit(job_id)
        else:
            self.executor.submit(self._run_audit, job_id)

    _CACHE_FILE_NAMES = ("layout_reference.json", "term_reference.json", "textbook_content_cache.json")

    def _run_audit(self, job_id: str) -> None:
        job = self.store.job(job_id)
        if not job:
            return
        if self._is_cancel_requested(job_id):
            self._clear_cancel_requested(job_id)
            self.store.update_job(job_id, status="cancelled", error="사용자가 취소했습니다.")
            return
        curriculum_path = _curriculum_file_path(
            self.store.root, job.get("school_level") or "", job.get("subject") or "",
            job.get("sub_subject") or "",
        )
        if not curriculum_path.is_file():
            self.store.update_job(
                job_id, status="failed",
                error=f"선택한 교육과정 PDF가 없습니다: {curriculum_path.relative_to(self.store.root)}",
            )
            return
        self.store.update_job(job_id, status="running")
        try:
            from curriculum_audit import audit_manuscript, compare_manuscript_audits
            manuscript_path = Path(job["stored_path"])
            blob_store.pull_if_missing(manuscript_path, self.store._blob_key(manuscript_path))
            blob_store.pull_if_missing(curriculum_path, self.store._blob_key(curriculum_path))
            for name in self._CACHE_FILE_NAMES:
                blob_store.pull_if_missing(self.store.results / name, self.store._blob_key(self.store.results / name))
            textbook_dir = self.store.textbook_directory()
            # ANTHROPIC_API_KEY가 설정된 경우에만 AI 활동문 추천 어댑터를 사용한다.
            # 키가 없으면 ai_module=None이 되어 기존 규칙 기반 추천으로 그대로 동작한다.
            ai_module = "ai_activity_adapter" if os.environ.get("ANTHROPIC_API_KEY") else None
            result = audit_manuscript(
                manuscript_path, curriculum_path,
                self.store.results, ai_module, textbook_dir,
                job.get("target_level") or "고등학교 1학년",
                work_titles=[title.strip() for title in re.split(r"[,\n]", job.get("work_titles") or "") if title.strip()],
                suppressed_fingerprints=self.store.false_positive_fingerprints(),
                on_page_progress=lambda current, total: self._check_cancel(job_id, current, total),
            )
            diff_path = None
            previous = self.store.previous_completed_job(job["original_name"], job_id)
            if previous and previous.get("result_path"):
                try:
                    previous_json = Path(previous["result_path"]).with_suffix(".json")
                    blob_store.pull_if_missing(previous_json, self.store._blob_key(previous_json))
                    diff_path = str(compare_manuscript_audits(previous_json, result, result.parent))
                except Exception:
                    diff_path = None
            self.store.update_job(
                job_id, status="completed", result_path=str(result.with_suffix(".html")),
                diff_path=diff_path,
            )
            for name in self._CACHE_FILE_NAMES:
                blob_store.push(self.store.results / name, self.store._blob_key(self.store.results / name))
            for file_path in result.parent.rglob("*"):
                if file_path.is_file():
                    blob_store.push(file_path, self.store._blob_key(file_path))
        except _JobCancelled:
            self.store.update_job(job_id, status="cancelled", error="사용자가 취소했습니다.")
        except Exception as exc:
            self.store.update_job(job_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            self._clear_cancel_requested(job_id)


def _handler(application: TeamApplication) -> type[BaseHTTPRequestHandler]:
    class TeamHandler(BaseHTTPRequestHandler):
        server_version = "TextbookAuditTeam/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"[{self.log_date_time_string()}] {fmt % args}")

        def _send_json(self, data: Any, status: int = 200) -> None:
            payload = _json_bytes(data)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _send_file(self, path: Path, content_type: str | None = None) -> None:
            if not path.is_file():
                self.send_error(404)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _multipart(self) -> cgi.FieldStorage:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0 or length > MAX_REFERENCE_REQUEST_BYTES:
                raise ValueError("업로드 요청 용량이 너무 크거나 비어 있습니다.")
            storage = application.store.storage_usage()
            if storage["used_bytes"] + length > storage["capacity_bytes"]:
                raise ValueError(
                    f"서버 PDF 저장 공간이 부족합니다. 남은 용량은 "
                    f"{storage['remaining_bytes'] / (1024 * 1024):.1f}MB입니다."
                )
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                raise ValueError("multipart/form-data 형식이 필요합니다.")
            return cgi.FieldStorage(
                fp=self.rfile, headers=self.headers,
                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type,
                         "CONTENT_LENGTH": str(length)},
            )

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path == "/":
                self._send_file(application.store.web_root / "index.html", "text/html; charset=utf-8")
                return
            if path == "/api/dashboard":
                references = application.store.references()
                grouped = {kind: [] for kind in ALLOWED_KINDS}
                for item in references:
                    # 예전 kind(curriculum/evaluation)로 등록된 레코드가 남아 있을 수
                    # 있으니(더 이상 화면에 표시하지 않음), 알 수 없는 kind는 조용히 건너뛴다.
                    if item["kind"] in grouped:
                        grouped[item["kind"]].append(item)
                self._send_json({
                    "references": grouped,
                    "jobs": application.store.jobs(),
                    "storage": application.store.storage_usage(),
                    "limits": {"manuscript_mb": MAX_MANUSCRIPT_BYTES // (1024 * 1024),
                               "manuscript_pages": MAX_MANUSCRIPT_PAGES,
                               "reference_file_mb": MAX_MANUSCRIPT_BYTES // (1024 * 1024),
                               "request_mb": MAX_REFERENCE_REQUEST_BYTES // (1024 * 1024)},
                    "curricula": _curriculum_taxonomy_status(application.store.root),
                })
                return
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})", path)
            if match:
                job = application.store.job(match.group(1))
                self._send_json(job or {"error": "작업을 찾지 못했습니다."}, 200 if job else 404)
                return
            if path == "/api/false-positives":
                self._send_json(application.store.false_positives())
                return
            match = re.fullmatch(r"/results/([0-9a-f]{32})", path)
            if match:
                job = application.store.job(match.group(1))
                if not job or job["status"] != "completed" or not job.get("result_path"):
                    self.send_error(404, "분석 결과가 아직 준비되지 않았습니다.")
                    return
                result = Path(job["result_path"])
                relative = result.relative_to(application.store.results).as_posix()
                self.send_response(302)
                self.send_header("Location", "/result-files/" + quote(relative))
                self.end_headers()
                return
            match = re.fullmatch(r"/diff/([0-9a-f]{32})", path)
            if match:
                job = application.store.job(match.group(1))
                if not job or not job.get("diff_path"):
                    self.send_error(404, "비교할 이전 분석 결과가 없습니다.")
                    return
                diff_result = Path(job["diff_path"])
                relative = diff_result.relative_to(application.store.results).as_posix()
                self.send_response(302)
                self.send_header("Location", "/result-files/" + quote(relative))
                self.end_headers()
                return
            if path.startswith("/result-files/"):
                relative = path.removeprefix("/result-files/")
                target = (application.store.results / relative).resolve()
                if application.store.results not in target.parents:
                    self.send_error(403)
                    return
                blob_store.pull_if_missing(target, application.store._blob_key(target))
                self._send_file(target)
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            try:
                path = unquote(urlparse(self.path).path)
                cancel_match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/cancel", path)
                retry_match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/retry", path)
                if self.path == "/api/references":
                    self._upload_references()
                elif self.path == "/api/manuscripts":
                    self._upload_manuscript()
                elif self.path == "/api/false-positives":
                    self._mark_false_positive()
                elif self.path == "/api/reset":
                    if self.headers.get("X-Reset-Confirm") != "RESET_ALL":
                        raise ValueError("초기화 확인 값이 올바르지 않습니다.")
                    application.store.reset_all()
                    self._send_json({"message": "등록 자료와 원고 분석 이력을 초기화했습니다."})
                elif cancel_match:
                    job = application.cancel(cancel_match.group(1))
                    self._send_json({"message": f"{job['original_name']} 분석 취소를 요청했습니다.",
                                     "id": job["id"]})
                elif retry_match:
                    new_job_id = application.retry(retry_match.group(1))
                    self._send_json({"message": "원고 분석을 다시 시작했습니다.",
                                     "job_id": new_job_id}, 202)
                else:
                    self.send_error(404)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
            except Exception as exc:
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        def do_DELETE(self) -> None:  # noqa: N802
            try:
                path = unquote(urlparse(self.path).path)
                match = re.fullmatch(r"/api/references/([0-9a-f]{32})", path)
                if match:
                    removed = application.store.delete_reference(match.group(1))
                    self._send_json({
                        "message": f"{removed['original_name']} 파일을 삭제했습니다.",
                        "id": removed["id"], "kind": removed["kind"],
                    })
                    return
                match = re.fullmatch(r"/api/false-positives/([0-9a-f]{16})", path)
                if match:
                    application.store.unmark_false_positive(match.group(1))
                    self._send_json({"message": "오탐 표시를 해제했습니다.", "fingerprint": match.group(1)})
                    return
                match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})", path)
                if match:
                    removed = application.store.delete_job(match.group(1))
                    self._send_json({
                        "message": f"{removed['original_name']} 분석 이력을 삭제했습니다.",
                        "id": removed["id"],
                    })
                    return
                self.send_error(404)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
            except Exception as exc:
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        def _json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0 or length > 10_000:
                raise ValueError("요청 본문이 비어 있거나 너무 큽니다.")
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("JSON 본문을 해석할 수 없습니다.") from exc
            if not isinstance(data, dict):
                raise ValueError("JSON 객체가 필요합니다.")
            return data

        def _mark_false_positive(self) -> None:
            body = self._json_body()
            fingerprint = str(body.get("fingerprint", "")).strip()
            section = str(body.get("section", "")).strip()
            manuscript_text = str(body.get("manuscript_text", "")).strip()[:2000]
            if not re.fullmatch(r"[0-9a-f]{16}", fingerprint):
                raise ValueError("올바르지 않은 지문(fingerprint)입니다.")
            if not section:
                raise ValueError("구분(section)이 필요합니다.")
            application.store.mark_false_positive(fingerprint, section, manuscript_text)
            self._send_json({"message": "오탐으로 표시했습니다.", "fingerprint": fingerprint}, 201)

        def _upload_references(self) -> None:
            form = self._multipart()
            kind = form.getfirst("kind", "")
            if kind not in ALLOWED_KINDS:
                raise ValueError("등록할 기준 자료 종류가 올바르지 않습니다.")
            revision = form.getfirst("revision", "")
            subject = form.getfirst("subject", "")
            fields = form["files"] if "files" in form else []
            if not isinstance(fields, list):
                fields = [fields]
            fields = [field for field in fields if getattr(field, "filename", None)]
            if not fields:
                raise ValueError("PDF 파일을 선택해 주세요.")
            saved = []
            for field in fields:
                original = _safe_name(field.filename)
                if not original.lower().endswith(".pdf"):
                    raise ValueError("PDF 파일만 등록할 수 있습니다.")
                token = uuid.uuid4().hex
                target = application.store.uploads / "references" / kind / f"{token}_{original}"
                digest, size = _copy_upload(field, target, MAX_MANUSCRIPT_BYTES)
                blob_store.push(target, application.store._blob_key(target))
                saved.append(application.store.add_reference(
                    kind, original, target, digest, size, revision, subject
                ))
            self._send_json({"message": f"{KIND_LABELS[kind]} 등록 완료", "files": saved}, 201)

        def _upload_manuscript(self) -> None:
            form = self._multipart()
            school_level = form.getfirst("school_level", "")
            subject = form.getfirst("subject", "")
            sub_subject = form.getfirst("sub_subject", "")
            if school_level not in CURRICULUM_TAXONOMY:
                raise ValueError("학교급 선택이 올바르지 않습니다.")
            if subject not in CURRICULUM_TAXONOMY[school_level]["subjects"]:
                raise ValueError("과목 선택이 올바르지 않습니다.")
            expected_sub_subjects = _curriculum_sub_subjects(school_level, subject)
            if expected_sub_subjects and sub_subject not in expected_sub_subjects:
                raise ValueError("세부 과목 선택이 올바르지 않습니다.")
            if not expected_sub_subjects:
                sub_subject = ""
            curriculum_path = _curriculum_file_path(application.store.root, school_level, subject, sub_subject)
            if not curriculum_path.is_file():
                raise ValueError(
                    f"선택한 교육과정 PDF가 아직 등록되지 않았습니다: "
                    f"{curriculum_path.relative_to(application.store.root)}"
                )
            target_level = form.getfirst("target_level", "고등학교 1학년")
            if target_level not in TARGET_LEVEL_OPTIONS:
                raise ValueError("학습자 수준 선택이 올바르지 않습니다.")
            work_titles = re.sub(r"\s+", " ", form.getfirst("work_titles", "")).strip()
            title_items = [item.strip() for item in work_titles.split(",") if item.strip()]
            if len(work_titles) > 200 or len(title_items) > 10:
                raise ValueError("곡명은 쉼표로 구분해 최대 10개, 전체 200자 이내로 입력해 주세요.")
            work_titles = ", ".join(dict.fromkeys(title_items))
            field = form["file"] if "file" in form else None
            if field is None or not getattr(field, "filename", None):
                raise ValueError("원고 PDF를 선택해 주세요.")
            original = _safe_name(field.filename)
            if not original.lower().endswith(".pdf"):
                raise ValueError("PDF 파일만 업로드할 수 있습니다.")
            token = uuid.uuid4().hex
            target = application.store.uploads / "manuscripts" / token / original
            digest, size = _copy_upload(field, target, MAX_MANUSCRIPT_BYTES)
            try:
                pages = _pdf_page_count(target)
            except Exception as exc:
                target.unlink(missing_ok=True)
                raise ValueError(f"PDF 페이지를 읽을 수 없습니다: {exc}") from exc
            if pages > MAX_MANUSCRIPT_PAGES:
                target.unlink(missing_ok=True)
                raise ValueError(f"원고는 최대 {MAX_MANUSCRIPT_PAGES}쪽까지 업로드할 수 있습니다.")
            blob_store.push(target, application.store._blob_key(target))
            job_id = application.store.create_job(
                original, target, digest, size, target_level, work_titles,
                school_level, subject, sub_subject,
            )
            application.submit(job_id)
            self._send_json({"message": "원고 분석을 시작했습니다.", "job_id": job_id,
                             "pages": pages, "target_level": target_level,
                             "work_titles": title_items}, 202)

    return TeamHandler


def _local_network_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stream:
            stream.connect(("8.8.8.8", 80))
            return stream.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def serve_team_app(data_root: Path = Path("team_data"), host: str = "127.0.0.1",
                   port: int = 8780, open_browser: bool = True) -> None:
    application = TeamApplication(data_root)
    server = ThreadingHTTPServer((host, port), _handler(application))
    local_url = f"http://127.0.0.1:{port}/"
    print(f"팀 원고 점검 프로그램: {local_url}")
    if host == "0.0.0.0":
        print(f"같은 네트워크 팀원 접속 주소: http://{_local_network_ip()}:{port}/")
    print("종료하려면 Ctrl+C를 누르세요.")
    if open_browser:
        threading.Timer(.5, functools.partial(webbrowser.open, local_url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        application.executor.shutdown(wait=False, cancel_futures=False)
        server.server_close()


if __name__ == "__main__":
    import argparse
    # Render/Railway 등 PaaS는 PORT 환경변수로 리스닝 포트를 지정하고 0.0.0.0 바인딩을
    # 요구한다. PORT가 있으면 그 값을, 없으면 로컬 기본값(127.0.0.1:8780)을 사용한다.
    hosted_port = os.environ.get("PORT")
    parser = argparse.ArgumentParser(description="팀용 교과서 원고 점검 웹 프로그램")
    parser.add_argument("--data", type=Path,
                        default=Path(os.environ.get("TEAM_DATA_DIR", "team_data")))
    parser.add_argument("--host", default="0.0.0.0" if hosted_port else "127.0.0.1")
    parser.add_argument("--port", type=int, default=int(hosted_port or 8780))
    parser.add_argument("--no-open", action="store_true")
    arguments = parser.parse_args()
    serve_team_app(arguments.data, arguments.host, arguments.port,
                   not arguments.no_open and not hosted_port)
