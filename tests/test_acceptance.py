from __future__ import annotations

import asyncio

from boss_zhipin import acceptance
from boss_zhipin.acceptance import AcceptanceWizard, STEPS
from boss_zhipin.batch_runner import BatchOptions
from boss_zhipin.cli_tools import DoctorReport
from boss_zhipin.resume_profile import ResumeProfile
from boss_zhipin.state_store import StateStore


def profile() -> ResumeProfile:
    return ResumeProfile(source="boss_online", display_name="王小明", desired_positions=["游戏运营"], education="本科", work_experiences=["运营"], project_experiences=["项目"], skills=["数据分析"], raw_text="游戏运营项目经验 数据分析 用户研究 " * 8, fingerprint="resume-fingerprint")


def wizard(tmp_path) -> tuple[AcceptanceWizard, StateStore]:
    store = StateStore(str(tmp_path / "state.sqlite3"))
    return AcceptanceWizard(store, BatchOptions(resume_source="text", resume_path="ignored"), "code-under-test"), store


def mark_passed(guide: AcceptanceWizard, store: StateStore, *steps: str) -> None:
    for step in steps:
        store.save_acceptance_step(step=step, status="passed", code_version="code-under-test", account_key="boss-account:fixture", resume_fingerprint="resume-fingerprint", config_fingerprint=guide.config_fingerprint)


def install_b_resume(monkeypatch):
    async def browser(*_args):
        return None
    async def login():
        return True
    async def account():
        return "boss-account:fixture"
    class Source:
        async def load(self):
            return profile()
    monkeypatch.setattr(acceptance.finding_jobs, "open_browser_with_options", browser)
    monkeypatch.setattr(acceptance.finding_jobs, "log_in", login)
    monkeypatch.setattr(acceptance.finding_jobs, "current_account_key", account)
    monkeypatch.setattr(acceptance, "BossOnlineResumeSource", Source)


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


def test_b_requires_second_confirmation_after_showing_summary(monkeypatch, tmp_path):
    guide, store = wizard(tmp_path)
    install_b_resume(monkeypatch)
    monkeypatch.setattr(acceptance, "doctor_report", lambda **_kwargs: DoctorReport(True, []))
    monkeypatch.setattr(acceptance, "print_doctor_report", lambda _report: None)
    answers = iter((True, False))
    monkeypatch.setattr(acceptance, "confirm", lambda _prompt: next(answers))
    asyncio.run(guide.run())
    row = {item["step"]: item for item in store.acceptance_steps()}["resume_check"]
    assert row["status"] == "pending_user_action"
    assert row["detail"]["reason"] == "用户未确认简历摘要"


def test_single_live_requires_separate_user_authorisation(monkeypatch, tmp_path):
    guide, store = wizard(tmp_path); mark_passed(guide, store, *STEPS[:4]); install_b_resume(monkeypatch)
    answers = iter((True, True, False))  # B read, B summary, E
    monkeypatch.setattr(acceptance, "confirm", lambda _prompt: next(answers))
    asyncio.run(guide.run())
    assert {row["step"]: row for row in store.acceptance_steps()}["single_live_check"]["status"] == "pending_user_action"


def test_small_batch_requires_its_own_authorisation(monkeypatch, tmp_path):
    guide, store = wizard(tmp_path); mark_passed(guide, store, *STEPS[:5]); install_b_resume(monkeypatch)
    answers = iter((True, True, False))
    monkeypatch.setattr(acceptance, "confirm", lambda _prompt: next(answers))
    asyncio.run(guide.run())
    assert {row["step"]: row for row in store.acceptance_steps()}["small_batch_check"]["status"] == "pending_user_action"


def test_changed_code_or_context_invalidates_prior_pass(tmp_path):
    guide, store = wizard(tmp_path)
    store.save_acceptance_step(step="environment_check", status="passed", code_version="old-code")
    assert guide._passed("environment_check") is False
    guide.account_key, guide.resume_fingerprint = "other", "other"
    store.save_acceptance_step(step="resume_check", status="passed", code_version="code-under-test", account_key="boss-account:fixture", resume_fingerprint="resume-fingerprint", config_fingerprint=guide.config_fingerprint)
    assert guide._passed("resume_check") is False


def test_c_records_all_requested_scan_failures(monkeypatch, tmp_path):
    guide, store = wizard(tmp_path); mark_passed(guide, store, *STEPS[:2]); install_b_resume(monkeypatch)
    async def navigate(*_args):
        return None
    async def no_snapshot(_index):
        return None
    monkeypatch.setattr(acceptance.finding_jobs, "navigate_to", navigate)
    monkeypatch.setattr(acceptance.finding_jobs, "get_job_snapshot_by_index", no_snapshot)
    answers = iter((True, True, True))
    monkeypatch.setattr(acceptance, "confirm", lambda _prompt: next(answers))
    asyncio.run(guide.run())
    row = {item["step"]: item for item in store.acceptance_steps()}["job_discovery_check"]
    assert row["status"] == "failed"
    assert row["detail"]["requested"] == 3
    assert row["detail"]["successful"] == 0
