# -*- coding: utf-8 -*-
"""v0.34.1 回归：真正实现 read_tabular。

实录：模型在"读服务器指标画图"任务里反复调用 read_tabular 并带
`file_path='__noop__'`（它想当"带 pandas 的沙箱"用），插件里没有这个工具，
AstrBot 每轮都"未找到指定的工具，将跳过"，模型失去数据通道只剩空头承诺
（截图里连续七次"我这就去画"）。这里覆盖：沙箱模式、占位路径、真实表格文件、
报错/超时/越界路径、工具注册与调度接入。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import data_tools  # noqa: E402
from src.data_tools import run_tabular  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"tab-chart"


class _StubFrame:
    def __init__(self, rows):
        self._rows = rows

    def __len__(self):
        return len(self._rows)

    @property
    def columns(self):
        return list(self._rows[0]) if self._rows else []

    def __getitem__(self, key):
        return [row.get(key) for row in self._rows]


def _pandas_stub() -> types.ModuleType:
    """最小 pandas 替身：本地测试环境没装 pandas，避免触发真实 pip 安装。"""
    module = types.ModuleType("pandas")
    rows = [{"name": "cpu", "value": 42}, {"name": "mem", "value": 7}]
    module.DataFrame = lambda data, *a, **k: _StubFrame(
        [dict(row) for row in data] if isinstance(data, list) else [])
    module.read_csv = lambda path, *a, **k: _StubFrame(rows)
    module.read_excel = lambda path, *a, **k: _StubFrame(rows)
    module.read_json = lambda path, *a, **k: _StubFrame(rows)
    return module


class TabularTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self._had_pandas = sys.modules.get("pandas")
        sys.modules["pandas"] = _pandas_stub()
        self._state = data_tools._PANDAS_STATE
        data_tools._PANDAS_STATE = (True, "ok")

        def _restore():
            data_tools._PANDAS_STATE = self._state
            if self._had_pandas is None:
                sys.modules.pop("pandas", None)
            else:
                sys.modules["pandas"] = self._had_pandas

        self.addCleanup(_restore)

    def _resolve(self, raw):
        return self.base / str(raw)

    async def test_sandbox_mode_without_file(self):
        # 实录参数 `file_path: '__noop__', pandas_operations: 'result = 1'` 的真实回放
        output = await run_tabular(
            "result = 1", file_path="__noop__", resolve_path=self._resolve)
        self.assertIn("RESULT: 1", output)
        self.assertNotIn("不存在", output)

    async def test_placeholder_keeps_df_out_of_namespace(self):
        output = await run_tabular(
            "print('df' in globals())", file_path="N/A", resolve_path=self._resolve)
        self.assertIn("False", output)

    async def test_reads_csv_into_df(self):
        (self.base / "hosts.csv").write_text("name,value\ncpu,1\n", encoding="utf-8")
        output = await run_tabular(
            "print(len(df)); result = df.columns",
            file_path="hosts.csv", resolve_path=self._resolve)
        self.assertIn("2", output)
        self.assertIn("RESULT: ['name', 'value']", output)

    async def test_excel_and_tsv_and_json_paths(self):
        for name in ("book.xlsx", "data.tsv", "rows.json"):
            (self.base / name).write_text("x", encoding="utf-8")
            output = await run_tabular(
                "result = len(df)", file_path=name, resolve_path=self._resolve)
            self.assertIn("RESULT: 2", output, name)

    async def test_missing_file_error_points_to_hint(self):
        output = await run_tabular(
            "result = 1", file_path="nope.csv", resolve_path=self._resolve)
        self.assertIn("不存在", output)
        self.assertIn("留空", output)

    async def test_empty_code_prompt(self):
        output = await run_tabular("   ", resolve_path=self._resolve)
        self.assertIn("pandas_operations", output)

    async def test_outside_path_rejected(self):
        def _deny(raw):
            raise ValueError("工作区外路径被拦截")

        output = await run_tabular(
            "result = 1", file_path="../../etc/passwd", resolve_path=_deny)
        self.assertIn("不允许", output)

    async def test_runtime_error_reports_partial_output(self):
        output = await run_tabular(
            "print('before'); raise ValueError('boom')", resolve_path=self._resolve)
        self.assertIn("ValueError", output)
        self.assertIn("before", output)

    async def test_silent_code_gets_hint(self):
        output = await run_tabular("x = 1", resolve_path=self._resolve)
        self.assertIn("无输出", output)

    async def test_timeout_interrupts(self):
        output = await run_tabular(
            "import time; time.sleep(3)", resolve_path=self._resolve, timeout=0.2)
        self.assertIn("执行超时", output)

    async def test_bot_globals_are_injected(self):
        seen = {}

        def _fake_bot_marker():
            seen["called"] = True
            return "ok"

        output = await run_tabular(
            "result = bot.marker()",
            resolve_path=self._resolve,
            extra_globals={"bot": types.SimpleNamespace(marker=_fake_bot_marker)},
        )
        self.assertIn("RESULT: ok", output)
        self.assertTrue(seen.get("called"))


def _matplotlib_stub() -> types.ModuleType:
    module = types.ModuleType("matplotlib")
    module.rcParams = {}
    module.use = lambda backend, *a, **k: None
    pyplot = types.ModuleType("matplotlib.pyplot")
    saved: list[str] = []
    pyplot.plot = lambda *a, **k: None
    pyplot.savefig = lambda path, *a, **k: saved.append(str(path))
    pyplot.saved = saved
    module.pyplot = pyplot
    module._saved = saved
    return module


class TabularPlotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self._had = {name: sys.modules.get(name) for name in ("pandas", "matplotlib",
                                                             "matplotlib.pyplot")}
        sys.modules["pandas"] = _pandas_stub()
        mpl = _matplotlib_stub()
        sys.modules["matplotlib"] = mpl
        sys.modules["matplotlib.pyplot"] = mpl.pyplot
        self._pandas_state = data_tools._PANDAS_STATE
        self._mpl_state = data_tools._MPL_STATE
        data_tools._PANDAS_STATE = (True, "ok")
        data_tools._MPL_STATE = (True, "ok")

        def _restore():
            data_tools._PANDAS_STATE = self._pandas_state
            data_tools._MPL_STATE = self._mpl_state
            for name, value in self._had.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value

        self.addCleanup(_restore)

    async def test_plot_code_gets_plt_and_save_chart(self):
        output = await run_tabular(
            "plt.plot([1, 2, 3])\nresult = save_chart('charts/status.png')",
            resolve_path=lambda raw: self.base / str(raw),
        )
        self.assertIn("RESULT:", output)
        chart = self.base / "charts" / "status.png"
        self.assertTrue(chart.parent.is_dir())
        saved = sys.modules["matplotlib.pyplot"].saved
        self.assertEqual([str(chart)], saved)

    async def test_non_plot_code_does_not_touch_matplotlib(self):
        data_tools._MPL_STATE = (False, "不该被调用")
        output = await run_tabular("result = 1 + 1", resolve_path=lambda raw: self.base)
        self.assertIn("RESULT: 2", output)
        self.assertNotIn("不该被调用", output)

    async def test_plot_unavailable_is_graceful(self):
        data_tools._MPL_STATE = (False, "matplotlib 自动安装失败：改用 design_render")
        output = await run_tabular("plt.plot([1])", resolve_path=lambda raw: self.base)
        self.assertIn("design_render", output)


class NonQqMediaSendTests(unittest.IsolatedAsyncioTestCase):
    """实录（OpenAPI webchat 会话）：send_local_file 报
    "发送失败：invalid literal for int() with base 10: 'Rikka0612'"
    ——NapCat 网关只认数字 QQ 目标，非 OneBot 会话必须走平台原生发送。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    async def _plugin(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        return plugin, hooks

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    @staticmethod
    def _webchat_event(hooks, raw, text):
        """webchat 形态的会话：平台不是 aiocqhttp，会话 id 不是数字。"""
        event = hooks.FakeEvent(raw, text, hooks.FakeBot(), wake=True)
        event.get_platform_name = lambda: "webchat"
        event.get_group_id = lambda: ""
        event.get_sender_id = lambda: "Rikka0612"
        return event

    async def test_webchat_session_uses_native_send(self):
        plugin, hooks = await self._plugin()
        workspace = plugin.storage.path.parent / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "chart.png").write_bytes(PNG)
        event = self._webchat_event(hooks, hooks._group_raw("发我", 6501), "发我")
        result = json.loads(await plugin.send_local_file_tool(event, path="chart.png"))
        self.assertTrue(result["ok"], result)
        self.assertEqual("astrbot", result["via"], result)
        self.assertEqual(1, len(event.sent), "应走 AstrBot 原生通道发出")
        component = event.sent[0].chain[0]
        self.assertTrue(str(getattr(component, "file", "")).endswith("chart.png"))

    async def test_qq_group_still_uses_gateway(self):
        plugin, hooks = await self._plugin()
        workspace = plugin.storage.path.parent / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "chart.png").write_bytes(PNG)
        bot = hooks.FakeBot()
        event = hooks.FakeEvent(
            hooks._group_raw("发我", 6502), "发我", bot, wake=True)
        result = json.loads(await plugin.send_local_file_tool(event, path="chart.png"))
        self.assertEqual("gateway", result["via"], result)
        self.assertTrue([c for c in bot.calls if c[0] == "send_group_msg"])

    async def test_non_numeric_explicit_target_is_refused(self):
        plugin, hooks = await self._plugin()
        workspace = plugin.storage.path.parent / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "chart.png").write_bytes(PNG)
        event = hooks.FakeEvent(
            hooks._group_raw("发他", 6503), "发他", hooks.FakeBot(), wake=True)
        result = await plugin.send_local_file_tool(
            event, path="chart.png", user_id="Rikka0612")
        self.assertIn("不是 QQ 号", result)


def _hooks():
    import tests.test_plugin_hooks as hooks
    return hooks


class TabularToolWiringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self._had_pandas = sys.modules.get("pandas")
        sys.modules["pandas"] = _pandas_stub()
        self._state = data_tools._PANDAS_STATE
        data_tools._PANDAS_STATE = (True, "ok")

        def _restore():
            data_tools._PANDAS_STATE = self._state
            if self._had_pandas is None:
                sys.modules.pop("pandas", None)
            else:
                sys.modules["pandas"] = self._had_pandas

        self.addCleanup(_restore)

    async def test_tool_registered_in_workspace_pack(self):
        from src.extensions import BUILTIN_PACKS

        self.assertIn("read_tabular", BUILTIN_PACKS["workspace"][2])

    async def test_llm_tool_and_dispatch_route(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext(), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)

        self.assertTrue(callable(getattr(plugin, "read_tabular_tool", None)))
        event = hooks.FakeEvent(
            hooks._group_raw("画个图", 6101), "画个图", hooks.FakeBot(), wake=True)
        direct = await plugin.read_tabular_tool(event, "result = 2+2")
        self.assertIn("RESULT: 4", direct)

        # 任务/计划执行路径（_run_plan_item 走的就是 _dispatch_task_action）
        routed = await plugin._dispatch_task_action(
            "read_tabular", {"pandas_operations": "result = 6*7", "file_path": "__noop__"},
            "qq:group:123")
        self.assertIn("RESULT: 42", routed)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
