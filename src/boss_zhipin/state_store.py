"""SQLite-backed state, idempotency and acceptance progress.

The database is intentionally additive: it is a new file and does not mutate
existing JSONL audit logs.  Every state mutation is wrapped in a transaction.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from boss_zhipin.domain import ALLOWED_TRANSITIONS, ApplicationStatus, JobIdentity, as_jsonable


SCHEMA_VERSION = 1
DEFAULT_DB_PATH = "data/boss_zhipin.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            yield conn
        finally:
            conn.close()

    def _initialise(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_runs (
                    batch_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    account_key TEXT NOT NULL,
                    resume_fingerprint TEXT NOT NULL,
                    search_summary TEXT NOT NULL,
                    batch_limit INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    ended_at TEXT,
                    status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_key TEXT PRIMARY KEY,
                    confidence TEXT NOT NULL,
                    job_id TEXT,
                    job_url TEXT,
                    company TEXT,
                    title TEXT,
                    recruiter TEXT,
                    identity_evidence TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS applications (
                    job_key TEXT PRIMARY KEY REFERENCES jobs(job_key),
                    status TEXT NOT NULL,
                    batch_id TEXT REFERENCES batch_runs(batch_id),
                    account_key TEXT NOT NULL,
                    resume_fingerprint TEXT NOT NULL,
                    match_score INTEGER,
                    letter TEXT,
                    resume_share_status TEXT NOT NULL DEFAULT 'not_requested',
                    confirmation_evidence TEXT,
                    error TEXT,
                    attempted_at TEXT,
                    confirmed_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS application_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_key TEXT NOT NULL REFERENCES jobs(job_key),
                    batch_id TEXT,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS acceptance_steps (
                    step TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    code_version TEXT NOT NULL,
                    resume_fingerprint TEXT,
                    batch_id TEXT,
                    detail_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
                CREATE INDEX IF NOT EXISTS idx_applications_confirmed ON applications(confirmed_at);
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def create_batch(
        self, *, mode: str, account_key: str, resume_fingerprint: str,
        search_summary: dict[str, Any], batch_limit: int,
    ) -> str:
        batch_id = uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO batch_runs
                   (batch_id, mode, account_key, resume_fingerprint, search_summary, batch_limit, created_at, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'running')""",
                (batch_id, mode, account_key, resume_fingerprint, json.dumps(search_summary, ensure_ascii=False), batch_limit, _now()),
            )
            conn.execute("COMMIT")
        return batch_id

    def finish_batch(self, batch_id: str, status: str = "completed") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE batch_runs SET status=?, ended_at=? WHERE batch_id=?",
                (status, _now(), batch_id),
            )

    def observe_job(self, identity: JobIdentity) -> None:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """INSERT INTO jobs(job_key, confidence, job_id, job_url, company, title, recruiter, identity_evidence, first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_key) DO UPDATE SET
                     confidence=excluded.confidence, job_id=excluded.job_id, job_url=excluded.job_url,
                     company=excluded.company, title=excluded.title, recruiter=excluded.recruiter,
                     identity_evidence=excluded.identity_evidence, last_seen_at=excluded.last_seen_at""",
                (identity.key, str(identity.confidence), identity.job_id, identity.job_url, identity.company,
                 identity.title, identity.recruiter, identity.evidence, now, now),
            )
            conn.execute("COMMIT")

    def application(self, job_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM applications WHERE job_key=?", (job_key,)).fetchone()
            return dict(row) if row else None

    def is_auto_send_blocked(self, job_key: str) -> bool:
        row = self.application(job_key)
        if not row:
            return False
        return row["status"] in {
            ApplicationStatus.SENDING,
            ApplicationStatus.CONTACT_CONFIRMED,
            ApplicationStatus.UNKNOWN,
        }

    def transition(
        self, *, identity: JobIdentity, to_status: ApplicationStatus, account_key: str,
        resume_fingerprint: str, batch_id: str | None = None, match_score: int | None = None,
        letter: str | None = None, resume_share_status: str | None = None,
        evidence: str = "", error: str = "", detail: dict[str, Any] | None = None,
    ) -> None:
        self.observe_job(identity)
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT status FROM applications WHERE job_key=?", (identity.key,)).fetchone()
            previous = ApplicationStatus(existing["status"]) if existing else None
            if previous is not None and to_status != previous and to_status not in ALLOWED_TRANSITIONS[previous]:
                conn.execute("ROLLBACK")
                raise ValueError(f"非法状态迁移：{previous} -> {to_status}")
            attempted_at = now if to_status == ApplicationStatus.SENDING else None
            confirmed_at = now if to_status == ApplicationStatus.CONTACT_CONFIRMED else None
            conn.execute(
                """INSERT INTO applications
                   (job_key,status,batch_id,account_key,resume_fingerprint,match_score,letter,resume_share_status,
                    confirmation_evidence,error,attempted_at,confirmed_at,updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'not_requested'), ?, ?, ?, ?, ?)
                   ON CONFLICT(job_key) DO UPDATE SET
                    status=excluded.status, batch_id=COALESCE(excluded.batch_id, applications.batch_id),
                    match_score=COALESCE(excluded.match_score, applications.match_score),
                    letter=COALESCE(excluded.letter, applications.letter),
                    resume_share_status=COALESCE(excluded.resume_share_status, applications.resume_share_status),
                    confirmation_evidence=COALESCE(NULLIF(excluded.confirmation_evidence, ''), applications.confirmation_evidence),
                    error=COALESCE(NULLIF(excluded.error, ''), applications.error),
                    attempted_at=COALESCE(excluded.attempted_at, applications.attempted_at),
                    confirmed_at=COALESCE(excluded.confirmed_at, applications.confirmed_at), updated_at=excluded.updated_at""",
                (identity.key, str(to_status), batch_id, account_key, resume_fingerprint, match_score, letter,
                 resume_share_status, evidence, error, attempted_at, confirmed_at, now),
            )
            conn.execute(
                """INSERT INTO application_events(job_key,batch_id,from_status,to_status,detail_json,created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (identity.key, batch_id, str(previous) if previous else None, str(to_status),
                 json.dumps(as_jsonable(detail or {}), ensure_ascii=False), now),
            )
            conn.execute("COMMIT")

    def recover_interrupted_sends(self) -> int:
        """A crashed process can never prove whether Enter reached the platform."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT job_key, batch_id FROM applications WHERE status='sending'").fetchall()
            now = _now()
            for row in rows:
                conn.execute(
                    "UPDATE applications SET status='unknown', error=?, updated_at=? WHERE job_key=?",
                    ("进程在发送临界区中断；禁止自动重发，需人工核查", now, row["job_key"]),
                )
                conn.execute(
                    "INSERT INTO application_events(job_key,batch_id,from_status,to_status,detail_json,created_at) VALUES (?, ?, 'sending', 'unknown', ?, ?)",
                    (row["job_key"], row["batch_id"], json.dumps({"reason": "startup_recovery"}, ensure_ascii=False), now),
                )
            conn.execute("COMMIT")
        return len(rows)

    def confirmed_today(self, account_key: str) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM applications WHERE account_key=?
                   AND status='contact_confirmed' AND substr(confirmed_at, 1, 10)=?""",
                (account_key, today),
            ).fetchone()
            return int(row["n"])

    def save_acceptance_step(
        self, *, step: str, status: str, code_version: str, resume_fingerprint: str = "",
        batch_id: str | None = None, detail: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO acceptance_steps(step,status,code_version,resume_fingerprint,batch_id,detail_json,updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(step) DO UPDATE SET status=excluded.status, code_version=excluded.code_version,
                   resume_fingerprint=excluded.resume_fingerprint, batch_id=excluded.batch_id,
                   detail_json=excluded.detail_json, updated_at=excluded.updated_at""",
                (step, status, code_version, resume_fingerprint, batch_id,
                 json.dumps(as_jsonable(detail or {}), ensure_ascii=False), _now()),
            )

    def acceptance_steps(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM acceptance_steps ORDER BY step").fetchall()
        return [{**dict(row), "detail": json.loads(row["detail_json"])} for row in rows]

    def recent_history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT a.*, j.company, j.title, j.job_url, j.confidence
                   FROM applications a JOIN jobs j ON a.job_key=j.job_key
                   ORDER BY a.updated_at DESC LIMIT ?""", (max(1, min(limit, 500)),),
            ).fetchall()
            return [dict(row) for row in rows]
