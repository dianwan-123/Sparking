# -*- coding: utf-8 -*-
"""pytest 入口：先把 AstrBot 桩装好，再让各测试去 import 插件模块。

src/ 现在按插件市场规范用 ``from astrbot.api import logger``（禁止内置
logging.getLogger），所以任何直接 ``import src.*`` 的测试都必须在导入前有桩——
放这里一次性解决，也让 ``import DFYChat.main`` 这类路径可用。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_PARENT = PROJECT_ROOT.parent
for path in (str(PROJECT_ROOT), str(PROJECT_PARENT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import tests.test_plugin_hooks as _hooks  # noqa: E402  （内部自带 stub 安装）

_hooks._install_astrbot_stub()
