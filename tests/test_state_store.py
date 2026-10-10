from __future__ import annotations

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
