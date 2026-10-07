"""Lazy playwright session: real-render page operations for the LLM tools.

One shared headless chromium page backed by a **persistent user-data profile**
(created on first use, reused across tool calls). Because the profile lives on
disk, cookies / logins / localStorage survive restarts — it behaves like a real
browser, only headless and lightweight (chromium only, no extra services).

Raises :class:`BrowserError` with actionable Chinese messages when the kernel is
missing, so the calling tools can degrade gracefully.
"""
from __future__ import annotations

from astrbot.api import logger

import asyncio
import glob
import os
import random
import re
import time
from pathlib import Path
from typing import Any

from .browser import BrowserError, assert_browsable

# 市场规范：日志器必须取自 astrbot.api，插件不自建日志器

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# DOM click/fill target a snapshot element that is visible by construction, so a
# short actionability budget is enough; a miss should fail fast (not stall 30s
# on a hidden element, which is what happened on Baidu's hidden <textarea>s).
_ACTION_TIMEOUT_MS = 8000

# Standard, safe headless flags. We deliberately do NOT force --no-proxy-server
# / DoH-off / QUIC-off here: those were added while mis-diagnosing a hang that
# turned out to be the security suite blocking playwright's headless_shell.exe.
# The real fix is launching the full chrome binary (see _resolve_full_chromium).
_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-background-networking",
    "--no-first-run",
    "--no-default-browser-check",
]


def _resolve_full_chromium() -> str:
    """Path to Playwright's FULL chromium binary (``chrome.exe`` / ``chrome``),
    NOT the ``chrome-headless-shell`` that ``headless=True`` uses by default.

    On some hosts a security suite blocks ``headless_shell.exe`` from the network
    (every navigation then times out) while the full ``chrome.exe`` is allowed —
    so we point Playwright at the full binary via ``executable_path``. Globs the
    revision dir so it keeps working after Playwright downloads a newer chromium.
    """
    bases: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        bases.append(Path(local) / "ms-playwright")
    bases.append(Path.home() / ".cache" / "ms-playwright")   # linux / mac
    patterns = (
        "chromium-*/chrome-win64/chrome.exe",
        "chromium-*/chrome-win/chrome.exe",
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
    )
    hits: list[str] = []
    for base in bases:
        for pattern in patterns:
            hits.extend(glob.glob(str(base / pattern)))
    if not hits:
        return ""

    def _revision(path: str) -> int:
        match = re.search(r"chromium-(\d+)", path)
        return int(match.group(1)) if match else 0

    return max(hits, key=_revision)

_DOM_SNAPSHOT_JS = r"""() => {
    const visible = (el) => {
        if (el.type === 'hidden') return false;
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) return false;
        if (r.bottom < 0 || r.right < 0 ||
            r.top > (innerHeight || 1e9) || r.left > (innerWidth || 1e9)) return false;
        const s = getComputedStyle(el);
        if (s.visibility === 'hidden' || s.display === 'none') return false;
        if (parseFloat(s.opacity || '1') < 0.05) return false;
        return true;
    };
    const sel = 'a[href], button, input, textarea, select, [role=button],'
        + ' [role=link], [role=checkbox], [role=tab], [onclick],'
        + ' [contenteditable=""], [contenteditable=true]';
    const els = Array.from(document.querySelectorAll(sel));
    const out = [];
    let i = 0;
    for (const el of els) {
        if (!visible(el)) continue;
        el.setAttribute('data-pw-idx', String(i));
        const r = el.getBoundingClientRect();
        const text = (el.innerText || el.value || el.placeholder ||
                      el.getAttribute('aria-label') || el.name || '').trim();
        out.push({ index: i, tag: el.tagName.toLowerCase(),
                   type: el.type || el.getAttribute('role') || null,
                   text: text.slice(0, 60), href: el.href || null,
                   cx: Math.round(r.left + r.width / 2),
                   cy: Math.round(r.top + r.height / 2) });
        i++;
        if (i >= 120) break;
    }
    return out;
}"""

_GRID_ADD_JS = r"""() => {
    const id = '__lma_grid__';
    const old = document.getElementById(id); if (old) old.remove();
    const d = document.createElement('div');
    d.id = id;
    d.style.cssText = 'position:fixed;left:0;top:0;width:100vw;height:100vh;'
        + 'z-index:2147483647;pointer-events:none;';
    const W = window.innerWidth, H = window.innerHeight;
    let html = '';
    for (let x = 0; x <= W; x += 100) {
        html += '<div style="position:absolute;left:' + x + 'px;top:0;width:1px;'
            + 'height:100%;background:rgba(255,0,0,.35)"></div>';
        html += '<div style="position:absolute;left:' + (x+2) + 'px;top:1px;'
            + 'font:11px monospace;color:#c00;background:rgba(255,255,255,.75)">' + x + '</div>';
    }
    for (let y = 0; y <= H; y += 100) {
        html += '<div style="position:absolute;top:' + y + 'px;left:0;height:1px;'
            + 'width:100%;background:rgba(255,0,0,.35)"></div>';
        html += '<div style="position:absolute;top:' + (y+2) + 'px;left:1px;'
            + 'font:11px monospace;color:#c00;background:rgba(255,255,255,.75)">' + y + '</div>';
    }
    d.innerHTML = html;
    (document.body || document.documentElement).appendChild(d);
}"""

_GRID_REMOVE_JS = ("() => { const e = document.getElementById('__lma_grid__');"
                   " if (e) e.remove(); }")


def _context_dead(context: Any) -> bool:
    """Playwright 对象挂掉后调用其属性会抛（Target closed 等）。"""
    try:
        _ = context.pages
        return False
    except Exception:
        return True


def _closed(page: Any) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:
        return True


class PlaywrightDriver:
    """Single shared headless-chromium page with lazy startup.

    When ``user_data_dir`` is given the page runs inside a persistent context so
    cookies / sessions are kept on disk between runs; otherwise it falls back to
    an ephemeral context (used in tests and when persistence is disabled).
    """

    def __init__(self, *, user_data_dir: str | Path | None = None,
                 timeout: float = 30.0,
                 executable_path: str | Path | None = None) -> None:
        self._timeout = max(5.0, float(timeout))
        self._user_data_dir = str(user_data_dir) if user_data_dir else ""
        self._executable_path = str(executable_path) if executable_path else ""
        self._pw: Any = None
        self._context: Any = None       # BrowserContext (persistent or ephemeral)
        self._browser: Any = None        # only set for the ephemeral fallback
        self._page: Any = None
        self._lock = asyncio.Lock()
        self.last_nav_error = ""   # why a live navigation failed (for diagnostics)
        # 守护统计（参考 browser-main 的 supervisor）：重启次数与最近一次恢复原因
        self.restart_count = 0
        self.last_recovery_reason = ""
        self.started_at = ""
        # index → {"cx","cy"} from the most recent dom_snapshot; survives SPA
        # re-renders that wipe data-pw-idx attributes, so fill/click can fall
        # back to snapshot-time coordinates instead of failing on the selector.
        self._last_boxes: dict[int, dict[str, float]] = {}

    def _chromium_executable(self) -> str:
        """Configured full-chromium path, else auto-resolved; "" if none usable."""
        exe = self._executable_path or _resolve_full_chromium()
        if exe and not os.path.exists(exe):
            exe = ""
        return exe

    async def _launch_context(self) -> Any:
        """Persistent context when a profile dir is set, else ephemeral.

        Prefer launching the FULL chromium (executable_path) so we don't hit the
        headless_shell binary that some security suites block; fall back to the
        default headless_shell launch if that specific binary can't start.
        """
        viewport = {"width": 1280, "height": 860}
        exe = self._chromium_executable()
        if self._user_data_dir:
            try:
                Path(self._user_data_dir).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            launch = self._pw.chromium.launch_persistent_context
            if exe:
                try:
                    return await launch(
                        self._user_data_dir, executable_path=exe, headless=True,
                        args=_LAUNCH_ARGS, viewport=viewport, user_agent=_USER_AGENT)
                except Exception:
                    pass  # bad/incompatible binary → default launch below
            return await launch(
                self._user_data_dir, headless=True, args=_LAUNCH_ARGS,
                viewport=viewport, user_agent=_USER_AGENT)
        browser = None
        if exe:
            try:
                browser = await self._pw.chromium.launch(
                    executable_path=exe, headless=True, args=_LAUNCH_ARGS)
            except Exception:
                browser = None
        if browser is None:
            browser = await self._pw.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        self._browser = browser
        return await browser.new_context(viewport=viewport, user_agent=_USER_AGENT)

    async def _page_or_raise(self) -> Any:
        if self._page is not None and not _closed(self._page):
            return self._page
        # 守护自愈（browser-main supervisor 思路）：context 挂了/页面全关时，
        # 记录原因并整体重启一次，而不是把 BrowserError 抛给调用方
        if self._context is not None and _context_dead(self._context):
            self.last_recovery_reason = "context closed"
            self.restart_count += 1
            self.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            logger.warning("长程记忆：浏览器内核疑似挂掉（第%d次自动恢复）",
                           self.restart_count)
            await self._reset()
        async with self._lock:
            if self._page is not None and not _closed(self._page):
                return self._page
            self._page = None
            try:
                from playwright.async_api import async_playwright
            except Exception as error:
                raise BrowserError(
                    "playwright 未安装（重启插件后后台会自动安装，"
                    "或在日志里查看手动安装说明）") from error
            try:
                if self._pw is None:
                    self._pw = await async_playwright().start()
                if self._context is None:
                    self._context = await self._launch_context()
                self._page = await self._acquire_page()
                self._page.set_default_timeout(int(self._timeout * 1000))
            except BrowserError:
                raise
            except Exception as error:
                # The persistent profile may be locked/corrupt or the kernel
                # crashed: drop everything and try one clean relaunch.
                await self._reset()
                try:
                    if self._pw is None:
                        self._pw = await async_playwright().start()
                    self._context = await self._launch_context()
                    self._page = await self._acquire_page()
                    self._page.set_default_timeout(int(self._timeout * 1000))
                except Exception as retry_error:
                    await self._reset()
                    raise BrowserError(
                        "浏览器内核启动失败（可能 chromium 未下载，"
                        "重启插件后台会自动安装）："
                        f"{str(retry_error or error)[:160]}") from retry_error
        return self._page

    async def _acquire_page(self) -> Any:
        """Reuse the context's existing blank page (persistent contexts open
        one automatically) instead of piling up tabs across restarts."""
        try:
            for page in self._context.pages:
                if not _closed(page):
                    return page
        except Exception:
            pass
        return await self._context.new_page()

    async def goto(self, url: str) -> dict[str, Any]:
        """Slow sites must not stall 30s: commit as soon as the server answers,
        then a short settle for the first paint."""
        page = await self._page_or_raise()
        try:
            await page.goto(assert_browsable(url), wait_until="commit",
                            timeout=int(self._timeout * 1000))
        except Exception as error:
            raise BrowserError(
                f"页面打不开（站点无响应或被墙）：{str(error)[:140]}") from error
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass  # slow site: work with whatever has rendered so far
        await page.wait_for_timeout(900)
        return {"url": page.url, "title": await page.title()}

    async def dom_snapshot(self, limit: int = 60) -> dict[str, Any]:
        """Interactive-element snapshot; each element gets a stable data-pw-idx."""
        page = await self._page_or_raise()
        items = await page.evaluate(_DOM_SNAPSHOT_JS)
        self._last_boxes = {
            int(item.get("index")): {"cx": float(item.get("cx") or 0), "cy": float(item.get("cy") or 0)}
            for item in items[: max(1, int(limit))]
            if item.get("index") is not None
        }
        return {
            "url": page.url,
            "title": await page.title(),
            "elements": items[: max(1, int(limit))],
            "hint": "只列可见可交互元素；每项带中心坐标 cx,cy（视口像素，可直接喂 browser_click_xy/browser_slide）。用 browser_click_element(index) 点击、browser_type_text(index, text) 输入",
        }

    async def click(self, index: int) -> dict[str, Any]:
        page = await self._page_or_raise()
        selector = f'[data-pw-idx="{int(index)}"]'
        try:
            await page.click(selector, timeout=_ACTION_TIMEOUT_MS)
        except Exception:
            # 同 fill：SPA 重渲染后按快照坐标点中心
            box = self._last_boxes.get(int(index))
            if box is None:
                raise BrowserError(
                    "点击失败且没有缓存的元素坐标（先 browser_dom 刷新）") from None
            try:
                await page.mouse.click(float(box["cx"]), float(box["cy"]))
            except Exception as error:
                raise BrowserError(
                    f"点击失败（先 browser_dom 刷新元素序号）：{str(error)[:140]}") from error
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            pass
        await page.wait_for_timeout(500)
        body = ""
        try:
            body = (await page.inner_text("body"))[:1500]
        except Exception:
            pass
        return {"url": page.url, "title": await page.title(), "text": body}

    async def fill(self, index: int, text: str, submit: bool = False) -> dict[str, Any]:
        page = await self._page_or_raise()
        selector = f'[data-pw-idx="{int(index)}"]'
        try:
            await page.fill(selector, str(text), timeout=_ACTION_TIMEOUT_MS)
        except Exception:
            # SPA 重渲染会清掉 data-pw-idx，属性定位器直接超时。兜底：用上次
            # 快照缓存 的中心坐标点一下，再键盘输入——坐标比选择器抗重渲染。
            box = self._last_boxes.get(int(index))
            if box is None:
                raise BrowserError(
                    "输入失败且没有缓存的元素坐标（先 browser_dom 刷新）") from None
            try:
                await page.mouse.click(float(box["cx"]), float(box["cy"]))
                await page.wait_for_timeout(150)
                await page.keyboard.insert_text(str(text))
            except Exception as error:
                raise BrowserError(
                    f"输入失败（先 browser_dom 刷新元素序号）：{str(error)[:140]}") from error
        try:
            if submit:
                await page.keyboard.press("Enter")
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                await page.wait_for_timeout(600)
        except Exception as error:
            raise BrowserError(
                f"输入成功但提交失败：{str(error)[:140]}") from error
        body = ""
        try:
            body = (await page.inner_text("body"))[:1500]
        except Exception:
            pass
        return {"url": page.url, "title": await page.title(), "text": body}

    async def screenshot(self, url: str | None = None, full_page: bool = False,
                         *, fallback_html: str | None = None,
                         base_url: str | None = None, grid: bool = False) -> bytes:
        """commit + settle like goto; on slow-load still capture what rendered.

        If the live navigation can't connect (some sites are reachable by a
        plain HTTP fetch but not by the browser's network stack), and the caller
        handed us the already-fetched HTML, render that instead so a screenshot
        is still produced rather than a hard failure.
        """
        page = await self._page_or_raise()
        self.last_nav_error = ""
        # 无导航 + 带 fallback_html：直接在全新页渲染（url=None 时 fallback_html
        # 过去根本不生效——截的是当前页＝纯白图，designer/直通画图全中招）
        if not url and fallback_html:
            return await self._screenshot_html(fallback_html, base_url, full_page)
        if url:
            # Capture the concrete chromium net error of the main navigation:
            # a bare goto timeout says "no response in 15s" but not why, whereas
            # requestfailed carries net::ERR_PROXY_CONNECTION_FAILED /
            # ERR_NAME_NOT_RESOLVED / ERR_TIMED_OUT etc. — the decisive clue.
            nav_failures: list[str] = []

            def _on_failed(request: Any) -> None:
                try:
                    if request.is_navigation_request():
                        nav_failures.append(f"{request.failure or ''} {request.url}".strip())
                except Exception:
                    pass

            try:
                page.on("requestfailed", _on_failed)
            except Exception:
                pass
            # fail fast so the fallback isn't gated behind the full 30s timeout
            nav_timeout = int(min(self._timeout, 15.0) * 1000)
            try:
                await page.goto(assert_browsable(url), wait_until="commit",
                                timeout=nav_timeout)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass  # slow site: capture whatever rendered
                await page.wait_for_timeout(900)
            except Exception as error:
                detail = nav_failures[-1] if nav_failures else str(error).splitlines()[0]
                self.last_nav_error = str(detail)[:160]
                if not fallback_html:
                    raise BrowserError(
                        f"页面打不开（站点无响应或被墙）：{self.last_nav_error}") from error
                # The failed navigation leaves this page mid-flight, so calling
                # set_content on it dies with "Execution context was destroyed".
                # Render the fetched HTML on a FRESH page instead.
                return await self._screenshot_html(
                    fallback_html, base_url or url, full_page)
            finally:
                try:
                    page.remove_listener("requestfailed", _on_failed)
                except Exception:
                    pass
        if grid:
            return await self._screenshot_with_grid(page, full_page)
        return await page.screenshot(full_page=bool(full_page))

    @staticmethod
    def _with_base(html: str, base_url: str | None) -> str:
        """Inject <base href> so relative CSS/images can resolve."""
        markup = str(html or "")
        if not base_url or "<base " in markup[:4000].lower():
            return markup
        tag = f'<base href="{base_url}">'
        lower = markup.lower()
        head = lower.find("<head")
        if head >= 0:
            insert = lower.find(">", head)
            if insert >= 0:
                return markup[:insert + 1] + tag + markup[insert + 1:]
        return tag + markup

    async def _screenshot_html(self, html: str, base_url: str | None,
                               full_page: bool) -> bytes:
        """Render caller-supplied HTML on a fresh page and screenshot it.

        A fresh page has no in-flight navigation (avoids "Execution context was
        destroyed"), and ``wait_until="commit"`` returns without waiting for the
        "load" event (avoids the 8s timeout when external resources can't load).
        """
        if self._context is None:
            raise BrowserError("浏览器上下文不可用")
        markup = self._with_base(html, base_url)
        render_page = await self._context.new_page()
        try:
            render_page.set_default_timeout(int(self._timeout * 1000))
            try:
                await render_page.set_content(markup, wait_until="commit", timeout=8000)
            except Exception:
                await render_page.set_content(
                    markup, wait_until="domcontentloaded", timeout=8000)
            await render_page.wait_for_timeout(500)
            # 内容自适应：HTML 标了 data-shot-fit 的元素（聊天卡片就是）就按它的
            # 实际尺寸定视口，再截图——否则固定 1280 宽的视口会把 520px 宽的卡片
            # 塞在左上角，其余全白（用户：「消息只占左上角一点点」）。
            box = await self._measure_fit(render_page)
            if box:
                # 窄卡片（聊天卡片 520px）放 2 倍：图片尺寸接近手机截图，字也清晰
                zoom = 2 if box[0] <= 700 else 1
                if zoom > 1:
                    try:
                        await render_page.evaluate(
                            f"document.body.style.zoom = '{zoom}'")
                        await render_page.wait_for_timeout(100)
                        box = (box[0] * zoom, box[1] * zoom)
                    except Exception:
                        pass
                width = max(240, min(2400, box[0]))
                height = max(160, min(24000, box[1]))
                try:
                    await render_page.set_viewport_size(
                        {"width": int(width), "height": int(height)})
                    await render_page.wait_for_timeout(120)
                except Exception:
                    pass
                return await render_page.screenshot()
            return await render_page.screenshot(full_page=bool(full_page))
        finally:
            try:
                await render_page.close()
            except Exception:
                pass

    @staticmethod
    async def _measure_fit(page: Any) -> tuple[float, float] | None:
        """量 data-shot-fit 元素的外框（含外边距），拿不到返回 None。"""
        script = """() => {
            const node = document.querySelector('[data-shot-fit]');
            if (!node) return null;
            const rect = node.getBoundingClientRect();
            if (!rect || rect.width < 1 || rect.height < 1) return null;
            const style = getComputedStyle(node);
            const extra = ['marginTop', 'marginBottom', 'marginLeft', 'marginRight']
                .map((key) => parseFloat(style[key]) || 0);
            return [rect.width + extra[2] + extra[3], rect.height + extra[0] + extra[1]];
        }"""
        try:
            box = await page.evaluate(script)
        except Exception:
            return None
        if not box or len(box) != 2:
            return None
        try:
            return float(box[0]), float(box[1])
        except (TypeError, ValueError):
            return None


    async def _screenshot_with_grid(self, page: Any, full_page: bool) -> bytes:
        """Screenshot with a temporary coordinate grid overlaid, so the vision
        model can read approximate x/y for captcha clicks/slides, then removed."""
        try:
            await page.evaluate(_GRID_ADD_JS)
        except Exception:
            pass
        try:
            return await page.screenshot(full_page=bool(full_page))
        finally:
            try:
                await page.evaluate(_GRID_REMOVE_JS)
            except Exception:
                pass

    async def slide(self, x1: float, y1: float, x2: float, y2: float,
                    *, steps: int = 30) -> dict[str, Any]:
        """Human-like sustained drag from (x1,y1) to (x2,y2) — for slider captchas.

        Presses at the start, moves in eased steps with小 timing/貼 y jitter (so the
        anti-bot sees non-linear, human-ish motion), then releases.
        """
        page = await self._page_or_raise()
        sx, sy, ex, ey = float(x1), float(y1), float(x2), float(y2)
        try:
            await page.mouse.move(sx, sy)
            await page.mouse.down()
            count = max(8, min(int(steps), 120))
            dx, dy = ex - sx, ey - sy
            for i in range(1, count + 1):
                t = i / count
                ease = 1 - (1 - t) * (1 - t)   # ease-out: fast then settle
                await page.mouse.move(sx + dx * ease,
                                      sy + dy * ease + random.uniform(-1.2, 1.2))
                await page.wait_for_timeout(random.randint(8, 26))
            await page.mouse.up()
        except Exception as error:
            try:
                await page.mouse.up()
            except Exception:
                pass
            raise BrowserError(f"滑动失败：{str(error)[:140]}") from error
        await page.wait_for_timeout(800)
        return {"url": page.url, "title": await page.title()}

    async def click_xy(self, x: float, y: float) -> dict[str, Any]:
        """Click at absolute viewport pixel (for captcha targets not in the DOM)."""
        page = await self._page_or_raise()
        try:
            await page.mouse.click(float(x), float(y))
        except Exception as error:
            raise BrowserError(f"坐标点击失败：{str(error)[:140]}") from error
        await page.wait_for_timeout(500)
        return {"url": page.url, "title": await page.title()}

    async def element_box(self, index: int) -> dict[str, Any]:
        """Bounding box + center of the N-th data-pw-idx element (from browser_dom)."""
        page = await self._page_or_raise()
        selector = f'[data-pw-idx="{int(index)}"]'
        try:
            return await page.eval_on_selector(
                selector,
                "el => { const r = el.getBoundingClientRect();"
                " return {x:r.x, y:r.y, w:r.width, h:r.height,"
                " cx:r.x + r.width/2, cy:r.y + r.height/2}; }")
        except Exception as error:
            raise BrowserError(
                f"取元素坐标失败（先 browser_dom 刷新序号）：{str(error)[:120]}") from error

    async def drag_element(self, index: int, dx: float, dy: float = 0.0,
                           *, steps: int = 30) -> dict[str, Any]:
        """Press the N-th element's center and drag by (dx,dy) — slider handle in DOM."""
        box = await self.element_box(index)
        cx, cy = float(box["cx"]), float(box["cy"])
        return await self.slide(cx, cy, cx + float(dx), cy + float(dy), steps=steps)

    async def scroll(self, amount: int = 600, *, to_bottom: bool = False,
                     index: int | None = None) -> dict[str, Any]:
        """Scroll the page: wheel by ``amount`` px (negative = up), jump to the
        bottom, or center the N-th data-pw-idx element. DOM snapshots only list
        viewport-visible elements, so long pages need this between snapshots."""
        page = await self._page_or_raise()
        if index is not None and int(index) >= 0:
            selector = f'[data-pw-idx="{int(index)}"]'
            try:
                await page.eval_on_selector(
                    selector,
                    "el => el.scrollIntoView({block: 'center', behavior: 'instant'})")
            except Exception as error:
                raise BrowserError(
                    f"滚动到元素失败（先 browser_dom 刷新序号）：{str(error)[:140]}") from error
        elif to_bottom:
            await page.evaluate(
                "window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'})")
        else:
            try:
                await page.mouse.wheel(0, int(amount))
            except Exception as error:
                raise BrowserError(f"滚动失败：{str(error)[:140]}") from error
        await page.wait_for_timeout(400)
        state: dict[str, Any] = {
            "scroll_y": 0, "page_height": 0, "viewport": 0, "at_bottom": False,
        }
        try:
            state["scroll_y"] = int(await page.evaluate("window.scrollY") or 0)
            state["page_height"] = int(await page.evaluate("document.body.scrollHeight") or 0)
            state["viewport"] = int(await page.evaluate("window.innerHeight") or 0)
            state["at_bottom"] = (
                state["scroll_y"] + state["viewport"] >= state["page_height"] - 4
            )
        except Exception:
            pass
        return state

    async def _reset(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    await closer.close()
            except Exception:
                pass
        self._context = self._browser = self._page = None

    async def close(self) -> None:
        await self._reset()
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = None
