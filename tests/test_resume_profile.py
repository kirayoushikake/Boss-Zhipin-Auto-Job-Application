from __future__ import annotations

import pytest

from boss_zhipin import resume_profile
from boss_zhipin.resume_profile import (
    enrich_profile_from_env,
    enrich_profile_with_supplement,
    profile_from_local_text,
    profile_from_pdf,
    profile_from_text,
    redact_name,
)


RESUME_TEXT = """王小明
求职意向
Python 后端工程师
意向城市
深圳
教育经历
本科 计算机科学
工作经历
某科技公司 Python 开发
负责 API 服务与数据库设计
项目经历
招聘平台服务
技能
Python
FastAPI
MySQL
Docker
个人优势
熟悉后端系统设计和交付，具备持续学习能力。"""


def test_profile_from_text_extracts_safe_summary_and_fingerprint():
    profile = profile_from_text(RESUME_TEXT, "local_text")
    assert profile.display_name == "王小明"
    assert profile.desired_positions == ["Python 后端工程师"]
    assert profile.skills[:2] == ["Python", "FastAPI"]
    assert profile.fingerprint
    assert profile.quality["suitable_for_auto_matching"] is True
    assert redact_name(profile.display_name) == "王**"


def test_local_text_empty_and_missing_file_have_clear_errors(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("   ", encoding="utf-8")
    with pytest.raises(ValueError, match="为空"):
        profile_from_local_text(str(empty))
    with pytest.raises(FileNotFoundError, match="找不到"):
        profile_from_local_text(str(tmp_path / "missing.txt"))


def test_local_text_supports_chinese_path(tmp_path):
    path = tmp_path / "简历目录" / "我的简历.txt"
    path.parent.mkdir()
    path.write_text(RESUME_TEXT, encoding="utf-8")
    assert profile_from_local_text(str(path)).fingerprint


def test_pdf_empty_or_scan_like_input_requests_text_fallback(monkeypatch, tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"not used by mocked reader")

    class EmptyReader:
        pages = [type("Page", (), {"extract_text": lambda self: ""})()]

    monkeypatch.setattr(resume_profile, "PdfReader", lambda path: EmptyReader())
    with pytest.raises(ValueError, match="扫描件"):
        profile_from_pdf(str(pdf))


def test_short_profile_is_not_eligible_for_auto_matching():
    profile = profile_from_text("李四\nPython", "local_text")
    assert profile.quality["suitable_for_auto_matching"] is False


def test_duplicate_navigation_headings_choose_richer_resume_section():
    text = """求职期望
工作经历
项目经历
教育经历
专业技能
个人优势
期望职位
Python 后端工程师
深圳
工作经历
某科技公司 后端开发工程师
负责 Python API、数据库与 Docker 服务维护
项目经历
招聘系统
负责高并发服务设计和交付
教育经历
本科 计算机科学
专业技能
Python FastAPI MySQL Docker Redis
个人优势
具备后端工程化经验与良好协作能力。"""
    profile = profile_from_text(text, "boss_online")
    assert "Python 后端工程师" in profile.desired_positions
    assert profile.work_experiences
    assert profile.project_experiences
    assert profile.skills
    assert profile.quality["suitable_for_auto_matching"] is True


def test_private_project_supplement_changes_evidence_and_fingerprint(monkeypatch, tmp_path):
    supplement = tmp_path / "candidate_profile.md"
    supplement.write_text("- 游戏直播复盘：整理观众反馈并形成建议。", encoding="utf-8")
    profile = profile_from_text(RESUME_TEXT, "local_text")
    enriched = enrich_profile_with_supplement(profile, str(supplement))
    assert "游戏直播复盘" in enriched.raw_text
    assert enriched.fingerprint != profile.fingerprint
    monkeypatch.setenv("BOSS_PROFILE_SUPPLEMENT_PATH", str(supplement))
    assert enrich_profile_from_env(profile).fingerprint == enriched.fingerprint
