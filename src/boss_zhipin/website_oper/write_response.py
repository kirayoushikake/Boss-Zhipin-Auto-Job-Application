"""单个岗位的主循环：抓 JD → 生成招呼语 → 校验 → 发送（或 dry-run）。

调用图：

.. code-block:: text

    main.py
      └─ send_job_descriptions_to_chat
           ├─ finding_jobs.open_browser_with_options / log_in
           ├─ finding_jobs.select_dropdown_option (一次)
           └─ while True:
                ├─ finding_jobs.get_job_description_by_index
                ├─ generate_letter (RAG + OpenAI 兼容端点)
                ├─ validate_letter / log_attempt
                └─ finding_jobs.{click_by_xpath,wait_for_css,send_chat_message,navigate_back}

终止条件：
- 连续 ``MAX_CONSECUTIVE_MISSES`` 次拿不到 JD（推测到列表底部）
- 任何 exception 命中外层 try
"""
from __future__ import annotations

import asyncio
import logging
import os

from boss_zhipin.audit import log_attempt, validate_letter
from boss_zhipin.gui.events import emit as _emit_progress
from boss_zhipin.models.job_matcher import should_apply
from boss_zhipin.models.llm import current_provider_label, generate_letter
from boss_zhipin.website_oper import finding_jobs

log = logging.getLogger(__name__)


async def send_response_and_go_back(response: str, resume_image_path: str = "") -> bool:
    """Legacy helper with visible-message confirmation before navigation.

    It remains for imports only; the safe CLI routes real sends through
    :class:`BatchRunner`, which records the ``sending`` state before invoking
    this kind of page action.
    """
    if not await finding_jobs.send_chat_message(response):
        return False
    if resume_image_path:
        if not await finding_jobs.send_image_message(resume_image_path):
            return False
    await asyncio.sleep(10)
    await finding_jobs.navigate_back()
    return True


async def send_job_descriptions_to_chat(
    usr_name: str,
    url: str,
    browser_type: str,
    label: str,
    vectorstore=None,
    dry_run: bool = False,
    resume_keywords: list[str] | None = None,
    resume_text: str | None = None,
    resume_image_path: str = "",
    min_keyword_match: int = 2,
    min_llm_score: int = 70,
    exclude_keywords: list[str] | None = None,
    external_llm_authorised: bool = False,
) -> None:
    """主循环（async）。

    用哪个 LLM 端点 / model 由 ``LLM_*`` 环境变量决定（见 ``llm._build_client``），
    不再按 provider 分支。``label`` 为空字符串时跳过下拉筛选，沿用 BOSS 默认推荐 feed。

    ``dry_run=True`` 时不点"立即沟通"，但 LLM 仍会调、招呼语仍会校验和写日志，
    用来调 prompt。

    整段必须包在 ``uc.loop().run_until_complete(...)`` 里一次性跑完，
    不能拆成多个 ``run_until_complete`` —— nodriver CDP 在事件循环停顿期间
    会进入半死状态，下次 evaluate 直接 hang。详见模块 docstring。
    """
    # This public legacy loop used to contain an unbounded real-send path.
    # Keep dry-run diagnostics available for explicit programmatic callers,
    # but never let it bypass the stateful batch runner for platform writes.
    if not external_llm_authorised:
        raise PermissionError("旧流程调用 LLM 前也必须由调用方显式授权")
    if not dry_run:
        raise PermissionError(
            "旧发送循环已停用，防止绕过 SQLite 去重、限额和页面确认；"
            "请使用 CLI `batch --mode execute`。"
        )

    # audit log（letters.jsonl）那一列的 provider/model 标签——从当前 LLM_* 推。
    # 真实运行时 token / 成本由 generate_letter 里的 telemetry 单独记。
    llm_model = os.getenv("LLM_MODEL", "").strip()
    provider_label = current_provider_label()

    await finding_jobs.open_browser_with_options(url, browser_type)
    _emit_progress("browser_started")
    if not await finding_jobs.log_in():
        log.error("登录未完成，旧 dry-run 流程安全停止")
        _emit_progress("login_failed")
        return
    _emit_progress("login_ok")

    job_index = 1
    iteration = 0
    consecutive_misses = 0
    # 一旦 LLM 评分走了 fail-open（缺配置 / 调用挂 / 解析不出分），第二层过滤其实在
    # 全放行。只在本轮**首次**遇到时提示一次，别每个岗位刷屏。
    degraded_warned = False
    # 推荐 feed 末尾、或者某条岗位卡 DOM 没渲染好，都会让 get_job_description
    # 返回 None。连续 N 次拿不到就当列表到底了停掉，否则 job_index 会无限涨。
    MAX_CONSECUTIVE_MISSES = 3
    await finding_jobs.select_dropdown_option(label)
    # BOSS 改版后 job cards 可能异步渲染，等它们就绪再进主循环
    for _ in range(15):
        probe = await finding_jobs._safe_evaluate(
            'JSON.stringify({total: document.querySelectorAll(".job-card-box").length})'
        )
        if probe.get("total", 0) > 0:
            log.info("检测到 %d 个岗位卡片，开始处理", probe["total"])
            break
        await asyncio.sleep(1)
    while True:
        try:
            iteration += 1
            log.info("=== 第 %d 轮: 处理 job_index=%d ===", iteration, job_index)
            job_description = await finding_jobs.get_job_description_by_index(job_index)
            if job_description:
                consecutive_misses = 0
                _emit_progress("job_found", index=job_index, jd_preview=job_description[:80])
                element = await finding_jobs.get_text_by_css(".op-btn.op-btn-chat")
                log.info("chat 按钮文字: %r", element)
                if element == "立即沟通":
                    # ====== 多层过滤：黑名单 + 关键词 + 向量 + LLM ======
                    if resume_text:
                        apply, details = await asyncio.to_thread(
                            should_apply,
                            job_description, resume_keywords, resume_text,
                            min_keyword_match, min_llm_score, exclude_keywords, vectorstore
                        )
                        # 评分降级（fail-open）首次出现时显式告警一次——否则用户只看到
                        # 一堆岗位"通过"，不知道第二层 LLM 过滤其实没在跑。
                        if details.get("scoring_degraded") and not degraded_warned:
                            degraded_warned = True
                            log.warning(
                                "⚠️ LLM 评分暂时不可用（%s），本轮所有岗位按放行处理",
                                details.get("reason", ""),
                            )
                            _emit_progress("scoring_degraded", detail=details.get("reason", ""))
                        if not apply:
                            stage = details.get("stage", "unknown")
                            if stage == "blacklist":
                                log.info(
                                    "⏭️ [跳过 #%d] 触发黑名单: %s",
                                    job_index, details["reason"],
                                )
                            elif stage == "keyword":
                                log.info(
                                    "⏭️ [跳过 #%d] 关键词匹配不足: 命中 %s - %s",
                                    job_index, details["matched_keywords"], details["reason"],
                                )
                            elif stage == "vector_search":
                                log.info(
                                    "⏭️ [跳过 #%d] 语义不匹配: %s",
                                    job_index, details["reason"],
                                )
                            else:
                                log.info(
                                    "⏭️ [跳过 #%d] LLM 评分 %s/%s: %s",
                                    job_index, details.get("score"), details.get("threshold"), details.get("reason"),
                                )
                            _emit_progress(
                                "job_skipped",
                                index=job_index,
                                reason=stage,
                                detail=details.get("reason", ""),
                                score=details.get("score"),
                                threshold=details.get("threshold"),
                                matched=details.get("matched_keywords"),
                            )
                            job_index += 1
                            await asyncio.sleep(3)
                            continue
                        log.info("✅ [匹配 #%d] 关键词命中: %s", job_index, details["matched_keywords"])
                        if "score" in details:
                            log.info("   LLM 评分: %s/100 - %s", details["score"], details["reason"])
                    # ====== 过滤结束 ======

                    # LLM 评分（resume_text 为空时没跑过滤 → None），带进 letter_sent 事件，
                    # 让 GUI 进度面板能看到"这条招呼语对应的岗位匹配多少分"。
                    match_score = details.get("score") if resume_text else None

                    # LLM 调用是同步阻塞的 HTTP 请求，扔到 thread pool 跑
                    # 避免阻塞事件循环 → 卡死 nodriver CDP heartbeat
                    response = await asyncio.to_thread(
                        generate_letter,
                        usr_name, vectorstore, job_description,
                    )

                    validation = validate_letter(response)

                    if not validation.ok:
                        log.warning(
                            "[BLOCKED] %s — preview: %r",
                            validation.reasons, response[:80],
                        )
                        log_attempt(
                            provider=provider_label, model=llm_model,
                            job_description=job_description, letter=response,
                            validation=validation, dry_run=dry_run, sent=False,
                        )
                        _emit_progress("letter_sent", index=job_index, status="blocked",
                                       score=match_score, letter_len=len(response))
                    elif dry_run:
                        log.info(
                            "[DRY-RUN] 招呼语 (%d 字符) 不发送。--- letter ---\n%s\n--------------",
                            len(response), response,
                        )
                        log_attempt(
                            provider=provider_label, model=llm_model,
                            job_description=job_description, letter=response,
                            validation=validation, dry_run=True, sent=False,
                        )
                        _emit_progress("letter_sent", index=job_index, status="dry_run",
                                       score=match_score, letter_len=len(response))
                    else:
                        log.info("发送招呼语：%s", response)
                        await asyncio.sleep(1)
                        # 旧版绝对 xpath 在 2026-05 BOSS 改版后失效，
                        # 改成 class 匹配，跟 get_text_by_css(".op-btn.op-btn-chat") 配套。
                        contact_xpath = "//a[contains(@class, 'op-btn-chat')]"
                        await finding_jobs.click_by_xpath(contact_xpath, timeout=10)
                        # BOSS 点击"立即沟通"后可能弹出确认层，需要再点"继续沟通"才进聊天页
                        try:
                            continue_btn = await finding_jobs._tab.find("继续沟通", best_match=True, timeout=5)
                            if continue_btn:
                                await continue_btn.click()
                                log.info("已点击「继续沟通」")
                                await asyncio.sleep(2)
                        except Exception:
                            pass
                        await finding_jobs.wait_for_css("#chat-input", timeout=50)
                        await send_response_and_go_back(response, resume_image_path)
                        log_attempt(
                            provider=provider_label, model=llm_model,
                            job_description=job_description, letter=response,
                            validation=validation, dry_run=False, sent=True,
                        )
                        _emit_progress("letter_sent", index=job_index, status="sent",
                                       score=match_score, letter_len=len(response))
            else:
                consecutive_misses += 1
                log.info(
                    "job_index=%d 拿不到 JD（连续第 %d 次）",
                    job_index, consecutive_misses,
                )

                # 在尝试滚动加载之前，先检查 CDP 是否还活着。
                # 如果 _js_wait_text 的超时已经把 CDP 连接弄死了，
                # 滚动也必然全超时，不如直接报错退出。
                if not await finding_jobs._check_cdp_health(timeout=3.0):
                    log.error(
                        "CDP 连接已死（job_index=%d），无法继续，退出主循环",
                        job_index,
                    )
                    _emit_progress(
                        "error",
                        stage=f"job_index={job_index}",
                        message="CDP connection dead after JD extraction timeout",
                    )
                    break

                # 尝试滚动加载更多卡片（最多 5 次），任一次成功就重试当前 index
                _cdp_died = False
                for i in range(5):
                    old_total = await finding_jobs.scroll_job_list()
                    if old_total > 0:
                        log.info("滚动第 %d 次成功: %d -> 更多卡片", i + 1, old_total)
                        consecutive_misses = 0
                        # 新卡片追加在旧列表末尾，重置 job_index 到第一批新卡
                        job_index = old_total + 1
                        break
                    # scroll_job_list 返回 0：可能到底了，也可能 CDP 死了
                    if not await finding_jobs._check_cdp_health(timeout=3.0):
                        log.error(
                            "滚动失败 + CDP 健康检查失败，连接已死，退出主循环"
                        )
                        _emit_progress(
                            "error",
                            stage=f"job_index={job_index}",
                            message="CDP connection dead during scroll",
                        )
                        _cdp_died = True
                        break
                    await asyncio.sleep(2)

                if _cdp_died:
                    break

                if consecutive_misses == 0:
                    continue  # 新卡片已加载，从新卡位置重试

                # 连续失败达到阈值：最后一搏滚 5 次再放弃
                if consecutive_misses >= MAX_CONSECUTIVE_MISSES:
                    log.info("即将结束，最后一搏：滚动 5 次...")
                    for i in range(5):
                        old_total = await finding_jobs.scroll_job_list()
                        if old_total > 0:
                            log.info("最后一搏第 %d 次成功！", i + 1)
                            consecutive_misses = 0
                            job_index = old_total + 1
                            break
                        await asyncio.sleep(2)

                    if consecutive_misses == 0:
                        continue  # 最后一搏成功，重试当前 job_index

                    log.info("连续 %d 个岗位拿不到，已到底，结束", MAX_CONSECUTIVE_MISSES)
                    _emit_progress("feed_exhausted", total=job_index - 1)
                    break

            job_index += 1

        except Exception as e:
            log.exception("主循环抛异常: %s", e)
            _emit_progress(
                "error",
                stage=f"job_index={job_index}",
                message=f"{type(e).__name__}: {e}",
            )
            break
