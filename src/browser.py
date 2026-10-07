"""Zero-dependency browser engine for LLM web browsing.

The deployment host may have no Chrome/Playwright and no third-party HTTP
libraries, so everything here is built on the Python standard library
(`urllib.request`, `http.cookiejar`, `html.parser`, `ssl`).

It models the useful part of a real browser session for an agent:

* tabs      — several independent pages with their own history cursor
* history   — back/forward over visited URLs
* links     — parsed anchors with stable indices for "click link 3"
* forms     — input/textarea/select fields with the values to submit
* cookies   — one cookie jar per session, sent on later requests
* text      — readable main text extracted from HTML

Everything is bounded (bytes, links, fields, history) and every fetch is
guarded by the same SSRF policy the media archive uses.
"""
from __future__ import annotations

import asyncio
import gzip
import http.cookiejar
import io
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from .media_archive import is_blocked_host

DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
MAX_TABS = 6
MAX_HISTORY = 30
MAX_LINKS = 120
MAX_FORMS = 40

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "section", "article", "header", "footer",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table",
}


class BrowserError(RuntimeError):
    """Raised for transport, policy, or parsing failures."""


# 本机宿主端口白名单：插件自己的 studio 服务（designer 渲染窗口 / programmer
# 小程序）跑在 127.0.0.1 上，bot 的浏览器工具需要能访问它。只放行显式注册的
# 端口，其余内网/本机地址照旧拒绝（SSRF 防护不受影响）。
_LOCAL_ALLOWED_PORTS: set[int] = set()


def allow_local_port(port: int) -> None:
    """Whitelist one loopback port for the browser tools (plugin studio host)."""
    try:
        _LOCAL_ALLOWED_PORTS.add(int(port))
    except (TypeError, ValueError):
        pass


def assert_browsable(url: str) -> str:
    """Only public http(s) targets may be browsed (SSRF guard)."""
    url = str(url or "").strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BrowserError("只支持 http/https 网址")
    if is_blocked_host(parsed.hostname):
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        host = (parsed.hostname or "").strip().rstrip(".").casefold()
        if host in {"127.0.0.1", "localhost", "::1"} and port in _LOCAL_ALLOWED_PORTS:
            return url
        raise BrowserError("拒绝访问内网或本机地址")
    return url


@dataclass(slots=True)
class Link:
    index: int
    text: str
    href: str


@dataclass(slots=True)
class FormField:
    name: str
    kind: str
    value: str = ""
    placeholder: str = ""


@dataclass(slots=True)
class Form:
    index: int
    action: str
    method: str
    fields: list[FormField] = field(default_factory=list)


@dataclass(slots=True)
class Page:
    url: str
    status: int
    title: str
    text: str
    links: list[Link] = field(default_factory=list)
    forms: list[Form] = field(default_factory=list)
    content_type: str = "text/html"
    truncated: bool = False


@dataclass
class Tab:
    tab_id: int
    history: list[str] = field(default_factory=list)
    cursor: int = -1
    page: Page | None = None

    @property
    def current_url(self) -> str:
        if 0 <= self.cursor < len(self.history):
            return self.history[self.cursor]
        return ""


class _HtmlExtractor(HTMLParser):
    """Single-pass HTML reader: readable text, anchors, and form controls."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[Link] = []
        self.forms: list[Form] = []
        self._skip_depth = 0
        self._in_title = False
        self._anchor: dict[str, Any] | None = None
        self._form: Form | None = None

    # -- helpers ---------------------------------------------------------
    def _emit(self, piece: str) -> None:
        if piece.strip():
            self.text_parts.append(piece)

    def _absolute(self, href: str) -> str:
        try:
            return urllib.parse.urljoin(self.base_url, href.strip())
        except Exception:
            return href.strip()

    # -- parser callbacks ------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        data = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "a":
            self._anchor = {"href": data.get("href", ""), "text": []}
        elif tag == "form":
            if len(self.forms) < MAX_FORMS:
                self._form = Form(
                    index=len(self.forms),
                    action=self._absolute(data.get("action", "") or self.base_url),
                    method=(data.get("method", "get") or "get").lower(),
                )
        elif tag in {"input", "textarea", "select"} and self._form is not None:
            name = data.get("name", "")
            if name:
                self._form.fields.append(FormField(
                    name=name,
                    kind=tag if tag != "input" else (data.get("type", "text") or "text").lower(),
                    value=data.get("value", ""),
                    placeholder=data.get("placeholder", ""),
                ))
        elif tag in _BLOCK_TAGS:
            self._emit("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._anchor is not None:
            text = re.sub(r"\s+", " ", "".join(self._anchor["text"])).strip()
            href = self._absolute(str(self._anchor.get("href", "")))
            if text and href.startswith(("http://", "https://")) and len(self.links) < MAX_LINKS:
                self.links.append(Link(len(self.links), text[:120], href))
            self._anchor = None
        elif tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None
        elif tag in _BLOCK_TAGS:
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
            return  # the document title is not body text
        if self._anchor is not None:
            self._anchor["text"].append(data)
        self.text_parts.append(data)

    # -- result ----------------------------------------------------------
    def title(self) -> str:
        return re.sub(r"\s+", " ", "".join(self.title_parts)).strip()[:200]

    def text(self) -> str:
        raw = "".join(self.text_parts)
        raw = raw.replace("\u00a0", " ")
        raw = re.sub(r"[ \t\f\v]+", " ", raw)
        lines = [line.strip() for line in raw.split("\n")]
        kept: list[str] = []
        for line in lines:
            if line:
                kept.append(line)
        return "\n".join(kept)


def parse_html(html: str, base_url: str) -> tuple[str, str, list[Link], list[Form]]:
    parser = _HtmlExtractor(base_url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # malformed markup must not kill the fetch
        pass
    return parser.title(), parser.text(), parser.links, parser.forms


def _decode_body(data: bytes, encoding: str | None) -> str:
    for candidate in (encoding, "utf-8", "gb18030", "latin-1"):
        if not candidate:
            continue
        try:
            return data.decode(candidate, "replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


def _decompress(data: bytes, headers: Any) -> bytes:
    encoding = str(headers.get("Content-Encoding", "") or "").lower()
    try:
        if "gzip" in encoding:
            return gzip.decompress(data)
        if "deflate" in encoding:
            try:
                return zlib.decompress(data)
            except zlib.error:
                return zlib.decompress(data, -zlib.MAX_WBITS)
    except Exception:
        return data
    return data


class BrowserSession:
    """A small multi-tab browser backed by the standard library only.

    One instance owns a cookie jar (so logins/redirects behave), several tabs
    with independent history, and a per-URL text cache. All methods are
    async-friendly: blocking socket work runs in a worker thread.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        user_agent: str = _USER_AGENT,
    ) -> None:
        self.timeout = max(3.0, float(timeout))
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self.user_agent = user_agent
        self._jar = http.cookiejar.CookieJar()
        self._opener: urllib.request.OpenerDirector | None = None
        self._tabs: dict[int, Tab] = {}
        self._active = 0
        self._next_tab = 1
        self._cache: dict[str, Page] = {}
        self._lock = asyncio.Lock()
        self._context = ssl.create_default_context()

    # ------------------------------------------------------------------ tabs
    def _tab(self, tab_id: int | None = None) -> Tab:
        wanted = self._active if tab_id is None else int(tab_id)
        tab = self._tabs.get(wanted)
        if tab is None:
            if self._tabs:
                tab = self._tabs[sorted(self._tabs)[-1]]
            else:
                tab = self.new_tab()
        return tab

    def new_tab(self) -> Tab:
        if len(self._tabs) >= MAX_TABS:
            oldest = sorted(self._tabs)[0]
            self._tabs.pop(oldest, None)
        tab = Tab(tab_id=self._next_tab)
        self._next_tab += 1
        self._tabs[tab.tab_id] = tab
        # a freshly opened tab becomes the active one, like a real browser
        self._active = tab.tab_id
        return tab

    def select_tab(self, tab_id: int) -> dict[str, Any]:
        if int(tab_id) not in self._tabs:
            raise BrowserError(f"没有这个标签页：{tab_id}")
        self._active = int(tab_id)
        return self.tabs()

    def close_tab(self, tab_id: int) -> dict[str, Any]:
        self._tabs.pop(int(tab_id), None)
        if self._active == int(tab_id):
            self._active = sorted(self._tabs)[-1] if self._tabs else 0
        return self.tabs()

    def tabs(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "tabs": [
                {
                    "tab_id": tab.tab_id,
                    "active": tab.tab_id == self._active,
                    "url": tab.current_url,
                    "title": (tab.page.title if tab.page else ""),
                    "history_len": len(tab.history),
                    "cursor": tab.cursor,
                    "can_back": tab.cursor > 0,
                    "can_forward": tab.cursor < len(tab.history) - 1,
                }
                for tab in sorted(self._tabs.values(), key=lambda t: t.tab_id)
            ],
        }

    # ----------------------------------------------------------------- fetch
    def _opener_for(self) -> urllib.request.OpenerDirector:
        if self._opener is None:
            handlers: list[Any] = [
                urllib.request.HTTPCookieProcessor(self._jar),
                urllib.request.HTTPSHandler(context=self._context),
            ]
            self._opener = urllib.request.build_opener(*handlers)
        return self._opener

    def _fetch_sync(self, url: str, method: str = "GET",
                    payload: dict[str, str] | None = None) -> tuple[Page, str]:
        assert_browsable(url)
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        }
        data: bytes | None = None
        target = url
        if payload:
            body = urllib.parse.urlencode(payload).encode("utf-8")
            if method.upper() == "POST":
                data = body
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                joiner = "&" if "?" in url else "?"
                target = f"{url}{joiner}{body.decode('utf-8')}"
        request = urllib.request.Request(target, data=data, headers=headers,
                                         method=method.upper())
        opener = self._opener_for()
        try:
            with opener.open(request, timeout=self.timeout) as response:
                raw = response.read(self.max_bytes + 1)
                truncated = len(raw) > self.max_bytes
                raw = raw[:self.max_bytes]
                content_type = str(response.headers.get("Content-Type", "") or "")
                final_url = str(getattr(response, "url", target) or target)
                status = int(getattr(response, "status", 200) or 200)
                charset = None
                match = re.search(r"charset=([\w\-]+)", content_type, re.I)
                if match:
                    charset = match.group(1)
                body_text = _decode_body(_decompress(raw, response.headers), charset)
        except urllib.error.HTTPError as error:
            return (
                Page(url=url, status=int(error.code or 0), title="",
                     text=f"HTTP {error.code}: {error.reason}", content_type="text/plain"),
                "error",
            )
        except Exception as error:
            raise BrowserError(f"抓取失败：{type(error).__name__}: {str(error)[:160]}") from error

        mime = content_type.split(";")[0].strip() or "text/html"
        if "json" in mime or mime.startswith("text/plain"):
            title = ""
            text = body_text[:20000]
            links: list[Link] = []
            forms: list[Form] = []
        elif "html" in mime or not mime:
            title, text, links, forms = parse_html(body_text, final_url)
        else:
            raise BrowserError(f"不支持的内容类型：{mime}")
        page = Page(url=final_url, status=status, title=title, text=text[:40000],
                    links=links, forms=forms, content_type=mime, truncated=truncated)
        return page, "ok"

    def _fetch_html_sync(self, url: str) -> tuple[str, str]:
        """Raw decoded HTML (not extracted text) for handing to a real renderer.

        Returns ``(final_url, html)``. Uses the same opener/headers/SSL as a
        normal fetch, so it succeeds wherever :meth:`fetch` does.
        """
        assert_browsable(url)
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        }
        request = urllib.request.Request(url, headers=headers, method="GET")
        with self._opener_for().open(request, timeout=self.timeout) as response:
            raw = response.read(self.max_bytes)
            content_type = str(response.headers.get("Content-Type", "") or "")
            final_url = str(getattr(response, "url", url) or url)
            match = re.search(r"charset=([\w\-]+)", content_type, re.I)
            html = _decode_body(_decompress(raw, response.headers),
                                match.group(1) if match else None)
        return final_url, html

    async def fetch_html(self, url: str) -> tuple[str, str]:
        return await asyncio.to_thread(self._fetch_html_sync, assert_browsable(url))

    async def fetch(self, url: str, method: str = "GET",
                    payload: dict[str, str] | None = None,
                    *, tab_id: int | None = None, remember: bool = True) -> Page:
        url = assert_browsable(url)
        async with self._lock:
            cached = self._cache.get(url) if method.upper() == "GET" and not payload else None
            if cached is not None:
                page = cached
            else:
                page, _state = await asyncio.to_thread(
                    self._fetch_sync, url, method, payload)
                if page.status == 200 and method.upper() == "GET":
                    self._cache[url] = page
                    if len(self._cache) > 40:
                        self._cache.pop(next(iter(self._cache)), None)
            tab = self._tab(tab_id)
            if remember and page.status < 500:
                tab.history = tab.history[: tab.cursor + 1]
                tab.history.append(page.url)
                if len(tab.history) > MAX_HISTORY:
                    tab.history = tab.history[-MAX_HISTORY:]
                tab.cursor = len(tab.history) - 1
            tab.page = page
            return page

    # -------------------------------------------------------------- actions
    def back(self, tab_id: int | None = None) -> str:
        tab = self._tab(tab_id)
        if tab.cursor <= 0:
            raise BrowserError("没有上一页")
        tab.cursor -= 1
        return tab.history[tab.cursor]

    def forward(self, tab_id: int | None = None) -> str:
        tab = self._tab(tab_id)
        if tab.cursor >= len(tab.history) - 1:
            raise BrowserError("没有下一页")
        tab.cursor += 1
        return tab.history[tab.cursor]

    def link(self, index: int, tab_id: int | None = None) -> Link:
        tab = self._tab(tab_id)
        page = tab.page
        if page is None or not page.links:
            raise BrowserError("当前页面没有可点击的链接")
        for item in page.links:
            if item.index == int(index):
                return item
        raise BrowserError(f"没有第 {index} 号链接（共 {len(page.links)} 个）")

    def form(self, index: int = 0, tab_id: int | None = None) -> Form:
        tab = self._tab(tab_id)
        page = tab.page
        if page is None or not page.forms:
            raise BrowserError("当前页面没有表单")
        for item in page.forms:
            if item.index == int(index):
                return item
        raise BrowserError(f"没有第 {index} 号表单（共 {len(page.forms)} 个）")

    def snapshot(self, tab_id: int | None = None, *, text_limit: int = 6000,
                 link_limit: int = 40) -> dict[str, Any]:
        tab = self._tab(tab_id)
        page = tab.page
        if page is None:
            return {"tab_id": tab.tab_id, "url": "", "title": "", "text": "",
                    "links": [], "forms": [], "note": "这个标签页还没有打开任何页面"}
        return {
            "tab_id": tab.tab_id,
            "url": page.url,
            "status": page.status,
            "title": page.title,
            "text": page.text[:text_limit],
            "links": [
                {"index": link.index, "text": link.text, "url": link.href}
                for link in page.links[:link_limit]
            ],
            "link_count": len(page.links),
            "forms": [
                {
                    "index": form.index, "action": form.action, "method": form.method,
                    "fields": [
                        {"name": f.name, "type": f.kind,
                         "value": f.value, "placeholder": f.placeholder}
                        for f in form.fields
                    ],
                }
                for form in page.forms
            ],
            "truncated": page.truncated,
        }

    def cookies(self) -> list[dict[str, str]]:
        return [
            {"domain": cookie.domain, "name": cookie.name, "value": cookie.value}
            for cookie in self._jar
        ]

    def clear_cache(self) -> None:
        self._cache.clear()


def page_text(page: Page, limit: int = 20000) -> str:
    """Compact textual view of a page for memory notes."""
    parts = [f"[{page.title}] {page.url}"] if page.title else [page.url]
    parts.append(page.text[:limit])
    return "\n".join(part for part in parts if part).strip()


def extract_readable(html: str, base_url: str = "https://example.com/") -> str:
    """HTML → readable text (kept for callers that already have markup)."""
    _, text, _, _ = parse_html(html, base_url)
    return text
