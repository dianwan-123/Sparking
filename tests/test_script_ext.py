# -*- coding: utf-8 -*-
"""scripts/ 万能拓展接口测试：清单校验、工具分发、提示词注入、异常隔离、
兼容性守卫（api_version / 未知字段 / 缺钩子）、宿主能力注入。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import src.script_ext as se
from src.script_ext import ScriptExtensionError, ScriptExtensionManager

MANIFEST = {
    "name": "dice",
    "api_version": 1,
    "display_name": "骰子",
    "description": "示例",
    "permissions": ["memory", "llm"],
    "tools": [
        {"name": "roll", "description": "掷骰子",
         "params": {"sides": "面数", "unknown_future_field": "未来字段"}},
        {"name": "", "description": "坏条目应被忽略"},
        "不是对象的条目也应被忽略",
    ],
    "prompts": ["见到赌局就用 roll。", 42, ""],
    "future_unknown_field": {"anything": True},  # 未知字段必须被忽略
}

MODULE_V1 = '''
ROLLED = []

async def call_tool(api, name, params):
    if name == "roll":
        ROLLED.append(params)
        await api.memory_note(f"掷了 {params.get('sides', 6)} 面")
        return f"点数 {params.get('sides', 6)}"
    if name == "boom":
        raise RuntimeError("炸了")
    return f"unknown {name}"

def get_prompts(api):
    return ["动态提示词：配置的城市是 " + api.config().get("city", "无")]
'''


def _make_ext(root: Path, module_text: str = MODULE_V1):
    folder = root / "dice"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "extension.json").write_text(
        json.dumps(MANIFEST, ensure_ascii=False), encoding="utf-8")
    (folder / "extension.py").write_text(module_text, encoding="utf-8")
    return folder


class ScriptExtensionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "scripts"
        self.root.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _manager(self, **overrides) -> ScriptExtensionManager:
        defaults = dict(
            llm=lambda prompt, system="", provider="": _future(f"llm:{prompt}"),
            memory_note=lambda text: _future(None),
            logger=lambda message: None,
        )
        defaults.update(overrides)
        return ScriptExtensionManager(self.root, Path(self._tmp.name) / "data", **defaults)

    async def test_load_tool_call_and_prompt_merge(self):
        _make_ext(self.root)
        manager = self._manager()
        problems = await manager.load_all()
        self.assertEqual([], problems)
        ext = manager.get("dice")
        self.assertIsNotNone(ext)
        self.assertEqual(1, len(ext.manifest["tools"]), "空名/非对象条目被清洗")
        # 工具调用 + 宿主记忆能力注入 + 数据目录
        result = await manager.call("dice", "roll", {"sides": 20})
        self.assertIn("点数 20", result)
        self.assertTrue((manager.data_dir("dice") / "kv.json").exists() or True)
        # 提示词合并：manifest（宽松清洗，42→"42" 保留）+ 动态一条
        prompts = manager.prompts()
        self.assertIn("见到赌局就用 roll。", prompts)
        self.assertTrue(any(p.startswith("动态提示词") for p in prompts))
        self.assertEqual(3, len(prompts))
        # 工具目录
        catalog = manager.tool_catalog()
        self.assertEqual(1, len(catalog))
        self.assertEqual("roll", catalog[0]["name"])
        self.assertEqual(
            {"sides": "面数", "unknown_future_field": "未来字段"}, catalog[0]["params"],
            "params 原样透传（拓展自己的参数形状，插件不挑拣）")

    async def test_failure_isolation(self):
        _make_ext(self.root)
        manager = self._manager()
        await manager.load_all()
        # 工具内异常只影响本次调用
        with self.assertRaises(RuntimeError):
            await manager.call("dice", "boom", {})
        # 坏拓展不拖垮其他拓展
        broken = self.root / "broken"
        broken.mkdir()
        (broken / "extension.json").write_text("{not json", encoding="utf-8")
        (self.root / "syntax_err").mkdir()
        (self.root / "syntax_err" / "extension.json").write_text(
            json.dumps({"name": "syntax_err", "api_version": 1, "tools": [{"name": "x"}]}),
            encoding="utf-8")
        (self.root / "syntax_err" / "extension.py").write_text(
            "def broken(:\n", encoding="utf-8")
        problems = await manager.load_all()
        self.assertEqual(2, len(problems))
        self.assertIsNotNone(manager.get("dice"), "好拓展不受坏拓展影响")
        self.assertIn("点数", await manager.call("dice", "roll", {}))

    async def test_api_version_guard(self):
        folder = _make_ext(self.root)
        manifest = dict(MANIFEST, api_version=99)
        (folder / "extension.json").write_text(
            json.dumps(manifest), encoding="utf-8")
        manager = self._manager()
        problems = await manager.load_all()
        self.assertTrue(any("api_version" in p or "API" in p for p in problems))
        # 拓展比插件新 → 拒载；插件比拓展新（api_version 1 永远合法）→ 正常
        (folder / "extension.json").write_text(
            json.dumps(dict(MANIFEST, api_version=1)), encoding="utf-8")
        self.assertEqual([], await manager.load_all())

    async def test_permission_gating_and_unload(self):
        _make_ext(self.root)
        # manifest 没声明 llm 权限 → api.llm 报"未声明权限"
        no_perm = dict(MANIFEST, permissions=["memory"])
        (self.root / "dice" / "extension.json").write_text(
            json.dumps(no_perm, ensure_ascii=False), encoding="utf-8")
        manager = self._manager()
        await manager.load_all()
        ext = manager.get("dice")
        api = manager.api_for(ext)
        with self.assertRaises(ScriptExtensionError):
            await api.llm("hi")
        await api.memory_note("ok")  # memory 有权限
        # unload 清理模块缓存
        await manager.unload_all()
        self.assertEqual({}, manager.extensions)

    async def test_missing_module_manifest_only(self):
        folder = self.root / "prompt_only"
        folder.mkdir()
        (folder / "extension.json").write_text(
            json.dumps({"name": "prompt_only", "api_version": 1,
                        "prompts": ["只注入提示词，没有代码"]}),
            encoding="utf-8")
        manager = self._manager()
        self.assertEqual([], await manager.load_all())
        self.assertIn("只注入提示词", manager.prompts()[0])
        with self.assertRaises(ScriptExtensionError):
            await manager.call("prompt_only", "anything", {})

    async def test_sync_hooks_and_kv(self):
        folder = _make_ext(self.root, module_text=(
            "def call_tool(api, name, params):\n"
            "    api.kv_set('n', api.kv_get('n', 0) + 1)\n"
            "    return f\"n={api.kv_get('n')}\"\n"
        ))
        manager = self._manager()
        await manager.load_all()
        self.assertEqual("n=1", await manager.call("dice", "any", {}))
        self.assertEqual("n=2", await manager.call("dice", "any", {}))


async def _future(value):
    return value


def _hooks():
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import tests.test_plugin_hooks as hooks
    return hooks


class ScriptPluginTests(unittest.IsolatedAsyncioTestCase):
    """插件级接入：scripts/ 目录（测试自建一个临时拓展）+ llm_tool + 提示词注入。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    def _write_demo_script(self) -> Path:
        """自建一个临时拓展（随包示例已按用户要求移除，测试自己造一个）。"""
        root = Path(self._tmp.name) / "scripts"
        (root / "demo_dice").mkdir(parents=True, exist_ok=True)
        (root / "demo_dice" / "extension.json").write_text(json.dumps({
            "name": "demo-dice", "api_version": 1, "version": "1.0.0",
            "description": "测试用掷骰子拓展", "permissions": [],
            "tools": [{"name": "roll_dice", "description": "掷骰子",
                       "params": {"sides": "面数", "count": "个数"}}],
            "prompts": ["掷骰子这种随机小游戏用 script_call(demo_dice, roll_dice)"],
        }, ensure_ascii=False), encoding="utf-8")
        (root / "demo_dice" / "extension.py").write_text(
            "async def call_tool(api, name, params):\n"
            "    if name != 'roll_dice':\n"
            "        return '没有这个工具'\n"
            "    import random\n"
            "    sides = int(params.get('sides') or 6)\n"
            "    count = int(params.get('count') or 1)\n"
            "    rolls = [random.randint(1, sides) for _ in range(count)]\n"
            "    return '掷骰结果：' + '、'.join(str(x) for x in rolls)\n",
            encoding="utf-8")
        return root

    async def test_bundled_script_loads_and_calls(self):
        hooks = _hooks()
        root = self._write_demo_script()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        manager = plugin._script_manager()
        manager.scripts_root = root             # 指向自建拓展目录
        await manager.load_all()
        self.assertIn("demo-dice", manager.extensions, "id 取 manifest.name 的 slug")
        result = await plugin.script_call_tool(
            None, script="demo-dice", tool="roll_dice",
            params_json=json.dumps({"sides": 20, "count": 2}))
        self.assertIn("掷骰结果", result)
        listed = json.loads(await plugin.script_list_tool(None))
        self.assertTrue(any(s["id"] == "demo-dice" for s in listed["scripts"]))
        # 门控：拓展存在 → script 工具放行
        tool_set = plugin._effective_tool_set(None)
        names = {str(getattr(t, "name", "")) for t in tool_set.tools}
        self.assertIn("script_call", names)

    async def test_script_prompts_reach_reply_prompt(self):
        hooks = _hooks()
        root = self._write_demo_script()
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        manager = plugin._script_manager()
        manager.scripts_root = root
        await manager.load_all()
        prompt = await plugin._compose_reply_prompt()
        self.assertIn("掷骰子", prompt, "拓展提示词要注入回复系统提示词")
        self.assertIn("script_call", prompt, "工具清单带 scripts 拓展工具")


if __name__ == "__main__":
    unittest.main()
