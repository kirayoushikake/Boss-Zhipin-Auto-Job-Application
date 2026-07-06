"""nodriver-backed BOSS automation.

整个模块对外**全部是 async 函数**。调用方（``write_response.py`` 主循环）
也必须 async，并由 ``main.py`` 用 ``uc.loop().run_until_complete(...)`` **整段
跑在一个事件循环里**。

为什么这么设计：之前对外是 sync facade（每个公开函数内部 ``_run`` 调一次
``uc.loop().run_until_complete``），结果每次 enter/exit 事件循环都让 nodriver
的 CDP websocket 进入半死状态，下一次 evaluate 直接 hang 到 timeout。
``scripts/probe_click_card.py`` 单 coroutine 跑同样的 evaluate 是秒返回，
证实就是 sync facade 模式跟 nodriver 的活跃性需求不兼容。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys

import nodriver as uc
from nodriver import Config

log = logging.getLogger(__name__)

# 持久化 Chrome profile：第一次手动扫码后 cookie 会留下，之后跑就跳过登录。
# 可用 BOSS_CHROME_PROFILE 环境变量覆盖路径。
CHROME_PROFILE_DIR = os.path.abspath(
    os.environ.get("BOSS_CHROME_PROFILE", "./chrome_profile")
)

_browser: uc.Browser | None = None
_tab: uc.Tab | None = None


def get_tab() -> uc.Tab | None:
    """同步读当前控制的 Tab 引用。仅用作内省。"""
    return _tab


def get_driver():
    raise RuntimeError(
        "get_driver() 已移除：项目已迁到 nodriver，没有 Selenium driver。"
        "请改用 finding_jobs 里的 async helper（get_text_by_css/click_by_xpath/"
        "wait_for_css/send_chat_message/navigate_back）。"
    )


def _on_login_page(url: str) -> bool:
    return any(s in url for s in ("/web/user/", "passport-zp", "/login"))


async def _is_logged_in() -> bool:
    """判定登录态：URL + DOM 双 check。

    早期 BOSS 对未登录用户一定 redirect 到 ``/web/user/`` 类路径，只看 URL 够用。
    现在它经常**不 redirect**，而是直接在 ``/web/geek/...`` 原地盖一个登录浮层。
    只看 URL 会把这种情况误判成已登录，后续 ``get_job_description`` 抓到的是
    浮层背后的 ``<style>`` 噪音（关键词命中 0/2 → feed_exhausted 假阴性）。

    所以加一层 DOM 探测：页面里有可见的登录浮层就强制判未登录。选不到浮层时
    退回原 URL 判定，避免 BOSS 改 class 名后把真实已登录态误杀。
    """
    if _tab is None:
        return False
    # URL 命中已知登录页路径 → 直接未登录，连 DOM 都不用问
    if _on_login_page(_tab.url):
        return False

    # BOSS 的登录浮层 class 名换过几版：boss-login-dialog / login-dialog-wrap /
    # loginDialog 等，统一用 attribute selector 兜
    js = """
    JSON.stringify((() => {
      const visible = (el) => {
        if (!el) return false;
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      };
      const wall = document.querySelector(
        '[class*="login-dialog"], [class*="boss-login"], '
        + '[class*="loginDialog"], [class*="login-wrap"]'
      );
      return { loginWallVisible: visible(wall) };
    })());
    """
    info = await _safe_evaluate(js, timeout=5)
    if info.get("loginWallVisible"):
        log.info("URL 看起来已登录但页面上有登录浮层 → 判未登录，走扫码")
        return False
    return True


async def _wait_url_stable(stable_for: float = 2.0, timeout: float = 30) -> str:
    """等到 tab.url 连续 stable_for 秒不变，避开 BOSS 登录页的重定向抖动。"""
    end = asyncio.get_event_loop().time() + timeout
    last_url = _tab.url
    last_change = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() < end:
        cur = _tab.url
        now = asyncio.get_event_loop().time()
        if cur != last_url:
            last_url = cur
            last_change = now
        elif now - last_change >= stable_for:
            return cur
        await asyncio.sleep(0.2)
    return last_url


# ---------- 浏览器生命周期 ----------


async def shutdown() -> None:
    """关 Chrome 并清空模块级 ``_browser`` / ``_tab``。

    给 GUI 用——用户点"重置"想从头来一遍时调一次，否则下次
    ``open_browser_with_options`` 会留旧 Chrome 进程。CLI 不需要：``main.py``
    退出时 OS 会清理子进程。

    ``Browser.stop()`` 是同步函数，但本函数声明 ``async`` 是为了让 GUI
    runner 能 ``await shutdown()``（runner 一律 await，sync/async 不混用）。

    重启注意：这只关 Chrome 进程；nodriver 跟 uvloop 的重启不兼容问题需要
    GUI 入口传 ``loop="asyncio"`` 才能彻底解决（见 project memory）。
    """
    global _browser, _tab
    if _browser is not None:
        try:
            _browser.stop()
        except Exception as e:
            log.warning("browser.stop() 失败（不致命）: %s", e)
    _browser = None
    _tab = None


def _clear_singleton_locks(profile_dir: str) -> None:
    """删掉 profile 里残留的 Singleton 锁文件。

    上次 Chrome 没退干净会留下 ``SingletonLock/Cookie/Socket``，新 Chrome 看到会
    误判 profile 被占。纯文件操作，便于单测。
    """
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.remove(os.path.join(profile_dir, name))
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001 — best-effort 清理，失败不致命
            log.debug("清 Singleton 锁 %s 跳过：%s", name, e)


def _kill_profile_chrome(profile_dir: str) -> None:
    """杀掉占着本 profile 的残留 Chrome（上次 run 崩了没收掉的孤儿）。

    profile 目录本工具独占，``--user-data-dir=<profile>`` 命中的一定是我们自己的
    残留，杀掉安全。这是用户实测的"连接失败"根因：上次启动起了 Chrome 但 CDP 没
    连上，孤儿一直占着 profile，下次再起就被锁。
    """
    abs_dir = os.path.abspath(profile_dir)
    if sys.platform == "win32":
        # 用 PowerShell 查命令行中包含 profile 路径的 chrome.exe 进程并杀。
        # 只杀跟本 profile 相关的孤儿 Chrome，不动用户日常浏览器。
        ps_cmd = (
            'Get-CimInstance Win32_Process -Filter "Name=\'chrome.exe\'" | '
            'Where-Object { $_.CommandLine -like \'*' + abs_dir + '*\' } | '
            'ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }'
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, timeout=10,
            )
        except Exception as e:  # noqa: BLE001
            log.debug("Windows 杀残留 Chrome 跳过：%s", e)
        return
    try:
        subprocess.run(
            ["pkill", "-f", abs_dir],
            capture_output=True, timeout=5,
        )
    except Exception as e:  # noqa: BLE001 — 没装 pkill / 无匹配都不致命
        log.debug("pkill 残留 Chrome 跳过：%s", e)


def _reap_profile_chrome(profile_dir: str) -> None:
    """起浏览器前的自清理：先收掉占 profile 的孤儿 Chrome，再清 Singleton 锁。"""
    _kill_profile_chrome(profile_dir)
    _clear_singleton_locks(profile_dir)


async def _start_browser_with_retry(config: Config, attempts: int = 3) -> uc.Browser:
    """启动并连上 Chrome，失败重试。

    新版 Chrome（如 149）+ 新 macOS 冷启动时，CDP 端口起得慢，nodriver 第一次连
    经常 timeout 报 "Failed to connect to browser"，但 Chrome 其实已经起来了 →
    变孤儿占住 profile。所以每次失败都先 reap（杀掉这次起的、没连上的 Chrome +
    清锁），再退避重试。
    """
    last_err: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return await uc.start(config=config)
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("浏览器启动/连接失败（第 %d/%d 次）：%s", i, attempts, e)
            _reap_profile_chrome(config.user_data_dir)
            if i < attempts:
                await asyncio.sleep(2.0 * i)
    assert last_err is not None
    raise last_err


async def open_browser_with_options(url: str, browser: str) -> None:
    """启动 Chrome 并打开 url。``browser`` 仅接受 ``"chrome"``。"""
    global _browser, _tab
    if browser != "chrome":
        raise NotImplementedError(
            f"browser={browser!r} 不再支持；nodriver 只走 Chrome。"
        )
    os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
    # 起之前先收掉上次没退干净、占着本 profile 的孤儿 Chrome + 清残留锁，
    # 否则会复现用户实测的 "Failed to connect to browser"（profile 被旧实例锁住）。
    _reap_profile_chrome(CHROME_PROFILE_DIR)
    config = Config()
    config.user_data_dir = CHROME_PROFILE_DIR
    config.headless = False
    _browser = await _start_browser_with_retry(config)

    # 持久化 profile 启动时 Chrome 会把上次的 tab 都恢复出来；脚本控制的 tab
    # 直接放到一个独立的新窗口里，跟历史窗口井水不犯河水，新窗口默认抢焦。
    _tab = await _browser.get(url, new_window=True)
    try:
        await _tab.activate()
        await _tab.bring_to_front()
    except Exception as e:
        log.warning("激活控制 tab 失败（%s），不影响后续操作", e)

    log.info("页面加载中... 当前URL: %s", _tab.url)
    stable_url = await _wait_url_stable(stable_for=2.0, timeout=30)
    log.info("页面已稳定，当前URL: %s", stable_url)


async def log_in() -> None:
    """识别登录状态；未登录则点开微信扫码，等用户扫码登录。"""
    if await _is_logged_in():
        log.info("检测到已登录（profile: %s），跳过扫码", CHROME_PROFILE_DIR)
        return

    cur_url = _tab.url
    log.info("log_in 入口 URL: %s", cur_url)

    if not _on_login_page(cur_url):
        try:
            login_btn = await _tab.find("登录", best_match=True, timeout=15)
            if login_btn:
                await login_btn.click()
                log.info("已点击 header 登录入口")
                await _wait_url_stable(stable_for=2.0, timeout=15)
        except Exception as e:
            log.warning("找不到 header 登录入口（%s），尝试直接在当前页找微信入口", e)

    try:
        wechat_btn = await _tab.find("微信", best_match=True, timeout=10)
        if wechat_btn:
            await wechat_btn.click()
            log.info("已点击微信登录入口，请扫码...")
        else:
            log.warning("未自动点上微信入口，请在浏览器里手动选择登录方式")
    except Exception as e:
        log.warning("查找微信入口出错（%s），请手动选择登录方式", e)

    log.info("等待扫码登录... (最多 300 秒)")
    deadline = asyncio.get_event_loop().time() + 300
    while asyncio.get_event_loop().time() < deadline:
        if await _is_logged_in():
            log.info("登录成功！cookie 已写入 profile，下次跑应该不用再扫")
            return
        await asyncio.sleep(2)
    log.warning("登录超时，请确认是否已扫码登录")


# ---------- JS 评估辅助 ----------


async def _check_cdp_health(timeout: float = 3.0) -> bool:
    """Check if the CDP connection is still alive.

    Two checks:
    1. The internal ``_listener_task`` must be running (not crashed / done)
    2. A trivial evaluate must return within *timeout* seconds

    Returns True if both pass. If the listener is dead, nodriver's ``send()``
    will hang forever because no one is processing incoming CDP responses.
    """
    if _tab is None:
        return False

    # Check 1: listener task alive
    listener = getattr(_tab, "_listener_task", None)
    if listener is not None and listener.done():
        exc = listener.exception()
        log.warning(
            "CDP listener task is dead! Exception: %s. "
            "All future evaluate calls will hang.",
            exc,
        )
        return False

    # Check 2: trivial evaluate (no JSON.stringify needed for raw ping)
    try:
        task = asyncio.ensure_future(
            _tab.evaluate('JSON.stringify({_p:1})')
        )
        raw = await asyncio.wait_for(task, timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        log.warning("CDP health ping timed out after %ss — connection is dead", timeout)
        return False
    except Exception as e:
        log.warning("CDP health ping failed: %s: %s", type(e).__name__, e)
        return False

    # Parse ping response
    if isinstance(raw, tuple):
        raw = raw[0]
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            if data.get("_p") == 1:
                return True
        except json.JSONDecodeError:
            pass
    return False


async def _safe_evaluate(js: str, timeout: float = 10) -> dict:
    """跑 ``tab.evaluate(js)``，期望 JS 自己 ``JSON.stringify`` 返回字符串。

    自带 ``asyncio.wait_for`` 兜底 + JSON 解析容错 —— 永远不抛、永远返回 dict
    （失败时空 dict）。

    超时时显式 cancel 底层 task，然后 yield 给 event loop 让 nodriver 内部的
    ``Transaction`` 有机会被 CDP 响应清理，防止对后续 evaluate 造成干扰。
    """
    js_head = js.strip()[:80].replace("\n", " ")
    task: asyncio.Task | None = None
    try:
        task = asyncio.ensure_future(_tab.evaluate(js))
        raw = await asyncio.wait_for(task, timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("evaluate 超过 %ss 没返回（JS: %s...）", timeout, js_head)
        if task is not None and not task.done():
            task.cancel()
            # Yield to event loop so the cancelled coroutine can fully unwind
            # and nodriver's internal Transaction gets cleaned up by the
            # _listener. Without this, the next evaluate can race with stale
            # CDP state.
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        return {}
    except asyncio.CancelledError:
        log.warning("evaluate 被取消（JS: %s...）", js_head)
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        return {}
    except Exception as e:
        log.warning("evaluate 抛 %s: %s（JS: %s...）", type(e).__name__, e, js_head)
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        return {}
    if isinstance(raw, tuple):
        raw = raw[0]
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning("JS 返回不是合法 JSON：%s（前 200 字符：%r）", e, raw[:200])
            return {}
    if isinstance(raw, dict):
        return raw
    log.warning("evaluate 返回类型 %s（不是 str/dict）：%r", type(raw).__name__, repr(raw)[:200])
    return {}


async def _js_click_at_index(css_selector: str, index_1: int) -> dict:
    """在 JS 里点 ``css_selector`` 命中的第 N 个元素（1-indexed）。"""
    js = f"""
    JSON.stringify((() => {{
      try {{
        const els = document.querySelectorAll({json.dumps(css_selector)});
        if (els.length < {index_1}) return {{ok: false, total: els.length}};
        els[{index_1 - 1}].click();
        return {{ok: true, total: els.length}};
      }} catch (e) {{
        return {{ok: false, error: String(e), stack: e.stack || ''}};
      }}
    }})())
    """
    return await _safe_evaluate(js)


async def _js_wait_text(css_selector: str, min_len: int, timeout_s: float) -> str | None:
    """轮询直到 ``css_selector`` 命中且 ``text.length >= min_len``，返回 text；超时返回 None。"""
    deadline = asyncio.get_event_loop().time() + timeout_s
    _consecutive_timeouts = 0
    while asyncio.get_event_loop().time() < deadline:
        js = f"""
        JSON.stringify((() => {{
          try {{
            const el = document.querySelector({json.dumps(css_selector)});
            if (!el) return {{ok: false, reason: 'not_found'}};
            // 用 innerText 而非 textContent：BOSS 反爬往 JD 里塞了 <style> 块（其 CSS
            // 源码会被 textContent 读出来）+ display:none / width:0.1px 的隐藏诱饵 span
            // （逐字插 "来自BOSS直聘"/"kanzhun" 把真词劈开，导致关键词匹配全废、命中骤降）。
            // innerText 只返回「渲染可见」文本，自动排除 <style> 源码和隐藏元素，拿到干净
            // JD 正文。诊断见 scripts/probe_jd_extract.py（s1=textContent 脏 / s2=innerText 净）。
            const text = (el.innerText || '').trim();
            if (text.length < {min_len}) return {{ok: false, reason: 'too_short', len: text.length}};
            return {{ok: true, text: text}};
          }} catch (e) {{
            return {{ok: false, error: String(e)}};
          }}
        }})())
        """
        result = await _safe_evaluate(js, timeout=5)
        if result.get("ok"):
            return result["text"]

        # Track timeouts — if CDP is dead, abort early instead of
        # burning through the full deadline with hung evaluate calls
        if not result:
            _consecutive_timeouts += 1
            if _consecutive_timeouts >= 2:
                log.warning(
                    "_js_wait_text: %d consecutive evaluate timeouts — "
                    "checking CDP health", _consecutive_timeouts,
                )
                if not await _check_cdp_health(timeout=3.0):
                    log.error(
                        "_js_wait_text: CDP connection is DEAD, aborting poll"
                    )
                    return None
        else:
            _consecutive_timeouts = 0

        await asyncio.sleep(0.5)
    return None


async def _xpath_safe(xp: str, timeout: float = 3.0) -> list:
    """``tab.xpath`` 的兜底封装（保留给 ``select_dropdown_option`` 用，主路径已不依赖它）。"""
    try:
        result = await asyncio.wait_for(
            _tab.xpath(xp, timeout=timeout),
            timeout=timeout + 2,
        )
        return result or []
    except asyncio.TimeoutError:
        log.warning("xpath 超时: %s", xp[:80])
        return []
    except Exception as e:
        log.warning("xpath 出错: %s: %s", type(e).__name__, e)
        return []


# ---------- 业务 helper ----------


async def select_dropdown_option(label: str) -> None:
    """空 label 表示用 BOSS 默认推荐 feed，不主动选 tag。"""
    if not label:
        log.info("[select_dropdown_option] label 为空，沿用当前推荐 feed")
        return
    log.info("[select_dropdown_option] label=%r", label)

    log.info("  路径 1: 找推荐 tag chip ...")
    chip_xp = (
        "//*[contains(@class,'recommend-job-btn')"
        " and contains(@class,'has-tooltip')]"
    )
    chips = await _xpath_safe(chip_xp, timeout=3)
    log.info("    找到 %d 个 tag chip", len(chips))
    for el in chips:
        text = (el.text or "").strip()
        if label in text:
            log.info("    → 命中 %r，点击", text)
            await el.click()
            return

    log.info("  路径 2: 找下拉菜单触发器 ...")
    trigger = await _xpath_safe(
        "//*[@id='wrap']/div[2]/div[1]/div/div[1]/div", timeout=3
    )
    if trigger:
        log.info("    → 点开下拉")
        await trigger[0].click()
        await _xpath_safe(
            "//ul[contains(@class,'dropdown-expect-list')]", timeout=3
        )
        options = await _xpath_safe(
            f"//li[contains(text(), '{label}')]", timeout=3
        )
        if options:
            log.info("    → 命中下拉 option %r，点击", label)
            await options[0].click()
            return
        log.info("    下拉里没有 %r", label)
    else:
        log.info("    没找到下拉触发器")

    log.info("  路径 3: fallback —— 用 BOSS 默认的推荐 feed 继续（不主动选 tag）")


# BOSS 详情面板顶部的 UI 文字（按钮 / 区块标题），不是 JD 正文。innerText 会把它们
# 带在最前面（"举报\n微信扫码分享\n职位描述\n…"），剥掉让喂给匹配/LLM 的更纯。
_JD_NOISE_LINES = frozenset({"举报", "微信扫码分享", "微信分享", "分享", "职位描述", "立即沟通", "收藏"})


def _strip_jd_noise(text: str) -> str:
    """剥掉 JD 文本**开头连续**的页面 UI 噪声行。

    只剥开头那几行（举报 / 微信扫码分享 / 职位描述 …）；正文里万一再出现同样的词
    不动。纯函数，便于单测（见 tests/test_finding_jobs_text.py）。
    """
    lines = (text or "").split("\n")
    i = 0
    while i < len(lines) and lines[i].strip() in _JD_NOISE_LINES:
        i += 1
    return "\n".join(lines[i:]).strip()


async def get_job_description_by_index(index: int) -> str | None:
    """点开第 N 个岗位卡（1-indexed），返回右侧 JD 详情面板的文本；失败返回 None。"""
    log.info("[get_job_description_by_index] index=%d", index)
    # 2026-07 BOSS 改版后部分页面 job cards 渲染较慢，先轮询等它们出现再点。
    click_result = await _js_click_at_index(".job-card-box", index)
    log.info("  点击 .job-card-box[%d]: %s", index, click_result)
    if not click_result.get("ok"):
        return None

    jd = await _js_wait_text(".job-detail-body", min_len=50, timeout_s=10)
    if jd is None:
        log.info("  10s 内 .job-detail-body 没出现或文本太短")
        return None
    jd = _strip_jd_noise(jd)
    log.info("  JD 长度 %d 字符", len(jd))
    return jd


async def get_text_by_css(selector: str, timeout: float = 5) -> str | None:
    """通用：返回 CSS 选择器命中元素的 text，找不到返回 None。"""
    try:
        el = await _tab.select(selector, timeout=timeout)
    except Exception:
        return None
    return el.text if el else None


async def click_by_xpath(xpath: str, timeout: float = 10) -> bool:
    """通过 xpath 找到元素并点击。成功返回 True。"""
    els = await _xpath_safe(xpath, timeout=timeout)
    if not els:
        return False
    await els[0].click()
    return True


async def wait_for_css(selector: str, timeout: float = 50) -> bool:
    """等 CSS 选择器命中。成功返回 True，超时返回 False。"""
    try:
        el = await _tab.select(selector, timeout=timeout)
    except Exception:
        return False
    return el is not None


async def send_chat_message(text: str) -> None:
    """把 text 打进 ``#chat-input`` 然后回车发送。"""
    chat = await _tab.select("#chat-input", timeout=10)
    if not chat:
        raise RuntimeError("chat input (#chat-input) 未找到")
    await chat.send_keys(text)
    await asyncio.sleep(3)
    await chat.send_keys("\n")
    await asyncio.sleep(1)


async def send_image_message(image_path: str) -> bool:
    """在 BOSS 聊天窗口里上传并发送一张图片（简历等）。

    用 CDP ``DOM.setFileInputFiles`` 直接给 ``input[type=file]`` 填文件路径，
    比 ``send_keys`` 更可靠（不受元素可见性影响）。成功返回 True。
    """
    if not os.path.isfile(image_path):
        log.warning("图片文件不存在: %s", image_path)
        return False
    try:
        abs_path = os.path.abspath(image_path)
        # 方式 1: CDP DOM.setFileInputFiles（最可靠，不依赖元素可见性）
        try:
            cdp_result = await _tab.evaluate(
                'JSON.stringify((()=>{'
                'const inp=document.querySelector(\'input[type="file"]\');'
                'if(!inp)return{found:false};'
                'inp.style.display="block";inp.style.visibility="visible";'
                'return{found:true};'
                '})())'
            )
            cdp_data = json.loads(cdp_result[0] if isinstance(cdp_result, tuple) else cdp_result)
            if cdp_data.get("found"):
                file_input = await _tab.select('input[type="file"]', timeout=5)
                if file_input:
                    await file_input.send_keys(abs_path)
                    log.info("已选择图片: %s", abs_path)
                else:
                    log.warning("找到 file input 但 select 失败")
                    return False
            else:
                log.warning("聊天页未找到 input[type=file]")
                return False
        except Exception as e:
            log.warning("CDP 方式选文件失败: %s", e)
            return False

        # 等 BOSS 上传 + 生成缩略图
        await asyncio.sleep(4)
        # 找图片专用的发送按钮（BOSS 选图后会弹出确认发送按钮）
        send_btn = await _tab.find("发送", best_match=True, timeout=8)
        if send_btn:
            await send_btn.click()
            log.info("图片已发送")
            await asyncio.sleep(1)
            return True
        log.info("未找到发送按钮，可能已自动发送")
        return True
    except Exception as e:
        log.warning("发送图片失败: %s", e)
        return False


async def navigate_back() -> None:
    """``history.back()`` —— 浏览器返回上一页。"""
    await _tab.evaluate("history.back()")
    await asyncio.sleep(3)


async def scroll_job_list() -> int:
    """对 ``window`` 做快速增量滚动到底部，触发 BOSS 加载更多岗位卡片。

    经验证（15→30→45→60）：BOSS 的无限滚动靠 ``window`` 滚动位置触发——
    当 ``scrollY + innerHeight >= body.scrollHeight`` 时加载下一批 15 张。

    关键：用简单的 ``window.scrollBy(0, 500)`` 单条语句，150ms 快速连滚。
    不能滚 ``.job-detail-container``（那是公司详情面板），也不能用多语句 JS。

    Returns:
        滚动前已有的卡片数（>0 表示新卡片已加载），0 表示未加载新卡片。
    """
    _COUNT_JS = 'JSON.stringify({total:document.querySelectorAll(".job-card-box").length})'
    _PROBE_JS = (
        'JSON.stringify({'
        'y:window.scrollY,ih:window.innerHeight,sh:document.body.scrollHeight'
        '})'
    )

    try:
        # Health check first — if CDP is dead, scrolling will never work
        if not await _check_cdp_health(timeout=3.0):
            log.error("scroll_job_list: CDP connection is DEAD, cannot scroll")
            return 0

        probe = await _safe_evaluate(_COUNT_JS, timeout=5)
        old_total = probe.get("total", 0)
        if old_total == 0:
            log.warning("无法读取卡片数（CDP 可能卡死），放弃滚动")
            return 0
        probe = await _safe_evaluate(_PROBE_JS, timeout=5)
        old_body_sh = probe.get("sh", 0)
        if old_body_sh == 0:
            log.warning("无法读取 bodySH（CDP 可能卡死），放弃滚动")
            return 0
        log.info("开始滚动: cards=%d bodySH=%d", old_total, old_body_sh)

        _consecutive_timeouts = 0

        # 持续滚到底直到 bodySH 不再增长或卡片数增加
        for _ in range(40):  # 最多 40 步
            # 检查卡片数
            probe = await _safe_evaluate(_COUNT_JS, timeout=5)
            if not probe:  # 空 dict = timeout
                _consecutive_timeouts += 1
                if _consecutive_timeouts >= 3:
                    # Verify CDP isn't dead before giving up
                    if not await _check_cdp_health(timeout=3.0):
                        log.error(
                            "连续 %d 次 evaluate 超时 + CDP 健康检查失败，"
                            "连接已死，放弃滚动",
                            _consecutive_timeouts,
                        )
                    else:
                        log.warning(
                            "连续 %d 次 evaluate 超时（CDP 健康检查通过），"
                            "放弃滚动",
                            _consecutive_timeouts,
                        )
                    return 0
                continue
            _consecutive_timeouts = 0

            if probe.get("total", 0) > old_total:
                log.info("滚动加载成功: %d -> %d 个岗位卡片", old_total, probe["total"])
                return old_total

            # 获取当前位置
            pos = await _safe_evaluate(_PROBE_JS, timeout=5)
            if not pos:
                _consecutive_timeouts += 1
                if _consecutive_timeouts >= 3:
                    if not await _check_cdp_health(timeout=3.0):
                        log.error(
                            "连续 %d 次 evaluate 超时 + CDP 健康检查失败，"
                            "连接已死，放弃滚动",
                            _consecutive_timeouts,
                        )
                    else:
                        log.warning(
                            "连续 %d 次 evaluate 超时（CDP 健康检查通过），"
                            "放弃滚动",
                            _consecutive_timeouts,
                        )
                    return 0
                continue
            _consecutive_timeouts = 0
            cur_y = pos.get("y", 0)
            cur_sh = pos.get("sh", 0)
            ih = pos.get("ih", 800)

            at_bottom = cur_y + ih >= cur_sh - 5

            if at_bottom:
                # 已在底部，等 BOSS 加载
                await asyncio.sleep(1.5)
                pos = await _safe_evaluate(_PROBE_JS, timeout=5)
                if not pos:
                    _consecutive_timeouts += 1
                    if _consecutive_timeouts >= 3:
                        if not await _check_cdp_health(timeout=3.0):
                            log.error(
                                "连续 %d 次 evaluate 超时 + CDP 健康检查失败，"
                                "连接已死，放弃滚动",
                                _consecutive_timeouts,
                            )
                        else:
                            log.warning(
                                "连续 %d 次 evaluate 超时（CDP 健康检查通过），"
                                "放弃滚动",
                                _consecutive_timeouts,
                            )
                        return 0
                    continue
                _consecutive_timeouts = 0
                new_sh = pos.get("sh", 0)
                if new_sh > cur_sh + 200:
                    # body 变高了，新内容已注入，继续滚
                    log.info("  bodySH %d -> %d，新内容已加载", cur_sh, new_sh)
                    continue
                # body 没变，可能到底了，回滚再试一次
                await _safe_evaluate(
                    'JSON.stringify((()=>{window.scrollBy(0,-300);return{ok:true};})())',
                    timeout=5,
                )
                await asyncio.sleep(0.3)
                await _safe_evaluate(
                    'JSON.stringify((()=>{window.scrollBy(0,500);return{ok:true};})())',
                    timeout=5,
                )
                await asyncio.sleep(0.3)
            else:
                # 还没到底，继续滚
                await _safe_evaluate(
                    'JSON.stringify((()=>{'
                    'window.scrollBy(0,500);'
                    'return{ok:true};'
                    '})())',
                    timeout=5,
                )
                await asyncio.sleep(0.15)

        # 最终检查
        probe = await _safe_evaluate(_COUNT_JS, timeout=5)
        new_total = probe.get("total", 0)
        if new_total > old_total:
            log.info("滚动加载成功: %d -> %d 个岗位卡片", old_total, new_total)
            return old_total
        log.info("滚动后无新卡片（可能已到底）: %d 个", old_total)
        return 0
    except Exception as e:
        log.warning("滚动失败: %s", e)
        return 0


# Variables（保持向后兼容）
url = "https://www.zhipin.com/web/geek/job-recommend?ka=header-job-recommend"
browser_type = "chrome"
