from __future__ import annotations

import asyncio

from boss_zhipin import acceptance
from boss_zhipin.acceptance import AcceptanceWizard, STEPS
from boss_zhipin.batch_runner import BatchOptions
from boss_zhipin.cli_tools import DoctorReport
from boss_zhipin.state_store import StateStore


def wizard(tmp_path) -> tuple[AcceptanceWizard, StateStore]:
    store = StateStore(str(tmp_path / "state.sqlite3"))
    return AcceptanceWizard(store, BatchOptions(resume_source="text", resume_path="ignored"), "code-under-test"), store


def mark_passed(store: StateStore, *steps: str) -> None:
    for step in steps:
        store.save_acceptance_step(step=step, status="passed", code_version="code-under-test")


def test_environment_failure_does_not_open_browser(monkeypatch, tmp_path):
    guide, store = wizard(tmp_path)
    monkeypatch.setattr(acceptance, "doctor_report", lambda **_kwargs: DoctorReport(False, [{"name": "Python", "ok": False, "detail": "bad"}]))
    monkeypatch.setattr(acceptance, "print_doctor_report", lambda _report: None)
    called = {"browser": 0}

    async def browser(*_args):
        called["browser"] += 1

    monkeypatch.setattr(acceptance.finding_jobs, "open_browser_with_options", browser)
    asyncio.run(guide.run())
    assert called["browser"] == 0
    assert store.acceptance_steps()[0]["status"] == "failed"


def test_single_live_requires_separate_user_authorisation(monkeypatch, tmp_path):
    guide, store = wizard(tmp_path)
    mark_passed(store, *STEPS[:4])
    monkeypatch.setattr(acceptance, "confirm", lambda _prompt: False)
    asyncio.run(guide.run())
    records = {row["step"]: row for row in store.acceptance_steps()}
    assert records["single_live_check"]["status"] == "pending_user_action"
    assert "small_batch_check" not in records


def test_small_batch_requires_its_own_authorisation(monkeypatch, tmp_path):
    guide, store = wizard(tmp_path)
    mark_passed(store, *STEPS[:5])
    monkeypatch.setattr(acceptance, "confirm", lambda _prompt: False)
    asyncio.run(guide.run())
    records = {row["step"]: row for row in store.acceptance_steps()}
    assert records["small_batch_check"]["status"] == "pending_user_action"


def test_changed_code_version_invalidates_prior_pass(tmp_path):
    guide, store = wizard(tmp_path)
    store.save_acceptance_step(step="environment_check", status="passed", code_version="old-code")
    assert guide._passed("environment_check") is False


def test_resumed_job_scan_reopens_browser_before_reading_cards(monkeypatch, tmp_path):
    guide, store = wizard(tmp_path)
    mark_passed(store, *STEPS[:2])
    opened = {"count": 0}

    async def open_browser(*_args):
        opened["count"] += 1

    async def logged_in():
        return True

    async def navigate(*_args):
        return None

    async def no_snapshot(_index):
        return None

    monkeypatch.setattr(acceptance, "confirm", lambda _prompt: True)
    monkeypatch.setattr(acceptance.finding_jobs, "get_tab", lambda: None)
    monkeypatch.setattr(acceptance.finding_jobs, "open_browser_with_options", open_browser)
    monkeypatch.setattr(acceptance.finding_jobs, "log_in", logged_in)
    monkeypatch.setattr(acceptance.finding_jobs, "navigate_to", navigate)
    monkeypatch.setattr(acceptance.finding_jobs, "get_job_snapshot_by_index", no_snapshot)
    asyncio.run(guide.run())
    assert opened["count"] == 1
    records = {row["step"]: row for row in store.acceptance_steps()}
    assert records["job_discovery_check"]["status"] == "failed"
