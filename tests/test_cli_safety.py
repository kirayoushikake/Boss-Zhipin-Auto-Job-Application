from __future__ import annotations

from boss_zhipin import cli
from boss_zhipin.state_store import LIVE_READY_STEPS, StateStore


def test_default_queries_are_derived_not_secretly_game_defaults(monkeypatch):
    monkeypatch.delenv("BOSS_QUERIES", raising=False)
    monkeypatch.delenv("BOSS_JOB_PRESET", raising=False)
    args = cli.build_parser().parse_args(["batch", "--resume-source", "text", "--resume-path", r"E:\简历\候选人.txt"])
    options = cli._make_options(args)
    assert options.search_queries == ()
    assert options.preset == ""
    game = cli.build_parser().parse_args(["batch", "--resume-source", "text", "--resume-path", r"E:\简历\候选人.txt", "--preset", "game"])
    assert cli._make_options(game).search_queries == ("游戏运营", "游戏策划", "游戏用户研究")


def test_windows_path_and_review_execute_parser_round_trip():
    parser = cli.build_parser()
    args = parser.parse_args(["review", "execute", "--job-key", "boss-job:123", "--resume-source", "text", "--resume-path", r"E:\a办公\简历\我的简历.txt", "--query", "游戏运营"])
    assert args.job_key == "boss-job:123"
    assert cli._make_options(args).resume_path == r"E:\a办公\简历\我的简历.txt"


def test_live_ready_requires_exact_account_resume_and_config(tmp_path):
    store = StateStore(str(tmp_path / "state.sqlite3"))
    for step in LIVE_READY_STEPS:
        store.save_acceptance_step(step=step, status="passed", code_version="code", account_key="account", resume_fingerprint="resume", config_fingerprint="policy")
    assert store.is_live_ready(code_version="code", account_key="account", resume_fingerprint="resume", config_fingerprint="policy")
    assert not store.is_live_ready(code_version="code", account_key="account", resume_fingerprint="changed", config_fingerprint="policy")
    assert not store.is_live_ready(code_version="code", account_key="account", resume_fingerprint="resume", config_fingerprint="changed")


def test_new_resume_binding_never_relabels_old_live_evidence(tmp_path):
    store = StateStore(str(tmp_path / "state.sqlite3"))
    store.save_acceptance_step(step="environment_check", status="passed", code_version="code")
    store.save_acceptance_step(step="job_discovery_check", status="passed", code_version="code", account_key="old-account", resume_fingerprint="old-resume", config_fingerprint="old-policy")
    store.bind_acceptance_context(code_version="code", account_key="new-account", resume_fingerprint="new-resume", config_fingerprint="new-policy")
    rows = {row["step"]: row for row in store.acceptance_steps()}
    assert rows["environment_check"]["resume_fingerprint"] == "new-resume"
    assert rows["job_discovery_check"]["resume_fingerprint"] == "old-resume"
