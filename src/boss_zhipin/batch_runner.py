"""Safe, serial batch workflow for BOSS job discovery and communication.

This module owns policy.  Browser functions only describe observable page
actions; they cannot decide that a weak identity or a degraded LLM score is
safe to send.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from urllib.parse import quote_plus

from boss_zhipin.audit import validate_letter_grounding
from boss_zhipin.domain import ApplicationStatus, MatchResult, ResumeShareStatus
from boss_zhipin.models.job_matcher import extract_keywords_from_text, should_apply
from boss_zhipin.models.llm import generate_letter
from boss_zhipin.resume_profile import (
    BossOnlineResumeSource,
    ResumeProfile,
    enrich_profile_from_env,
    profile_from_local_text,
    profile_from_pdf,
)
from boss_zhipin.state_store import StateStore
from boss_zhipin.vectorization import embed_resume
from boss_zhipin.website_oper import finding_jobs


log = logging.getLogger(__name__)
DEFAULT_BATCH_LIMIT = 5
DEFAULT_DAILY_LIMIT = 20
DEFAULT_GAME_QUERIES = ("游戏运营", "游戏策划", "游戏用户研究")
# This is deliberately a ranking hint, rather than an eligibility rule.  A
# company name rarely advertises every game it publishes, so excluding a card
# merely because it has no ACG keyword would create false negatives.
DEFAULT_PREFERRED_COMPANY_KEYWORDS = (
    "米哈游", "库洛", "鹰角", "叠纸", "散爆", "悠星", "二次元", "动漫", "ACG", "二游", "开放世界",
)


def _normalise_terms(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Trim, deduplicate and preserve the user's declared search order."""
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        term = str(value).strip()
        if term and term not in seen:
            seen.add(term)
            result.append(term)
    return tuple(result)


@dataclass(frozen=True)
class BatchOptions:
    resume_source: str = "boss"
    resume_path: str = ""
    query: str = ""
    queries: tuple[str, ...] | list[str] = field(default_factory=tuple)
    prefer_company_keywords: tuple[str, ...] | list[str] = field(
        default_factory=lambda: DEFAULT_PREFERRED_COMPANY_KEYWORDS
    )
    city_code: str = "101280600"
    scan_limit: int = 10
    batch_limit: int = DEFAULT_BATCH_LIMIT
    daily_limit: int = DEFAULT_DAILY_LIMIT
    min_score: int = 50
    exclude_keywords: list[str] = field(default_factory=list)
    mode: str = "dry-run"  # dry-run | review | execute
    resume_delivery: str = "best_effort"  # best_effort | require_confirmed_share
    delay_seconds: float = 2.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "queries", _normalise_terms(self.queries))
        object.__setattr__(self, "prefer_company_keywords", _normalise_terms(self.prefer_company_keywords))
        if self.resume_source not in {"boss", "pdf", "text"}:
            raise ValueError("resume_source 必须为 boss、pdf 或 text")
        if self.mode not in {"dry-run", "review", "execute"}:
            raise ValueError("mode 必须为 dry-run、review 或 execute")
        if self.resume_delivery not in {"best_effort", "require_confirmed_share"}:
            raise ValueError("resume_delivery 配置无效")
        if not 1 <= self.scan_limit <= 100:
            raise ValueError("scan_limit 必须在 1 到 100 之间")
        if not 1 <= self.batch_limit <= 20:
            raise ValueError("batch_limit 必须在 1 到 20 之间")
        if not 1 <= self.daily_limit <= 100:
            raise ValueError("daily_limit 必须在 1 到 100 之间")
        if not 0 <= self.min_score <= 100:
            raise ValueError("min_score 必须在 0 到 100 之间")

    @property
    def search_queries(self) -> tuple[str, ...]:
        """Ordered normal-UI searches for the candidate's target roles.

        ``query`` remains a compatible, explicit single-search override.  The
        CLI supplies all three current game-job targets by default; direct
        library callers without a query get the first safe game target rather
        than an unrelated recommendation feed.
        """
        if self.query.strip():
            return (self.query.strip(),)
        if self.queries:
            return self.queries
        return (DEFAULT_GAME_QUERIES[0],)

    def search_url_for(self, query: str) -> str:
        parts = [f"city={quote_plus(self.city_code)}"]
        if query.strip():
            parts.append(f"query={quote_plus(query.strip())}")
        return "https://www.zhipin.com/web/geek/job?" + "&".join(parts)

    @property
    def search_url(self) -> str:
        """Compatibility URL for callers that perform one normal page load."""
        return self.search_url_for(self.search_queries[0])


@dataclass
class BatchSummary:
    batch_id: str
    mode: str
    scanned: int = 0
    extracted: int = 0
    duplicates: int = 0
    skipped: int = 0
    eligible: int = 0
    drafted: int = 0
    attempted: int = 0
    page_confirmed: int = 0
    resume_shared: int = 0
    unknown: int = 0
    failed: int = 0
    safety_stop: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


async def load_resume_profile(options: BatchOptions) -> ResumeProfile:
    if options.resume_source == "boss":
        profile = await BossOnlineResumeSource().load()
    elif not options.resume_path:
        raise ValueError("使用本地简历时必须提供 --resume-path")
    elif options.resume_source == "pdf":
        profile = profile_from_pdf(options.resume_path)
    else:
        profile = profile_from_local_text(options.resume_path)
    return enrich_profile_from_env(profile)


def to_match_result(apply: bool, details: dict) -> MatchResult:
    score = details.get("score")
    return MatchResult(
        score=int(score) if isinstance(score, int) else None,
        reason=str(details.get("reason") or ""),
        matched_requirements=list(details.get("matched_keywords") or []),
        uncertain_requirements=["LLM 评分不可用"] if details.get("scoring_degraded") else [],
        degraded=bool(details.get("scoring_degraded")),
        eligible_for_auto_send=bool(apply and not details.get("scoring_degraded")),
    )


class BatchRunner:
    def __init__(self, store: StateStore, options: BatchOptions) -> None:
        self.store = store
        self.options = options

    async def _scan_targets(self):
        """Yield cards from ordered searches while retaining one browser tab.

        The limit is deliberately per declared role.  All roles get a chance
        to surface candidates, but the browser only changes location between
        searches and never opens a window/tab per job.
        """
        for query_index, query in enumerate(self.options.search_queries):
            if query_index:
                await finding_jobs.navigate_to(self.options.search_url_for(query))
            indices = await finding_jobs.prioritized_job_card_indices(
                self.options.prefer_company_keywords,
                self.options.scan_limit,
            )
            for index in indices:
                yield query, index

    def _preference_detail(self, snapshot, query: str) -> dict[str, object]:
        searchable = "\n".join((snapshot.identity.company, snapshot.identity.title, snapshot.card_text)).lower()
        hits = [term for term in self.options.prefer_company_keywords if term.lower() in searchable]
        return {"query": query, "company_preference_hits": hits, "company_preference_score": len(hits)}

    async def _execute_verified_draft(
        self,
        *,
        identity,
        profile: ResumeProfile,
        account_key: str,
        batch_id: str,
        summary: BatchSummary,
        letter: str,
        match_score: int | None,
    ) -> bool:
        """Perform one already-approved page write; return True on safety stop."""
        # The durable marker is written immediately before the page write.
        # A crash after this point always recovers as UNKNOWN.
        self.store.transition(
            identity=identity, to_status=ApplicationStatus.SENDING,
            account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id,
            match_score=match_score, letter=letter,
        )
        summary.attempted += 1
        chat_ok, chat_evidence = await finding_jobs.open_and_verify_chat(identity)
        if not chat_ok:
            self.store.transition(
                identity=identity, to_status=ApplicationStatus.FAILED,
                account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id,
                error=chat_evidence,
            )
            summary.failed += 1
            summary.safety_stop = "聊天目标验证失败，已安全停止"
            return True
        try:
            visible_echo = await finding_jobs.send_chat_message(letter)
        except Exception as exc:  # after SENDING, never retry automatically
            self.store.transition(
                identity=identity, to_status=ApplicationStatus.UNKNOWN,
                account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id,
                evidence=chat_evidence,
                error=f"发送临界区异常：{type(exc).__name__}: {exc}；禁止自动重发",
            )
            summary.unknown += 1
            summary.safety_stop = "发送临界区异常，已安全停止"
            return True
        if not visible_echo:
            self.store.transition(
                identity=identity, to_status=ApplicationStatus.UNKNOWN,
                account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id,
                evidence=chat_evidence, error="回车后未观察到消息回显；禁止自动重发",
            )
            summary.unknown += 1
            summary.safety_stop = "出现结果未知的真实发送，已安全停止"
            return True
        share_status = await finding_jobs.try_share_online_resume()
        if self.options.resume_delivery == "require_confirmed_share" and share_status != ResumeShareStatus.SHARED_CONFIRMED:
            self.store.transition(
                identity=identity, to_status=ApplicationStatus.CONTACT_CONFIRMED,
                account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id,
                resume_share_status=str(share_status), evidence=chat_evidence,
                error="沟通页面已确认，但在线简历未获明确分享确认；不自动重发",
            )
            summary.page_confirmed += 1
            summary.safety_stop = "在线简历分享未确认，已安全停止"
            return True
        self.store.transition(
            identity=identity, to_status=ApplicationStatus.CONTACT_CONFIRMED,
            account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id,
            resume_share_status=str(share_status), evidence=chat_evidence,
        )
        summary.page_confirmed += 1
        if share_status == ResumeShareStatus.SHARED_CONFIRMED:
            summary.resume_shared += 1
        await asyncio.sleep(max(0.5, self.options.delay_seconds))
        await finding_jobs.navigate_back()
        return False

    async def run(
        self,
        *,
        external_llm_authorised: bool = False,
        explicit_execute_authorised: bool = False,
    ) -> BatchSummary:
        """Run one serial batch after the caller crossed both consent gates.

        ``dry-run`` is intentionally not an exemption: it does not write to
        BOSS, but it does transmit resume/JD text to the configured external
        LLM endpoint. The CLI collects a fresh explicit authorisation before
        setting this flag.
        """
        if not external_llm_authorised:
            raise PermissionError("任何批次调用外部 LLM 前都必须在当前终端明确授权")
        if self.options.mode == "execute" and not explicit_execute_authorised:
            raise PermissionError("execute 模式必须在当前终端明确授权")

        recovered = self.store.recover_interrupted_sends()
        if recovered:
            log.warning("发现 %d 条中断发送记录，已标记为 unknown，禁止自动重发", recovered)

        await finding_jobs.open_browser_with_options(self.options.search_url, "chrome")
        if not await finding_jobs.log_in():
            raise RuntimeError("登录未完成，安全停止；请在 Chrome 中完成正常登录后重试")

        profile = await load_resume_profile(self.options)
        quality = profile.quality
        if not quality["suitable_for_auto_matching"]:
            raise RuntimeError(str(quality["reason"]))
        # Online resume loading navigates to the normal resume page.  Return
        # the *same* controlled tab to the first search only when necessary;
        # local resume sources already remain on the search page.
        if self.options.resume_source == "boss":
            await finding_jobs.navigate_to(self.options.search_url)

        account_key = f"resume:{profile.fingerprint[:24]}"
        batch_id = self.store.create_batch(
            mode=self.options.mode,
            account_key=account_key,
            resume_fingerprint=profile.fingerprint,
            search_summary={
                "queries": list(self.options.search_queries),
                "city_code": self.options.city_code,
                "prefer_company_keywords": list(self.options.prefer_company_keywords),
            },
            batch_limit=self.options.batch_limit,
        )
        summary = BatchSummary(batch_id=batch_id, mode=self.options.mode)
        keywords = await asyncio.to_thread(extract_keywords_from_text, profile.raw_text)
        if not keywords:
            self.store.finish_batch(batch_id, "blocked")
            raise RuntimeError("未获得有效简历关键词，禁止自动匹配和发送；请检查 LLM 配置或使用 review")
        vectorstore = await asyncio.to_thread(embed_resume, profile.raw_text, "./vectorstores")

        try:
            async for query, index in self._scan_targets():
                if self.options.mode == "execute" and summary.attempted >= self.options.batch_limit:
                    summary.safety_stop = "已达到本批次真实执行上限"
                    break
                if self.options.mode == "execute" and self.store.confirmed_today(account_key) >= self.options.daily_limit:
                    summary.safety_stop = "已达到每日页面确认沟通上限"
                    break

                summary.scanned += 1
                snapshot = await finding_jobs.get_job_snapshot_by_index(index)
                if snapshot is None:
                    # A missing card/JD is not a reason to keep incrementing
                    # indefinitely; the configured scan limit is the outer cap.
                    summary.failed += 1
                    continue
                summary.extracted += 1
                identity = snapshot.identity
                discovery_detail = {
                    "jd_evidence": snapshot.jd_evidence,
                    **self._preference_detail(snapshot, query),
                }
                self.store.observe_job(identity)
                existing = self.store.application(identity.key)
                reusable_draft = bool(
                    self.options.mode == "execute"
                    and existing is not None
                    and existing["status"] == ApplicationStatus.DRAFTED
                    and existing.get("letter")
                )
                # A prior SKIPPED record never touched the platform. It may be
                # reconsidered when the user intentionally reruns a dry-run
                # after changing local matching policy. Every state that could
                # have written/queued a message remains an absolute dedupe.
                # The sole exception is a validated DRAFTED letter: it was
                # generated by a prior dry-run and has no BOSS side effect, so
                # execute may use it only after the current card/JD gate below.
                if existing is not None and existing["status"] != ApplicationStatus.SKIPPED and not reusable_draft:
                    summary.duplicates += 1
                    continue
                if not snapshot.jd_changed_after_click or not identity.safe_for_auto_send:
                    if reusable_draft:
                        self.store.transition(
                            identity=identity, to_status=ApplicationStatus.SKIPPED,
                            account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id,
                            error="当前岗位身份不可靠或 JD 未证明刷新，未使用历史草稿",
                        )
                    else:
                        self.store.transition(
                            identity=identity, to_status=ApplicationStatus.DISCOVERED,
                            account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id,
                            detail=discovery_detail,
                        )
                        self.store.transition(
                            identity=identity, to_status=ApplicationStatus.SKIPPED,
                            account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id,
                            error="岗位身份不可靠或 JD 未证明随当前卡片刷新",
                        )
                    summary.skipped += 1
                    continue
                if reusable_draft:
                    letter = str(existing["letter"])
                    validation = validate_letter_grounding(letter, profile.raw_text)
                    if not validation.ok:
                        self.store.transition(
                            identity=identity, to_status=ApplicationStatus.SKIPPED,
                            account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id,
                            error="历史草稿未通过当前简历事实校验：" + "; ".join(validation.reasons),
                        )
                        summary.skipped += 1
                        continue
                    match_score = existing.get("match_score")
                    summary.eligible += 1
                    summary.drafted += 1
                    print(f"\n[execute/reuse-draft] {identity.company} | {identity.title} | 分数 {match_score}\n{letter}\n")
                    if await self._execute_verified_draft(
                        identity=identity, profile=profile, account_key=account_key,
                        batch_id=batch_id, summary=summary, letter=letter, match_score=match_score,
                    ):
                        break
                    continue
                self.store.transition(
                    identity=identity, to_status=ApplicationStatus.DISCOVERED,
                    account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id,
                    detail=discovery_detail,
                )
                self.store.transition(
                    identity=identity, to_status=ApplicationStatus.EVALUATING,
                    account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id,
                )
                apply, details = await asyncio.to_thread(
                    should_apply, snapshot.description, keywords, profile.raw_text,
                    2, self.options.min_score, self.options.exclude_keywords, vectorstore,
                )
                match = to_match_result(apply, details)
                if not match.eligible_for_auto_send:
                    self.store.transition(
                        identity=identity, to_status=ApplicationStatus.SKIPPED,
                        account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id,
                        match_score=match.score, error=match.reason,
                        detail={"match": asdict(match)},
                    )
                    summary.skipped += 1
                    continue
                self.store.transition(
                    identity=identity, to_status=ApplicationStatus.ELIGIBLE,
                    account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id,
                    match_score=match.score, detail={"match": asdict(match)},
                )
                summary.eligible += 1
                letter = await asyncio.to_thread(generate_letter, profile.display_name or "求职者", vectorstore, snapshot.description)
                validation = validate_letter_grounding(letter, profile.raw_text)
                if not validation.ok:
                    self.store.transition(
                        identity=identity, to_status=ApplicationStatus.FAILED,
                        account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id,
                        match_score=match.score, letter=letter, error="; ".join(validation.reasons),
                    )
                    summary.failed += 1
                    continue
                self.store.transition(
                    identity=identity, to_status=ApplicationStatus.DRAFTED,
                    account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id,
                    match_score=match.score, letter=letter,
                )
                summary.drafted += 1
                print(f"\n[{self.options.mode}] {identity.company} | {identity.title} | 分数 {match.score}\n{letter}\n")
                if self.options.mode == "dry-run":
                    continue
                if self.options.mode == "review":
                    self.store.transition(
                        identity=identity, to_status=ApplicationStatus.PENDING_REVIEW,
                        account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id,
                        match_score=match.score, letter=letter,
                    )
                    continue

                if await self._execute_verified_draft(
                    identity=identity, profile=profile, account_key=account_key,
                    batch_id=batch_id, summary=summary, letter=letter, match_score=match.score,
                ):
                    break
        except asyncio.CancelledError:
            summary.safety_stop = "用户取消；发送临界区记录会在下次启动时转为 unknown"
            self.store.finish_batch(batch_id, "cancelled")
            raise
        except Exception:
            self.store.finish_batch(batch_id, "failed")
            raise
        else:
            self.store.finish_batch(batch_id, "safety_stopped" if summary.safety_stop else "completed")
        return summary
