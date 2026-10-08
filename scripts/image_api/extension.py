# -*- coding: utf-8 -*-
"""生图 API 拓展（**尚未完工，暂不可用**）。

接入 OpenAI 格式的生图接口（``POST {base_url}/images/generations``）：
在控制台「拓展」面板里填 ``base_url`` / ``api_key`` / ``model`` 后，bot 就能调用
``image_api_draw`` 出图（比例自动、1K 质量、带冷却）。

**为什么标"尚未完工"**：各家的 OpenAI 兼容生图接口在响应字段（b64_json / url）、
尺寸命名、质量参数上并不统一，这里只做了最通用的那一版，还没有在真实接口上跑通验证。
所以：默认 ``enabled=false``；工具被调用时会**如实回报当前状态**（未启用 / 缺配置 /
调用失败），绝不假装画好了。

约定（与插件其余部分一致）：
- 配置读 ``api.config()``（模板 ``config.default.json`` + 用户改动），每次调用都重读；
- 出图成功后用 ``api.save_image(bytes)`` 存进媒体归档，返回 ``media_id``，
  交给 bot 用 ``send_image`` 发出去；
- 冷却存在拓展自己的 kv 里（跨重启保留）。
"""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request

UNFINISHED_NOTE = "本拓展尚未完工，暂不可用（仅作占位与联调）"

# 提示词 → 比例的粗略判断（比例自动：不指定 size 时按内容挑一个）
_LANDSCAPE_HINTS = ("风景", "海报", "横幅", "壁纸", "全景", "landscape", "wide", "banner")
_PORTRAIT_HINTS = ("人像", "立绘", "全身", "竖", "手机壁纸", "portrait", "tall", "avatar")
_SIZE_LANDSCAPE = "1216x832"
_SIZE_PORTRAIT = "832x1216"
_SIZE_SQUARE = "1024x1024"


def _clean(value) -> str:
    return str(value or "").strip()


def _pick_size(prompt: str, config: dict) -> str:
    """比例自动：按提示词里的词挑横/竖/方；关掉 auto_aspect 就用 default_size。"""
    if not bool(config.get("auto_aspect", True)):
        return _clean(config.get("default_size")) or _SIZE_SQUARE
    text = _clean(prompt).lower()
    if any(word in text for word in _PORTRAIT_HINTS):
        return _SIZE_PORTRAIT
    if any(word in text for word in _LANDSCAPE_HINTS):
        return _SIZE_LANDSCAPE
    return _SIZE_SQUARE


def _endpoint(base_url: str) -> str:
    base = _clean(base_url).rstrip("/")
    if not base:
        return ""
    if base.endswith("/images/generations"):
        return base
    if base.endswith("/v1"):
        return base + "/images/generations"
    return base + "/v1/images/generations"


def _extract_image(payload: dict) -> bytes | None:
    """从响应里抠出图片字节：认 b64_json 与 url 两种（各家写法不一）。"""
    items = payload.get("data")
    if not isinstance(items, list) or not items:
        return None
    first = items[0] if isinstance(items[0], dict) else {}
    encoded = _clean(first.get("b64_json"))
    if encoded:
        try:
            return base64.b64decode(encoded)
        except Exception:
            return None
    url = _clean(first.get("url"))
    if url.startswith("http"):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 longmem-script"})
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except Exception:
            return None
    return None


def _cooldown_left(api, seconds: int) -> int:
    """冷却还剩多少秒（0=可以画）。时间戳存在 kv 里，跨重启保留。"""
    if seconds <= 0:
        return 0
    import time

    last = api.kv_get("last_draw_at", 0) or 0
    try:
        last = float(last)
    except (TypeError, ValueError):
        last = 0.0
    gap = int(seconds) - int(time.time() - last)
    return max(0, gap)


async def call_tool(api, name: str, params: dict) -> str:
    if name != "image_api_draw":
        return f"未知工具：{name}"

    config = api.config()
    if not bool(config.get("enabled", False)):
        return (f"{UNFINISHED_NOTE}：请在控制台「拓展」里打开它并填好 base_url / api_key / model。"
                "现在要画图请用 draw_picture。")

    prompt = _clean(params.get("prompt") or params.get("text"))
    if not prompt:
        return "要画什么？给个 prompt。"

    base_url = _clean(config.get("base_url"))
    api_key = _clean(config.get("api_key"))
    model = _clean(config.get("model"))
    missing = [label for label, value in
               (("base_url", base_url), ("api_key", api_key), ("model", model)) if not value]
    if missing:
        return f"{UNFINISHED_NOTE}：还缺配置 {'、'.join(missing)}（控制台「拓展」→ 生图 API 里填）。"

    cooldown = int(config.get("cooldown_seconds") or 0)
    left = _cooldown_left(api, cooldown)
    if left > 0:
        return f"刚画过，冷却中（还要 {left} 秒）。"

    size = _clean(params.get("size")) or _pick_size(prompt, config)
    body = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": size,
        "quality": _clean(config.get("quality")) or "1k",
        "response_format": "b64_json",
    }
    extra = config.get("extra_body")
    if isinstance(extra, dict):
        body.update(extra)

    url = _endpoint(base_url)
    timeout = int(config.get("timeout_seconds") or 120)
    status, text = await _post_json(api, url, body, api_key, timeout)
    if status <= 0:
        return f"{UNFINISHED_NOTE}：调用失败（{text}）"
    if status >= 400:
        return f"{UNFINISHED_NOTE}：接口返回 {status}：{text[:200]}"

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return f"{UNFINISHED_NOTE}：接口返回的不是 JSON：{text[:200]}"
    image = _extract_image(payload if isinstance(payload, dict) else {})
    if image is None:
        return (f"{UNFINISHED_NOTE}：响应里没找到图片（要 b64_json 或 url 字段）："
                f"{text[:200]}")

    import time

    api.kv_set("last_draw_at", time.time())
    media_id = await api.save_image(image, note=f"image_api: {prompt[:60]}")
    return json.dumps({
        "ok": True, "media_id": media_id, "size": size,
        "hint": "用 send_image(media_id) 发给用户",
        "note": UNFINISHED_NOTE,
    }, ensure_ascii=False)


async def _post_json(api, url: str, body: dict, api_key: str,
                     timeout: int) -> tuple[int, str]:
    """POST JSON（拓展 API 只给了固定签名的 http_post_json，这里要带鉴权头，自己发）。"""
    import asyncio

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    def _fetch() -> tuple[int, str]:
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}",
                     "User-Agent": "Mozilla/5.0 longmem-script"})
        try:
            with urllib.request.urlopen(request, timeout=int(timeout)) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode("utf-8", "replace")
        except Exception as error:
            return -1, f"{type(error).__name__}: {error}"

    return await asyncio.to_thread(_fetch)
