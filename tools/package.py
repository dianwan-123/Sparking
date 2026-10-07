"""打包插件 zip。

既作命令行脚本（``python tools/package.py``），也作可导入函数（``build(out_dir)``），
测试现场打包校验包内容，不依赖 dist 里是否有历史产物。
"""

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "dist"
ZIP_NAME = "astrbot_plugin_long_memory_agent-v1.0.0.zip"
EXCLUDE_DIRS = {".references", ".refs", ".validation", ".zcode", ".napcat_docs", "plugin_data", "tests", "tools", "dist", "__pycache__", "build"}
EXCLUDE_FILES = {"tests_last_run.txt"}


def _skip(name: str) -> bool:
    """隐藏文件/临时文件一律不进包（曾把 .oc.json 等探测用临时文件打进 zip）。"""
    return (name.startswith(".") and name not in {".gitignore", ".gitkeep"}) \
        or name in EXCLUDE_FILES or name.endswith(".pyc") or name.endswith(".tmp")


def build(out_dir: Path | None = None, root: Path | None = None) -> Path:
    """打包并返回 zip 路径。打包前校验 schema JSON 与版本号，杜绝打坏包。"""
    root = Path(root or ROOT)
    target_dir = Path(out_dir or DEFAULT_OUT)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / ZIP_NAME

    with open(root / "_conf_schema.json", encoding="utf-8") as handle:
        json.load(handle)
    with open(root / "metadata.yaml", encoding="utf-8") as handle:
        assert "v1.0.0" in handle.read()

    if target.exists():
        target.unlink()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if any(part in EXCLUDE_DIRS for part in relative.parts):
                continue
            # 隐藏目录（.pytest_cache/.zcode/…）整棵跳过，别只看文件名
            if any(part.startswith(".") and part not in {".gitignore", ".gitkeep",
                                                         ".astrbot-plugin"}
                   for part in relative.parts):
                continue
            if path.is_dir() or _skip(path.name):
                continue
            bundle.write(path, f"astrbot_plugin_long_memory_agent/{relative.as_posix()}")
    return target


if __name__ == "__main__":
    bundle_path = build()
    print("packaged:", bundle_path)
    print("size:", bundle_path.stat().st_size, "bytes")
    with zipfile.ZipFile(bundle_path) as bundle:
        names = bundle.namelist()
    print("files:", len(names))
    for name in names:
        print(" -", name)
