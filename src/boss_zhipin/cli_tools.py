"""Non-browser CLI utilities: doctor, history and safe text rendering."""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from boss_zhipin.state_store import DEFAULT_DB_PATH, StateStore


@dataclass(frozen=True)
class DoctorReport:
    ok: bool
    checks: list[dict[str, Any]]


def _chrome_path() -> str:
    candidates = [
        shutil.which("chrome"),
        shutil.which("chrome.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path(os.getenv("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe"),
    ]
    return next((str(path) for path in candidates if path and Path(path).is_file()), "")


def _test_suite_check() -> tuple[bool, str]:
    """Run isolated unit tests for acceptance stage A, never browser/LLM I/O."""
    project_root = Path(__file__).resolve().parents[2]
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=short"],
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"未能完成：{type(exc).__name__}: {exc}"
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    summary = next((line for line in reversed(lines) if "passed" in line or "failed" in line), "未返回 pytest 摘要")
    return completed.returncode == 0, summary


def doctor_report(*, run_tests: bool = False) -> DoctorReport:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    py_ok = sys.version_info >= (3, 11)
    add("Python", py_ok, f"{sys.version.split()[0]}（需要 3.11+）")
    for package in ("nodriver", "openai", "chromadb", "pypdf", "dotenv"):
        add(f"依赖 {package}", importlib.util.find_spec(package) is not None, "已可导入" if importlib.util.find_spec(package) else "缺失；运行 uv sync --no-group tauri")
    chrome = _chrome_path()
    add("Chrome", bool(chrome), chrome or "未找到 Chrome")
    api_key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    add("LLM API Key", bool(api_key), "已配置（已隐藏）" if api_key else "未配置")
    add("LLM 模型", bool(model), model if model else "未配置")
    try:
        StateStore(DEFAULT_DB_PATH)
        add("SQLite 状态库", True, str(Path(DEFAULT_DB_PATH).resolve()))
    except Exception as exc:  # noqa: BLE001
        add("SQLite 状态库", False, f"{type(exc).__name__}: {exc}")
    desktop_sources = all(Path(path).exists() for path in ("src-tauri", "tauri-ui", "src/boss_zhipin/tauri/frontend"))
    add("桌面端源码", desktop_sources, "完整" if desktop_sources else "缺失；本版本仅保证 CLI")
    if run_tests:
        tests_ok, tests_detail = _test_suite_check()
        add("核心自动化测试", tests_ok, tests_detail)
    return DoctorReport(ok=all(item["ok"] for item in checks if item["name"] != "桌面端源码"), checks=checks)


def print_doctor_report(report: DoctorReport) -> None:
    print("\nBOSS 直聘智能求职助手 · 环境检查")
    for item in report.checks:
        mark = "✓" if item["ok"] else "✗"
        print(f"[{mark}] {item['name']}: {item['detail']}")
    print("\n结果：" + ("CLI 基线可用" if report.ok else "CLI 基线未通过；请先修复标记为 ✗ 的项目"))


def print_history(store: StateStore, limit: int = 20) -> None:
    rows = store.recent_history(limit)
    if not rows:
        print("暂无投递历史。")
        return
    print("\n最近投递历史（不展示完整招呼语）：")
    for row in rows:
        print(
            f"- {row.get('updated_at')} | {row.get('status')} | "
            f"{row.get('company') or '(未知公司)'} | {row.get('title') or '(未知岗位)'} | "
            f"score={row.get('match_score')} | identity={row.get('confidence')}"
        )


def confirm(prompt: str) -> bool:
    """Explicit terminal confirmation; EOF is always a refusal."""
    try:
        return input(f"{prompt}\n输入 YES 继续，其它任意输入取消：").strip() == "YES"
    except EOFError:
        return False
