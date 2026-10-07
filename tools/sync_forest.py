"""把 pages/tree/forest.js 整体同步进 pages/memory/app.js 的内联副本。

插件页只暴露自己的静态目录，控制台没法用 <script src="../tree/forest.js">（会 404），
所以渲染器在 app.js 里留了一份同源拷贝。改渲染器后跑一下本脚本，
tests/test_webui_api.py 里那条"两份必须逐行一致"的守卫才过得去。
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "pages" / "tree" / "forest.js"
TARGET = ROOT / "pages" / "memory" / "app.js"
BANNER = ("// ===== 记忆森林渲染器（与 pages/tree/forest.js 同源，内联以保证插件页可用）=====\n"
          "// 改渲染器请改 pages/tree/forest.js，然后跑 tools/sync_forest.py 同步这一份。\n")


def main() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    target = TARGET.read_text(encoding="utf-8")
    start = target.index("// ===== 记忆森林渲染器")
    end = target.index("})();", target.index("(function () {", start)) + len("})();\n")
    updated = target[:start] + BANNER + "\n" + source.rstrip("\n") + "\n" + target[end:]
    TARGET.write_text(updated, encoding="utf-8")
    print("同步完成：", SOURCE.name, "→", TARGET.name)


if __name__ == "__main__":
    main()
