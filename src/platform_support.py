"""跨平台支持：Windows / Linux / macOS 的差异都收敛在这里。

插件主要跑在 Linux 服务器上（Ubuntu/Debian 云主机），也有人在 Windows 桌面
上装。两边不一样的地方有：

- **浏览器内核缓存目录**：Windows 在 ``%LOCALAPPDATA%\\ms-playwright``，
  Linux 在 ``~/.cache/ms-playwright``，macOS 在 ``~/Library/Caches/ms-playwright``；
  设置了 ``PLAYWRIGHT_BROWSERS_PATH`` 时以它为准。
- **有头浏览器**：Linux 服务器通常没有 X/Wayland，``headless=False`` 会直接
  起不来（Missing X server or $DISPLAY），需要自动降级为无头。
- **chromium 的系统库**：Linux 上的 chromium 依赖 libnss3 / libgbm 等一堆
  ``.so``，缺了就是 ``error while loading shared libraries``。这里用 ``ldd``
  查出来并给出对应的 apt 包名（有 root 时直接 ``playwright install-deps``）。
- **中文字体**：服务器上不装中文字体，截图里的中文是豆腐块，matplotlib 画的
  图也是。这里做一次统一发现（含 ``fc-list :lang=zh`` 兜底）。
- **子进程**：编码（UTF-8 环境变量）与超时后的整棵进程树回收，Windows 与
  POSIX 的写法完全不同。

所有函数都只做探测与信息整理，不改任何状态；装依赖由调用方决定。
"""
from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

#: 中文字体候选（按优先级：黑体/雅黑类优先，衬线兜底）
_CJK_FONT_PATHS = (
    # 环境变量优先（用户自带字体时不用改代码）
    # —— 见 cjk_font_files() 里的 SPARKING_CJK_FONT
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
)

#: chromium 缺失共享库 → Debian/Ubuntu 包名
_LIB_PACKAGES = {
    "libnss3.so": "libnss3",
    "libnspr4.so": "libnspr4",
    "libnssutil3.so": "libnss3",
    "libsmime3.so": "libnss3",
    "libatk-1.0.so.0": "libatk1.0-0",
    "libatk-bridge-2.0.so.0": "libatk-bridge2.0-0",
    "libatspi.so.0": "libatspi2.0-0",
    "libcups.so.2": "libcups2",
    "libdrm.so.2": "libdrm2",
    "libxkbcommon.so.0": "libxkbcommon0",
    "libXcomposite.so.1": "libxcomposite1",
    "libXdamage.so.1": "libXdamage1",
    "libXfixes.so.3": "libXfixes3",
    "libXrandr.so.2": "libxrandr2",
    "libXrender.so.1": "libxrender1",
    "libXtst.so.6": "libxtst6",
    "libXi.so.6": "libxi6",
    "libgbm.so.1": "libgbm1",
    "libpango-1.0.so.0": "libpango-1.0-0",
    "libpangocairo-1.0.so.0": "libpango-1.0-0",
    "libcairo.so.2": "libcairo2",
    "libasound.so.2": "libasound2",
    "libexpat.so.1": "libexpat1",
    "libdbus-1.so.3": "libdbus-1-3",
    "libglib-2.0.so.0": "libglib2.0-0",
}


def which(name: str) -> str:
    """命令的绝对路径（找不到返回空串）。"""
    try:
        return shutil.which(str(name)) or ""
    except Exception:
        return ""


def is_root() -> bool:
    getter = getattr(os, "geteuid", None)
    if getter is None:
        return False
    try:
        return getter() == 0
    except Exception:
        return False


def display_available() -> bool:
    """有头浏览器能不能起来：桌面系统直接算有；Linux 看 DISPLAY/Wayland。"""
    if IS_WINDOWS or IS_MAC:
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def home_dir() -> Path | None:
    """用户主目录；服务账号没设 HOME 时 ``Path.home()`` 会抛异常，这里兜住。"""
    try:
        return Path.home()
    except Exception:
        return None


def playwright_cache_roots() -> list[Path]:
    """playwright 内核缓存目录候选（按优先级去重）。"""
    roots: list[Path] = []
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if override and override not in {"0", "1"}:
        try:
            roots.append(Path(override).expanduser())
        except Exception:
            roots.append(Path(override))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "ms-playwright")
    home = home_dir()
    if home is not None:
        if IS_MAC:
            roots.append(home / "Library" / "Caches" / "ms-playwright")
        roots.append(home / ".cache" / "ms-playwright")
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def chromium_patterns() -> tuple[str, ...]:
    """各平台 playwright 完整 chromium 可执行文件的相对路径模式。"""
    return (
        "chromium-*/chrome-win64/chrome.exe",
        "chromium-*/chrome-win/chrome.exe",
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-linux64/chrome",
        "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
        "chromium-*/chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
    )


def missing_shared_libs(binary: str | Path) -> list[str]:
    """Linux：ldd 里 ``not found`` 的库名（其它平台返回空）。"""
    if not IS_LINUX:
        return []
    exe = str(binary or "")
    if not exe or not Path(exe).exists() or not which("ldd"):
        return []
    try:
        proc = subprocess.run(["ldd", exe], capture_output=True, text=True,
                              timeout=25, errors="replace")
    except Exception:
        return []
    missing: list[str] = []
    for line in (proc.stdout or "").splitlines():
        if "not found" not in line:
            continue
        head = line.split("=>")[0].strip()
        name = head.split()[0] if head else ""
        if name and name not in missing:
            missing.append(name)
    return missing


def apt_packages_for(libs: Iterable[str]) -> list[str]:
    """缺失库 → 需要的 apt 包（去重保序，未知库丢弃）。"""
    packages: list[str] = []
    for lib in libs:
        package = _LIB_PACKAGES.get(str(lib))
        if package and package not in packages:
            packages.append(package)
    return packages


def cjk_font_files() -> list[str]:
    """本机能用的中文字体文件（环境变量 → 常见路径 → fc-list）。"""
    found: list[str] = []
    override = os.environ.get("SPARKING_CJK_FONT", "").strip()
    if override:
        try:
            candidate = Path(override).expanduser()
        except Exception:
            candidate = Path(override)
        if candidate.is_file():
            found.append(str(candidate))
    for candidate in _CJK_FONT_PATHS:
        if candidate not in found and Path(candidate).is_file():
            found.append(candidate)
    if not found:
        found.extend(_fc_list_cjk())
    return found


def _fc_list_cjk() -> list[str]:
    """``fc-list :lang=zh file`` 的兜底（字体装在非常规目录时也能找到）。"""
    binary = which("fc-list")
    if not binary:
        return []
    try:
        proc = subprocess.run([binary, ":lang=zh", "file"], capture_output=True,
                              text=True, timeout=20, errors="replace")
    except Exception:
        return []
    out: list[str] = []
    for line in (proc.stdout or "").splitlines():
        path = line.split(":", 1)[0].strip()
        if not path:
            continue
        for suffix in (".ttc", ".ttf", ".otf", ".otc"):
            if path.lower().endswith(suffix):
                out.append(path)
                break
    return out


_CJK_MARKERS = ("noto", "wqy", "zenhei", "microhei", "uming", "ukai", "droid",
                "sourcehan", "msyh", "simhei", "simsun", "pingfang", "hiragino",
                "arial unicode", "fallback")


def _is_cjk_paths(paths: Iterable[str]) -> bool:
    """给出的字体文件里有没有真正的中文字体（DejaVu 这种不算）。"""
    for path in paths:
        lowered = str(path).lower()
        if any(marker in lowered for marker in _CJK_MARKERS):
            return True
    return False


def has_cjk_font() -> bool:
    """有真正的中文字体吗（DejaVu 这种不含中文的不算）。"""
    return _is_cjk_paths(cjk_font_files())


def utf8_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """子进程环境：保证 Python 脚本按 UTF-8 读写（Windows 默认 GBK 会炸中文）。"""
    env = dict(base if base is not None else os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    if IS_WINDOWS:
        env.setdefault("PYTHONLEGACYWINDOWSSTDIO", "0")
    return env


def spawn_kwargs() -> dict[str, Any]:
    """子进程启动参数：Windows 隐藏控制台窗口，POSIX 建独立进程组。"""
    if IS_WINDOWS:
        flags = 0
        creator = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        flags |= creator
        return {"creationflags": flags} if flags else {}
    return {"start_new_session": True}


def terminate_tree(process: Any) -> None:
    """超时后连子孙进程一起收掉（否则 python 脚本再起子进程会变成孤儿）。"""
    if process is None:
        return
    pid = getattr(process, "pid", None)
    if not pid:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=20)
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def run_capture(command: Sequence[str], *, cwd: str | Path | None = None,
                timeout: float = 60.0, env: dict[str, str] | None = None
                ) -> dict[str, Any]:
    """跑一个子进程并收输出；超时返回 ``timed_out`` 而不是抛异常。

    进程用独立会话启动（POSIX）／隐藏窗口（Windows），超时按进程组回收，
    输出统一按 UTF-8 解码（解码失败替换字符，不炸）。
    """
    payload: dict[str, Any] = {"exit_code": -1, "stdout": "", "stderr": "",
                               "timed_out": False}
    try:
        process = subprocess.Popen(
            [str(part) for part in command],
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=utf8_env(env),
            **spawn_kwargs(),
        )
    except FileNotFoundError as error:
        payload["exit_code"] = 127
        payload["stderr"] = f"not found: {error}"
        return payload
    except Exception as error:
        payload["stderr"] = f"spawn failed: {error}"
        return payload
    try:
        out, err = process.communicate(timeout=max(1.0, float(timeout)))
        payload["exit_code"] = int(process.returncode or 0)
        payload["stdout"] = (out or b"").decode("utf-8", "replace")
        payload["stderr"] = (err or b"").decode("utf-8", "replace")
        return payload
    except subprocess.TimeoutExpired:
        payload["timed_out"] = True
        terminate_tree(process)
        try:
            out, err = process.communicate(timeout=10)
            payload["stdout"] = (out or b"").decode("utf-8", "replace")
            payload["stderr"] = (err or b"").decode("utf-8", "replace")
        except Exception:
            pass
        return payload


def shell_interpreters() -> dict[str, list[str] | None]:
    """按本机实际安装情况给出各脚本后缀的解释器（缺的给 None）。"""
    bash = which("bash") or which("sh")
    node = which("node")
    pwsh = which("pwsh") or which("powershell")
    mapping: dict[str, list[str] | None] = {
        ".py": None,  # 用当前解释器
        ".js": [node] if node else None,
        ".mjs": [node] if node else None,
        ".cjs": [node] if node else None,
        ".sh": [bash] if bash else None,
        ".bash": [bash] if bash else None,
        ".ps1": [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
                if pwsh else None,
        ".bat": ["cmd", "/c"] if IS_WINDOWS else None,
        ".cmd": ["cmd", "/c"] if IS_WINDOWS else None,
    }
    return mapping


def system_report(*, browser_executable: str = "",
                  browser_env: dict[str, Any] | None = None) -> dict[str, Any]:
    """平台自检报告（控制台「系统与环境」与 bot 自查工具共用）。"""
    fonts = cjk_font_files()
    has_cjk = _is_cjk_paths(fonts)
    report: dict[str, Any] = {
        "os": f"{platform.system()} {platform.release()}",
        "platform": sys.platform,
        "machine": platform.machine(),
        "python": platform.python_version(),
        "executable": sys.executable,
        "is_root": is_root(),
        "display": display_available(),
        "cjk_font": has_cjk,
        "cjk_font_files": fonts[:4],
        "commands": {name: which(name) for name in
                     ("bash", "sh", "node", "pwsh", "ffmpeg", "fc-list",
                      "apt-get", "ldd")},
    }
    exe = str(browser_executable or "")
    if not exe:
        try:
            from .pw_driver import _resolve_full_chromium

            exe = _resolve_full_chromium()
        except Exception:
            exe = ""
    missing = missing_shared_libs(exe)
    packages = apt_packages_for(missing)
    report["browser"] = {
        "executable": exe,
        "missing_libs": missing,
        "apt_packages": packages,
        "hint": ("sudo apt-get install -y " + " ".join(packages)) if packages else "",
    }
    if browser_env:
        report["browser"].update({
            "playwright": bool(browser_env.get("playwright")),
            "chromium": bool(browser_env.get("chromium")),
            "error": str(browser_env.get("error") or "")[:300],
        })
    if IS_LINUX and not has_cjk:
        report["hint"] = ("服务器没装中文字体，截图/图表里的中文会显示成方块："
                          "sudo apt-get install -y fonts-noto-cjk")
    return report
