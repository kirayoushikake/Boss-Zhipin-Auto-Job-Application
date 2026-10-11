"""Durable batch state, review workflow and real-send safety locks."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from boss_zhipin.domain import ALLOWED_TRANSITIONS, ApplicationStatus, IdentityConfidence, JobIdentity, as_jsonable

SCHEMA_VERSION = 3
DEFAULT_DB_PATH = "data/boss_zhipin.sqlite3"
DEFAULT_LOCK_LEASE_SECONDS = 300
LIVE_READY_STEPS = ("environment_check", "resume_check", "job_discovery_check", "dry_run_check", "single_live_check", "small_batch_check")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


class StateStore:
    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 15000")
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _initialise(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS batch_runs (
                batch_id TEXT PRIMARY KEY, mode TEXT NOT NULL, account_key TEXT NOT NULL,
                resume_fingerprint TEXT NOT NULL, search_summary TEXT NOT NULL, batch_limit INTEGER NOT NULL,
                created_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL,
                owner_token TEXT, heartbeat_at TEXT, policy_fingerprint TEXT, business_timezone TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs (
                job_key TEXT PRIMARY KEY, confidence TEXT NOT NULL, job_id TEXT, job_url TEXT,
                company TEXT, title TEXT, recruiter TEXT, identity_evidence TEXT,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS applications (
                job_key TEXT PRIMARY KEY REFERENCES jobs(job_key), status TEXT NOT NULL,
                batch_id TEXT REFERENCES batch_runs(batch_id), account_key TEXT NOT NULL,
                resume_fingerprint TEXT NOT NULL, match_score INTEGER, letter TEXT,
                resume_share_status TEXT NOT NULL DEFAULT 'not_requested', confirmation_evidence TEXT,
                error TEXT, attempted_at TEXT, confirmed_at TEXT, updated_at TEXT NOT NULL,
                jd_fingerprint TEXT, policy_fingerprint TEXT, draft_validated INTEGER NOT NULL DEFAULT 0,
                review_approved_at TEXT, review_approved_by TEXT
            );
            CREATE TABLE IF NOT EXISTS application_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_key TEXT NOT NULL REFERENCES jobs(job_key),
                batch_id TEXT, from_status TEXT, to_status TEXT NOT NULL, detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS acceptance_steps (
                step TEXT PRIMARY KEY, status TEXT NOT NULL, code_version TEXT NOT NULL,
                resume_fingerprint TEXT, batch_id TEXT, detail_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                account_key TEXT, config_fingerprint TEXT
            );
            CREATE TABLE IF NOT EXISTS execution_locks (
                account_key TEXT PRIMARY KEY, batch_id TEXT NOT NULL, owner_token TEXT NOT NULL,
                acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
            CREATE INDEX IF NOT EXISTS idx_applications_attempted ON applications(account_key, attempted_at);
            """)
            # Migrate v1 databases without dropping their audit/history data.
            self._add_columns(conn, "batch_runs", {"owner_token": "TEXT", "heartbeat_at": "TEXT", "policy_fingerprint": "TEXT", "business_timezone": "TEXT"})
            self._add_columns(conn, "applications", {"jd_fingerprint": "TEXT", "policy_fingerprint": "TEXT", "draft_validated": "INTEGER NOT NULL DEFAULT 0", "review_approved_at": "TEXT", "review_approved_by": "TEXT"})
            self._add_columns(conn, "acceptance_steps", {"account_key": "TEXT", "config_fingerprint": "TEXT"})
            conn.execute("INSERT INTO schema_meta(key,value) VALUES ('schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(SCHEMA_VERSION),))

    @staticmethod
    def _stale(lock: sqlite3.Row, now: datetime, seconds: int) -> bool:
        heartbeat = _as_utc(lock["heartbeat_at"])
        return heartbeat is None or heartbeat < now - timedelta(seconds=max(1, seconds))

    def _remove_stale_lock(self, conn: sqlite3.Connection, account_key: str, now: datetime, seconds: int) -> None:
        lock = conn.execute("SELECT * FROM execution_locks WHERE account_key=?", (account_key,)).fetchone()
        if lock and self._stale(lock, now, seconds):
            conn.execute("DELETE FROM execution_locks WHERE account_key=?", (account_key,))

    def create_batch(self, *, mode: str, account_key: str, resume_fingerprint: str,
                     search_summary: dict[str, Any], batch_limit: int, policy_fingerprint: str = "",
                     business_timezone: str = "Asia/Shanghai", owner_token: str = "",
                     acquire_execution_lock: bool = False) -> str:
        if acquire_execution_lock and not owner_token:
            raise ValueError("真实执行批次必须提供运行所有者令牌")
        batch_id, now = uuid4().hex, _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if acquire_execution_lock:
                    self._remove_stale_lock(conn, account_key, datetime.now(timezone.utc), DEFAULT_LOCK_LEASE_SECONDS)
                    current = conn.execute("SELECT batch_id FROM execution_locks WHERE account_key=?", (account_key,)).fetchone()
                    if current:
                        raise RuntimeError(f"账号已有活动真实批次（{current['batch_id'][:8]}），拒绝并发执行")
                conn.execute("""INSERT INTO batch_runs(batch_id,mode,account_key,resume_fingerprint,search_summary,batch_limit,created_at,status,owner_token,heartbeat_at,policy_fingerprint,business_timezone)
                                VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)""",
                             (batch_id, mode, account_key, resume_fingerprint, json.dumps(search_summary, ensure_ascii=False), batch_limit, now, owner_token or None, now, policy_fingerprint, business_timezone))
                if acquire_execution_lock:
                    conn.execute("INSERT INTO execution_locks(account_key,batch_id,owner_token,acquired_at,heartbeat_at) VALUES (?, ?, ?, ?, ?)", (account_key, batch_id, owner_token, now, now))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return batch_id

    def acquire_execution_lock(self, *, account_key: str, batch_id: str, owner_token: str, lease_seconds: int = DEFAULT_LOCK_LEASE_SECONDS) -> bool:
        now_dt, now = datetime.now(timezone.utc), _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._remove_stale_lock(conn, account_key, now_dt, lease_seconds)
                old = conn.execute("SELECT * FROM execution_locks WHERE account_key=?", (account_key,)).fetchone()
                if old and (old["batch_id"], old["owner_token"]) != (batch_id, owner_token):
                    conn.execute("ROLLBACK")
                    return False
                conn.execute("INSERT INTO execution_locks(account_key,batch_id,owner_token,acquired_at,heartbeat_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(account_key) DO UPDATE SET batch_id=excluded.batch_id,owner_token=excluded.owner_token,heartbeat_at=excluded.heartbeat_at", (account_key, batch_id, owner_token, now, now))
                conn.execute("UPDATE batch_runs SET owner_token=?,heartbeat_at=? WHERE batch_id=?", (owner_token, now, batch_id))
                conn.execute("COMMIT")
                return True
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def heartbeat_batch(self, *, batch_id: str, account_key: str, owner_token: str) -> bool:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            owned = conn.execute("SELECT 1 FROM execution_locks WHERE account_key=? AND batch_id=? AND owner_token=?", (account_key, batch_id, owner_token)).fetchone()
            if not owned:
                conn.execute("ROLLBACK")
                return False
            conn.execute("UPDATE execution_locks SET heartbeat_at=? WHERE account_key=?", (now, account_key))
            conn.execute("UPDATE batch_runs SET heartbeat_at=? WHERE batch_id=?", (now, batch_id))
            conn.execute("COMMIT")
            return True

    def finish_batch(self, batch_id: str, status: str = "completed", *, owner_token: str = "") -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = _now()
            conn.execute("UPDATE batch_runs SET status=?,ended_at=?,heartbeat_at=? WHERE batch_id=?", (status, now, now, batch_id))
            if owner_token:
                conn.execute("DELETE FROM execution_locks WHERE batch_id=? AND owner_token=?", (batch_id, owner_token))
            else:
                conn.execute("DELETE FROM execution_locks WHERE batch_id=?", (batch_id,))
            conn.execute("COMMIT")

    def _observe(self, conn: sqlite3.Connection, identity: JobIdentity, now: str) -> None:
        conn.execute("""INSERT INTO jobs(job_key,confidence,job_id,job_url,company,title,recruiter,identity_evidence,first_seen_at,last_seen_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(job_key) DO UPDATE SET confidence=excluded.confidence,job_id=excluded.job_id,job_url=excluded.job_url,company=excluded.company,title=excluded.title,recruiter=excluded.recruiter,identity_evidence=excluded.identity_evidence,last_seen_at=excluded.last_seen_at""",
                     (identity.key, str(identity.confidence), identity.job_id, identity.job_url, identity.company, identity.title, identity.recruiter, identity.evidence, now, now))

    def observe_job(self, identity: JobIdentity) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._observe(conn, identity, _now())
            conn.execute("COMMIT")

    def application(self, job_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM applications WHERE job_key=?", (job_key,)).fetchone()
            return dict(row) if row else None

    def applications_for_batch(self, batch_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM applications WHERE batch_id=? ORDER BY updated_at", (batch_id,)).fetchall()]

    def is_auto_send_blocked(self, job_key: str) -> bool:
        row = self.application(job_key)
        return bool(row and row["status"] in {ApplicationStatus.SENDING, ApplicationStatus.CONTACT_CONFIRMED, ApplicationStatus.UNKNOWN})

    def _transition(self, conn: sqlite3.Connection, *, identity: JobIdentity, to_status: ApplicationStatus, account_key: str, resume_fingerprint: str, batch_id: str | None = None, match_score: int | None = None, letter: str | None = None, resume_share_status: str | None = None, evidence: str = "", error: str = "", detail: dict[str, Any] | None = None, jd_fingerprint: str = "", policy_fingerprint: str = "", draft_validated: bool | None = None, review_approved_by: str = "") -> None:
        now = _now()
        self._observe(conn, identity, now)
        prior_row = conn.execute("SELECT status FROM applications WHERE job_key=?", (identity.key,)).fetchone()
        prior = ApplicationStatus(prior_row["status"]) if prior_row else None
        if prior is not None and to_status != prior and to_status not in ALLOWED_TRANSITIONS[prior]:
            raise ValueError(f"非法状态迁移：{prior} -> {to_status}")
        attempted = now if to_status == ApplicationStatus.SENDING else None
        confirmed = now if to_status == ApplicationStatus.CONTACT_CONFIRMED else None
        approved = now if to_status == ApplicationStatus.REVIEW_APPROVED else None
        validated = int(draft_validated) if draft_validated is not None else 0
        conn.execute("""INSERT INTO applications(job_key,status,batch_id,account_key,resume_fingerprint,match_score,letter,resume_share_status,confirmation_evidence,error,attempted_at,confirmed_at,updated_at,jd_fingerprint,policy_fingerprint,draft_validated,review_approved_at,review_approved_by)
                        VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, 'not_requested'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(job_key) DO UPDATE SET status=excluded.status,batch_id=COALESCE(excluded.batch_id,applications.batch_id),account_key=excluded.account_key,resume_fingerprint=excluded.resume_fingerprint,match_score=COALESCE(excluded.match_score,applications.match_score),letter=COALESCE(excluded.letter,applications.letter),resume_share_status=COALESCE(excluded.resume_share_status,applications.resume_share_status),confirmation_evidence=COALESCE(NULLIF(excluded.confirmation_evidence,''),applications.confirmation_evidence),error=COALESCE(NULLIF(excluded.error,''),applications.error),attempted_at=COALESCE(excluded.attempted_at,applications.attempted_at),confirmed_at=COALESCE(excluded.confirmed_at,applications.confirmed_at),updated_at=excluded.updated_at,jd_fingerprint=COALESCE(NULLIF(excluded.jd_fingerprint,''),applications.jd_fingerprint),policy_fingerprint=COALESCE(NULLIF(excluded.policy_fingerprint,''),applications.policy_fingerprint),draft_validated=CASE WHEN excluded.draft_validated=1 THEN 1 ELSE applications.draft_validated END,review_approved_at=COALESCE(excluded.review_approved_at,applications.review_approved_at),review_approved_by=COALESCE(NULLIF(excluded.review_approved_by,''),applications.review_approved_by)""",
                     (identity.key, str(to_status), batch_id, account_key, resume_fingerprint, match_score, letter, resume_share_status, evidence, error, attempted, confirmed, now, jd_fingerprint, policy_fingerprint, validated, approved, review_approved_by))
        conn.execute("INSERT INTO application_events(job_key,batch_id,from_status,to_status,detail_json,created_at) VALUES (?, ?, ?, ?, ?, ?)", (identity.key, batch_id, str(prior) if prior else None, str(to_status), json.dumps(as_jsonable(detail or {}), ensure_ascii=False), now))

    def transition(self, *, identity: JobIdentity, to_status: ApplicationStatus, account_key: str, resume_fingerprint: str, batch_id: str | None = None, match_score: int | None = None, letter: str | None = None, resume_share_status: str | None = None, evidence: str = "", error: str = "", detail: dict[str, Any] | None = None, jd_fingerprint: str = "", policy_fingerprint: str = "", draft_validated: bool | None = None, review_approved_by: str = "") -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._transition(conn, identity=identity, to_status=to_status, account_key=account_key, resume_fingerprint=resume_fingerprint, batch_id=batch_id, match_score=match_score, letter=letter, resume_share_status=resume_share_status, evidence=evidence, error=error, detail=detail, jd_fingerprint=jd_fingerprint, policy_fingerprint=policy_fingerprint, draft_validated=draft_validated, review_approved_by=review_approved_by)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def begin_send(self, *, identity: JobIdentity, account_key: str, resume_fingerprint: str, batch_id: str, owner_token: str, match_score: int | None, letter: str, jd_fingerprint: str, policy_fingerprint: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                owned = conn.execute("SELECT 1 FROM execution_locks WHERE account_key=? AND batch_id=? AND owner_token=?", (account_key, batch_id, owner_token)).fetchone()
                if not owned:
                    raise RuntimeError("真实批次执行锁已失效；拒绝进入发送临界区")
                self._transition(conn, identity=identity, to_status=ApplicationStatus.SENDING, account_key=account_key, resume_fingerprint=resume_fingerprint, batch_id=batch_id, match_score=match_score, letter=letter, jd_fingerprint=jd_fingerprint, policy_fingerprint=policy_fingerprint, draft_validated=True)
                now = _now()
                conn.execute("UPDATE execution_locks SET heartbeat_at=? WHERE account_key=?", (now, account_key))
                conn.execute("UPDATE batch_runs SET heartbeat_at=? WHERE batch_id=?", (now, batch_id))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def recover_interrupted_sends(self, *, stale_after_seconds: int = DEFAULT_LOCK_LEASE_SECONDS) -> int:
        """Only stale/unowned sends are recovered; healthy peers remain untouched."""
        now_dt, now, recovered = datetime.now(timezone.utc), _now(), 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in conn.execute("SELECT job_key,batch_id FROM applications WHERE status='sending'").fetchall():
                lock = conn.execute("SELECT * FROM execution_locks WHERE batch_id=?", (row["batch_id"],)).fetchone() if row["batch_id"] else None
                if lock and not self._stale(lock, now_dt, stale_after_seconds):
                    continue
                changed = conn.execute("UPDATE applications SET status='unknown',error=?,updated_at=? WHERE job_key=? AND status='sending'", ("进程在发送临界区中断或租约失效；禁止自动重发，需人工核查", now, row["job_key"])).rowcount
                if changed:
                    conn.execute("INSERT INTO application_events(job_key,batch_id,from_status,to_status,detail_json,created_at) VALUES (?, ?, 'sending', 'unknown', ?, ?)", (row["job_key"], row["batch_id"], json.dumps({"reason": "stale_send_recovery"}, ensure_ascii=False), now))
                    recovered += 1
            for lock in conn.execute("SELECT * FROM execution_locks").fetchall():
                if self._stale(lock, now_dt, stale_after_seconds):
                    conn.execute("DELETE FROM execution_locks WHERE account_key=?", (lock["account_key"],))
            conn.execute("COMMIT")
        return recovered

    def attempted_today(self, account_key: str, business_timezone: str = "Asia/Shanghai", *, now: datetime | None = None) -> int:
        try:
            zone = ZoneInfo(business_timezone)
        except ZoneInfoNotFoundError as exc:
            # Windows/Python embedded distributions often omit the IANA
            # tzdata package.  The supported mainland/Singapore business
            # zones are fixed UTC+8 (no DST), so retain deterministic quota
            # boundaries instead of silently falling back to UTC.
            if business_timezone in {"Asia/Shanghai", "Asia/Singapore"}:
                zone = timezone(timedelta(hours=8), business_timezone)
            elif business_timezone in {"UTC", "Etc/UTC"}:
                zone = timezone.utc
            else:
                raise ValueError(f"无效业务时区：{business_timezone}") from exc
        reference = now.astimezone(zone) if now and now.tzinfo else (now.replace(tzinfo=zone) if now else datetime.now(zone))
        start = reference.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        end = start + timedelta(days=1)
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM applications WHERE account_key=? AND attempted_at>=? AND attempted_at<?", (account_key, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))).fetchone()
            return int(row["n"])

    def confirmed_today(self, account_key: str) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) AS n FROM applications WHERE account_key=? AND status='contact_confirmed' AND substr(confirmed_at,1,10)=?", (account_key, today)).fetchone()["n"])

    def draft_is_reusable(self, row: dict[str, Any] | None, *, account_key: str, resume_fingerprint: str, jd_fingerprint: str, policy_fingerprint: str) -> bool:
        return bool(row and row.get("status") in {ApplicationStatus.DRAFTED, ApplicationStatus.REVIEW_APPROVED} and row.get("letter") and row.get("draft_validated") and not row.get("attempted_at") and not row.get("confirmed_at") and row.get("account_key") == account_key and row.get("resume_fingerprint") == resume_fingerprint and row.get("jd_fingerprint") == jd_fingerprint and row.get("policy_fingerprint") == policy_fingerprint)

    def review_items(self, *, account_key: str = "", limit: int = 100) -> list[dict[str, Any]]:
        query, params = "SELECT a.*,j.company,j.title,j.job_url,j.confidence FROM applications a JOIN jobs j ON j.job_key=a.job_key WHERE a.status='pending_review'", []
        if account_key:
            query += " AND a.account_key=?"; params.append(account_key)
        query += " ORDER BY a.updated_at DESC LIMIT ?"; params.append(max(1, min(limit, 500)))
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def _identity(self, conn: sqlite3.Connection, job_key: str) -> JobIdentity:
        row = conn.execute("SELECT * FROM jobs WHERE job_key=?", (job_key,)).fetchone()
        if not row:
            raise KeyError(f"未知岗位：{job_key}")
        return JobIdentity(key=row["job_key"], confidence=IdentityConfidence(row["confidence"]), job_id=row["job_id"] or "", job_url=row["job_url"] or "", company=row["company"] or "", title=row["title"] or "", recruiter=row["recruiter"] or "", evidence=row["identity_evidence"] or "")

    def approve_review(self, job_key: str, *, approved_by: str = "local_user") -> None:
        self._change_review(job_key, ApplicationStatus.REVIEW_APPROVED, approved_by=approved_by)

    def reject_review(self, job_key: str, *, reason: str = "用户拒绝草稿") -> None:
        self._change_review(job_key, ApplicationStatus.SKIPPED, error=reason)

    def _change_review(self, job_key: str, target: ApplicationStatus, *, approved_by: str = "", error: str = "") -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM applications WHERE job_key=?", (job_key,)).fetchone()
                if not row or row["status"] != ApplicationStatus.PENDING_REVIEW:
                    raise ValueError("该岗位不处于待审核状态")
                self._transition(conn, identity=self._identity(conn, job_key), to_status=target, account_key=row["account_key"], resume_fingerprint=row["resume_fingerprint"], batch_id=row["batch_id"], match_score=row["match_score"], letter=row["letter"], error=error, jd_fingerprint=row["jd_fingerprint"] or "", policy_fingerprint=row["policy_fingerprint"] or "", draft_validated=bool(row["draft_validated"]), review_approved_by=approved_by, detail={"review": "approved" if target == ApplicationStatus.REVIEW_APPROVED else "rejected"})
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def save_acceptance_step(self, *, step: str, status: str, code_version: str, resume_fingerprint: str = "", account_key: str = "", config_fingerprint: str = "", batch_id: str | None = None, detail: dict[str, Any] | None = None) -> None:
        with self._connect() as conn:
            conn.execute("""INSERT INTO acceptance_steps(step,status,code_version,resume_fingerprint,account_key,config_fingerprint,batch_id,detail_json,updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(step) DO UPDATE SET status=excluded.status,code_version=excluded.code_version,resume_fingerprint=excluded.resume_fingerprint,account_key=excluded.account_key,config_fingerprint=excluded.config_fingerprint,batch_id=excluded.batch_id,detail_json=excluded.detail_json,updated_at=excluded.updated_at""", (step, status, code_version, resume_fingerprint, account_key, config_fingerprint, batch_id, json.dumps(as_jsonable(detail or {}), ensure_ascii=False), _now()))

    def bind_acceptance_context(self, *, code_version: str, account_key: str, resume_fingerprint: str, config_fingerprint: str) -> None:
        with self._connect() as conn:
            # A executes before we can read a real account/resume.  Backfill
            # only that prerequisite.  Never rewrite C--F with a newly read
            # resume: doing so would incorrectly turn stale live evidence
            # into evidence for a different profile/configuration.
            conn.execute("UPDATE acceptance_steps SET account_key=?,resume_fingerprint=?,config_fingerprint=?,updated_at=? WHERE step='environment_check' AND code_version=? AND status='passed'", (account_key, resume_fingerprint, config_fingerprint, _now(), code_version))

    def acceptance_steps(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [{**dict(row), "detail": json.loads(row["detail_json"])} for row in conn.execute("SELECT * FROM acceptance_steps ORDER BY step").fetchall()]

    def is_live_ready(self, *, code_version: str, account_key: str, resume_fingerprint: str, config_fingerprint: str) -> bool:
        rows = {row["step"]: row for row in self.acceptance_steps()}
        return all((row := rows.get(step)) and row["status"] == "passed" and row["code_version"] == code_version and row.get("account_key") == account_key and row.get("resume_fingerprint") == resume_fingerprint and row.get("config_fingerprint") == config_fingerprint for step in LIVE_READY_STEPS)

    def recent_history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT a.*,j.company,j.title,j.job_url,j.confidence FROM applications a JOIN jobs j ON a.job_key=j.job_key ORDER BY a.updated_at DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()]
