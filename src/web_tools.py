"""Zero-dependency web helpers: DuckDuckGo search plus page fetching.

Everything runs on the standard library so the plugin works on hosts without
aiohttp/bs4 (and without any browser engine). Page fetching delegates to
`src.browser` so the search/fetch tools and the browsing tools share one
SSRF policy and one HTML understanding.
"""
from __future__ import annotations

import asyncio
import re
import urllib.parse
import urllib.request
from typing import Any

from .browser import (
    DEFAULT_MAX_BYTES,
    DEFAULT_TIMEOUT,
    BrowserError,
    assert_browsable,
    parse_html,
)

_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_USER_AGENT = "Mozilla/5.0 (compatible; AstrBotAgent)"


class WebToolError(RuntimeError):
    pass


def assert_safe_url(url: str) -> str:
    try:
        return assert_browsable(url)
    except BrowserError as error:
        raise WebToolError(str(error)) from error


def _read_bounded(url: str, *, max_bytes: int, timeout: float,
                  params: dict[str, str] | None = None) -> tuple[str, str, int]:
    """Blocking GET returning (text, final_url, status); never raises for HTTP."""
    target = url
    if params:
        joiner = "&" if "?" in url else "?"
        target = f"{url}{joiner}{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(target, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "identity",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(max_bytes)
            charset = None
            content_type = str(response.headers.get("Content-Type", "") or "")
            match = re.search(r"charset=([\w\-]+)", content_type, re.I)
            if match:
                charset = match.group(1)
            text = raw.decode(charset or "utf-8", "replace")
            return text, str(getattr(response, "url", target) or target), int(
                getattr(response, "status", 200) or 200)
    except Exception as error:
        raise WebToolError(f"{type(error).__name__}: {str(error)[:160]}") from error


async def fetch_text(url: str, *, max_bytes: int = DEFAULT_MAX_BYTES,
                     timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Fetch a public web page and return bounded readable text."""
    url = assert_safe_url(url)
    text, final_url, status = await asyncio.to_thread(
        _read_bounded, url, max_bytes=max_bytes, timeout=timeout)
    title, body, _, _ = parse_html(text, final_url)
    return {
        "url": final_url,
        "status": status,
        "content_type": "text/html",
        "title": title,
        "text": body[:20000],
    }


async def search_web(query: str, *, limit: int = 8, timeout: float = 15.0) -> list[dict[str, str]]:
    """Best-effort DuckDuckGo HTML search returning title/url pairs."""
    query = str(query).strip()[:200]
    if not query:
        return []
    html, _, _ = await asyncio.to_thread(
        _read_bounded, "https://html.duckduckgo.com/html/",
        max_bytes=2 * 1024 * 1024, timeout=timeout, params={"q": query})
    results: list[dict[str, str]] = []
    for match in _RESULT_RE.finditer(html):
        href, title = match.group(1), _TAG_RE.sub("", match.group(2)).strip()
        if href.startswith("//duckduckgo.com/l/?uddg="):
            parsed = urllib.parse.urlsplit("https:" + href)
            href = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0] or href
        if title and href.startswith("http"):
            results.append({"title": title[:200], "url": href})
        if len(results) >= min(max(int(limit), 1), 15):
            break
    return results


def _strip_html(html: str) -> str:
    """Kept for callers that already hold markup."""
    _, text, _, _ = parse_html(html, "https://example.com/")
    return text
