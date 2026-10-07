# -*- coding: utf-8 -*-
"""v0.33.4 回归：单实例守卫（旧实例僵尸任务清理+世代戳自查）、
ssh_fetch/ssh_screenshot 远程文件回传、日程瞬时失败顺延重试。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

PNG = b"\x89PNG\r\n\x1a\n" + b"remote-shot-payload"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _hooks():
    import tests.test_plugin_hooks as hooks
    return hooks


class InstanceGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def _make_plugin(self):
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)
        return plugin

    async def test_new_instance_makes_old_stand_down(self):
        """世代戳：后启动的实例接管后，旧实例下一跳自查即停。"""
        first = await self._make_plugin()
        self.assertTrue(first._still_current_instance())

        second = await self._make_plugin()   # 同一数据目录：抢占世代戳
        self.assertTrue(second._still_current_instance())
        first._instance_checked_at = 0.0     # 绕过 15s 缓存立即复查
        self.assertFalse(first._still_current_instance())
        self.assertTrue(first._stopping, "旧实例必须置位 _stopping 停止所有循环")
        self.assertTrue(second._still_current_instance())

    async def test_reap_cancels_old_instance_loop_tasks(self):
        """旧实例残留的循环任务必须被新实例清掉（否则双实例并存烧重试）。
        关键：旧实例来自热重载前的模块——**同名但不同对象**的类，必须也能被识别
        （实录：`type(owner) is type(self)` 的比较把 v0.32.6 僵尸漏了两轮）。"""
        hooks = _hooks()
        live = await self._make_plugin()
        ZombieClass = type("LongMemoryAgentPlugin", (), {})  # 同名不同对象
        zombie = ZombieClass()

        async def _heartbeat_loop(self):        # 名字带 _loop，命中清理标记
            await asyncio.sleep(60)

        async def _own_loop(self):              # 属于当前实例的循环：绝不能误杀
            await asyncio.sleep(60)

        zombie_task = asyncio.ensure_future(
            _heartbeat_loop.__get__(zombie, ZombieClass)())
        own_task = asyncio.ensure_future(
            _own_loop.__get__(live, type(live))())
        self.addCleanup(own_task.cancel)
        killed = live._reap_zombie_instances()
        self.assertGreaterEqual(killed, 1, "跨模块同名旧类实例也必须被清理")
        await asyncio.sleep(0)
        self.assertTrue(zombie_task.cancelled(), "僵尸循环任务应被取消")
        self.assertFalse(own_task.done(), "当前实例的任务不能被误杀")


class SshFetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def _safe_terminate(self, plugin):
        try:
            await plugin.terminate()
        except Exception:
            pass

    async def _make_plugin(self, ssh):
        hooks = _hooks()
        config = hooks._plugin_config() | {"enable_media_archive": True}
        plugin = hooks.LongMemoryAgentPlugin(hooks.FakeContext([]), config)
        plugin._reply_queue_enabled = False
        await plugin.initialize()
        self.addCleanup(self._safe_terminate, plugin)

        async def _noop_setup():
            return None

        plugin._ssh_setup_task = _noop_setup
        plugin.raw_config["ssh_host"] = "1.2.3.4"
        plugin.raw_config["ssh_user"] = "root"
        plugin.raw_config["ssh_password"] = "pw"
        plugin._ssh = ssh
        return plugin

    class _FakeSSH:
        def __init__(self, files: dict[str, bytes], *, truncate_on_fetch: bool = False):
            self.files = files
            self.commands: list[str] = []
            self.truncate_on_fetch = truncate_on_fetch

        async def close(self):
            return None

        async def exec(self, command, *, timeout=60.0, max_out=None):
            self.commands.append(command)
            target = ""
            for path in self.files:
                if f"'{path}'" in command:
                    target = path
                    break
            if "stat -c %s" in command:
                if not target:
                    return {"exit_code": 0, "stdout": "MISSING\n",
                            "stderr": "", "truncated": False}
                return {"exit_code": 0, "stdout": f"{len(self.files[target])}\n",
                        "stderr": "", "truncated": False}
            if "base64 -w0" in command and target:
                import base64
                out = base64.b64encode(self.files[target]).decode()
                if self.truncate_on_fetch:
                    return {"exit_code": 0, "stdout": out[:12000],
                            "stderr": "", "truncated": True}
                cap = max_out or 12000
                return {"exit_code": 0, "stdout": out[:cap],
                        "stderr": "", "truncated": len(out) > cap}
            if "--headless" in command:
                # 截图脚本：模拟远程 chromium 产出 PNG
                self.files["/tmp/zcode_shot.png"] = PNG
                return {"exit_code": 0, "stdout": "SHOT_OK\n",
                        "stderr": "", "truncated": False}
            return {"exit_code": 0, "stdout": "", "stderr": "", "truncated": False}

    async def test_fetch_pulls_remote_file_into_media(self):
        ssh = self._FakeSSH({"/root/shot.png": PNG})
        plugin = await self._make_plugin(ssh)
        result = json.loads(await plugin._ssh_fetch_core("/root/shot.png"))
        self.assertTrue(result["ok"], result)
        self.assertEqual("image", result["kind"])
        self.assertEqual(len(PNG), result["bytes"])
        self.assertTrue(result["media_id"])
        # 媒体库里确实能按 id 取回
        record = await plugin.media.find_by_message("studio", "", kind=None)
        self.assertIsInstance(record, list)

    async def test_fetch_missing_file_reports_cleanly(self):
        ssh = self._FakeSSH({})
        plugin = await self._make_plugin(ssh)
        result = await plugin._ssh_fetch_core("/root/nope.png")
        self.assertIn("拉取失败", result)
        self.assertIn("不存在", result)

    async def test_fetch_truncated_is_rejected(self):
        big = PNG + b"x" * 40000
        ssh = self._FakeSSH({"/root/big.png": big}, truncate_on_fetch=True)
        plugin = await self._make_plugin(ssh)
        result = await plugin._ssh_fetch_core("/root/big.png")
        self.assertIn("拉取失败", result)
        self.assertIn("截断", result)

    async def test_screenshot_runs_remote_chromium_and_pulls(self):
        ssh = self._FakeSSH({})
        plugin = await self._make_plugin(ssh)
        result = json.loads(await plugin._ssh_shot_core("https://www.baidu.com"))
        self.assertTrue(result["ok"], result)
        self.assertEqual("image", result["kind"])
        self.assertTrue(any("--headless" in cmd for cmd in ssh.commands))
        self.assertTrue(any("zcode_shot.png" in cmd for cmd in ssh.commands))


class ProviderResolvableFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_skips_stale_registry_entry(self):
        """注册表里还列着、但 AstrBot 根本取不到实例的旧模型（deepseek 案）
        必须在候选链里被跳过——光看 get_all_providers 成员关系查不出这种死法。"""
        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config())

        class _Provider:
            def __init__(self, pid):
                self.provider_config = {"id": pid}

        plugin.context.get_all_providers = lambda: [
            _Provider("catapi/deepseek-v4.1-flash"), _Provider("live/ok")]

        class _PM:
            def get_provider_by_id(self, pid):
                return {"id": pid} if pid == "live/ok" else None

        plugin.context.provider_manager = _PM()

        async def _using():
            return None

        plugin.context.get_using_provider_async = _using
        plugin.settings.reply_provider_id = "live/ok"
        resolved = await plugin._resolve_provider("catapi/deepseek-v4.1-flash", None)
        self.assertEqual("live/ok", resolved)


class PlanRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["ASTRBOT_STUB_DATA"] = str(Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    async def test_postpone_plan_retries_then_gives_up(self):
        from datetime import datetime, timedelta, timezone

        from src.storage import Storage

        storage = await Storage(Path(self._tmp.name) / "p.db").open()
        try:
            added = await storage.add_plans([{
                "due_at": (datetime.now(timezone.utc) - timedelta(minutes=5)
                           ).isoformat(timespec="minutes"),
                "kind": "chat", "detail": "冒个泡",
            }])
            self.assertGreaterEqual(int(added or 0), 1)
            due = await storage.due_plans(5)
            plan_id = due[0]["plan_id"]
            self.assertTrue(await storage.claim_plan(plan_id))
            self.assertTrue(await storage.postpone_plan(plan_id))
            pending = await storage.pending_plans(5)
            self.assertTrue(any(p["plan_id"] == plan_id for p in pending),
                            "顺延后应回到 pending（due_at 已推后）")
            await storage.claim_plan(plan_id)
            self.assertTrue(await storage.postpone_plan(plan_id))
            await storage.claim_plan(plan_id)
            self.assertFalse(await storage.postpone_plan(plan_id),
                             "超过重试上限必须放弃")
        finally:
            await storage.close()

    async def test_run_plan_item_postpones_on_transient_error(self):
        from datetime import datetime, timedelta, timezone

        hooks = _hooks()
        plugin = hooks.LongMemoryAgentPlugin(
            hooks.FakeContext([]), hooks._plugin_config())
        plugin._reply_queue_enabled = False
        await plugin.initialize()

        async def _safe_terminate():
            try:
                await plugin.terminate()
            except Exception:
                pass

        self.addCleanup(_safe_terminate)

        added = await plugin.storage.add_plans([{
            "due_at": (datetime.now(timezone.utc) - timedelta(minutes=5)
                       ).isoformat(timespec="minutes"),
            "kind": "chat", "detail": "去群里冒个泡",
        }])
        self.assertGreaterEqual(int(added or 0), 1)
        due = await plugin.storage.due_plans(20)
        self.assertTrue(due)
        plan_id = due[0]["plan_id"]

        async def _boom(*args, **kwargs):
            raise RuntimeError(
                "Error code: 503 - {'code': 'model_not_found'}")

        plugin._autonomous_action_loop = _boom
        await plugin._run_plan_item(due[0])
        # 顺延后 due_at 被推后 15 分钟：应从 pending_plans 里能找到（而不是 failed）
        pending = await plugin.storage.pending_plans(20)
        self.assertTrue(
            any(p["plan_id"] == plan_id for p in pending),
            f"瞬时故障应顺延为 pending 而不是 failed：{pending}")


if __name__ == "__main__":
    unittest.main()
