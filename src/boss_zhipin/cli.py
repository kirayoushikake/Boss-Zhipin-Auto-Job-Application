"""CLI entry point for the safe, CLI-first BOSS job assistant.

The command line is deliberately the policy boundary. In particular, the
library-level batch runner refuses both LLM use and real BOSS writes unless
the caller supplies an authorisation flag; this module obtains those flags
only from a literal, interactive ``YES`` at the current terminal.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import logging
import os
import sys
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv

from boss_zhipin.providers import LLM_PRESETS, is_llm_configured

log = logging.getLogger(__name__)

# Retained as public compatibility constants for the old GUI/import callers.
RECOMMEND_URL = "https://www.zhipin.com/web/geek/job?city=101280600&query=%E6%B8%B8%E6%88%8F%E8%BF%90%E8%90%A5"
DEFAULT_RESUME_PATH = "resume/my_cover.pdf"

__all__ = [
    "LLM_PRESETS", "is_llm_configured", "RECOMMEND_URL", "DEFAULT_RESUME_PATH",
    "ensure_llm_configured", "ensure_usr_name", "ensure_resume_path", "get_label", "run_provider",
    "build_parser", "_cli_main",
]


def ensure_llm_configured() -> None:
    """Exit with actionable configuration help when no LLM key is available."""
    if is_llm_configured():
        return
    print("❌ 未配置 LLM_API_KEY。请复制 .env.example 为 .env 并填写 LLM_* 配置。")
    print("   当前 CLI 不会打印或保存 API Key。常用兼容端点：")
    for preset in LLM_PRESETS.values():
        print(f"     • {preset['label']}: {preset['signup_url']}")
    raise SystemExit(1)


def ensure_usr_name() -> str:
    """Legacy helper retained for old desktop integration."""
    name = os.getenv("BOSS_USR_NAME", "").strip()
    if name:
        return name
    while True:
        name = input("请输入你的名字（用于招呼语署名）: ").strip()
        if name:
            return name
        print("不能为空")


def ensure_resume_path() -> str:
    """Legacy local-PDF path resolver retained for callers that import it."""
    resume_path = os.getenv("RESUME_PATH", "").strip() or DEFAULT_RESUME_PATH
    if not Path(resume_path).is_file():
        print(f"❌ 找不到简历文件：{resume_path}")
        print("   请提供 --resume-path，或配置 RESUME_PATH。")
        raise SystemExit(1)
    return resume_path


def get_label() -> str:
    """Legacy optional BOSS recommendation tag accessor."""
    return os.getenv("BOSS_LABEL", "").strip()


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, min(maximum, int(raw)))
    except ValueError:
        return default


# Kept for existing GUI/tests that imported the old helper name.
def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    return _env_int(name, default, lo, hi)


def _default_source() -> str:
    source = os.getenv("BOSS_RESUME_SOURCE", "boss").strip().lower()
    return source if source in {"boss", "pdf", "text"} else "boss"


def _add_resume_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--resume-source", choices=("boss", "pdf", "text"), default=_default_source(), help="简历来源；默认 BOSS 在线简历")
    parser.add_argument("--resume-path", default=os.getenv("RESUME_PATH", ""), help="本地 PDF/text 简历路径（本地来源必填）")


def _add_batch_arguments(parser: argparse.ArgumentParser, *, include_mode: bool = True) -> None:
    _add_resume_arguments(parser)
    parser.add_argument("--query", default=os.getenv("BOSS_QUERY", ""), help="单个职位搜索关键词；设置后会覆盖 --queries")
    parser.add_argument(
        "--queries",
        default=os.getenv("BOSS_QUERIES", ""),
        help="逗号分隔的搜索关键词；为空时根据已确认简历的求职意向建议",
    )
    parser.add_argument("--preset", choices=("game",), default=os.getenv("BOSS_JOB_PRESET", ""), help="可选搜索预设；game=游戏运营、策划、用户研究")
    parser.add_argument(
        "--prefer-company-keywords",
        default=os.getenv(
            "BOSS_PREFER_COMPANY_KEYWORDS",
            "米哈游,库洛,鹰角,叠纸,散爆,悠星,二次元,动漫,ACG,二游,开放世界",
        ),
        help="逗号分隔的软优先关键词；只调整读取顺序，不排除其他公司",
    )
    parser.add_argument("--city-code", default=os.getenv("BOSS_CITY_CODE", "101280600"), help="BOSS 城市代码")
    parser.add_argument("--scan-limit", type=int, default=_env_int("BOSS_SCAN_LIMIT", 10, 1, 100), help="每个搜索词最多读取岗位卡数（1–100）")
    parser.add_argument("--total-scan-limit", type=int, default=_env_int("BOSS_TOTAL_SCAN_LIMIT", 0, 0, 300), help="整个批次最多读取岗位卡数；0=每词上限之和")
    parser.add_argument("--batch-limit", type=int, default=_env_int("BOSS_BATCH_LIMIT", 5, 1, 20), help="单批最多真实沟通数（1–20）")
    parser.add_argument("--daily-limit", type=int, default=_env_int("BOSS_DAILY_LIMIT", 20, 1, 100), help="每日真实沟通尝试上限（含 unknown，1–100）")
    parser.add_argument("--min-score", type=int, default=_env_int("BOSS_MIN_MATCH_SCORE", 50, 0, 100), help="LLM 匹配最低分（0–100）")
    parser.add_argument("--exclude", default=os.getenv("BOSS_EXCLUDE_KEYWORDS", ""), help="逗号分隔的黑名单关键词")
    parser.add_argument("--resume-delivery", choices=("best_effort", "require_confirmed_share"), default=os.getenv("BOSS_RESUME_DELIVERY", "best_effort"), help="在线简历分享策略")
    parser.add_argument("--delay-seconds", type=float, default=2.0, help="真实沟通之间的最短等待秒数")
    parser.add_argument("--business-timezone", default=os.getenv("BOSS_BUSINESS_TIMEZONE", "Asia/Shanghai"), help="每日额度的业务时区（默认 Asia/Shanghai）")
    parser.add_argument("--state-db", default=os.getenv("BOSS_STATE_DB_PATH", "data/boss_zhipin.sqlite3"), help="SQLite 状态库路径")
    if include_mode:
        parser.add_argument("--mode", choices=("dry-run", "review", "execute"), default="dry-run", help="默认 dry-run；review 只入待审核；execute 会进行真实沟通")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boss-zhipin",
        description="BOSS 直聘在线简历驱动的批量智能求职助手（默认无平台写入）",
    )
    parser.add_argument("--version", action="version", version=_code_version())
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("doctor", help="检查 CLI 运行环境和配置（不访问 BOSS/LLM）")
    inspect = sub.add_parser("inspect-resume", help="读取并脱敏展示简历质量")
    _add_resume_arguments(inspect)
    batch = sub.add_parser("batch", help="扫描、评分并按模式处理岗位")
    _add_batch_arguments(batch)
    history = sub.add_parser("history", help="查看 SQLite 历史记录（不展示完整招呼语）")
    history.add_argument("--limit", type=int, default=20, help="显示条数（1–500）")
    history.add_argument("--state-db", default=os.getenv("BOSS_STATE_DB_PATH", "data/boss_zhipin.sqlite3"))
    acceptance = sub.add_parser("acceptance", help="运行可恢复的 A–F 验收向导")
    _add_batch_arguments(acceptance, include_mode=False)
    review = sub.add_parser("review", help="查看、批准、拒绝或执行已审核草稿")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    review_list = review_sub.add_parser("list", help="列出 pending_review 草稿（不显示完整简历）")
    review_list.add_argument("--state-db", default=os.getenv("BOSS_STATE_DB_PATH", "data/boss_zhipin.sqlite3"))
    review_list.add_argument("--limit", type=int, default=50)
    for action, help_text in (("approve", "批准指定待审核草稿"), ("reject", "拒绝指定待审核草稿")):
        item = review_sub.add_parser(action, help=help_text)
        item.add_argument("--job-key", required=True)
        item.add_argument("--state-db", default=os.getenv("BOSS_STATE_DB_PATH", "data/boss_zhipin.sqlite3"))
        if action == "reject":
            item.add_argument("--reason", default="用户拒绝草稿")
    review_execute = review_sub.add_parser("execute", help="扫描并执行一个已批准草稿；仍需 LIVE_READY 和本次 YES")
    review_execute.add_argument("--job-key", required=True)
    _add_batch_arguments(review_execute, include_mode=False)
    return parser


def _code_version() -> str:
    try:
        package_version = importlib.metadata.version("boss-zhipin-job-search")
    except importlib.metadata.PackageNotFoundError:
        package_version = "development"
    # Acceptance evidence is bound to the code that reads identities, sends
    # messages and persists state, not merely to packaging metadata. A change
    # to any of these files therefore makes prior live validation stale.
    package_dir = Path(__file__).resolve().parent
    critical_files = (
        package_dir / "batch_runner.py",
        package_dir / "state_store.py",
        package_dir / "domain.py",
        package_dir / "acceptance.py",
        package_dir / "cli.py",
        package_dir / "resume_profile.py",
        package_dir / "website_oper" / "finding_jobs.py",
        package_dir / "models" / "job_matcher.py",
        package_dir / "models" / "llm.py",
        package_dir / "audit" / "__init__.py",
    )
    digest = hashlib.sha256()
    for path in critical_files:
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(str(path).encode("utf-8"))
    return f"{package_version}+safe.{digest.hexdigest()[:12]}"


def _make_options(args: argparse.Namespace):
    from boss_zhipin.batch_runner import BatchOptions

    excludes = [item.strip() for item in args.exclude.split(",") if item.strip()]
    # ``--query`` is the documented one-search override even though argparse
    # has already filled ``--queries`` from its environment/default value.
    queries = [] if args.query.strip() else [item.strip() for item in args.queries.split(",") if item.strip()]
    preferred_company_keywords = [item.strip() for item in args.prefer_company_keywords.split(",") if item.strip()]
    return BatchOptions(
        resume_source=args.resume_source,
        resume_path=args.resume_path,
        query=args.query,
        queries=queries,
        preset=args.preset,
        prefer_company_keywords=preferred_company_keywords,
        city_code=args.city_code,
        scan_limit=args.scan_limit,
        total_scan_limit=args.total_scan_limit,
        batch_limit=args.batch_limit,
        daily_limit=args.daily_limit,
        min_score=args.min_score,
        exclude_keywords=excludes,
        mode=getattr(args, "mode", "dry-run"),
        resume_delivery=args.resume_delivery,
        delay_seconds=args.delay_seconds,
        business_timezone=args.business_timezone,
        approved_job_key=getattr(args, "job_key", ""),
    )


async def _inspect_resume(args: argparse.Namespace) -> int:
    from boss_zhipin.batch_runner import BatchOptions, load_resume_profile
    from boss_zhipin.resume_profile import redact_name

    options = BatchOptions(resume_source=args.resume_source, resume_path=args.resume_path)
    if args.resume_source == "boss":
        from boss_zhipin.website_oper import finding_jobs
        await finding_jobs.open_browser_with_options("https://www.zhipin.com/web/geek/resume", "chrome")
        if not await finding_jobs.log_in():
            print("登录未完成；未读取在线简历。")
            return 1
    profile = await load_resume_profile(options)
    quality = profile.quality
    print("\n简历检查（敏感内容已省略）")
    print(f"来源：{profile.source}")
    print(f"姓名：{redact_name(profile.display_name)}")
    print(f"求职方向：{'、'.join(profile.desired_positions) or '(未识别)'}")
    print(f"意向城市：{'、'.join(profile.desired_cities) or '(未识别)'}")
    print(f"学历摘要：{profile.education or '(未识别)'}")
    print(f"工作/项目条目：{len(profile.work_experiences)}/{len(profile.project_experiences)}")
    print(f"技能：{'、'.join(profile.skills[:12]) or '(未识别)'}")
    print(f"文本长度：{quality['text_length']}；自动匹配：{'可用' if quality['suitable_for_auto_matching'] else '不可用'}")
    if quality["missing_fields"]:
        print("缺少字段：" + "、".join(quality["missing_fields"]))
    return 0 if quality["suitable_for_auto_matching"] else 2


async def _run_batch(args: argparse.Namespace) -> int:
    from boss_zhipin.batch_runner import BatchRunner
    from boss_zhipin.cli_tools import confirm
    from boss_zhipin.state_store import StateStore

    ensure_llm_configured()
    options = _make_options(args)
    print("\n批次预览")
    print(f"模式：{options.mode}；简历：{options.resume_source}；搜索：{'、'.join(options.search_queries) or '从已确认简历求职意向生成'} / {options.city_code}")
    print(f"每个搜索词扫描上限：{options.scan_limit}；整体扫描上限：{options.total_scan_limit or '每词上限之和'}；真实沟通上限：{options.batch_limit}；每日尝试上限：{options.daily_limit}")
    print(f"公司/品类软优先：{'、'.join(options.prefer_company_keywords) or '无'}（不硬性排除其他游戏公司）")
    print("LLM 将接收本次简历内容与岗位 JD，用于关键词、匹配评分和招呼语生成。")
    if not confirm("是否授权本次外部 LLM 调用？"):
        print("已取消：没有调用 LLM，也没有访问 BOSS。")
        return 0
    execute_authorised = False
    if options.mode == "execute":
        print("\n警告：execute 会通过 BOSS 正常网页发起真实沟通，最多执行上方的批次数量；操作可能无法撤回。")
        execute_authorised = confirm("是否授权当前批次的真实 BOSS 沟通？")
        if not execute_authorised:
            print("已取消：没有进行 BOSS 平台写入。")
            return 0
    summary = await BatchRunner(StateStore(args.state_db), options).run(
        external_llm_authorised=True,
        explicit_execute_authorised=execute_authorised,
    )
    print("\n批次完成：")
    for key, value in summary.as_dict().items():
        print(f"- {key}: {value}")
    return 0


async def _run_acceptance(args: argparse.Namespace) -> int:
    from boss_zhipin.acceptance import AcceptanceWizard
    from boss_zhipin.state_store import StateStore

    options = _make_options(args)
    wizard = AcceptanceWizard(StateStore(args.state_db), options, _code_version())
    await wizard.run()
    return 0


def _run_review_command(args: argparse.Namespace) -> int:
    """Local review mutations never open BOSS or call the LLM."""
    from boss_zhipin.state_store import StateStore

    store = StateStore(args.state_db)
    if args.review_command == "list":
        rows = store.review_items(limit=args.limit)
        if not rows:
            print("暂无待审核草稿。")
            return 0
        for row in rows:
            print(f"- {row['job_key']} | {row.get('company') or '(未知公司)'} | {row.get('title') or '(未知岗位)'} | score={row.get('match_score')}\n  草稿：{str(row.get('letter') or '')[:160]}")
        return 0
    if args.review_command == "approve":
        store.approve_review(args.job_key)
        print("已批准草稿；真实执行仍需 LIVE_READY 与当前终端授权。")
        return 0
    if args.review_command == "reject":
        store.reject_review(args.job_key, reason=args.reason)
        print("已拒绝草稿；不会进入自动发送候选。")
        return 0
    raise ValueError(f"未知 review 动作：{args.review_command}")


async def run_provider(
    usr_name: str,
    label: str,
    dry_run: bool,
    resume_path: str,
    resume_image_path: str = "",
) -> None:
    """Legacy GUI/programmatic entry point.

    The historic implementation could send messages without a current terminal
    batch confirmation. That is no longer a safe API. Existing integrations
    keep a clear error instead of silently retaining the unsafe path.
    """
    del usr_name, label, dry_run, resume_path, resume_image_path
    raise RuntimeError(
        "旧 run_provider 已停用以防绕过批次授权；请使用 "
        "`python -m boss_zhipin batch --resume-source pdf --resume-path ... --mode dry-run|review|execute`。"
    )


def _configure_runtime() -> None:
    # Avoid mojibake in legacy Windows PowerShell/console combinations. The
    # operations are best-effort because test runners may replace streams.
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, OSError):
                pass
    load_dotenv()
    logging.basicConfig(
        level=os.getenv("LOGLEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _cli_main(argv: Sequence[str] | None = None) -> int:
    _configure_runtime()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        print("\n为安全起见，未带子命令不会启动浏览器、调用 LLM 或发送消息。先运行 `doctor` 或 `acceptance`。")
        return 0
    try:
        if args.command == "doctor":
            from boss_zhipin.cli_tools import doctor_report, print_doctor_report
            print_doctor_report(doctor_report())
            return 0
        if args.command == "history":
            from boss_zhipin.cli_tools import print_history
            from boss_zhipin.state_store import StateStore
            print_history(StateStore(args.state_db), args.limit)
            return 0
        if args.command == "review" and args.review_command != "execute":
            return _run_review_command(args)
        # nodriver requires one uninterrupted loop; create it only for a
        # command that can need browser work.
        import nodriver as uc
        if args.command == "inspect-resume":
            return uc.loop().run_until_complete(_inspect_resume(args))
        if args.command == "batch":
            return uc.loop().run_until_complete(_run_batch(args))
        if args.command == "review" and args.review_command == "execute":
            args.mode = "execute"
            return uc.loop().run_until_complete(_run_batch(args))
        if args.command == "acceptance":
            return uc.loop().run_until_complete(_run_acceptance(args))
    except KeyboardInterrupt:
        print("\n已由用户中断；若处于发送临界区，下次启动会将记录转为 unknown。")
        return 130
    except SystemExit:
        raise
    except (PermissionError, RuntimeError, ValueError, FileNotFoundError) as exc:
        print(f"❌ {exc}")
        return 1
    log.exception("未处理的 CLI 错误")
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli_main())
