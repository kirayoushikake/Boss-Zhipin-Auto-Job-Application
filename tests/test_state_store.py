from __future__ import annotations

from datetime import datetime, timezone

import pytest

from boss_zhipin.domain import ApplicationStatus, IdentityConfidence, JobIdentity
from boss_zhipin.state_store import StateStore


def identity(key: str = "boss-job:42") -> JobIdentity:
    return JobIdentity(
        key=key,
        confidence=IdentityConfidence.STRONG,
        job_id=key.rsplit(":", 1)[-1],
        company="测试公司",
        title="Python 工程师",
    )


def transition(store: StateStore, target: ApplicationStatus, *, key: str = "boss-job:42") -> None:
    store.transition(
        identity=identity(key),
        to_status=target,
        account_key="resume:test",
        resume_fingerprint="fingerprint",
    )


def test_schema_and_normal_transition(tmp_path):
    store = StateStore(str(tmp_path / "state.sqlite3"))
    store.create_batch(
        mode="dry-run", account_key="resume:test", resume_fingerprint="fingerprint",
        search_summary={"query": "Python"}, batch_limit=5,
    )
    transition(store, ApplicationStatus.DISCOVERED)
    transition(store, ApplicationStatus.EVALUATING)
    transition(store, ApplicationStatus.ELIGIBLE)
    transition(store, ApplicationStatus.DRAFTED)
    assert store.application("boss-job:42")["status"] == "drafted"
    assert len(store.recent_history()) == 1


def test_illegal_transition_is_rejected(tmp_path):
    store = StateStore(str(tmp_path / "state.sqlite3"))
    transition(store, ApplicationStatus.DISCOVERED)
    with pytest.raises(ValueError, match="非法状态迁移"):
        transition(store, ApplicationStatus.SENDING)


def test_repeat_same_transition_is_idempotent_and_records_history(tmp_path):
    store = StateStore(str(tmp_path / "state.sqlite3"))
    transition(store, ApplicationStatus.DISCOVERED)
    transition(store, ApplicationStatus.DISCOVERED)
    assert store.application("boss-job:42")["status"] == "discovered"


def test_recover_sending_marks_unknown_and_blocks_auto_send(tmp_path):
    store = StateStore(str(tmp_path / "state.sqlite3"))
    for status in (ApplicationStatus.DISCOVERED, ApplicationStatus.EVALUATING, ApplicationStatus.ELIGIBLE, ApplicationStatus.DRAFTED, ApplicationStatus.SENDING):
        transition(store, status)
    assert store.recover_interrupted_sends() == 1
    row = store.application("boss-job:42")
    assert row["status"] == "unknown"
    assert store.is_auto_send_blocked("boss-job:42") is True
    assert store.recover_interrupted_sends() == 0


def test_confirmed_daily_count_and_acceptance_persistence(tmp_path):
    store = StateStore(str(tmp_path / "state.sqlite3"))
    for status in (ApplicationStatus.DISCOVERED, ApplicationStatus.EVALUATING, ApplicationStatus.ELIGIBLE, ApplicationStatus.DRAFTED, ApplicationStatus.SENDING, ApplicationStatus.CONTACT_CONFIRMED):
        transition(store, status)
    assert store.confirmed_today("resume:test") == 1
    store.save_acceptance_step(
        step="environment_check", status="passed", code_version="test-code",
        resume_fingerprint="fingerprint", detail={"sqlite": True},
    )
    restored = StateStore(str(tmp_path / "state.sqlite3"))
    row = restored.acceptance_steps()[0]
    assert row["step"] == "environment_check"
    assert row["detail"] == {"sqlite": True}


def test_execution_lock_blocks_second_real_batch_and_active_send_is_not_recovered(tmp_path):
    db_path = str(tmp_path / "state.sqlite3")
    store = StateStore(db_path)
    batch_id = store.create_batch(mode="execute", account_key="account-a", resume_fingerprint="fp", search_summary={}, batch_limit=1, owner_token="owner-a", acquire_execution_lock=True)
    with pytest.raises(RuntimeError, match="活动真实批次"):
        StateStore(db_path).create_batch(mode="execute", account_key="account-a", resume_fingerprint="fp", search_summary={}, batch_limit=1, owner_token="owner-b", acquire_execution_lock=True)
    for status in (ApplicationStatus.DISCOVERED, ApplicationStatus.EVALUATING, ApplicationStatus.ELIGIBLE, ApplicationStatus.DRAFTED):
        store.transition(identity=identity(), to_status=status, account_key="account-a", resume_fingerprint="fingerprint")
    store.begin_send(identity=identity(), account_key="account-a", resume_fingerprint="fingerprint", batch_id=batch_id, owner_token="owner-a", match_score=80, letter="test", jd_fingerprint="jd", policy_fingerprint="policy")
    assert store.recover_interrupted_sends() == 0
    assert store.application("boss-job:42")["status"] == ApplicationStatus.SENDING


def test_stale_send_becomes_unknown_and_never_retries(tmp_path):
    store = StateStore(str(tmp_path / "state.sqlite3"))
    batch_id = store.create_batch(mode="execute", account_key="resume:test", resume_fingerprint="fingerprint", search_summary={}, batch_limit=1, owner_token="owner", acquire_execution_lock=True)
    for status in (ApplicationStatus.DISCOVERED, ApplicationStatus.EVALUATING, ApplicationStatus.ELIGIBLE, ApplicationStatus.DRAFTED):
        transition(store, status)
    store.begin_send(identity=identity(), account_key="resume:test", resume_fingerprint="fingerprint", batch_id=batch_id, owner_token="owner", match_score=80, letter="test", jd_fingerprint="jd", policy_fingerprint="policy")
    with store._connect() as conn:
        conn.execute("UPDATE execution_locks SET heartbeat_at='2000-01-01T00:00:00+00:00'")
    assert store.recover_interrupted_sends() == 1
    assert store.application("boss-job:42")["status"] == ApplicationStatus.UNKNOWN
    assert store.is_auto_send_blocked("boss-job:42") is True


def test_quota_counts_unknown_and_uses_business_timezone_boundary(tmp_path):
    store = StateStore(str(tmp_path / "state.sqlite3"))
    for status in (ApplicationStatus.DISCOVERED, ApplicationStatus.EVALUATING, ApplicationStatus.ELIGIBLE, ApplicationStatus.DRAFTED, ApplicationStatus.SENDING, ApplicationStatus.UNKNOWN):
        transition(store, status)
    with store._connect() as conn:
        conn.execute("UPDATE applications SET attempted_at=?", ("2026-01-02T15:59:59+00:00",))  # 23:59:59 Shanghai
    assert store.attempted_today("resume:test", "Asia/Shanghai", now=datetime(2026, 1, 2, 15, 59, 59, tzinfo=timezone.utc)) == 1
    assert store.attempted_today("resume:test", "Asia/Shanghai", now=datetime(2026, 1, 2, 16, 0, 1, tzinfo=timezone.utc)) == 0


def test_draft_context_requires_all_fingerprints_and_account(tmp_path):
    store = StateStore(str(tmp_path / "state.sqlite3"))
    for status in (ApplicationStatus.DISCOVERED, ApplicationStatus.EVALUATING, ApplicationStatus.ELIGIBLE, ApplicationStatus.DRAFTED):
        store.transition(identity=identity(), to_status=status, account_key="account-a", resume_fingerprint="resume-a", letter="letter", jd_fingerprint="jd-a", policy_fingerprint="policy-a", draft_validated=status == ApplicationStatus.DRAFTED)
    row = store.application("boss-job:42")
    assert store.draft_is_reusable(row, account_key="account-a", resume_fingerprint="resume-a", jd_fingerprint="jd-a", policy_fingerprint="policy-a")
    assert not store.draft_is_reusable(row, account_key="account-a", resume_fingerprint="resume-b", jd_fingerprint="jd-a", policy_fingerprint="policy-a")
    assert not store.draft_is_reusable(row, account_key="account-a", resume_fingerprint="resume-a", jd_fingerprint="jd-b", policy_fingerprint="policy-a")
    assert not store.draft_is_reusable(row, account_key="account-a", resume_fingerprint="resume-a", jd_fingerprint="jd-a", policy_fingerprint="policy-b")
    assert not store.draft_is_reusable(row, account_key="account-b", resume_fingerprint="resume-a", jd_fingerprint="jd-a", policy_fingerprint="policy-a")


def test_review_requires_explicit_approval_and_cancelled_send_recovers_unknown(tmp_path):
    store = StateStore(str(tmp_path / "state.sqlite3"))
    for status in (ApplicationStatus.DISCOVERED, ApplicationStatus.EVALUATING, ApplicationStatus.ELIGIBLE, ApplicationStatus.DRAFTED, ApplicationStatus.PENDING_REVIEW):
        store.transition(identity=identity(), to_status=status, account_key="account", resume_fingerprint="resume", letter="草稿", jd_fingerprint="jd", policy_fingerprint="policy", draft_validated=True)
    assert store.review_items()[0]["job_key"] == "boss-job:42"
    store.approve_review("boss-job:42")
    assert store.application("boss-job:42")["status"] == ApplicationStatus.REVIEW_APPROVED

    # A process cancelled after begin_send releases its lock but must leave the
    # uncertain row non-retryable; startup recovery makes that explicit.
    batch = store.create_batch(mode="execute", account_key="account", resume_fingerprint="resume", search_summary={}, batch_limit=1, owner_token="owner", acquire_execution_lock=True)
    store.begin_send(identity=identity(), account_key="account", resume_fingerprint="resume", batch_id=batch, owner_token="owner", match_score=80, letter="草稿", jd_fingerprint="jd", policy_fingerprint="policy")
    store.finish_batch(batch, "cancelled", owner_token="owner")
    assert store.recover_interrupted_sends() == 1
    assert store.application("boss-job:42")["status"] == ApplicationStatus.UNKNOWN
