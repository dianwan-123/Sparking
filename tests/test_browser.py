# -*- coding: utf-8 -*-
"""浏览器引擎与网络学习功能的测试（零第三方依赖）。"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from src.browser import (
    BrowserError,
    BrowserSession,
    assert_browsable,
    parse_html,
    page_text,
)

PAGE = """<html><head><title>示例新闻</title><style>b{}</style></head>
<body>
<h1>今日要闻</h1>
<p>第一段正文，包含关键词 量子计算。</p>
<a href="/detail/1">阅读详情一</a>
<a href="https://other.example.com/b">外站链接</a>
<form action="/search" method="get">
  <input name="q" placeholder="搜索"/>
</form>
<script>console.log('x')</script>
</body></html>"""


class ParserTests(unittest.TestCase):
    def test_extracts_title_text_links_forms(self):
        title, text, links, forms = parse_html(PAGE, "https://example.com/news")
        self.assertEqual("示例新闻", title)
        self.assertIn("今日要闻", text)
        self.assertIn("量子计算", text)
        self.assertNotIn("console.log", text)   # script stripped
        self.assertNotIn("b{}", text)           # style stripped
        self.assertEqual(2, len(links))
        self.assertEqual("https://example.com/detail/1", links[0].href)
        self.assertEqual("https://other.example.com/b", links[1].href)
        self.assertEqual(0, links[0].index)
        self.assertEqual(1, len(forms))
        self.assertEqual("get", forms[0].method)
        self.assertEqual("q", forms[0].fields[0].name)
        self.assertEqual("搜索", forms[0].fields[0].placeholder)

    def test_relative_and_absolute_urls_are_normalised(self):
        _, _, links, _ = parse_html(
            '<a href="../x">相对</a><a href="//cdn.example.com/y">协议相对</a>',
            "https://example.com/a/b/c")
        self.assertEqual("https://example.com/a/x", links[0].href)
        self.assertEqual("https://cdn.example.com/y", links[1].href)

    def test_ssrf_guard_blocks_private_targets(self):
        for url in ("http://127.0.0.1/admin", "http://localhost/x",
                    "http://192.168.1.1/", "file:///etc/passwd",
                    "http://[::1]/"):
            with self.assertRaises(BrowserError):
                assert_browsable(url)

    def test_page_text_includes_title_and_url(self):
        title, text, links, forms = parse_html(PAGE, "https://example.com/n")
        from src.browser import Page

        page = Page(url="https://example.com/n", status=200, title=title, text=text,
                    links=links, forms=forms)
        out = page_text(page)
        self.assertIn("示例新闻", out)
        self.assertIn("https://example.com/n", out)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def _fake_fetch(self, session, html, url):
        """Install a fake transport so tests never touch the network."""

        def _sync(target, method="GET", payload=None):
            _, text, links, forms = parse_html(html, target)
            from src.browser import Page

            return Page(url=target, status=200, title="示例", text=text,
                        links=links, forms=forms), "ok"

        session._fetch_sync = _sync  # type: ignore[assignment]

    async def test_tabs_history_and_click(self):
        session = BrowserSession()
        await self._fake_fetch(session, PAGE, "https://example.com/")
        first = await session.fetch("https://example.com/")
        self.assertEqual("示例", first.title)

        tabs = session.tabs()
        self.assertEqual(1, len(tabs["tabs"]))
        self.assertEqual(1, tabs["active"])

        link = session.link(0)
        self.assertIn("detail/1", link.href)

        # clicking a link pushes a new history entry, then back/forward move
        await session.fetch(link.href)
        self.assertEqual(2, session.tabs()["tabs"][0]["history_len"])
        self.assertEqual("https://example.com/", session.back())
        self.assertIn("detail/1", session.forward())

        # a second tab is independent
        tab = session.new_tab()
        await session.fetch("https://example.com/other", tab_id=tab.tab_id)
        self.assertEqual(2, len(session.tabs()["tabs"]))
        self.assertEqual(tab.tab_id, session.tabs()["active"])

    async def test_snapshot_shape(self):
        session = BrowserSession()
        await self._fake_fetch(session, PAGE, "https://example.com/")
        await session.fetch("https://example.com/")
        snap = session.snapshot()
        for key in ("tab_id", "url", "title", "text", "links", "forms"):
            self.assertIn(key, snap)
        self.assertEqual(2, snap["link_count"])

    async def test_empty_tab_snapshot_is_graceful(self):
        session = BrowserSession()
        snap = session.snapshot()
        self.assertIn("note", snap)


class LearningFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def _hooks(self):
        import sys

        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import tests.test_plugin_hooks as hooks

        return hooks

    async def test_browser_tools_are_defined_on_the_plugin(self):
        """工具以 @filter.llm_tool 注册；桩环境直接检查方法存在。"""
        hooks = self._hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config()
        )
        for name in ("browse_tool", "browser_links_tool", "browser_click_tool",
                     "browser_form_tool", "browser_back_tool",
                     "browser_forward_tool", "browser_tabs_tool",
                     "browser_find_tool", "browser_cookies_tool"):
            self.assertTrue(callable(getattr(plugin, name, None)), name)

    async def test_learning_digest_writes_long_term_memory(self):
        hooks = self._hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config()
        )
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        try:
            scope = await plugin.storage.get_or_create_scope("aiocqhttp", "1", "123")
            plugin._known_scopes["123"] = scope
            digest = await plugin._digest_web_learning(
                scope, "量子计算最近取得进展，某团队实现了 1000 量子比特。")
            self.assertTrue(digest)
            records = await plugin.storage.list_summaries(scope, 10, levels=(3,))
            self.assertTrue(any("网络学习" in r.title for r in records))
            # 账本条目要求证据 ID 合法，落库失败也不影响摘要（best-effort）
            catalog = await plugin.ledger.catalog((scope,), 20)
            self.assertIsInstance(catalog, list)
        finally:
            await plugin.terminate()

    async def test_recent_web_material_feeds_the_digest(self):
        hooks = self._hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config()
        )
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        try:
            session = plugin._browser_session()
            from src.browser import Page

            session._fetch_sync = lambda target, method="GET", payload=None: (
                Page(url=target, status=200, title="材料标题",
                     text="材料正文内容", links=[], forms=[]), "ok")
            await session.fetch("https://example.com/material")
            material = plugin._recent_web_material()
            self.assertIn("材料标题", material)
            self.assertIn("材料正文内容", material)
        finally:
            await plugin.terminate()

    async def test_idle_menu_has_news_and_learn(self):
        from src.rhythm import idle_choice

        seen = {idle_choice(10) for _ in range(200)}
        self.assertTrue(seen.issubset({
            "qzone", "news", "learn", "poke", "private_chat", "chat",
            "sticker", "memory", "profile", "none",
        }))
        self.assertIn("learn", {idle_choice(9) for _ in range(300)})


if __name__ == "__main__":
    unittest.main()
