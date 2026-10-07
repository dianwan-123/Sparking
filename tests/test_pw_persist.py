# -*- coding: utf-8 -*-
"""PlaywrightDriver 持久化测试：注入假 playwright，验证用磁盘用户数据目录启动。"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

from src.browser import BrowserError
from src.pw_driver import PlaywrightDriver


class FakeMouse:
    def __init__(self, rec):
        self._rec = rec
        self._rec.setdefault("moves", [])
        self._rec.setdefault("wheels", [])

    async def move(self, x, y, **kwargs):
        self._rec["moves"].append((x, y))

    async def down(self, **kwargs):
        self._rec["mouse_down"] = True

    async def up(self, **kwargs):
        self._rec["mouse_up"] = True

    async def click(self, x, y, **kwargs):
        self._rec["click_xy"] = (x, y)

    async def wheel(self, dx, dy):
        self._rec["wheels"].append((dx, dy))


class FakeKeyboard:
    def __init__(self, rec):
        self._rec = rec

    async def insert_text(self, text):
        self._rec.setdefault("inserted", []).append(text)

    async def press(self, key):
        self._rec.setdefault("pressed", []).append(key)


class FakePage:
    def __init__(self, rec=None):
        self._closed = False
        self.timeout = None
        self.url = "https://fake.example/"
        self._rec = rec if rec is not None else {}
        self.mouse = FakeMouse(self._rec)
        self.keyboard = FakeKeyboard(self._rec)

    def is_closed(self):
        return self._closed

    def set_default_timeout(self, ms):
        self.timeout = ms

    def on(self, event, handler):
        return None

    def remove_listener(self, event, handler):
        return None

    async def evaluate(self, script, *args):
        self._rec.setdefault("evaluate", []).append(script[:40])
        return None

    async def eval_on_selector(self, selector, script, *args):
        self._rec["eval_selector"] = selector
        if self._rec.get("eval_fails"):
            raise RuntimeError("element not found")
        return {"x": 100, "y": 500, "w": 40, "h": 40, "cx": 120, "cy": 520}

    async def fill(self, selector, text, **kwargs):
        self._rec.setdefault("fills", []).append((selector, text))
        if self._rec.get("fill_fails"):
            raise RuntimeError("Page.fill: Timeout 8000ms exceeded.")
        return None

    async def title(self):
        return "t"

    async def goto(self, url, **kwargs):
        self._rec.setdefault("goto", []).append(url)
        if self._rec.get("goto_fails"):
            raise RuntimeError("net::ERR_TIMED_OUT")
        return None

    async def wait_for_load_state(self, *args, **kwargs):
        return None

    async def wait_for_timeout(self, ms):
        return None

    async def set_content(self, html, **kwargs):
        self._rec["set_content"] = html
        return None

    async def screenshot(self, **kwargs):
        return b"PNGDATA"

    async def close(self):
        self._closed = True
        return None


class FakeContext:
    def __init__(self, persistent, rec):
        self.persistent = persistent
        self.rec = rec
        self.closed = False
        # a persistent context auto-opens one blank page (like real chromium)
        self.pages = [FakePage(rec)] if persistent else []

    async def new_page(self):
        page = FakePage(self.rec)
        self.pages.append(page)
        self.rec["new_page"] += 1
        return page

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, rec):
        self.rec = rec
        self.closed = False

    async def new_context(self, **kwargs):
        return FakeContext(False, self.rec)

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, rec):
        self.rec = rec

    async def launch_persistent_context(self, user_data_dir, **kwargs):
        self.rec["persistent_dir"] = user_data_dir
        self.rec["persistent_kwargs"] = kwargs
        self.rec["executable_path"] = kwargs.get("executable_path")
        return FakeContext(True, self.rec)

    async def launch(self, **kwargs):
        self.rec["ephemeral"] = True
        self.rec["executable_path"] = kwargs.get("executable_path")
        return FakeBrowser(self.rec)


class FakePW:
    def __init__(self, rec):
        self.chromium = FakeChromium(rec)
        self.stopped = False

    async def stop(self):
        self.stopped = True


class FakeAsyncPlaywright:
    def __init__(self, rec):
        self.rec = rec

    async def start(self):
        return FakePW(self.rec)


class PersistentDriverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rec = {"new_page": 0}
        self._saved = {name: sys.modules.get(name)
                       for name in ("playwright", "playwright.async_api")}
        pkg = types.ModuleType("playwright")
        mod = types.ModuleType("playwright.async_api")
        mod.async_playwright = lambda: FakeAsyncPlaywright(self.rec)
        sys.modules["playwright"] = pkg
        sys.modules["playwright.async_api"] = mod
        self.addCleanup(self._restore)

    def _restore(self):
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    async def test_persistent_profile_used_and_page_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "browser_profile"
            driver = PlaywrightDriver(user_data_dir=profile)
            page = await driver._page_or_raise()
            self.assertEqual(str(profile), self.rec["persistent_dir"])
            self.assertTrue(self.rec["persistent_kwargs"]["headless"])
            self.assertEqual({"width": 1280, "height": 860},
                             self.rec["persistent_kwargs"]["viewport"])
            # 持久化上下文自带一页 → 复用而不是新开
            self.assertEqual(0, self.rec["new_page"])
            # 磁盘 profile 目录被建出来（cookies 等就存这里）
            self.assertTrue(profile.exists())
            # 再次取用返回同一页（单页复用）
            self.assertIs(page, await driver._page_or_raise())
            await driver.close()

    async def test_ephemeral_when_no_profile(self):
        driver = PlaywrightDriver(user_data_dir=None)
        await driver._page_or_raise()
        self.assertTrue(self.rec.get("ephemeral"))
        self.assertIsNone(self.rec.get("persistent_dir"))
        # 临时上下文无自带页 → 需要新开一页
        self.assertEqual(1, self.rec["new_page"])
        await driver.close()

    async def test_fill_falls_back_to_cached_coordinates(self):
        """回归（豆包任务实录）：SPA 重渲染清掉 data-pw-idx，fill 超时——
        应按快照缓存坐标点中心+键盘输入，而不是报错让模型以为没有浏览器。"""
        driver = PlaywrightDriver(user_data_dir=None)
        await driver._page_or_raise()
        driver._last_boxes[7] = {"cx": 780.0, "cy": 772.0}
        self.rec["fill_fails"] = True
        await driver.fill(7, "你是什么模型", submit=True)
        self.assertEqual((780.0, 772.0), self.rec["click_xy"])
        self.assertEqual(["你是什么模型"], self.rec["inserted"])
        self.assertEqual(["Enter"], self.rec["pressed"])
        await driver.close()

    async def test_fill_without_cached_box_raises_actionable_error(self):
        driver = PlaywrightDriver(user_data_dir=None)
        await driver._page_or_raise()
        self.rec["fill_fails"] = True
        with self.assertRaises(BrowserError):
            await driver.fill(7, "x")
        await driver.close()

    async def test_scroll_wheel_bottom_and_element(self):
        """滚动三形态：滚轮像素 / 滚到底 / 滚到某元素居中；并回传滚动状态。"""
        driver = PlaywrightDriver(user_data_dir=None)
        await driver._page_or_raise()
        # 滚轮：wheel 事件被记录
        state = await driver.scroll(800)
        self.assertEqual([(0, 800)], self.rec["wheels"])
        self.assertIn("scroll_y", state)
        # 负数向上
        await driver.scroll(-400)
        self.assertEqual([(0, 800), (0, -400)], self.rec["wheels"])
        # 滚到底：走 evaluate 的 scrollTo，不触发 wheel
        wheels_before = len(self.rec["wheels"])
        await driver.scroll(to_bottom=True)
        self.assertEqual(wheels_before, len(self.rec["wheels"]))
        self.assertTrue(any("scrollTo" in s for s in self.rec["evaluate"]))
        # 滚到元素：eval_on_selector 用 data-pw-idx 定位
        await driver.scroll(index=3)
        self.assertEqual('[data-pw-idx="3"]', self.rec["eval_selector"])
        await driver.close()

    async def test_screenshot_falls_back_to_fetched_html(self):
        """站点浏览器直连打不开、但已有抓到的 HTML 时，用 set_content 渲染出图，
        而不是硬失败（正是 browse 成、screenshot 挂的场景）。"""
        self.rec["goto_fails"] = True
        driver = PlaywrightDriver(user_data_dir=None)
        png = await driver.screenshot(
            "https://math.bewiki.site",
            fallback_html="<html><head><title>t</title></head><body>hi</body></html>",
            base_url="https://math.bewiki.site")
        self.assertEqual(b"PNGDATA", png)
        # 渲染走了 set_content，并注入了 <base> 供相对资源解析
        self.assertIn("set_content", self.rec)
        self.assertIn('<base href="https://math.bewiki.site">', self.rec["set_content"])
        # 导航失败原因被记下，供工具回传/日志（诊断真实 net 错误）
        self.assertIn("ERR", driver.last_nav_error)
        await driver.close()

    async def test_screenshot_raises_without_fallback(self):
        from src.browser import BrowserError
        self.rec["goto_fails"] = True
        driver = PlaywrightDriver(user_data_dir=None)
        with self.assertRaises(BrowserError):
            await driver.screenshot("https://math.bewiki.site")
        await driver.close()

    async def test_configured_executable_path_is_used(self):
        """指定完整 chrome.exe 时，Playwright 用它启动（绕开被拦的 headless_shell）。"""
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "chrome.exe"
            exe.write_bytes(b"stub")
            driver = PlaywrightDriver(user_data_dir=None, executable_path=str(exe))
            await driver._page_or_raise()
            self.assertEqual(str(exe), self.rec.get("executable_path"))
            await driver.close()

    async def test_missing_executable_path_is_ignored(self):
        """指定路径不存在时安静忽略，回退默认启动（不崩）。"""
        driver = PlaywrightDriver(user_data_dir=None,
                                  executable_path=r"Z:\nope\chrome.exe")
        await driver._page_or_raise()
        self.assertIsNone(self.rec.get("executable_path"))
        await driver.close()

    async def test_slide_drags_and_releases(self):
        driver = PlaywrightDriver(user_data_dir=None)
        result = await driver.slide(100, 500, 300, 500, steps=10)
        self.assertTrue(self.rec.get("mouse_down"))
        self.assertTrue(self.rec.get("mouse_up"))
        moves = self.rec.get("moves", [])
        self.assertGreater(len(moves), 5)
        self.assertAlmostEqual(moves[-1][0], 300, delta=2)   # ends at target x
        self.assertEqual("t", result["title"])
        await driver.close()

    async def test_click_xy(self):
        driver = PlaywrightDriver(user_data_dir=None)
        await driver.click_xy(42, 84)
        self.assertEqual((42.0, 84.0), self.rec.get("click_xy"))
        await driver.close()

    async def test_grid_shot_overlays_and_removes(self):
        driver = PlaywrightDriver(user_data_dir=None)
        png = await driver.screenshot(None, grid=True)
        self.assertEqual(b"PNGDATA", png)
        self.assertGreaterEqual(len(self.rec.get("evaluate", [])), 2)  # add + remove
        await driver.close()

    async def test_drag_element_uses_box_center(self):
        driver = PlaywrightDriver(user_data_dir=None)
        await driver.drag_element(3, 150, 0, steps=8)
        self.assertEqual('[data-pw-idx="3"]', self.rec.get("eval_selector"))
        moves = self.rec.get("moves", [])
        self.assertAlmostEqual(moves[0][0], 120, delta=1)    # box center cx=120
        self.assertAlmostEqual(moves[-1][0], 270, delta=2)   # 120 + 150
        await driver.close()


if __name__ == "__main__":
    unittest.main()
