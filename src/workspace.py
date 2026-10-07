"""工作区文件能力（OpenClaw 文件/exec 工具的移植版）。

给 bot 一个受限的工作区：对 AstrBot 数据目录（含各插件的数据目录）与插件自己的
workspace 目录做文件增删改查、目录搜索，以及在**允许根内**运行脚本。所有路径都
先 resolve 再校验包含关系，越界（含符号链接逃逸）一律拒绝；读写有大小上限。
"""
from __future__ import annotations

import asyncio
import shlex
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_READ_CHARS = 200_000
MAX_WRITE_BYTES = 4 * 1024 * 1024
MAX_LIST_ENTRIES = 500
MAX_SEARCH_RESULTS = 200
MAX_SCRIPT_OUTPUT = 12_000
SCRIPT_TIMEOUT_LIMIT = 600


class WorkspaceError(RuntimeError):
    pass


def _stamp(path: Path) -> str:
    try:
        return datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return ""


class Workspace:
    """一个（或多个）允许根下的受限文件系统视图。"""

    def __init__(self, roots: Sequence[str | Path], *,
                 max_read_chars: int = MAX_READ_CHARS,
                 max_write_bytes: int = MAX_WRITE_BYTES) -> None:
        resolved: list[Path] = []
        for root in roots:
            candidate = Path(root).expanduser()
            candidate.mkdir(parents=True, exist_ok=True)
            resolved.append(candidate.resolve())
        if not resolved:
            raise WorkspaceError("至少需要一个工作区根目录")
        self.roots = tuple(resolved)
        self.max_read_chars = int(max_read_chars)
        self.max_write_bytes = int(max_write_bytes)

    @property
    def root(self) -> Path:
        """主工作目录（相对路径的基准）。"""
        return self.roots[-1] if len(self.roots) > 1 else self.roots[0]

    # ------------------------------------------------------------ 路径校验
    def resolve(self, path: str | Path, *, base: Path | None = None) -> Path:
        """把（可能是相对的）路径解析到允许根内；越界抛 WorkspaceError。"""
        raw = str(path or "").strip()
        if not raw:
            raw = "."
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = (base or self.root) / candidate
        try:
            resolved = candidate.resolve()
        except OSError as error:
            raise WorkspaceError(f"路径无法解析：{raw}") from error
        for root in self.roots:
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue
        raise WorkspaceError(
            f"路径不在允许的工作目录内：{raw}（允许："
            + "、".join(str(r) for r in self.roots) + "）")

    # ------------------------------------------------------------ 文件操作
    def list(self, path: str = ".", *, limit: int = MAX_LIST_ENTRIES) -> dict[str, Any]:
        target = self.resolve(path)
        if target.is_file():
            return {"path": str(target), "type": "file",
                    "size": target.stat().st_size, "modified": _stamp(target)}
        if not target.is_dir():
            raise WorkspaceError(f"路径不存在：{target}")
        entries: list[dict[str, Any]] = []
        for item in sorted(target.iterdir(),
                           key=lambda p: (p.is_file(), p.name.lower())):
            try:
                entries.append({
                    "name": item.name,
                    "type": "dir" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else 0,
                    "modified": _stamp(item),
                })
            except OSError:
                continue
            if len(entries) >= int(limit):
                break
        return {"path": str(target), "type": "dir", "entries": entries,
                "truncated": len(entries) >= int(limit)}

    def read(self, path: str, *, offset: int = 0,
             max_chars: int = MAX_READ_CHARS) -> dict[str, Any]:
        target = self.resolve(path)
        if not target.is_file():
            raise WorkspaceError(f"文件不存在：{target}")
        if target.stat().st_size > self.max_write_bytes * 4:
            raise WorkspaceError("文件过大，读不了（上限 %d 字节）" % (self.max_write_bytes * 4))
        data = target.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", "replace")
        offset = max(0, int(offset or 0))
        cap = min(max(200, int(max_chars or self.max_read_chars)), self.max_read_chars)
        chunk = text[offset:offset + cap]
        return {"path": str(target), "size": len(data), "offset": offset,
                "text": chunk, "truncated": offset + cap < len(text),
                "total_chars": len(text)}

    def write(self, path: str, content: str, *, append: bool = False) -> dict[str, Any]:
        target = self.resolve(path)
        data = str(content or "").encode("utf-8")
        if len(data) > self.max_write_bytes:
            raise WorkspaceError(f"内容超过写入上限（{self.max_write_bytes} 字节）")
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "ab" if append else "wb"
        with target.open(mode) as handle:
            handle.write(data)
        return {"path": str(target), "bytes": len(data),
                "size": target.stat().st_size, "appended": bool(append)}

    def delete(self, path: str, *, recursive: bool = False) -> dict[str, Any]:
        target = self.resolve(path)
        if target == self.root or any(target == r for r in self.roots):
            raise WorkspaceError("不允许删除工作区根目录本身")
        if target.is_dir():
            if any(target.iterdir()) and not recursive:
                raise WorkspaceError("目录非空：要递归删除请传 recursive=true")
            import shutil

            count = sum(1 for _ in target.rglob("*"))
            shutil.rmtree(target)
            return {"path": str(target), "removed": count + 1, "type": "dir"}
        if not target.exists():
            raise WorkspaceError(f"路径不存在：{target}")
        target.unlink()
        return {"path": str(target), "removed": 1, "type": "file"}

    def move(self, src: str, dst: str) -> dict[str, Any]:
        source = self.resolve(src)
        if not source.exists():
            raise WorkspaceError(f"源路径不存在：{source}")
        raw = str(dst or "").strip()
        if not raw:
            raise WorkspaceError("需要目标路径")
        target = self.resolve(raw)
        if target.exists() and target.is_dir():
            target = target / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        return {"from": str(source), "to": str(target)}

    def search(self, pattern: str, *, root: str = ".", limit: int = 50) -> dict[str, Any]:
        query = str(pattern or "").strip()
        if not query:
            raise WorkspaceError("需要搜索模式（glob，如 *.py 或 **/*.json）")
        base = self.resolve(root)
        cap = min(max(1, int(limit or 50)), MAX_SEARCH_RESULTS)
        matches: list[dict[str, Any]] = []
        for item in base.glob(query):
            try:
                if item.is_file():
                    matches.append({"path": str(item), "size": item.stat().st_size})
            except OSError:
                continue
            if len(matches) >= cap:
                break
        return {"root": str(base), "pattern": query, "matches": matches,
                "truncated": len(matches) >= cap}

    # ------------------------------------------------------------ 脚本运行
    _INTERPRETERS = {
        ".py": None,                     # 用当前解释器（sys.executable）
        ".js": ["node"], ".mjs": ["node"], ".cjs": ["node"],
        ".sh": ["bash"], ".bash": ["bash"],
        ".bat": ["cmd", "/c"], ".cmd": ["cmd", "/c"],
        ".ps1": ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"],
    }

    def script_command(self, script: Path, args: Sequence[str]) -> list[str]:
        if script.suffix.lower() == ".py":
            command = [sys.executable, str(script)]
        else:
            prefix = self._INTERPRETERS.get(script.suffix.lower())
            if prefix is None:
                raise WorkspaceError(
                    f"不支持的脚本类型：{script.suffix}（支持 .py/.js/.sh/.bat/.ps1）")
            command = [*prefix, str(script)]
        return [*command, *[str(a) for a in args]]

    async def run_script(self, path: str, *, args: str = "",
                         timeout: int = 60) -> dict[str, Any]:
        script = self.resolve(path)
        if not script.is_file():
            raise WorkspaceError(f"脚本不存在：{script}")
        limit = min(max(5, int(timeout or 60)), SCRIPT_TIMEOUT_LIMIT)
        try:
            extra = shlex.split(str(args or ""), posix=False)
        except ValueError as error:
            raise WorkspaceError(f"参数解析失败：{error}") from error
        command = self.script_command(script, extra)

        def _run() -> dict[str, Any]:
            try:
                completed = subprocess.run(
                    command, cwd=str(script.parent), capture_output=True,
                    timeout=limit, text=True, encoding="utf-8", errors="replace",
                )
                return {
                    "exit_code": completed.returncode,
                    "stdout": (completed.stdout or "")[:MAX_SCRIPT_OUTPUT],
                    "stderr": (completed.stderr or "")[:MAX_SCRIPT_OUTPUT],
                    "truncated": len(completed.stdout or "") > MAX_SCRIPT_OUTPUT
                    or len(completed.stderr or "") > MAX_SCRIPT_OUTPUT,
                }
            except subprocess.TimeoutExpired:
                return {"exit_code": -1, "stdout": "", "stderr": "",
                        "error": f"超时（{limit} 秒）"}

        result = await asyncio.to_thread(_run)
        return {"script": str(script), "command": command[0], **result}
