# -*- coding: utf-8 -*-
"""自定义 TTS（文字转语音）接入。

设计目标：让用户填一个 HTTP 接口就能用**自己的**语音合成服务，而不是只能用网关自带的
AI 语音。请求按主流自建 TTS 都兼容的形状发：

    POST <tts_api_url>
    Authorization: Bearer <tts_api_key>      （key 留空则不发这个头）
    Content-Type: application/json
    {"model": "...", "input": "要念的文字", "voice": "...", "response_format": "mp3"}

响应按二进制音频处理（也兼容 JSON 里带 base64 / url 的返回）。
网络请求全部走 aiohttp 且带超时与体积上限——**绝不在事件循环里做同步网络 I/O**。
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Mapping

# 常见的音频容器；不是这些就按服务端 content-type 猜
_AUDIO_FORMATS = {"mp3", "wav", "ogg", "opus", "aac", "m4a", "amr", "silk", "flac"}
_MAX_AUDIO_BYTES = 4 * 1024 * 1024


class TTSError(RuntimeError):
    """语音合成失败（调用方据此退回文字）。"""


def _aiohttp():
    """惰性导入 aiohttp（宿主环境通常自带；本地测试/精简环境可能没有）。"""
    try:
        import aiohttp  # noqa: F401

        return aiohttp
    except Exception as error:  # pragma: no cover - 取决于运行环境
        raise TTSError(f"没有可用的 HTTP 客户端（aiohttp）：{error}") from error


@dataclass(frozen=True, slots=True)
class TTSConfig:
    url: str = ""
    api_key: str = ""
    model: str = ""
    voice: str = ""
    audio_format: str = "mp3"
    timeout_seconds: float = 20.0

    @classmethod
    def from_settings(cls, settings: Any) -> "TTSConfig":
        return cls(
            url=str(getattr(settings, "tts_api_url", "") or "").strip(),
            api_key=str(getattr(settings, "tts_api_key", "") or "").strip(),
            model=str(getattr(settings, "tts_api_model", "") or "").strip(),
            voice=str(getattr(settings, "tts_api_voice", "") or "").strip(),
            audio_format=(str(getattr(settings, "tts_api_format", "") or "mp3").strip().lower()
                          or "mp3"),
            timeout_seconds=float(getattr(settings, "tts_api_timeout", 20) or 20),
        )

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def payload(self, text: str) -> dict[str, Any]:
        """请求体：只放服务端认识的字段（空值不发，避免某些服务报 422）。"""
        body: dict[str, Any] = {"input": text, "text": text}
        if self.model:
            body["model"] = self.model
        if self.voice:
            body["voice"] = self.voice
        if self.audio_format in _AUDIO_FORMATS:
            body["response_format"] = self.audio_format
            body["format"] = self.audio_format
        return body

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "audio/*, application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


def _guess_format(content_type: str, fallback: str) -> str:
    lowered = str(content_type or "").lower()
    for name in ("mpeg", "mp3", "wav", "ogg", "opus", "aac", "mp4", "amr", "silk", "flac"):
        if name in lowered:
            return "mp3" if name == "mpeg" else ("m4a" if name == "mp4" else name)
    return fallback if fallback in _AUDIO_FORMATS else "mp3"


def _from_json(payload: Mapping[str, Any]) -> tuple[bytes, str]:
    """有些服务把音频放在 JSON 里（base64 或 url）。"""
    for key in ("audio", "audio_base64", "base64", "data"):
        value = payload.get(key)
        if isinstance(value, str) and len(value) >= 8:
            raw = value.split(",", 1)[-1] if value.startswith("data:") else value
            try:
                blob = base64.b64decode(raw, validate=False)
            except Exception:
                continue
            if blob:
                return blob, "mp3"
    url = str(payload.get("url") or payload.get("audio_url") or "").strip()
    if url.startswith(("http://", "https://")):
        return b"", url
    raise TTSError("TTS 接口返回的 JSON 里没有音频")


async def synthesize(config: TTSConfig, text: str,
                     *, session: Any = None) -> tuple[bytes, str]:
    """把文字合成成音频，返回 (字节, 格式)。

    失败抛 TTSError（调用方退回文字或网关语音）；不做重试——语音是锦上添花，
    失败一次就该立刻走降级路径，别让回复卡住。
    """
    if not config.configured:
        raise TTSError("没有配置 TTS 接口地址")
    clean = " ".join(str(text or "").split())[:200]
    if not clean:
        raise TTSError("要念的文字是空的")
    own_session = session is None
    if own_session:
        aiohttp = _aiohttp()
        timeout = aiohttp.ClientTimeout(total=max(3.0, min(60.0, config.timeout_seconds)))
        client = aiohttp.ClientSession(timeout=timeout)
    else:
        # 调用方给了 session（也便于测试注入）：不依赖 aiohttp 的 timeout 类型
        client = session
        timeout = None
    try:
        async with client.post(config.url, json=config.payload(clean),
                               headers=config.headers()) as response:
            if response.status >= 400:
                detail = (await response.text())[:200]
                raise TTSError(f"HTTP {response.status}：{detail}")
            content_type = response.headers.get("Content-Type", "")
            if "json" in content_type.lower():
                try:
                    payload = await response.json(content_type=None)
                except Exception as error:
                    raise TTSError(f"返回不是合法 JSON：{str(error)[:120]}") from error
                blob, hint = _from_json(payload if isinstance(payload, Mapping) else {})
                if not blob:
                    # JSON 里给了音频 URL：再取一次
                    async with client.get(hint, **({"timeout": timeout} if timeout else {})) as media:
                        if media.status >= 400:
                            raise TTSError(f"音频下载失败：HTTP {media.status}")
                        blob = await media.content.read(_MAX_AUDIO_BYTES + 1)
                        content_type = media.headers.get("Content-Type", "")
                if len(blob) > _MAX_AUDIO_BYTES:
                    raise TTSError("音频超过 4MB")
                if not blob:
                    raise TTSError("音频内容为空")
                return blob, _guess_format(content_type, config.audio_format)
            blob = await response.content.read(_MAX_AUDIO_BYTES + 1)
            if len(blob) > _MAX_AUDIO_BYTES:
                raise TTSError("音频超过 4MB")
            if not blob:
                raise TTSError("音频内容为空")
            if blob[:1] == b"{":
                # 服务端没给 content-type 但实际是 JSON
                try:
                    payload = json.loads(blob.decode("utf-8", "replace"))
                except Exception:
                    payload = None
                if isinstance(payload, Mapping):
                    inner, hint = _from_json(payload)
                    if inner:
                        return inner, _guess_format("", config.audio_format)
                    if hint:
                        async with client.get(hint, **({"timeout": timeout} if timeout else {})) as media:
                            data = await media.content.read(_MAX_AUDIO_BYTES + 1)
                        if not data:
                            raise TTSError("音频内容为空")
                        return data, _guess_format(media.headers.get("Content-Type", ""),
                                                   config.audio_format)
            return blob, _guess_format(content_type, config.audio_format)
    except TTSError:
        raise
    except aiohttp.ClientError as error:
        raise TTSError(f"连接失败：{type(error).__name__}: {str(error)[:120]}") from error
    except Exception as error:
        raise TTSError(f"{type(error).__name__}: {str(error)[:120]}") from error
    finally:
        if own_session:
            await client.close()


async def synthesize_b64(config: TTSConfig, text: str) -> tuple[str, str]:
    """返回 (base64 音频, 格式)——直接喂给 OneBot 的 base64:// record。"""
    blob, audio_format = await synthesize(config, text)
    return base64.b64encode(blob).decode("ascii"), audio_format
