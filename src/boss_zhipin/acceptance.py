"""Persisted A--F acceptance guide with strict, context-bound gates."""
from __future__ import annotations

from dataclasses import replace

from boss_zhipin.batch_runner import BatchOptions, BatchRunner
from boss_zhipin.cli_tools import confirm, doctor_report, print_doctor_report
from boss_zhipin.domain import ApplicationStatus
from boss_zhipin.resume_profile import BossOnlineResumeSource, redact_name
from boss_zhipin.state_store import LIVE_READY_STEPS, StateStore
from boss_zhipin.website_oper import finding_jobs

STEPS = LIVE_READY_STEPS


class AcceptanceWizard:
    def __init__(self, store: StateStore, options: BatchOptions, code_version: str) -> None:
        self.store, self.options, self.code_version = store, options, code_version
        self.account_key = ""
        self.resume_fingerprint = ""
        self.config_fingerprint = options.policy_fingerprint

    def _save(self, step: str, status: str, *, batch_id: str | None = None, **detail) -> None:
        self.store.save_acceptance_step(step=step, status=status, code_version=self.code_version,
                                        account_key=self.account_key, resume_fingerprint=self.resume_fingerprint,
                                        config_fingerprint=self.config_fingerprint, batch_id=batch_id, detail=detail)

    def _passed(self, step: str) -> bool:
        row = next((item for item in self.store.acceptance_steps() if item["step"] == step), None)
        if not row or row["status"] != "passed" or row["code_version"] != self.code_version:
            return False
        # Once B has established a real context all later progress, including
        # A backfilled by bind_acceptance_context, must match exactly.
        if self.account_key and self.resume_fingerprint:
            return (row.get("account_key") == self.account_key and row.get("resume_fingerprint") == self.resume_fingerprint
                    and row.get("config_fingerprint") == self.config_fingerprint)
        return step == "environment_check"

    def show_status(self) -> None:
        records = {row["step"]: row for row in self.store.acceptance_steps()}
        print("\n验收阶段：")
        for step, label in zip(STEPS, ("A 环境检查", "B 在线简历", "C 岗位扫描", "D 模拟投递", "E 单岗位试投", "F 小批量验证")):
            status = records.get(step, {}).get("status", "not_started")
            print(f"[{'✓' if status == 'passed' else '·'}] {label}: {status}")

    @staticmethod
    def _summary(profile) -> dict[str, object]:
        return {
            "name": redact_name(profile.display_name), "source": profile.source,
            "desired_positions": profile.desired_positions[:8], "desired_cities": profile.desired_cities[:8],
            "education": profile.education[:160], "work_items": len(profile.work_experiences),
            "project_items": len(profile.project_experiences), "skills": profile.skills[:12],
            "text_length": len(profile.raw_text), "quality": profile.quality,
        }

    async def _read_and_confirm_resume(self) -> bool:
        if not confirm("下一阶段将通过正常 BOSS 页面读取在线简历（只读，不发送消息）。是否继续 B？"):
            self._save("resume_check", "pending_user_action")
            return False
        await finding_jobs.open_browser_with_options(self.options.search_url, "chrome")
        if not await finding_jobs.log_in():
            self._save("resume_check", "blocked", reason="登录未完成")
            return False
        try:
            profile = await BossOnlineResumeSource().load()
        except Exception as exc:  # noqa: BLE001
            self._save("resume_check", "failed", error=f"{type(exc).__name__}: {exc}")
            return False
        self.account_key = self.options.account_key.strip() or await finding_jobs.current_account_key()
        if not self.account_key:
            self._save("resume_check", "failed", reason="无法从页面确认稳定账号身份")
            return False
        self.resume_fingerprint = profile.fingerprint
        summary = self._summary(profile)
        print("\n在线简历脱敏摘要：")
        for key, value in summary.items():
            if key != "quality":
                print(f"- {key}: {value}")
        if not confirm("以上脱敏摘要确为当前账号的真实在线简历，并授权将其作为本次验收绑定对象吗？"):
            self._save("resume_check", "pending_user_action", summary=summary, reason="用户未确认简历摘要")
            return False
        self.store.bind_acceptance_context(code_version=self.code_version, account_key=self.account_key,
                                           resume_fingerprint=self.resume_fingerprint,
                                           config_fingerprint=self.config_fingerprint)
        self._save("resume_check", "passed", summary=summary)
        return True

    async def run(self) -> None:
        self.show_status()
        if not self._passed("environment_check"):
            report = doctor_report(run_tests=True)
            print_doctor_report(report)
            self._save("environment_check", "passed" if report.ok else "failed", checks=report.checks)
            if not report.ok:
                return

        # B is intentionally reread for every wizard invocation.  A stored
        # fingerprint alone cannot establish that the current web account has
        # not switched resumes since the previous terminal session.
        if not await self._read_and_confirm_resume():
            return

        if not self._passed("job_discovery_check"):
            requested = min(3, self.options.scan_limit)
            if not confirm(f"将读取并核验 {requested} 个岗位卡/JD（只读，不调用 LLM、不发消息）。是否继续 C？"):
                self._save("job_discovery_check", "pending_user_action", requested=requested)
                return
            await finding_jobs.navigate_to(self.options.search_url)
            samples, failures = [], []
            for index in range(1, requested + 1):
                snapshot = await finding_jobs.get_job_snapshot_by_index(index)
                if snapshot is None:
                    failures.append({"index": index, "reason": "未读取到岗位卡或 JD"}); continue
                ok = bool(snapshot.identity.safe_for_auto_send and snapshot.jd_changed_after_click and snapshot.identity_matches_detail)
                samples.append({"index": index, "key": snapshot.identity.key, "strong": snapshot.identity.safe_for_auto_send,
                                "jd_changed": snapshot.jd_changed_after_click, "identity_matches_detail": snapshot.identity_matches_detail,
                                "jd_length": len(snapshot.description), "ok": ok})
                if not ok:
                    failures.append({"index": index, "reason": snapshot.jd_evidence})
            successful = sum(item["ok"] for item in samples)
            rate = successful / requested if requested else 0.0
            passed = successful == requested and not failures
            self._save("job_discovery_check", "passed" if passed else "failed", requested=requested, read=len(samples), successful=successful, failed=requested-successful, pass_rate=rate, samples=samples, failure_reasons=failures)
            print(f"岗位只读扫描：要求 {requested}，成功 {successful}，失败 {requested-successful}，合格率 {rate:.0%}")
            if not passed:
                return

        if not self._passed("dry_run_check"):
            if not confirm("Dry-run 会将简历/JD提交给配置的 LLM 生成评分和草稿，但不会向 BOSS 写入。是否授权 D？"):
                self._save("dry_run_check", "pending_user_action"); return
            summary = await BatchRunner(self.store, replace(self.options, mode="dry-run", scan_limit=min(10, self.options.scan_limit), batch_limit=min(5, self.options.batch_limit), account_key=self.account_key)).run(external_llm_authorised=True)
            rows = self.store.applications_for_batch(summary.batch_id)
            quality_ok = bool(rows) and all((row["status"] != ApplicationStatus.FAILED and (row["match_score"] is None or int(row["match_score"]) >= self.options.min_score)) for row in rows if row["status"] == ApplicationStatus.DRAFTED)
            passed = summary.drafted > 0 and summary.attempted == 0 and summary.page_confirmed == 0 and summary.unknown == 0 and quality_ok
            self._save("dry_run_check", "passed" if passed else "failed", batch_id=summary.batch_id, summary=summary.as_dict(), platform_writes=summary.attempted, quality_ok=quality_ok)
            if not passed:
                return

        if not self._passed("single_live_check"):
            if not confirm("即将对最多 1 个已核验目标发起真实沟通，可能不可撤回。是否授权 E？"):
                self._save("single_live_check", "pending_user_action"); return
            summary = await BatchRunner(self.store, replace(self.options, mode="execute", batch_limit=1, account_key=self.account_key)).run(external_llm_authorised=True, explicit_execute_authorised=True, acceptance_authorised=True)
            passed = summary.attempted == 1 and summary.page_confirmed == 1 and summary.target_verified == 1 and len(summary.confirmed_job_keys) == 1 and summary.failed == 0 and summary.unknown == 0
            self._save("single_live_check", "passed" if passed else "failed", batch_id=summary.batch_id, expected_attempts=1, summary=summary.as_dict(), actual_confirmed_targets=summary.confirmed_job_keys)
            if not passed:
                return

        if not self._passed("small_batch_check"):
            limit = min(3, self.options.batch_limit)
            if not confirm(f"即将对最多 {limit} 个已核验目标发起真实沟通，可能不可撤回。是否授权 F？"):
                self._save("small_batch_check", "pending_user_action"); return
            before = self.store.attempted_today(self.account_key, self.options.business_timezone)
            expected = min(limit, max(0, self.options.daily_limit - before))
            summary = await BatchRunner(self.store, replace(self.options, mode="execute", batch_limit=limit, account_key=self.account_key)).run(external_llm_authorised=True, explicit_execute_authorised=True, acceptance_authorised=True)
            passed = expected > 0 and summary.attempted == expected and summary.page_confirmed == expected and summary.target_verified == expected and summary.failed == 0 and summary.unknown == 0 and self.store.attempted_today(self.account_key, self.options.business_timezone) == before + expected
            self._save("small_batch_check", "passed" if passed else "failed", batch_id=summary.batch_id, expected_attempts=expected, expected_quota_after=before+expected, summary=summary.as_dict(), actual_quota=self.store.attempted_today(self.account_key, self.options.business_timezone))
            if not passed:
                return

        if self.store.is_live_ready(code_version=self.code_version, account_key=self.account_key, resume_fingerprint=self.resume_fingerprint, config_fingerprint=self.config_fingerprint):
            print("\nLIVE_READY：A–F 在当前代码、账号、简历和关键配置下均已通过。每个新 execute 批次仍需当前终端明确授权。")
        else:
            print("\n验收记录不完整或上下文已变化；不得显示 LIVE_READY。")
