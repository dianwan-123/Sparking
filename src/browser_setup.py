"""Auto-detect and install a lightweight browser kernel (China-friendly mirrors).

Runs in the background after plugin install/startup:

1. ``playwright`` package missing and auto-install on → pip install from the
   Tsinghua mirror;
2. chromium kernel not cached → ``playwright install chromium`` with
   PLAYWRIGHT_DOWNLOAD_HOST pointed at npmmirror (domestic, fast).

Only the chromium kernel is downloaded (lightweight — no firefox/webkit, no
``--with-deps`` system packages). Every failure is reported in the returned
status dict and never breaks the plugin.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
PW_DOWNLOAD_HOSTS = [
    "https://npmmirror.com/mirrors/playwright/",
    "https://cdn.npmmirror.com/binaries/playwright/",
    "https://registry.npmmirror.com/-/binary/playwright/",
    "",  # 官方默认源（前几个镜像同步滞后时兜底）
]


def playwright_installed(name: str = "playwright") -> bool:
    return importlib.util.find_spec(name) is not None


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "ms-playwright")
    roots.append(Path.home() / ".cache" / "ms-playwright")
    return roots


def chromium_cached(root: Path | None = None) -> bool:
    """True when a downloaded chromium kernel directory exists."""
    roots = [root] if root is not None else _candidate_roots()
    for base in roots:
        try:
            if base.is_dir() and any(
                item.name.startswith("chromium") for item in base.iterdir()
            ):
                return True
        except OSError:
            continue
    return False


def _append_log(path: Path | None, text: str) -> None:
    """Full downloader output → data dir log; the console line is only a tail."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text.rstrip() + "\n\n")
    except OSError:
        pass


async def _run(cmd: list[str], *, timeout: int, env: dict[str, str] | None = None,
               runner: Callable[..., Awaitable[tuple[int, str]]] | None = None
               ) -> tuple[int, str]:
    if runner is not None:
        return await runner(cmd, timeout=timeout, env=env)

    def _sync() -> tuple[int, str]:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=timeout, env=env)
            tail = ((proc.stdout or "")[-600:] + (proc.stderr or "")[-400:])
            return proc.returncode, tail
        except FileNotFoundError as error:
            return 127, f"not found: {error}"
        except subprocess.TimeoutExpired:
            return 124, "timed out"

    return await asyncio.to_thread(_sync)


async def ensure_browser_environment(
    *,
    auto_install: bool = True,
    logger: Callable[[str], Any] | None = None,
    import_check: Callable[[str], bool] | None = None,
    runner: Callable[..., Awaitable[tuple[int, str]]] | None = None,
    chromium_check: Callable[[], bool] | None = None,
    log_file: Path | None = None,
) -> dict[str, Any]:
    """Detect (and optionally install) the browser environment.

    Returns a status dict: {playwright, chromium, steps, error}.
    """
    log = logger or (lambda _msg: None)
    check = import_check or playwright_installed
    cached = chromium_check or chromium_cached
    run = runner or _run
    status: dict[str, Any] = {"playwright": False, "chromium": False,
                              "steps": [], "error": ""}

    if not check("playwright"):
        if not auto_install:
            status["error"] = "playwright 未安装，且 browser_auto_install 已关闭"
            return status
        log("浏览器环境：playwright 未安装，正在通过清华镜像安装（轻量，仅本包）……")
        code, tail = await run(
            [sys.executable, "-m", "pip", "install", "playwright",
             "-i", PIP_MIRROR, "--quiet"], timeout=900)
        status["steps"].append(f"pip install playwright -> exit {code}")
        if code != 0 or not check("playwright"):
            status["error"] = f"playwright 安装失败：{tail[-200:]}"
            return status
        log("浏览器环境：playwright 安装成功")
    status["playwright"] = True

    if cached():
        status["chromium"] = True
        log("浏览器环境：chromium 内核已就绪，截图与页面操作可用")
        return status
    if not auto_install:
        status["error"] = "chromium 内核未下载，且 browser_auto_install 已关闭"
        return status
    log("浏览器环境：chromium 内核未缓存，开始下载（约100MB，只下载一次）……")
    env = os.environ.copy()
    last_tail = ""
    attempts = 0
    for host in PW_DOWNLOAD_HOSTS:
        attempts += 1
        if host:
            env["PLAYWRIGHT_DOWNLOAD_HOST"] = host
            label = host
        else:
            env.pop("PLAYWRIGHT_DOWNLOAD_HOST", None)
            label = "官方源"
        log(f"浏览器环境：尝试第{attempts}个下载源（{label}）……")
        code, tail = await run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            timeout=1800, env=env)
        last_tail = tail
        status["steps"].append(f"chromium via {label} -> exit {code}")
        _append_log(
            log_file,
            f"playwright install chromium | host={label} exit={code}\n{tail}",
        )
        if code == 0 and cached():
            status["chromium"] = True
            log("浏览器环境：chromium 内核下载完成，网页截图/点击/输入已解锁")
            return status
        log(f"浏览器环境：该下载源失败（exit {code}），换下一个……")
    status["error"] = (
        f"chromium 内核下载失败（已尝试{attempts}个下载源）。"
        f"最后错误：{last_tail[-160:]}"
        + (f"。完整输出见 {log_file}" if log_file else "")
    )
    return status
