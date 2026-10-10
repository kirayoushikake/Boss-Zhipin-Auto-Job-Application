"""finding_jobs 的纯文本处理（不碰浏览器的部分）。

``_strip_jd_noise`` 剥掉 JD 开头的页面 UI 噪声行（举报 / 微信扫码分享 / 职位描述…），
这些是 ``.job-detail-body`` 的 innerText 带进来的页面 chrome，不是 JD 正文。
"""
from boss_zhipin.domain import IdentityConfidence
import asyncio

from boss_zhipin.website_oper import finding_jobs
from boss_zhipin.website_oper.finding_jobs import _clear_singleton_locks, _identity_from_card, _strip_jd_noise


def test_strips_leading_ui_noise():
    raw = "举报\n微信扫码分享\n职位描述\n\n1、参与 AI 功能模块设计\n岗位要求：本科"
    out = _strip_jd_noise(raw)
    assert out.startswith("1、参与 AI 功能模块设计")
    assert "举报" not in out
    assert "微信扫码分享" not in out


def test_only_strips_leading_run_not_body():
    # 正文里再出现"职位描述"不该被剥——只剥开头连续的噪声行
    raw = "举报\n职位描述\n这个职位描述很详细\n岗位要求"
    assert _strip_jd_noise(raw) == "这个职位描述很详细\n岗位要求"


def test_no_noise_unchanged():
    raw = "1、岗位职责\n2、岗位要求"
    assert _strip_jd_noise(raw) == raw


def test_empty_and_none():
    assert _strip_jd_noise("") == ""
    assert _strip_jd_noise(None) == ""


def test_clear_singleton_locks_removes_locks_keeps_cookies(tmp_path):
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "Cookies"):
        (tmp_path / name).write_text("x")
    _clear_singleton_locks(str(tmp_path))
    # 三个 Singleton 锁删掉；登录态文件（Cookies）保留
    assert not (tmp_path / "SingletonLock").exists()
    assert not (tmp_path / "SingletonCookie").exists()
    assert not (tmp_path / "SingletonSocket").exists()
    assert (tmp_path / "Cookies").exists()


def test_clear_singleton_locks_missing_ok(tmp_path):
    _clear_singleton_locks(str(tmp_path))  # 没有锁文件也不报错


def test_card_job_id_is_strong_but_url_only_is_not_sendable():
    strong = _identity_from_card({"job_id": "123", "title": "Python", "company": "测试公司"})
    weak = _identity_from_card({"href": "https://www.zhipin.com/job_detail/abc", "title": "Python", "company": "测试公司"})
    assert strong.confidence == IdentityConfidence.STRONG
    assert strong.safe_for_auto_send is True
    assert weak.confidence == IdentityConfidence.WEAK
    assert weak.safe_for_auto_send is False


def test_canonical_boss_job_detail_url_is_stable_strong_identity():
    identity = _identity_from_card({
        "href": "https://www.zhipin.com/job_detail/abc123.html?ka=search_list_1",
        "title": "Python", "company": "测试公司",
    })
    assert identity.confidence == IdentityConfidence.STRONG
    assert identity.safe_for_auto_send is True
    assert identity.job_url == "https://www.zhipin.com/job_detail/abc123.html"


def test_company_preference_only_changes_card_order(monkeypatch):
    async def evaluate(_js, timeout=5):
        assert timeout == 5
        return {"cards": [
            {"index": 1, "company": "普通游戏公司", "title": "游戏运营", "card_text": "普通游戏公司"},
            {"index": 2, "company": "二次元游戏工作室", "title": "游戏策划", "card_text": "开放世界"},
            {"index": 3, "company": "其他公司", "title": "用户研究", "card_text": "其他"},
        ]}

    monkeypatch.setattr(finding_jobs, "_safe_evaluate", evaluate)
    assert asyncio.run(finding_jobs.prioritized_job_card_indices(["二次元", "开放世界"], 3)) == [2, 1, 3]


def test_open_browser_reuses_a_healthy_controlled_tab(monkeypatch):
    calls: list[str] = []

    async def healthy():
        return True

    async def navigate(url):
        calls.append(url)

    monkeypatch.setattr(finding_jobs, "_browser", object())
    monkeypatch.setattr(finding_jobs, "_tab", type("Tab", (), {"url": "https://www.zhipin.com/"})())
    monkeypatch.setattr(finding_jobs, "_check_cdp_health", healthy)
    monkeypatch.setattr(finding_jobs, "navigate_to", navigate)
    asyncio.run(finding_jobs.open_browser_with_options("https://www.zhipin.com/web/geek/job?query=%E6%B8%B8%E6%88%8F", "chrome"))
    assert calls == ["https://www.zhipin.com/web/geek/job?query=%E6%B8%B8%E6%88%8F"]
