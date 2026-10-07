"""把源代码渲染成 VS Code Dark+ 风格的 HTML 页（给 render_code 工具截图用）。

零依赖、零外链（服务器可能没有外网 CDN）：一个手写的正则 tokenizer 做轻量
语法高亮，覆盖 Python/JS/TS/JSON/HTML/CSS/C/Java/Go/Rust/Bash/SQL 的关键词、
字符串、注释、数字、函数名、装饰器；其余语言退化为通用高亮。
配色取自 VS Code "Dark+"：#1e1e1e 底、关键字 #569cd6、字符串 #ce9178、
注释 #6a9955、数字 #b5cea8、函数 #dcdcaa、类 #4ec9b8。
"""
from __future__ import annotations

import html
import re

_TOKEN_RE = re.compile(
    r"""(?P<block>/\*[\s\S]*?\*/)
      |(?P<comment>\#[^\n]*|//[^\n]*|--[^\n]*)
      |(?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)
      |(?P<decorator>@[A-Za-z_][\w.]*)
      |(?P<number>\b\d+(?:\.\d+)?\b)
      |(?P<word>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.M | re.X,
)

_KEYWORDS: dict[str, frozenset[str]] = {
    "python": frozenset(
        "def class return if elif else for while try except finally with as import from "
        "pass raise lambda global nonlocal yield assert del in is not and or None True "
        "False async await match case self".split()),
    "javascript": frozenset(
        "function const let var return if else for while try catch finally class extends "
        "new delete typeof instanceof in of this super import export from default async "
        "await yield switch case break continue throw null undefined true false".split()),
    "typescript": frozenset(
        "function const let var return if else for while try catch finally class extends "
        "implements interface type enum new delete typeof instanceof in of this super "
        "import export from default async await yield switch case break continue throw "
        "null undefined true false readonly public private protected static declare".split()),
    "json": frozenset({"true", "false", "null"}),
    "css": frozenset(
        "important media supports keyframes import from to and not only".split()),
    "c": frozenset(
        "int char float double void long short unsigned signed struct union enum static "
        "const return if else for while switch case break continue sizeof typedef "
        "include define ifdef endif NULL true false".split()),
    "java": frozenset(
        "public private protected static final void int long double float boolean char "
        "class interface extends implements new return if else for while switch case "
        "break continue try catch finally throw throws import package this super null "
        "true false enum record".split()),
    "go": frozenset(
        "func package import type struct interface map chan go defer select switch case "
        "return if else for range break continue var const nil true false".split()),
    "rust": frozenset(
        "fn let mut pub struct enum impl trait use mod match if else for while loop "
        "return break continue async await move ref static const crate self super "
        "Some None Ok Err true false".split()),
    "sql": frozenset(
        "SELECT FROM WHERE INSERT INTO VALUES UPDATE SET DELETE CREATE TABLE DROP ALTER "
        "JOIN LEFT RIGHT INNER OUTER ON GROUP ORDER BY LIMIT OFFSET AND OR NOT NULL AS "
        "DISTINCT COUNT SUM AVG MIN MAX".split()),
    "bash": frozenset(
        "if then else elif fi for while do done case esac function return export local "
        "echo cd sudo apt pip npm in".split()),
}

_LANGUAGE_ALIASES = {
    "py": "python", "python3": "python",
    "js": "javascript", "node": "javascript", "jsx": "javascript",
    "ts": "typescript", "tsx": "typescript",
    "c++": "c", "cpp": "c", "cc": "c", "hpp": "c", "h": "c",
    "kotlin": "java", "kt": "java",
    "shell": "bash", "sh": "bash", "zsh": "bash", "console": "bash",
    "golang": "go",
    "rs": "rust",
    "yml": "yaml",
}

_CANONICAL = set(_KEYWORDS) | {"yaml", "html", "xml", "markdown", "plain"}


def normalize_language(language: str) -> str:
    lang = str(language or "").strip().lower()
    lang = _LANGUAGE_ALIASES.get(lang, lang)
    return lang if lang in _CANONICAL else ""


def _keywords_for(language: str) -> frozenset[str]:
    if language in _KEYWORDS:
        return _KEYWORDS[language]
    return frozenset()


def highlight(code: str, language: str = "") -> str:
    """Escape + 轻量高亮，返回可直接嵌页面的 HTML 片段。"""
    lang = normalize_language(language)
    keywords = _keywords_for(lang)
    out: list[str] = []
    last = 0

    def emit(piece: str, cls: str = "") -> None:
        escaped = html.escape(piece)
        out.append(f'<span class="{cls}">{escaped}</span>' if cls else escaped)

    for match in _TOKEN_RE.finditer(code):
        emit(code[last:match.start()])
        kind = match.lastgroup
        value = match.group()
        if kind == "word":
            if value in keywords:
                emit(value, "kw")
            elif lang in {"json"} and value in {"true", "false", "null"}:
                emit(value, "kw")
            else:
                nxt = code[match.end():match.end() + 1]
                nxt_beyond = code[match.end():].lstrip()[:1]
                if nxt == "(" or (lang == "css" and nxt_beyond == ":"):
                    emit(value, "fn")
                elif value[:1].isupper():
                    emit(value, "cls")
                else:
                    emit(value)
        elif kind in {"comment", "block"}:
            emit(value, "cm")
        elif kind == "string":
            emit(value, "st")
        elif kind == "number":
            emit(value, "nm")
        elif kind == "decorator":
            emit(value, "dc")
        else:
            emit(value)
        last = match.end()
    emit(code[last:])
    return "".join(out)


def code_to_page(code: str, language: str = "", filename: str = "") -> str:
    """Wrap highlighted code in a VS Code-like window page (screenshot-ready)."""
    lang = normalize_language(language)
    name = html.escape(str(filename or f"code{('.' + lang) if lang else ''}"))
    lines = str(code).rstrip("\n").split("\n")[:400]
    body = "\n".join(highlight(line, lang) for line in lines)
    gutter = "\n".join(str(i) for i in range(1, len(lines) + 1))
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
        "html,body{margin:0;padding:0;background:#181818;}"
        ".window{width:900px;margin:24px auto;border-radius:10px;overflow:hidden;"
        "box-shadow:0 18px 48px rgba(0,0,0,.45);font-family:'JetBrains Mono',"
        "'Cascadia Code',Consolas,'Courier New',monospace;}"
        ".titlebar{display:flex;align-items:center;background:#323233;padding:9px 14px;}"
        ".dot{width:12px;height:12px;border-radius:50%;margin-right:8px;}"
        ".dot.r{background:#ff5f57}.dot.y{background:#febc2e}.dot.g{background:#28c840}"
        ".file{margin-left:14px;color:#cccccc;font-size:13px;"
        "background:#1e1e1e;padding:4px 14px;border-radius:6px 6px 0 0;}"
        ".lang{margin-left:auto;color:#858585;font-size:12px;}"
        ".editor{display:flex;background:#1e1e1e;padding:12px 0;"
        "max-height:640px;overflow:hidden;}"
        ".gutter{color:#6e7681;text-align:right;padding:0 14px;user-select:none;"
        "font-size:13px;line-height:20px;white-space:pre;}"
        "pre{margin:0;padding:0 18px 0 4px;font-size:13px;line-height:20px;"
        "color:#d4d4d4;white-space:pre-wrap;word-break:break-word;flex:1;}"
        ".kw{color:#569cd6}.st{color:#ce9178}.cm{color:#6a9955}"
        ".nm{color:#b5cea8}.fn{color:#dcdcaa}.cls{color:#4ec9b8}.dc{color:#dcdcaa}"
        "</style></head><body>"
        '<div class="window"><div class="titlebar">'
        '<span class="dot r"></span><span class="dot y"></span><span class="dot g"></span>'
        f'<span class="file">{name}</span>'
        f'<span class="lang">{html.escape(lang or "text")}</span>'
        "</div>"
        f'<div class="editor"><div class="gutter">{gutter}</div>'
        f"<pre>{body}</pre></div></div></body></html>"
    )
