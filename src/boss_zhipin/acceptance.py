"""Persisted, interactive acceptance guide.

The guide intentionally stops at each external side-effect boundary.  A prior
passed step never grants authority for a future real message send.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

from boss_zhipin.batch_runner import BatchOptions, BatchRunner
from boss_zhipin.cli_tools import confirm, doctor_report, print_doctor_report
from boss_zhipin.resume_profile import BossOnlineResumeSource
from boss_zhipin.state_store import StateStore
from boss_zhipin.website_oper import finding_jobs


STEPS = ("environment_check", "resume_check", "job_discovery_check", "dry_run_check", "single_live_check", "small_batch_check")


class AcceptanceWizard:
    def __init__(self, store: StateStore, options: BatchOptions, code_version: str) -> None:
        self.store = store
        self.options = options
        self.code_version = code_version

    def _save(
        self,
        step: str,
        status: str,
        *,
        resume_fingerprint: str = "",
        batch_id: str | None = None,
        **detail,
    ) -> None:
        self.store.save_acceptance_step(
            step=step,
            status=status,
            code_version=self.code_version,
            resume_fingerprint=resume_fingerprint,
            batch_id=batch_id,
            detail=detail,
        )

    def _passed(self, step: str) -> bool:
        # A source/version change that can affect extraction or sending makes
        # prior live evidence stale. The wizard then reruns its gates instead
        # of treating an old record as permanent authority.
        return any(
            row["step"] == step
            and row["status"] == "passed"
            and row["code_version"] == self.code_version
            for row in self.store.acceptance_steps()
        )

    def show_status(self) -> None:
        records = {row["step"]: row for row in self.store.acceptance_steps()}
        print("\n验收阶段：")
        for step, label in zip(STEPS, ("A 环境检查", "B 在线简历", "C 岗位扫描", "D 模拟投递", "E 单岗位试投", "F 小批量验证")):
            status = records.get(step, {}).get("status", "not_started")
            mark = "✓" if status == "passed" else "·"
            print(f"[{mark}] {label}: {status}")

    async def run(self) -> None:
        self.show_status()
        if not self._passed("environment_check"):
            report = doctor_report(run_tests=True)
            print_doctor_report(report)
            self._save("environment_check", "passed" if report.ok else "failed", checks=report.checks)
            if not report.ok:
                return

        if not self._passed("resume_check"):
            if not confirm("下一阶段将打开 Chrome，通过正常 BOSS 页面读取当前在线简历（只读，不发送消息）。是否继续 B？"):
                self._save("resume_check", "pending_user_action")
                return
            await finding_jobs.open_browser_with_options(self.options.search_url, "chrome")
            if not await finding_jobs.log_in():
                self._save("resume_check", "blocked", reason="登录未完成")
                return
            try:
                profile = await BossOnlineResumeSource().load()
            except Exception as exc:  # noqa: BLE001
                self._save("resume_check", "failed", error=f"{type(exc).__name__}: {exc}")
                print(f"在线简历验收失败：{exc}")
                return
            self._save("resume_check", "passed", resume_fingerprint=profile.fingerprint, quality=profile.quality)
            print(f"在线简历读取通过：{profile.source}，姓名 {profile.display_name[:1]}*，文本 {len(profile.raw_text)} 字符")

        if not self._passed("job_discovery_check"):
            if not confirm("将扫描最多 3 个岗位并读取公开可见 JD（只读，不调用 LLM、不发消息）。是否继续 C？"):
                self._save("job_discovery_check", "pending_user_action")
                return
            # Acceptance is resumable across processes. B may have passed in
            # an earlier invocation, in which case the old nodriver tab no
            # longer exists and C must establish a fresh normal UI session.
            if finding_jobs.get_tab() is None:
                await finding_jobs.open_browser_with_options(self.options.search_url, "chrome")
            if not await finding_jobs.log_in():
                self._save("job_discovery_check", "blocked", reason="登录未完成")
                return
            await finding_jobs.navigate_to(self.options.search_url)
            samples = []
            for index in range(1, 4):
                snapshot = await finding_jobs.get_job_snapshot_by_index(index)
                if snapshot:
                    samples.append({"key": snapshot.identity.key, "strong": snapshot.identity.safe_for_auto_send, "jd_changed": snapshot.jd_changed_after_click, "jd_length": len(snapshot.description)})
            passed = bool(samples) and all(item["strong"] and item["jd_changed"] for item in samples)
            self._save("job_discovery_check", "passed" if passed else "failed", samples=samples)
            print(f"岗位只读扫描：成功 {len(samples)}/3；" + ("身份与 JD 对应关系通过" if passed else "存在身份或 JD 刷新证据不足，禁止进入真实发送"))
            if not passed:
                return

        if not self._passed("dry_run_check"):
            if not confirm("Dry-run 会将简历/JD发送到你配置的 LLM 端点以生成评分和招呼语，但不会向 BOSS 写入。是否授权本次外部 LLM 调用并继续 D？"):
                self._save("dry_run_check", "pending_user_action")
                return
            summary = await BatchRunner(
                self.store,
                replace(self.options, mode="dry-run", scan_limit=min(10, self.options.scan_limit), batch_limit=min(5, self.options.batch_limit)),
            ).run(external_llm_authorised=True)
            passed = summary.drafted > 0 and summary.attempted == 0
            self._save("dry_run_check", "passed" if passed else "failed", batch_id=summary.batch_id, summary=summary.as_dict())
            print("Dry-run：" + str(summary.as_dict()))
            if not passed:
                return

        if not self._passed("single_live_check"):
            if not confirm("即将对最多 1 个岗位执行真实沟通；可能不可撤回，并会尝试正常 UI 支持的在线简历分享。是否授权 E？"):
                self._save("single_live_check", "pending_user_action")
                return
            summary = await BatchRunner(
                self.store,
                replace(self.options, mode="execute", batch_limit=1),
            ).run(external_llm_authorised=True, explicit_execute_authorised=True)
            passed = summary.page_confirmed == 1 and summary.unknown == 0
            self._save("single_live_check", "passed" if passed else "failed", batch_id=summary.batch_id, summary=summary.as_dict())
            print("单岗位试投：" + str(summary.as_dict()))
            if not passed:
                return

        if not self._passed("small_batch_check"):
            if not confirm("单岗位验收已通过。即将对最多 3 个岗位执行真实沟通；同批次不再逐条确认。是否授权 F？"):
                self._save("small_batch_check", "pending_user_action")
                return
            summary = await BatchRunner(
                self.store,
                replace(self.options, mode="execute", batch_limit=min(3, self.options.batch_limit)),
            ).run(external_llm_authorised=True, explicit_execute_authorised=True)
            passed = summary.page_confirmed > 0 and summary.unknown == 0 and not summary.safety_stop.startswith("出现结果未知")
            self._save("small_batch_check", "passed" if passed else "failed", batch_id=summary.batch_id, summary=summary.as_dict())
            print("小批量验收：" + str(summary.as_dict()))
            if not passed:
                return

        print("\nLIVE_READY：A–F 已通过。每个新的 execute 批次仍会要求当前终端明确授权。")
