"""Auto-detect and install a lightweight browser kernel (China-friendly mirrors).

Runs in the background after plugin install/startup:

1. ``playwright`` package missing and auto-install on → pip install from the
   Tsinghua mirror;
2. chromium kernel not cached → ``playwright install chromium`` with
   PLAYWRIGHT_DOWNLOAD_HOST pointed at npmmirror (domestic, fast).

Only the chromium kernel is downloaded (lightweight — no firefox/webkit). On
Linux the kernel additionally needs a handful of system shared libraries
(libnss3, libgbm …); when they are missing and we run as root they are installed
via ``playwright install-deps chromium``, otherwise the exact ``apt-get`` command
is reported. Every failure is reported in the returned status dict and never
breaks the plugin.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import platform_support as plat

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
    return plat.playwright_cache_roots()


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


def _chromium_binary() -> str:
    """已下载的完整 chromium 可执行文件（没有则空串）。"""
    try:
        from .pw_driver import _resolve_full_chromium

        return _resolve_full_chromium()
    except Exception:
        return ""


async def _ensure_system_deps(run: Callable[..., Awaitable[tuple[int, str]]],
                              log: Callable[[str], Any],
                              status: dict[str, Any],
                              log_file: Path | None = None) -> None:
    """Linux：chromium 缺系统库时补装（有 root 直接装，没有就给 apt 命令）。

    Windows/macOS 上 chromium 自带全部依赖，这一步是空操作。
    """
    exe = _chromium_binary()
    missing = plat.missing_shared_libs(exe)
    status["missing_libs"] = missing
    if not missing:
        return
    packages = plat.apt_packages_for(missing)
    status["apt_packages"] = packages
    hint = ("sudo apt-get install -y " + " ".join(packages)) if packages else ""
    log(f"浏览器环境：chromium 缺少系统库 {', '.join(missing[:6])}")
    if plat.is_root() and plat.which("apt-get"):
        log("浏览器环境：正在以 root 身份安装系统依赖（playwright install-deps）……")
        code, tail = await run(
            [sys.executable, "-m", "playwright", "install-deps", "chromium"],
            timeout=900)
        status.setdefault("steps", []).append(
            f"playwright install-deps chromium -> exit {code}")
        _append_log(log_file,
                    f"playwright install-deps chromium exit={code}\n{tail}")
        still = plat.missing_shared_libs(exe)
        status["missing_libs"] = still
        if not still:
            log("浏览器环境：系统依赖已装好")
            return
        status["error"] = (
            "chromium 系统依赖安装后仍缺库：" + ", ".join(still[:6])
            + (f"；可手动执行：{hint}" if hint else ""))
        return
    status["error"] = (
        "chromium 缺少系统库：" + ", ".join(missing[:6])
        + (f"；请执行：{hint}" if hint else "")
        + "（或用 root 运行插件让它自动安装）")


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

    Returns a status dict: {playwright, chromium, steps, error, missing_libs,
    cjk_font}。Linux 上还会补系统库，缺中文字体时给出提示。
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
        await _ensure_system_deps(run, log, status, log_file)
        _report_fonts(log, status)
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
            await _ensure_system_deps(run, log, status, log_file)
            _report_fonts(log, status)
            return status
        log(f"浏览器环境：该下载源失败（exit {code}），换下一个……")
    status["error"] = (
        f"chromium 内核下载失败（已尝试{attempts}个下载源）。"
        f"最后错误：{last_tail[-160:]}"
        + (f"。完整输出见 {log_file}" if log_file else "")
    )
    return status


def _report_fonts(log: Callable[[str], Any], status: dict[str, Any]) -> None:
    """中文页面截图需要系统里有中文字体，否则全是方块。"""
    ok = plat.has_cjk_font()
    status["cjk_font"] = ok
    if ok:
        return
    hint = ("浏览器环境：系统没有中文字体，截图里的中文会变成方块，"
            "建议安装：sudo apt-get install -y fonts-noto-cjk")
    log(hint)
    status["font_hint"] = hint
