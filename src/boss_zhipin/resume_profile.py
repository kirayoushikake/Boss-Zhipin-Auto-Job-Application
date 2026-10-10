"""Normalised resume sources used by matching and CLI inspection."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader


RESUME_SOURCES = frozenset({"boss_online", "local_pdf", "local_text"})
MAX_PROFILE_SUPPLEMENT_CHARS = 12_000


@dataclass(frozen=True)
class ResumeProfile:
    source: str
    display_name: str = ""
    desired_positions: list[str] = field(default_factory=list)
    desired_cities: list[str] = field(default_factory=list)
    expected_salary: str = ""
    education: str = ""
    work_experiences: list[str] = field(default_factory=list)
    project_experiences: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    summary: str = ""
    raw_text: str = ""
    fingerprint: str = ""
    extracted_at: str = ""

    @property
    def quality(self) -> dict[str, object]:
        text = self.raw_text.strip()
        fields = {
            "desired_positions": bool(self.desired_positions),
            "education": bool(self.education),
            "experience_or_projects": bool(self.work_experiences or self.project_experiences),
            "skills": bool(self.skills),
        }
        missing = [name for name, present in fields.items() if not present]
        enough_text = len(text) >= 120
        return {
            "text_length": len(text),
            "present_fields": [name for name, present in fields.items() if present],
            "missing_fields": missing,
            "suitable_for_auto_matching": enough_text and len(missing) <= 2,
            "reason": "" if enough_text and len(missing) <= 2 else "简历文本或关键字段不足，禁止自动发送",
        }


def _fingerprint(text: str) -> str:
    normalised = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        value = re.sub(r"\s+", " ", value).strip(" -：:，,;；")
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


_SECTION_LABELS = (
    "求职意向", "求职期望", "意向职位", "期望职位", "期望岗位", "目标职位",
    "意向城市", "期望城市", "工作城市", "技能", "专业技能", "技能特长", "技术栈",
    "工作经历", "工作经验", "工作履历", "实习经历", "实习经验", "项目经历", "项目经验",
    "教育经历", "学历", "教育背景", "期望薪资", "薪资", "个人优势", "个人亮点",
    "自我评价", "个人简介",
)


def _is_section_heading(line: str) -> bool:
    """Recognise short visible headings without treating resume prose as one."""
    compact = re.sub(r"[\s:：-]", "", line)
    return len(compact) <= 24 and any(label in compact for label in _SECTION_LABELS)


def _lines_after(text: str, labels: tuple[str, ...], limit: int = 4) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates: list[list[str]] = []
    for index, line in enumerate(lines):
        if any(label.lower() in line.lower() for label in labels):
            values: list[str] = []
            for candidate in lines[index + 1:]:
                # Visible resumes often render each heading and value on a
                # separate line. Do not let the next *short heading* leak into
                # the previous field merely because the DOM is flattened. A
                # page-level resume navigation duplicates every heading at the
                # top; retain all matches and choose the richest body section
                # below instead of stopping at that empty navigation entry.
                if _is_section_heading(candidate):
                    break
                values.append(candidate)
                if len(values) >= limit:
                    break
            cleaned = _unique(values)
            if cleaned:
                candidates.append(cleaned)
    return max(candidates, key=lambda items: sum(len(item) for item in items), default=[])


def profile_from_text(text: str, source: str, *, display_name: str = "") -> ResumeProfile:
    """Create a conservative profile from visible/PDF text.

    This intentionally does not pretend that all BOSS fields are available.  It
    extracts only clearly labelled sections and keeps ``raw_text`` as the
    matching source of truth.
    """
    if source not in RESUME_SOURCES:
        raise ValueError(f"Unsupported resume source: {source}")
    raw_text = text.strip()
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not display_name and lines:
        candidate = lines[0]
        if len(candidate) <= 40 and not any(token in candidate for token in ("简历", "求职", "教育", "工作")):
            display_name = candidate
    positions = _lines_after(raw_text, ("求职意向", "求职期望", "意向职位", "期望职位", "期望岗位", "目标职位"), 2)
    cities = _lines_after(raw_text, ("意向城市", "期望城市", "工作城市"), 2)
    skills = _lines_after(raw_text, ("技能", "专业技能", "技能特长", "技术栈"), 6)
    work = _lines_after(raw_text, ("工作经历", "工作经验", "工作履历", "实习经历", "实习经验"), 8)
    projects = _lines_after(raw_text, ("项目经历", "项目经验"), 8)
    education_items = _lines_after(raw_text, ("教育经历", "学历", "教育背景"), 3)
    salary_items = _lines_after(raw_text, ("期望薪资", "薪资"), 1)
    summary_items = _lines_after(raw_text, ("个人优势", "个人亮点", "自我评价", "个人简介"), 3)
    return ResumeProfile(
        source=source,
        display_name=display_name,
        desired_positions=positions,
        desired_cities=cities,
        expected_salary=salary_items[0] if salary_items else "",
        education=" / ".join(education_items),
        work_experiences=work,
        project_experiences=projects,
        skills=skills,
        summary=" ".join(summary_items),
        raw_text=raw_text,
        fingerprint=_fingerprint(raw_text),
        extracted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def profile_from_pdf(path: str) -> ResumeProfile:
    pdf_path = Path(path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"找不到简历文件：{pdf_path}")
    try:
        reader = PdfReader(str(pdf_path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    except Exception as exc:  # pypdf exposes several version-specific errors
        raise ValueError(f"无法解析 PDF 简历：{type(exc).__name__}: {exc}") from exc
    if not text:
        raise ValueError("PDF 未提取到可用文本；扫描件请提供可搜索 PDF 或本地文本简历")
    return profile_from_text(text, "local_pdf")


def profile_from_local_text(path: str) -> ResumeProfile:
    text_path = Path(path)
    if not text_path.is_file():
        raise FileNotFoundError(f"找不到文本简历：{text_path}")
    text = text_path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError("文本简历为空")
    return profile_from_text(text, "local_text")


def enrich_profile_with_supplement(profile: ResumeProfile, path: str) -> ResumeProfile:
    """Append user-maintained, evidence-only project facts to a profile.

    Online BOSS resumes are intentionally concise.  A private local supplement
    lets a candidate provide project context for matching and the RAG letter
    generator without pretending it was scraped from BOSS or committing their
    personal portfolio to the public repository.  The content still goes
    through the same grounding validator before any send.
    """
    supplement_path = Path(path)
    if not supplement_path.is_file():
        raise FileNotFoundError(f"找不到简历补充事实文件：{supplement_path}")
    try:
        supplement = supplement_path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("简历补充事实文件必须是 UTF-8 文本") from exc
    if not supplement:
        raise ValueError("简历补充事实文件为空")
    if len(supplement) > MAX_PROFILE_SUPPLEMENT_CHARS:
        raise ValueError(f"简历补充事实文件过长（最多 {MAX_PROFILE_SUPPLEMENT_CHARS} 字符）")
    raw_text = (
        profile.raw_text.rstrip()
        + "\n\n=== 候选人核验的补充项目事实（只能据此陈述，禁止推断） ===\n"
        + supplement
    )
    return replace(profile, raw_text=raw_text, fingerprint=_fingerprint(raw_text))


def enrich_profile_from_env(profile: ResumeProfile) -> ResumeProfile:
    """Apply the optional private supplement selected by the current runtime."""
    path = os.getenv("BOSS_PROFILE_SUPPLEMENT_PATH", "").strip()
    return enrich_profile_with_supplement(profile, path) if path else profile


def redact_name(name: str) -> str:
    if not name:
        return "(未识别)"
    if len(name) == 1:
        return "*"
    return name[0] + "*" * (len(name) - 1)


class BossOnlineResumeSource:
    """Read the currently logged-in user's visible BOSS resume page.

    The browser code is imported lazily so PDF/text-only CLI commands do not
    initialise nodriver.  This source deliberately has no API/cookie access.
    """

    async def load(self) -> ResumeProfile:
        from boss_zhipin.website_oper import finding_jobs

        if not await finding_jobs.is_logged_in():
            raise RuntimeError("BOSS 尚未登录；请在 Chrome 中完成正常扫码登录后重试")
        raw_text = await finding_jobs.read_online_resume_text()
        if not raw_text:
            raise RuntimeError(
                "未能从当前 BOSS 简历页面读取足够的可见内容；请检查在线简历是否已填写，"
                "或改用 --resume-source pdf/text 本地回退"
            )
        profile = profile_from_text(raw_text, "boss_online")
        quality = profile.quality
        if not quality["suitable_for_auto_matching"]:
            missing = "、".join(quality["missing_fields"]) or "可用简历文本"
            raise RuntimeError(f"在线简历信息不足（缺少：{missing}），禁止自动发送")
        return profile
