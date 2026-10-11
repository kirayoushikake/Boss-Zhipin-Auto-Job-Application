from __future__ import annotations

import asyncio

import pytest

from boss_zhipin import batch_runner
from boss_zhipin.batch_runner import BatchOptions, BatchRunner
from boss_zhipin.domain import (
    ApplicationStatus,
    IdentityConfidence,
    JobIdentity,
    JobSnapshot,
    ResumeShareStatus,
)
from boss_zhipin.resume_profile import ResumeProfile
from boss_zhipin.state_store import StateStore


def profile() -> ResumeProfile:
    text = "Python 后端工程师，具备 API、MySQL、Docker 与服务开发经验。" * 5
    return ResumeProfile(
        source="local_text", display_name="王小明", desired_positions=["Python 后端"],
        education="本科", work_experiences=["后端开发"], skills=["Python", "Docker"],
        raw_text=text, fingerprint="profile-fingerprint",
    )


def snapshot(number: int = 1, confidence: IdentityConfidence = IdentityConfidence.STRONG) -> JobSnapshot:
    identity = JobIdentity(
        key=f"boss-job:{number}", confidence=confidence, job_id=str(number) if confidence == IdentityConfidence.STRONG else "",
        company="测试公司", title="Python 工程师", recruiter="招聘者", evidence="fixture",
    )
    return JobSnapshot(identity=identity, description="需要 Python Docker MySQL 的后端工程师", card_text="测试公司 Python 工程师", jd_changed_after_click=True, jd_evidence="fixture changed", identity_matches_detail=True)


def install_common_mocks(monkeypatch, snapshots: list[JobSnapshot | None]) -> dict[str, int]:
    calls = {"send": 0, "open": 0}

    async def load(_options):
        return profile()

    async def open_browser(*_args):
        calls["open"] += 1

    async def logged_in():
        return True

    async def account_key():
        return "boss-account:fixture"

    async def navigate(*_args):
        return None

    async def next_snapshot(index):
        return snapshots[index - 1] if index <= len(snapshots) else None

    async def open_chat(_identity):
        return True, "fixture chat identity"

    async def send(_letter):
        calls["send"] += 1
        return True

    async def share():
        return ResumeShareStatus.NOT_AVAILABLE

    monkeypatch.setattr(batch_runner, "load_resume_profile", load)
    monkeypatch.setattr(batch_runner.finding_jobs, "open_browser_with_options", open_browser)
    monkeypatch.setattr(batch_runner.finding_jobs, "log_in", logged_in)
    monkeypatch.setattr(batch_runner.finding_jobs, "current_account_key", account_key)
    monkeypatch.setattr(batch_runner.finding_jobs, "navigate_to", navigate)
    monkeypatch.setattr(batch_runner.finding_jobs, "navigate_back", navigate)
    monkeypatch.setattr(batch_runner.finding_jobs, "get_job_snapshot_by_index", next_snapshot)
    monkeypatch.setattr(batch_runner.finding_jobs, "open_and_verify_chat", open_chat)
    monkeypatch.setattr(batch_runner.finding_jobs, "send_chat_message", send)
    monkeypatch.setattr(batch_runner.finding_jobs, "try_share_online_resume", share)
    monkeypatch.setattr(batch_runner, "extract_keywords_from_text", lambda _text: ["Python", "Docker"])
    monkeypatch.setattr(batch_runner, "embed_resume", lambda *_args: object())
    monkeypatch.setattr(batch_runner, "should_apply", lambda *_args: (True, {"score": 88, "reason": "匹配", "matched_keywords": ["Python"], "scoring_degraded": False}))
    monkeypatch.setattr(batch_runner, "generate_letter", lambda *_args: "您好，我有 Python 后端和 Docker 服务开发经验，希望进一步沟通岗位匹配情况。")
    return calls


def run(runner: BatchRunner, **kwargs):
    return asyncio.run(runner.run(**kwargs))


def test_game_search_defaults_and_explicit_single_query_override():
    defaults = BatchOptions(resume_source="text", resume_path="ignored", queries=["游戏运营", "游戏策划", "游戏运营"])
    assert defaults.search_queries == ("游戏运营", "游戏策划")
    assert "query=%E6%B8%B8%E6%88%8F%E8%BF%90%E8%90%A5" in defaults.search_url

    one_query = BatchOptions(resume_source="text", resume_path="ignored", query="游戏用户研究", queries=["游戏运营"])
    assert one_query.search_queries == ("游戏用户研究",)


def test_multi_query_scan_reuses_the_same_browser_navigation(monkeypatch, tmp_path):
    options = BatchOptions(
        resume_source="text", resume_path="ignored", queries=["游戏运营", "游戏策划"], scan_limit=2,
        prefer_company_keywords=["二次元"],
    )
    runner = BatchRunner(StateStore(str(tmp_path / "state.sqlite3")), options)
    navigated: list[str] = []

    async def cards(terms, limit):
        assert terms == ("二次元",)
        assert limit == 2
        return [2, 1]

    async def navigate(url):
        navigated.append(url)

    monkeypatch.setattr(batch_runner.finding_jobs, "prioritized_job_card_indices", cards)
    monkeypatch.setattr(batch_runner.finding_jobs, "navigate_to", navigate)
    targets = []

    async def collect():
        async for target in runner._scan_targets():
            targets.append(target)

    asyncio.run(collect())
    assert targets == [("游戏运营", 2), ("游戏运营", 1), ("游戏策划", 2), ("游戏策划", 1)]
    assert navigated == [options.search_url_for("游戏策划")]


def test_scan_uses_normal_page_scroll_when_visible_cards_are_insufficient(monkeypatch, tmp_path):
    options = BatchOptions(resume_source="text", resume_path="ignored", queries=["游戏运营"], scan_limit=2)
    runner = BatchRunner(StateStore(str(tmp_path / "state.sqlite3")), options)
    pages = iter(([1], [1, 2]))
    async def cards(*_args):
        return next(pages)
    scrolled = {"count": 0}
    async def scroll():
        scrolled["count"] += 1
        return 1
    monkeypatch.setattr(batch_runner.finding_jobs, "prioritized_job_card_indices", cards)
    monkeypatch.setattr(batch_runner.finding_jobs, "scroll_job_list", scroll)
    async def collect():
        return [item async for item in runner._scan_targets()]
    assert asyncio.run(collect()) == [("游戏运营", 1), ("游戏运营", 2)]
    assert scrolled["count"] == 1


def test_external_llm_authorisation_is_required_before_browser(monkeypatch, tmp_path):
    calls = install_common_mocks(monkeypatch, [snapshot()])
    runner = BatchRunner(StateStore(str(tmp_path / "state.sqlite3")), BatchOptions(resume_source="text", resume_path="ignored", mode="dry-run"))
    with pytest.raises(PermissionError, match="LLM"):
        run(runner)
    assert calls["open"] == 0


def test_dry_run_never_invokes_send(monkeypatch, tmp_path):
    calls = install_common_mocks(monkeypatch, [snapshot()])
    runner = BatchRunner(StateStore(str(tmp_path / "state.sqlite3")), BatchOptions(resume_source="text", resume_path="ignored", mode="dry-run", scan_limit=1))
    summary = run(runner, external_llm_authorised=True)
    assert summary.drafted == 1
    assert summary.attempted == 0
    assert calls["send"] == 0


def test_weak_identity_is_skipped_without_llm_or_send(monkeypatch, tmp_path):
    calls = install_common_mocks(monkeypatch, [snapshot(confidence=IdentityConfidence.WEAK)])
    monkeypatch.setattr(batch_runner, "generate_letter", lambda *_args: pytest.fail("weak identity must not generate a letter"))
    store = StateStore(str(tmp_path / "state.sqlite3"))
    summary = run(BatchRunner(store, BatchOptions(resume_source="text", resume_path="ignored", mode="dry-run", scan_limit=1)), external_llm_authorised=True)
    assert summary.skipped == 1
    assert calls["send"] == 0
    assert store.application("boss-job:1")["status"] == ApplicationStatus.SKIPPED


def test_unknown_page_send_stops_batch_and_never_retries(monkeypatch, tmp_path):
    calls = install_common_mocks(monkeypatch, [snapshot(1), snapshot(2)])

    async def no_echo(_letter):
        calls["send"] += 1
        return False

    monkeypatch.setattr(batch_runner.finding_jobs, "send_chat_message", no_echo)
    store = StateStore(str(tmp_path / "state.sqlite3"))
    runner = BatchRunner(store, BatchOptions(resume_source="text", resume_path="ignored", mode="execute", scan_limit=2, batch_limit=2, delay_seconds=0))
    summary = run(runner, external_llm_authorised=True, explicit_execute_authorised=True, acceptance_authorised=True)
    assert summary.unknown == 1
    assert "结果未知" in summary.safety_stop
    assert calls["send"] == 1
    assert store.application("boss-job:1")["status"] == ApplicationStatus.UNKNOWN
    assert store.application("boss-job:2") is None


def test_execute_obeys_batch_limit(monkeypatch, tmp_path):
    calls = install_common_mocks(monkeypatch, [snapshot(1), snapshot(2)])
    store = StateStore(str(tmp_path / "state.sqlite3"))
    runner = BatchRunner(store, BatchOptions(resume_source="text", resume_path="ignored", mode="execute", scan_limit=2, batch_limit=1, delay_seconds=0))
    summary = run(runner, external_llm_authorised=True, explicit_execute_authorised=True, acceptance_authorised=True)
    assert summary.attempted == 1
    assert summary.page_confirmed == 1
    assert calls["send"] == 1
    assert summary.safety_stop == "已达到本批次真实执行上限"


def test_existing_application_is_deduplicated_across_runs(monkeypatch, tmp_path):
    calls = install_common_mocks(monkeypatch, [snapshot(1)])
    store = StateStore(str(tmp_path / "state.sqlite3"))
    identity = snapshot(1).identity
    store.transition(
        identity=identity, to_status=ApplicationStatus.DISCOVERED,
        account_key="resume:profile-fingerprint", resume_fingerprint="profile-fingerprint",
    )
    summary = run(
        BatchRunner(store, BatchOptions(resume_source="text", resume_path="ignored", mode="dry-run", scan_limit=1)),
        external_llm_authorised=True,
    )
    assert summary.duplicates == 1
    assert calls["send"] == 0


def test_unsent_skipped_job_can_be_reevaluated_after_policy_change(monkeypatch, tmp_path):
    install_common_mocks(monkeypatch, [snapshot(1)])
    store = StateStore(str(tmp_path / "state.sqlite3"))
    identity = snapshot(1).identity
    store.transition(
        identity=identity, to_status=ApplicationStatus.DISCOVERED,
        account_key="resume:profile-fingerprint", resume_fingerprint="profile-fingerprint",
    )
    store.transition(
        identity=identity, to_status=ApplicationStatus.SKIPPED,
        account_key="resume:profile-fingerprint", resume_fingerprint="profile-fingerprint",
        error="旧向量阈值过严",
    )
    summary = run(
        BatchRunner(store, BatchOptions(resume_source="text", resume_path="ignored", mode="dry-run", scan_limit=1)),
        external_llm_authorised=True,
    )
    assert summary.duplicates == 0
    assert summary.drafted == 1


def test_execute_stops_when_daily_page_confirmed_limit_is_reached(monkeypatch, tmp_path):
    calls = install_common_mocks(monkeypatch, [snapshot(2)])
    store = StateStore(str(tmp_path / "state.sqlite3"))
    prior = snapshot(1).identity
    for status in (
        ApplicationStatus.DISCOVERED, ApplicationStatus.EVALUATING, ApplicationStatus.ELIGIBLE,
        ApplicationStatus.DRAFTED, ApplicationStatus.SENDING, ApplicationStatus.CONTACT_CONFIRMED,
    ):
        store.transition(
            identity=prior, to_status=status, account_key="boss-account:fixture",
            resume_fingerprint="profile-fingerprint",
        )
    summary = run(
        BatchRunner(store, BatchOptions(resume_source="text", resume_path="ignored", mode="execute", scan_limit=1, batch_limit=2, daily_limit=1)),
        external_llm_authorised=True,
        explicit_execute_authorised=True, acceptance_authorised=True,
    )
    assert summary.scanned == 0
    assert summary.safety_stop == "已达到每日真实沟通尝试上限（含 unknown）"
    assert calls["send"] == 0


def test_required_resume_share_stops_but_preserves_confirmed_message(monkeypatch, tmp_path):
    install_common_mocks(monkeypatch, [snapshot(1)])
    store = StateStore(str(tmp_path / "state.sqlite3"))
    summary = run(
        BatchRunner(
            store,
            BatchOptions(
                resume_source="text", resume_path="ignored", mode="execute", scan_limit=1,
                resume_delivery="require_confirmed_share", delay_seconds=0,
            ),
        ),
        external_llm_authorised=True,
        explicit_execute_authorised=True, acceptance_authorised=True,
    )
    assert summary.page_confirmed == 1
    assert summary.unknown == 0
    assert summary.safety_stop == "在线简历分享未确认，已安全停止"
    row = store.application("boss-job:1")
    assert row["status"] == ApplicationStatus.CONTACT_CONFIRMED
    assert row["resume_share_status"] == ResumeShareStatus.NOT_AVAILABLE


def test_execute_reuses_only_an_exact_context_never_sent_dry_run_draft(monkeypatch, tmp_path):
    calls = install_common_mocks(monkeypatch, [snapshot(1)])
    store = StateStore(str(tmp_path / "state.sqlite3"))
    identity = snapshot(1).identity
    options = BatchOptions(resume_source="text", resume_path="ignored", mode="execute", scan_limit=1, delay_seconds=0)
    jd_fingerprint = batch_runner.stable_fingerprint(snapshot(1).description)
    for status in (
        ApplicationStatus.DISCOVERED, ApplicationStatus.EVALUATING,
        ApplicationStatus.ELIGIBLE, ApplicationStatus.DRAFTED,
    ):
        store.transition(
            identity=identity, to_status=status, account_key="boss-account:fixture",
            resume_fingerprint="profile-fingerprint", match_score=88,
            letter="您好，我有 Python 后端和 Docker 服务开发经验，希望进一步沟通岗位匹配情况。",
            jd_fingerprint=jd_fingerprint, policy_fingerprint=options.policy_fingerprint,
            draft_validated=status == ApplicationStatus.DRAFTED,
        )
    monkeypatch.setattr(batch_runner, "generate_letter", lambda *_args: pytest.fail("existing draft must be reused without a new LLM letter"))
    summary = run(
        BatchRunner(store, options),
        external_llm_authorised=True,
        explicit_execute_authorised=True, acceptance_authorised=True,
    )
    assert summary.page_confirmed == 1
    assert calls["send"] == 1
    assert store.application("boss-job:1")["status"] == ApplicationStatus.CONTACT_CONFIRMED


@pytest.mark.parametrize("changed", ("resume", "jd", "policy", "account"))
def test_stale_draft_context_is_reevaluated_not_sent_as_old_text(monkeypatch, tmp_path, changed):
    calls = install_common_mocks(monkeypatch, [snapshot(1)])
    sent: list[str] = []
    async def record_send(letter, *_identity):
        sent.append(letter); calls["send"] += 1
        return True
    monkeypatch.setattr(batch_runner.finding_jobs, "send_chat_message", record_send)
    store = StateStore(str(tmp_path / "state.sqlite3"))
    options = BatchOptions(resume_source="text", resume_path="ignored", mode="execute", scan_limit=1, delay_seconds=0, min_score=60 if changed == "policy" else 50)
    old_account = "account-other" if changed == "account" else "boss-account:fixture"
    old_resume = "old-resume" if changed == "resume" else "profile-fingerprint"
    old_jd = "old-jd" if changed == "jd" else batch_runner.stable_fingerprint(snapshot(1).description)
    old_policy = "old-policy" if changed == "policy" else options.policy_fingerprint
    for status in (ApplicationStatus.DISCOVERED, ApplicationStatus.EVALUATING, ApplicationStatus.ELIGIBLE, ApplicationStatus.DRAFTED):
        store.transition(identity=snapshot(1).identity, to_status=status, account_key=old_account, resume_fingerprint=old_resume, match_score=88, letter="旧草稿文本", jd_fingerprint=old_jd, policy_fingerprint=old_policy, draft_validated=status == ApplicationStatus.DRAFTED)
    summary = run(BatchRunner(store, options), external_llm_authorised=True, explicit_execute_authorised=True, acceptance_authorised=True)
    assert summary.page_confirmed == 1
    assert sent and sent == ["您好，我有 Python 后端和 Docker 服务开发经验，希望进一步沟通岗位匹配情况。"]
    assert store.application("boss-job:1")["resume_fingerprint"] == "profile-fingerprint"


def test_pending_review_is_never_an_automatic_send_candidate(monkeypatch, tmp_path):
    calls = install_common_mocks(monkeypatch, [snapshot(1)])
    store = StateStore(str(tmp_path / "state.sqlite3"))
    options = BatchOptions(resume_source="text", resume_path="ignored", mode="execute", scan_limit=1)
    jd = batch_runner.stable_fingerprint(snapshot(1).description)
    for status in (ApplicationStatus.DISCOVERED, ApplicationStatus.EVALUATING, ApplicationStatus.ELIGIBLE, ApplicationStatus.DRAFTED, ApplicationStatus.PENDING_REVIEW):
        store.transition(identity=snapshot(1).identity, to_status=status, account_key="boss-account:fixture", resume_fingerprint="profile-fingerprint", match_score=88, letter="待审核草稿", jd_fingerprint=jd, policy_fingerprint=options.policy_fingerprint, draft_validated=True)
    summary = run(BatchRunner(store, options), external_llm_authorised=True, explicit_execute_authorised=True, acceptance_authorised=True)
    assert summary.attempted == 0
    assert calls["send"] == 0
    assert store.application("boss-job:1")["status"] == ApplicationStatus.PENDING_REVIEW


def test_normal_execute_is_blocked_without_current_live_ready(monkeypatch, tmp_path):
    calls = install_common_mocks(monkeypatch, [snapshot(1)])
    runner = BatchRunner(StateStore(str(tmp_path / "state.sqlite3")), BatchOptions(resume_source="text", resume_path="ignored", mode="execute", scan_limit=1))
    with pytest.raises(PermissionError, match="LIVE_READY"):
        run(runner, external_llm_authorised=True, explicit_execute_authorised=True)
    assert calls["send"] == 0
