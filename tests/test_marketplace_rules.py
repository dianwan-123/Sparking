# -*- coding: utf-8 -*-
"""插件市场的上架硬性规则 + 审查提醒的落地测试。

审查意见（v1.0.0 提交时）：
① **必须改**：日志器只能来自 astrbot.api，不许用 Python 内置 logging
   （点名 src/compression.py、src/pw_driver.py）。
② 提醒：运行时自动 pip 安装（data_tools / program_host / pdf_reader / browser_setup）
   与 exec LLM 生成代码的 program/scripts 能力，要**向用户明示**并**仅在主人授权配置下启用**。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

PACKAGED_DIRS = ("src",)
PACKAGED_FILES = ("main.py",)


def _packaged_python_files():
    for name in PACKAGED_FILES:
        yield PROJECT_ROOT / name
    for folder in PACKAGED_DIRS:
        for path in sorted((PROJECT_ROOT / folder).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


class LoggerRuleTests(unittest.TestCase):
    """① 硬性规则：只能 from astrbot.api import logger。"""

    def test_no_stdlib_logging_in_packaged_code(self):
        offenders: list[str] = []
        for path in _packaged_python_files():
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if ("import logging" in stripped
                        or "logging.getLogger" in stripped
                        or "logging.basicConfig" in stripped):
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{number} {stripped[:70]}")
        self.assertEqual([], offenders, "市场规则：日志器只能用 astrbot.api 的 logger")

    def test_pointed_files_use_astrbot_logger(self):
        for name in ("src/compression.py", "src/pw_driver.py"):
            text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
            self.assertIn("from astrbot.api import logger", text, f"{name} 要用 astrbot 的 logger")


class AutoInstallSwitchTests(unittest.TestCase):
    """② 自动安装必须在主人授权开关之下。"""

    def tearDown(self):
        # 只恢复、不删除模块级缓存——pop 掉会让后面的测试 AttributeError（踩过）
        from src import data_tools, pdf_reader, program_host

        for module in (data_tools, pdf_reader, program_host):
            module.set_auto_install(True)
        for name in ("_PANDAS_STATE", "_MATPLOTLIB_STATE"):
            if hasattr(data_tools, name):
                setattr(data_tools, name, None)
        if hasattr(pdf_reader, "_pypdf_state"):
            pdf_reader._pypdf_state = None
        if hasattr(program_host, "_flask_state"):
            program_host._flask_state = None

    def test_pip_not_run_when_switch_off(self):
        import builtins

        from src import data_tools, pdf_reader, program_host

        for module in (data_tools, pdf_reader, program_host):
            module.set_auto_install(False)
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            # 本机装了 pypdf/flask 会走"已存在"的早返回，这里强制走缺失分支
            if name.split(".")[0] in {"pypdf", "flask", "pandas", "matplotlib"}:
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=fake_import),                 mock.patch("subprocess.run") as runner:
            self.assertFalse(data_tools._pip_install(("pandas",)))
            pdf_reader._pypdf_state = None
            ok, note = pdf_reader.ensure_pypdf()
            self.assertFalse(ok)
            self.assertIn("auto_install_deps", note)
            program_host._flask_state = None
            flask_ok, flask_note = program_host.ensure_flask()
            self.assertFalse(flask_ok)
            self.assertIn("auto_install_deps", flask_note)
        self.assertFalse(runner.called, "关掉开关后不许再有 pip 调用")

    def test_module_flag_defaults_on(self):
        from src import data_tools

        data_tools.set_auto_install(True)
        self.assertTrue(data_tools.auto_install_allowed())
        data_tools.set_auto_install(False)
        self.assertFalse(data_tools.auto_install_allowed())


class ConfigAndWiringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import os
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    async def _plugin(self, extra=None):
        import tests.test_plugin_hooks as hooks

        config = hooks._plugin_config()
        config.update(extra or {})
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), config)
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._terminate, plugin)
        return plugin

    async def _terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def test_schema_declares_switch(self):
        import json

        data = json.loads((PROJECT_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertIn("auto_install_deps", data)
        self.assertEqual("bool", data["auto_install_deps"]["type"])
        self.assertIn("pip", data["auto_install_deps"]["description"].lower())

    async def test_plugin_pushes_setting_to_modules(self):
        """看插件实际引用的那份模块：测试里 `src.x` 与 `DFYChat.src.x` 是两个模块对象。"""
        import DFYChat.main as main_module

        await self._plugin({"auto_install_deps": False})
        for module in (main_module.data_tools, main_module.pdf_reader,
                       main_module.program_host):
            self.assertFalse(module.auto_install_allowed(),
                             "initialize 要把关掉的授权项同步给模块")
        main_module.data_tools.set_auto_install(True)

    async def test_readme_discloses_capabilities(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("auto_install_deps", readme, "要告诉用户这个开关")
        self.assertIn("会执行模型写的代码", readme, "要明示会执行模型生成的代码")


if __name__ == "__main__":
    unittest.main()
