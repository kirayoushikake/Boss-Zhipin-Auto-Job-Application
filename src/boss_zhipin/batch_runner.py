"""Serial, evidence-first BOSS batch workflow.

The module is intentionally conservative: an effect is never retried after a
send boundary, historical drafts are valid only in their original context,
and normal execute mode cannot bypass the persisted A--F acceptance gate.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from uuid import uuid4
from urllib.parse import quote_plus

from boss_zhipin.audit import validate_letter_grounding
from boss_zhipin.domain import ApplicationStatus, ChatOpenResult, MatchResult, MessageConfirmation, ResumeShareStatus
from boss_zhipin.models.job_matcher import extract_keywords_from_text, should_apply
from boss_zhipin.models.llm import generate_letter
from boss_zhipin.resume_profile import BossOnlineResumeSource, ResumeProfile, enrich_profile_from_env, profile_from_local_text, profile_from_pdf
from boss_zhipin.state_store import StateStore
from boss_zhipin.vectorization import embed_resume
from boss_zhipin.website_oper import finding_jobs

log = logging.getLogger(__name__)
DEFAULT_BATCH_LIMIT = 5
DEFAULT_DAILY_LIMIT = 20
DEFAULT_GAME_QUERIES = ("游戏运营", "游戏策划", "游戏用户研究")
DEFAULT_PREFERRED_COMPANY_KEYWORDS = ("米哈游", "库洛", "鹰角", "叠纸", "散爆", "悠星", "二次元", "动漫", "ACG", "二游", "开放世界")


def _normalise_terms(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    result: list[str] = []
    for raw in values:
        value = str(raw).strip()
        if value and value not in result:
            result.append(value)
    return tuple(result)


def stable_fingerprint(text: str) -> str:
    normalised = " ".join((text or "").lower().split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BatchOptions:
    resume_source: str = "boss"
    resume_path: str = ""
    query: str = ""
    queries: tuple[str, ...] | list[str] = field(default_factory=tuple)
    preset: str = ""  # Explicit optional preset, never a hidden default.
    prefer_company_keywords: tuple[str, ...] | list[str] = field(default_factory=lambda: DEFAULT_PREFERRED_COMPANY_KEYWORDS)
    city_code: str = "101280600"
    scan_limit: int = 10  # Per search term.
    total_scan_limit: int = 0  # 0 means scan_limit * resolved query count.
    batch_limit: int = DEFAULT_BATCH_LIMIT
    daily_limit: int = DEFAULT_DAILY_LIMIT
    min_score: int = 50
    exclude_keywords: list[str] = field(default_factory=list)
    mode: str = "dry-run"
    resume_delivery: str = "best_effort"
    delay_seconds: float = 2.0
    account_key: str = ""  # Explicit test/local override; never a resume hash.
    business_timezone: str = "Asia/Shanghai"
    approved_job_key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "queries", _normalise_terms(self.queries))
        object.__setattr__(self, "prefer_company_keywords", _normalise_terms(self.prefer_company_keywords))
        if self.resume_source not in {"boss", "pdf", "text"}:
            raise ValueError("resume_source 必须为 boss、pdf 或 text")
        if self.mode not in {"dry-run", "review", "execute"}:
            raise ValueError("mode 必须为 dry-run、review 或 execute")
        if self.preset not in {"", "game"}:
            raise ValueError("preset 仅支持 game")
        if self.resume_delivery not in {"best_effort", "require_confirmed_share"}:
            raise ValueError("resume_delivery 配置无效")
        if not 1 <= self.scan_limit <= 100 or not 0 <= self.total_scan_limit <= 300:
            raise ValueError("scan-limit / total-scan-limit 超出安全范围")
        if not 1 <= self.batch_limit <= 20 or not 1 <= self.daily_limit <= 100:
            raise ValueError("batch-limit / daily-limit 超出安全范围")
        if not 0 <= self.min_score <= 100:
            raise ValueError("min_score 必须在 0 到 100 之间")

    @property
    def search_queries(self) -> tuple[str, ...]:
        if self.query.strip():
            return (self.query.strip(),)
        if self.queries:
            return self.queries
        return DEFAULT_GAME_QUERIES if self.preset == "game" else ()

    def search_url_for(self, query: str) -> str:
        parts = [f"city={quote_plus(self.city_code)}"]
        if query.strip():
            parts.append(f"query={quote_plus(query.strip())}")
        return "https://www.zhipin.com/web/geek/job?" + "&".join(parts)

    @property
    def search_url(self) -> str:
        return self.search_url_for(self.search_queries[0] if self.search_queries else "")

    @property
    def policy_fingerprint(self) -> str:
        payload = {
            "policy_version": 3, "min_score": self.min_score,
            "exclude_keywords": sorted(term.strip().lower() for term in self.exclude_keywords if term.strip()),
            "resume_delivery": self.resume_delivery, "llm_model": os.getenv("LLM_MODEL", ""),
            "llm_base_url": os.getenv("LLM_BASE_URL", ""),
            "prefer_company_keywords": list(self.prefer_company_keywords),
        }
        return stable_fingerprint(json.dumps(payload, ensure_ascii=False, sort_keys=True))


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
    target_verified: int = 0
    confirmed_job_keys: list[str] = field(default_factory=list)
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
    return MatchResult(score=int(score) if isinstance(score, int) else None, reason=str(details.get("reason") or ""), matched_requirements=list(details.get("matched_keywords") or []), uncertain_requirements=["LLM 评分不可用"] if details.get("scoring_degraded") else [], degraded=bool(details.get("scoring_degraded")), eligible_for_auto_send=bool(apply and not details.get("scoring_degraded")))


def execution_code_version() -> str:
    # Keep the version source identical to the CLI acceptance wizard without a
    # top-level import cycle.
    from boss_zhipin.cli import _code_version
    return _code_version()


class BatchRunner:
    def __init__(self, store: StateStore, options: BatchOptions) -> None:
        self.store, self.options = store, options

    def _resolved_queries(self, profile: ResumeProfile) -> tuple[str, ...]:
        explicit = self.options.search_queries
        if explicit:
            return explicit
        suggestions = _normalise_terms(profile.desired_positions)
        if suggestions:
            return suggestions
        raise ValueError("未配置搜索关键词，且在线简历未识别到求职意向；请设置 --query/--queries 或 --preset game")

    async def _scan_targets(self, queries: tuple[str, ...] | None = None):
        """Scan with bounded per-query and whole-batch limits in one tab.

        When normal infinite scrolling has more cards than the visible list,
        it is used to fetch the next page before giving up.  No tab/window is
        created per card or per query.
        """
        queries = queries or self.options.search_queries
        total_limit = self.options.total_scan_limit or self.options.scan_limit * len(queries)
        yielded = 0
        for query_index, query in enumerate(queries):
            if query_index:
                await finding_jobs.navigate_to(self.options.search_url_for(query))
            seen: set[int] = set()
            while len(seen) < self.options.scan_limit and yielded < total_limit:
                indices = await finding_jobs.prioritized_job_card_indices(self.options.prefer_company_keywords, self.options.scan_limit)
                fresh = [index for index in indices if index not in seen]
                for index in fresh:
                    if len(seen) >= self.options.scan_limit or yielded >= total_limit:
                        break
                    seen.add(index); yielded += 1
                    yield query, index
                if len(seen) >= self.options.scan_limit or yielded >= total_limit:
                    break
                previous_count = await finding_jobs.scroll_job_list()
                if not previous_count:
                    break

    def _preference_detail(self, snapshot, query: str) -> dict[str, object]:
        text = "\n".join((snapshot.identity.company, snapshot.identity.title, snapshot.card_text)).lower()
        hits = [term for term in self.options.prefer_company_keywords if term.lower() in text]
        return {"query": query, "company_preference_hits": hits, "company_preference_score": len(hits)}

    @staticmethod
    def _chat_open(value) -> ChatOpenResult:
        if isinstance(value, ChatOpenResult):
            return value
        if isinstance(value, tuple) and len(value) >= 2:
            # Legacy adapters cannot tell if their click had a side effect.
            return ChatOpenResult(bool(value[0]), not bool(value[0]), str(value[1]))
        return ChatOpenResult(False, True, "聊天打开结果格式无效")

    @staticmethod
    def _message_confirmation(value) -> MessageConfirmation:
        if isinstance(value, MessageConfirmation):
            return value
        # Compatibility for test doubles only.  The production adapter always
        # returns MessageConfirmation with independent-node evidence.
        return MessageConfirmation(bool(value), "legacy test-double confirmation" if value else "", "未获消息确认" if not value else "")

    async def _execute_verified_draft(self, *, identity, profile: ResumeProfile, account_key: str, batch_id: str, owner_token: str, summary: BatchSummary, letter: str, match_score: int | None, jd_fingerprint: str, policy_fingerprint: str) -> bool:
        """Perform one write; any uncertain state is durable and stops the run."""
        self.store.begin_send(identity=identity, account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id, owner_token=owner_token, match_score=match_score, letter=letter, jd_fingerprint=jd_fingerprint, policy_fingerprint=policy_fingerprint)
        summary.attempted += 1
        self.store.heartbeat_batch(batch_id=batch_id, account_key=account_key, owner_token=owner_token)
        opened = self._chat_open(await finding_jobs.open_and_verify_chat(identity))
        if not opened.verified:
            target = ApplicationStatus.UNKNOWN if opened.may_have_side_effect else ApplicationStatus.FAILED
            self.store.transition(identity=identity, to_status=target, account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id, jd_fingerprint=jd_fingerprint, policy_fingerprint=policy_fingerprint, evidence=opened.evidence, error=("已可能触发立即沟通但聊天验证失败；禁止自动重发" if opened.may_have_side_effect else opened.evidence))
            if target == ApplicationStatus.UNKNOWN:
                summary.unknown += 1; summary.safety_stop = "聊天验证结果未知，已安全停止"
            else:
                summary.failed += 1; summary.safety_stop = "聊天目标未打开，已安全停止"
            return True
        summary.target_verified += 1
        try:
            try:
                sent_raw = await finding_jobs.send_chat_message(letter, identity)
            except TypeError:  # narrowly retain old test-double call shape
                sent_raw = await finding_jobs.send_chat_message(letter)
            sent = self._message_confirmation(sent_raw)
        except Exception as exc:
            sent = MessageConfirmation(False, error=f"发送临界区异常：{type(exc).__name__}: {exc}")
        if not sent.confirmed:
            self.store.transition(identity=identity, to_status=ApplicationStatus.UNKNOWN, account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id, jd_fingerprint=jd_fingerprint, policy_fingerprint=policy_fingerprint, evidence=opened.evidence, error=(sent.error or "无独立消息确认"))
            summary.unknown += 1; summary.safety_stop = "出现结果未知的真实发送，已安全停止"
            return True
        share_status = await finding_jobs.try_share_online_resume()
        share_text = str(share_status)
        error = ""
        if self.options.resume_delivery == "require_confirmed_share" and share_status != ResumeShareStatus.SHARED_CONFIRMED:
            error = "沟通已确认，但在线简历没有独立新增节点确认；不自动重发"
        self.store.transition(identity=identity, to_status=ApplicationStatus.CONTACT_CONFIRMED, account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id, jd_fingerprint=jd_fingerprint, policy_fingerprint=policy_fingerprint, resume_share_status=share_text, evidence=opened.evidence + "; " + sent.evidence, error=error)
        summary.page_confirmed += 1; summary.confirmed_job_keys.append(identity.key)
        if share_status == ResumeShareStatus.SHARED_CONFIRMED:
            summary.resume_shared += 1
        if error:
            summary.safety_stop = "在线简历分享未确认，已安全停止"
            return True
        await asyncio.sleep(max(0.5, self.options.delay_seconds))
        await finding_jobs.navigate_back()
        return False

    async def run(self, *, external_llm_authorised: bool = False, explicit_execute_authorised: bool = False, acceptance_authorised: bool = False) -> BatchSummary:
        if not external_llm_authorised:
            raise PermissionError("任何批次调用外部 LLM 前都必须在当前终端明确授权")
        if self.options.mode == "execute" and not explicit_execute_authorised:
            raise PermissionError("execute 模式必须在当前终端明确授权")

        await finding_jobs.open_browser_with_options(self.options.search_url, "chrome")
        if not await finding_jobs.log_in():
            raise RuntimeError("登录未完成，安全停止；请在 Chrome 中完成正常登录后重试")
        profile = await load_resume_profile(self.options)
        if not profile.quality["suitable_for_auto_matching"]:
            raise RuntimeError(str(profile.quality["reason"]))
        # BossOnlineResumeSource leaves the shared tab on the profile page,
        # which is the most reliable normal-UI location for its stable visible
        # account link.  Capture it before navigating back to search.
        account_key = self.options.account_key.strip() or await finding_jobs.current_account_key()
        queries = self._resolved_queries(profile)
        if self.options.resume_source == "boss":
            await finding_jobs.navigate_to(self.options.search_url_for(queries[0]))
        if not account_key:
            if self.options.mode == "execute":
                raise RuntimeError("无法从页面确认稳定账号身份；禁止真实发送")
            account_key = "unverified:" + profile.fingerprint[:24]
        policy = self.options.policy_fingerprint
        code_version = execution_code_version()
        if self.options.mode == "execute" and not acceptance_authorised and not self.store.is_live_ready(code_version=code_version, account_key=account_key, resume_fingerprint=profile.fingerprint, config_fingerprint=policy):
            raise PermissionError("execute 被拒绝：当前账号、简历、代码或关键配置尚未完成 A–F LIVE_READY 验收")

        owner_token = uuid4().hex if self.options.mode == "execute" else ""
        self.store.recover_interrupted_sends()
        batch_id = self.store.create_batch(mode=self.options.mode, account_key=account_key, resume_fingerprint=profile.fingerprint, policy_fingerprint=policy, business_timezone=self.options.business_timezone, owner_token=owner_token, acquire_execution_lock=self.options.mode == "execute", search_summary={"queries": list(queries), "city_code": self.options.city_code, "scan_limit_per_query": self.options.scan_limit, "total_scan_limit": self.options.total_scan_limit or self.options.scan_limit * len(queries), "prefer_company_keywords": list(self.options.prefer_company_keywords)}, batch_limit=self.options.batch_limit)
        summary = BatchSummary(batch_id=batch_id, mode=self.options.mode)
        try:
            keywords = await asyncio.to_thread(extract_keywords_from_text, profile.raw_text)
            if not keywords:
                raise RuntimeError("未获得有效简历关键词，禁止自动匹配和发送；请检查 LLM 配置或使用 review")
            vectorstore = await asyncio.to_thread(embed_resume, profile.raw_text, "./vectorstores")
            async for query, index in self._scan_targets(queries):
                if self.options.mode == "execute":
                    if summary.attempted >= self.options.batch_limit:
                        summary.safety_stop = "已达到本批次真实执行上限"; break
                    if self.store.attempted_today(account_key, self.options.business_timezone) >= self.options.daily_limit:
                        summary.safety_stop = "已达到每日真实沟通尝试上限（含 unknown）"; break
                    if not self.store.heartbeat_batch(batch_id=batch_id, account_key=account_key, owner_token=owner_token):
                        raise RuntimeError("真实批次执行锁已失效")
                summary.scanned += 1
                snapshot = await finding_jobs.get_job_snapshot_by_index(index)
                if snapshot is None:
                    summary.failed += 1; continue
                summary.extracted += 1
                identity, jd_fingerprint = snapshot.identity, stable_fingerprint(snapshot.description)
                if self.options.approved_job_key and identity.key != self.options.approved_job_key:
                    continue
                context = dict(account_key=account_key, resume_fingerprint=profile.fingerprint, batch_id=batch_id, jd_fingerprint=jd_fingerprint, policy_fingerprint=policy)
                detail = {"jd_evidence": snapshot.jd_evidence, "identity_matches_detail": snapshot.identity_matches_detail, **self._preference_detail(snapshot, query)}
                existing = self.store.application(identity.key)
                reusable = self.options.mode == "execute" and self.store.draft_is_reusable(existing, account_key=account_key, resume_fingerprint=profile.fingerprint, jd_fingerprint=jd_fingerprint, policy_fingerprint=policy)
                if existing and existing["status"] == ApplicationStatus.PENDING_REVIEW:
                    summary.duplicates += 1; continue
                if existing and existing["status"] not in {ApplicationStatus.SKIPPED, ApplicationStatus.DRAFTED, ApplicationStatus.REVIEW_APPROVED}:
                    summary.duplicates += 1; continue
                if existing and existing["status"] in {ApplicationStatus.DRAFTED, ApplicationStatus.REVIEW_APPROVED} and not reusable:
                    # Context changes invalidate a historical draft.  Preserve
                    # its audit trail, then do a fresh current-card evaluation.
                    self.store.transition(identity=identity, to_status=ApplicationStatus.SKIPPED, error="历史草稿上下文失效；禁止复用，重新评估", **context)
                    self.store.transition(identity=identity, to_status=ApplicationStatus.DISCOVERED, detail=detail, **context)
                    existing = None
                if not identity.safe_for_auto_send or not snapshot.jd_changed_after_click or not snapshot.identity_matches_detail:
                    if existing and existing["status"] in {ApplicationStatus.DRAFTED, ApplicationStatus.REVIEW_APPROVED}:
                        self.store.transition(identity=identity, to_status=ApplicationStatus.SKIPPED, error="当前岗位身份或 JD 对应关系不可靠，未使用草稿", **context)
                    else:
                        self.store.transition(identity=identity, to_status=ApplicationStatus.DISCOVERED, detail=detail, **context)
                        self.store.transition(identity=identity, to_status=ApplicationStatus.SKIPPED, error="岗位身份不可靠或 JD 未证明对应当前卡片", **context)
                    summary.skipped += 1; continue
                if reusable:
                    letter = str(existing["letter"])
                    validation = validate_letter_grounding(letter, profile.raw_text)
                    if not validation.ok or (existing.get("match_score") is not None and int(existing["match_score"]) < self.options.min_score):
                        self.store.transition(identity=identity, to_status=ApplicationStatus.SKIPPED, error="历史草稿未通过当前真实性/阈值校验", **context)
                        summary.skipped += 1; continue
                    summary.eligible += 1; summary.drafted += 1
                    if await self._execute_verified_draft(identity=identity, profile=profile, account_key=account_key, batch_id=batch_id, owner_token=owner_token, summary=summary, letter=letter, match_score=existing.get("match_score"), jd_fingerprint=jd_fingerprint, policy_fingerprint=policy):
                        break
                    continue
                self.store.transition(identity=identity, to_status=ApplicationStatus.DISCOVERED, detail=detail, **context)
                self.store.transition(identity=identity, to_status=ApplicationStatus.EVALUATING, **context)
                apply, raw = await asyncio.to_thread(should_apply, snapshot.description, keywords, profile.raw_text, 2, self.options.min_score, self.options.exclude_keywords, vectorstore)
                match = to_match_result(apply, raw)
                if not match.eligible_for_auto_send:
                    self.store.transition(identity=identity, to_status=ApplicationStatus.SKIPPED, match_score=match.score, error=match.reason, detail={"match": asdict(match)}, **context)
                    summary.skipped += 1; continue
                self.store.transition(identity=identity, to_status=ApplicationStatus.ELIGIBLE, match_score=match.score, detail={"match": asdict(match)}, **context)
                summary.eligible += 1
                letter = await asyncio.to_thread(generate_letter, profile.display_name or "求职者", vectorstore, snapshot.description)
                validation = validate_letter_grounding(letter, profile.raw_text)
                if not validation.ok:
                    self.store.transition(identity=identity, to_status=ApplicationStatus.FAILED, match_score=match.score, letter=letter, error="; ".join(validation.reasons), **context)
                    summary.failed += 1; continue
                self.store.transition(identity=identity, to_status=ApplicationStatus.DRAFTED, match_score=match.score, letter=letter, draft_validated=True, **context)
                summary.drafted += 1
                if self.options.mode == "dry-run":
                    continue
                if self.options.mode == "review":
                    self.store.transition(identity=identity, to_status=ApplicationStatus.PENDING_REVIEW, match_score=match.score, letter=letter, draft_validated=True, **context)
                    continue
                if await self._execute_verified_draft(identity=identity, profile=profile, account_key=account_key, batch_id=batch_id, owner_token=owner_token, summary=summary, letter=letter, match_score=match.score, jd_fingerprint=jd_fingerprint, policy_fingerprint=policy):
                    break
        except asyncio.CancelledError:
            summary.safety_stop = "用户取消；发送临界区记录将按失效租约转为 unknown"
            self.store.finish_batch(batch_id, "cancelled", owner_token=owner_token)
            raise
        except Exception:
            self.store.finish_batch(batch_id, "failed", owner_token=owner_token)
            raise
        else:
            self.store.finish_batch(batch_id, "safety_stopped" if summary.safety_stop else "completed", owner_token=owner_token)
        return summary
