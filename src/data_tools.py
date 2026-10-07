"""表格/数据工具（`read_tabular` 的真正实现）。

背景实录：模型在"读服务器指标画图"这类任务里反复调用 `read_tabular`
（读表格 + 跑 pandas 代码），而插件里没有这个名字的工具——AstrBot 直接
"未找到指定的工具，将跳过"，模型失去数据通道只剩空头承诺。这里把它做成
真工具：CSV/Excel/JSON/TSV 读成 df，或 file_path 留空时当"带 pandas 的
Python 沙箱"用（与 python_exec 同级信任，只是预装 pandas 语境）。
"""
from __future__ import annotations

import asyncio
import contextlib
import io as _io
import subprocess
import sys
from pathlib import Path
from typing import Any

_PANDAS_STATE: tuple[bool, str] | None = None
_MPL_STATE: tuple[bool, str] | None = None
MAX_OUTPUT = 8000

# matplotlib 中文字体候选（Windows 服务器/AstrBot 常见目录）
_CJK_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
)


def _pip_install(names: tuple[str, ...]) -> bool:
    for index in ("https://pypi.tuna.tsinghua.edu.cn/simple", ""):
        command = [sys.executable, "-m", "pip", "install", "--quiet", *names]
        if index:
            command += ["-i", index]
        try:
            subprocess.run(command, capture_output=True, timeout=600, check=True)
            return True
        except Exception:
            continue
    return False


def ensure_pandas() -> tuple[bool, str]:
    """Import pandas; on first miss pip-install it (China mirror first)."""
    global _PANDAS_STATE
    if _PANDAS_STATE is not None:
        return _PANDAS_STATE
    try:
        import pandas  # noqa: F401

        _PANDAS_STATE = (True, "ok")
        return _PANDAS_STATE
    except Exception:
        pass
    if _pip_install(("pandas", "openpyxl")):
        try:
            import pandas  # noqa: F401

            _PANDAS_STATE = (True, "ok")
            return _PANDAS_STATE
        except Exception:
            pass
    _PANDAS_STATE = (False, "pandas 自动安装失败（检查服务器网络/pip）")
    return _PANDAS_STATE


def ensure_matplotlib() -> tuple[bool, str]:
    """懒加载 matplotlib（只有代码里出现画图关键词才会走到这）。"""
    global _MPL_STATE
    if _MPL_STATE is not None:
        return _MPL_STATE
    try:
        import matplotlib  # noqa: F401

        _MPL_STATE = (True, "ok")
        return _MPL_STATE
    except Exception:
        pass
    if _pip_install(("matplotlib",)):
        try:
            import matplotlib  # noqa: F401

            _MPL_STATE = (True, "ok")
            return _MPL_STATE
        except Exception:
            pass
    _MPL_STATE = (False, "matplotlib 自动安装失败：可改用 design_render 画 SVG，"
                         "或先 python_exec 里 pip install matplotlib")
    return _MPL_STATE


def _apply_cjk_font(matplotlib_mod: Any) -> None:
    """给 matplotlib 挂上手边能用的中文字体，免得图上全是豆腐块。"""
    try:
        from matplotlib import font_manager
    except Exception:
        return
    for candidate in _CJK_FONT_CANDIDATES:
        path = Path(candidate)
        if not path.is_file():
            continue
        try:
            font_manager.fontManager.addfont(str(path))
            name = font_manager.FontProperties(fname=str(path)).get_name()
            matplotlib_mod.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            return
        except Exception:
            continue


def _make_save_chart(plt_mod: Any, resolve_path: Any) -> Any:
    def save_chart(name: str = "chart.png") -> str:
        """把当前 figure 存成工作区里的图片，返回绝对路径（再交给 send_local_file 发）。"""
        target = Path(resolve_path(str(name or "chart.png")))
        target.parent.mkdir(parents=True, exist_ok=True)
        plt_mod.savefig(target, dpi=140, bbox_inches="tight")
        return str(target)

    return save_chart


def _load_table(pandas_mod: Any, path: Path) -> Any:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        return pandas_mod.read_excel(path)
    if suffix == ".json":
        return pandas_mod.read_json(path)
    if suffix == ".tsv":
        return pandas_mod.read_csv(path, sep="\t")
    return pandas_mod.read_csv(path)


async def run_tabular(
    code: str,
    *,
    file_path: str = "",
    resolve_path: Any | None = None,
    extra_globals: dict[str, Any] | None = None,
    timeout: float = 60.0,
    max_output: int = MAX_OUTPUT,
) -> str:
    """执行 pandas 代码；file_path 非空时先读成 ``df``。

    ``resolve_path`` 形如 ``callable(raw) -> Path``（通常接 Workspace.resolve），
    用于把表格文件限制在允许目录内。
    """
    program = str(code or "").strip()
    if not program:
        return ("需要 pandas_operations（要执行的 pandas 代码；结果赋给 result "
                "变量或 print 出来）")
    ok, note = await asyncio.to_thread(ensure_pandas)
    if not ok:
        return note
    import pandas as pd

    table_ref = str(file_path or "").strip()
    if table_ref and table_ref.lower() in {"__noop__", "noop", "dummy", "n/a",
                                           "none", "null", "-"}:
        table_ref = ""
    namespace: dict[str, Any] = {"pd": pd, "__name__": "tabular",
                                 **(extra_globals or {})}
    if table_ref:
        if resolve_path is None:
            return "表格工具未配置路径校验（内部错误）"
        try:
            path = Path(resolve_path(table_ref))
        except Exception as error:
            return f"表格路径不允许：{str(error)[:150]}"
        if not path.is_file():
            return (f"表格文件不存在：{path}。要读文件请给工作区内的真实路径；"
                    "只想跑 pandas 代码就把 file_path 留空")
        if path.stat().st_size > 64 * 1024 * 1024:
            return "表格文件过大（>64MB）"
        namespace["df"] = await asyncio.to_thread(_load_table, pd, path)
    # 画图关键词命中才懒加载 matplotlib —— 沙箱里直接 plt.plot(...) 画图、
    # save_chart("状态图.png") 存进工作区，再 send_local_file 发出去。
    if any(token in program for token in ("matplotlib", "pyplot", "plt.", "save_chart")):
        plot_ok, plot_note = await asyncio.to_thread(ensure_matplotlib)
        if not plot_ok:
            return plot_note
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            matplotlib.rcParams["axes.unicode_minus"] = False
            await asyncio.to_thread(_apply_cjk_font, matplotlib)
            namespace["matplotlib"] = matplotlib
            namespace["plt"] = plt
            if resolve_path is not None:
                namespace["save_chart"] = _make_save_chart(plt, resolve_path)
        except Exception as error:
            return f"matplotlib 初始化失败：{type(error).__name__}: {error}"
    full_code = program + (
        "\nif globals().get('result') is not None:\n"
        "    print('RESULT:', result)\n")

    def _run() -> str:
        buffer = _io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                exec(compile(full_code, "<read_tabular>", "exec"), namespace)  # noqa: S102
        except Exception as error:
            partial = buffer.getvalue()[:2000]
            return (f"执行出错：{type(error).__name__}: {error}"
                    + (f"\n部分输出：{partial}" if partial else ""))
        return buffer.getvalue() or "（无输出；把结果赋给 result 或 print 出来）"

    try:
        async with asyncio.timeout(float(timeout)):
            output = await asyncio.to_thread(_run)
    except TimeoutError:
        return f"执行超时（{int(timeout)}秒）"
    return output[:max_output]
