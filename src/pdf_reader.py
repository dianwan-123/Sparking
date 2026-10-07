"""文档文本抽取：给 bot 的 read_document 工具供料。

- PDF 走 pypdf（纯 Python；缺失时自动 pip 安装，走清华镜像，与
  playwright/paramiko/flask 同策略——不进 requirements.txt 以避开核心依赖
  版本保护）。
- 纯文本类（txt/md/代码/json/csv 等）直接读。
- 扫描件（PDF 无文本层）如实报告，不做 OCR。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

MAX_PAGES = 60
MAX_CHARS = 24_000

_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".csv", ".log", ".json", ".yaml", ".yml",
    ".py", ".js", ".ts", ".html", ".css", ".xml", ".ini", ".toml", ".sql",
    ".sh", ".bat", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".rb",
}

_pypdf_state: tuple[bool, str] | None = None


def ensure_pypdf() -> tuple[bool, str]:
    """Import pypdf; on first miss pip-install it (China mirror first)."""
    global _pypdf_state
    if _pypdf_state is not None:
        return _pypdf_state
    try:
        import pypdf  # noqa: F401

        _pypdf_state = (True, "ok")
        return _pypdf_state
    except Exception:
        pass
    for index in ("https://pypi.tuna.tsinghua.edu.cn/simple", ""):
        command = [sys.executable, "-m", "pip", "install", "--quiet", "pypdf>=4.0"]
        if index:
            command += ["-i", index]
        try:
            subprocess.run(command, capture_output=True, timeout=300, check=True)
            import pypdf  # noqa: F401

            _pypdf_state = (True, "ok")
            return _pypdf_state
        except Exception:
            continue
    _pypdf_state = (False, "pypdf 自动安装失败（检查服务器网络/pip）")
    return _pypdf_state


def extract_document_text(
    path: str | Path,
    *,
    max_pages: int = MAX_PAGES,
    max_chars: int = MAX_CHARS,
) -> dict[str, Any]:
    """Extract text from a document file. Returns
    {kind, pages?, text, truncated, note}."""
    target = Path(path)
    if not target.is_file():
        return {"kind": "missing", "text": "", "truncated": False,
                "note": f"文件不存在：{target.name}"}
    suffix = target.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        raw = target.read_text(encoding="utf-8", errors="replace")
        truncated = len(raw) > max_chars
        return {"kind": "text", "text": raw[:max_chars], "truncated": truncated,
                "note": ""}
    if suffix != ".pdf":
        return {"kind": "unsupported", "text": "", "truncated": False,
                "note": f"暂不支持 {suffix or '无后缀'} 文件的文本抽取"}
    ok, detail = ensure_pypdf()
    if not ok:
        return {"kind": "error", "text": "", "truncated": False, "note": detail}
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(target))
        total = len(reader.pages)
        pages_text: list[str] = []
        read = min(total, max(1, int(max_pages)))
        for index in range(read):
            try:
                pages_text.append(reader.pages[index].extract_text() or "")
            except Exception as error:  # 单页损坏不报废整份文档
                pages_text.append(f"[第{index + 1}页解析失败：{type(error).__name__}]")
        joined = "\n".join(
            f"--- 第{i + 1}页 ---\n{chunk.strip()}"
            for i, chunk in enumerate(pages_text) if chunk.strip()
        )
        if not joined.strip():
            return {
                "kind": "pdf", "pages": total, "text": "", "truncated": False,
                "note": ("这份 PDF 没有文本层（多半是扫描件/图片型 PDF），"
                         "无法直接读出文字；如需内容请让用户发关键页的截图，用看图能力分析"),
            }
        truncated = read < total or len(joined) > max_chars
        return {"kind": "pdf", "pages": total, "text": joined[:max_chars],
                "truncated": truncated, "note": ""}
    except Exception as error:
        return {"kind": "error", "text": "", "truncated": False,
                "note": f"PDF 解析失败：{type(error).__name__}: {str(error)[:160]}"}
