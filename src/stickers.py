from __future__ import annotations

from astrbot.api import logger

import asyncio
import base64
import binascii
import hashlib
import inspect
import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol


class StickerError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StickerRecord:
    sha256: str
    path: str
    media_type: str
    size: int
    source_ref: str | None = None


@dataclass(frozen=True, slots=True)
class StickerStats:
    count: int
    total_bytes: int
    max_count: int
    max_bytes: int
    capacity_bytes: int | None


class StickerMetadataStore(Protocol):
    def save_sticker(self, record: StickerRecord) -> Any: ...


class StickerManager:
    """Stores only validated bytes returned by NapCat's get_image action."""

    def __init__(self, root: str | os.PathLike[str], *, max_bytes: int = 8_000_000,
                 max_count: int = 2000, capacity_bytes: int | None = None,
                 allowed_roots: Sequence[str | os.PathLike[str]] = (),
                 require_trusted_source: bool = True,
                 metadata_store: StickerMetadataStore | None = None,
                 metadata_callback: Callable[[StickerRecord], Any] | None = None) -> None:
        if max_bytes <= 0 or max_count <= 0 or (capacity_bytes is not None and capacity_bytes <= 0):
            raise ValueError("limits must be positive")
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes
        self.max_count = max_count
        self.capacity_bytes = capacity_bytes
        self.allowed_roots = tuple(Path(path).resolve() for path in allowed_roots)
        self.require_trusted_source = require_trusted_source
        self.metadata_store = metadata_store
        self.metadata_callback = metadata_callback
        self._lock = asyncio.Lock()

    async def ingest(self, get_image_result: Any, *, source_ref: str | None = None,
                     trusted_get_image: bool = False,
                     metadata: Mapping[str, Any] | None = None) -> StickerRecord:
        payload, local_path = self._extract_payload(get_image_result)
        data = await asyncio.to_thread(self._read_payload, payload, local_path, trusted_get_image)
        extension, media_type = self._detect(data)
        digest = hashlib.sha256(data).hexdigest()
        async with self._lock:
            record = await asyncio.to_thread(
                self._store, data, digest, extension, media_type, source_ref
            )
            await self._persist_metadata(record)
            await self.note(
                record.sha256,
                summary=str((metadata or {}).get("summary", "") or "") or None,
                bump_use=True,
            )
        return record

    learn = ingest

    def _index_path(self) -> Path:
        return self.root / "index.json"

    def _load_index(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self._index_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_index(self, index: dict[str, dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_path().write_text(
            json.dumps(index, ensure_ascii=False), encoding="utf-8"
        )

    async def note(self, digest: str, *, summary: str | None = None,
                   description: str | None = None, bump_use: bool = False) -> None:
        """Merge semantic metadata for one sticker into the on-disk index."""
        entry = self._load_index().get(digest) or {}
        if summary:
            entry["summary"] = summary[:120]
        if description:
            entry["description"] = description[:300]
        if bump_use:
            entry["uses"] = int(entry.get("uses", 0)) + 1
        entry["last_seen"] = datetime.now().astimezone().isoformat(timespec="seconds")
        entry.setdefault("first_seen", entry["last_seen"])
        index = self._load_index()
        index[digest] = entry
        await asyncio.to_thread(self._save_index, index)

    def catalog(self, limit: int = 30) -> list[dict[str, Any]]:
        """Semantic listing: digest id, text cues, usage count."""
        index = self._load_index()
        entries = []
        for path in self._files():
            digest = path.stem
            meta = index.get(digest) or {}
            entries.append({
                "sticker_id": digest,
                "summary": str(meta.get("summary", "")),
                "description": str(meta.get("description", "")),
                "uses": int(meta.get("uses", 0)),
                "last_seen": str(meta.get("last_seen", "")),
            })
        entries.sort(key=lambda item: (-item["uses"], item["sticker_id"]))
        return entries if limit <= 0 else entries[:limit]

    def pick(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Keyword match over summary/description; empty query returns popular ones."""
        words = [w for w in re.split(r"[\s,，。/]+", str(query or "").strip()) if w]
        scored: list[tuple[int, dict[str, Any]]] = []
        for entry in self.catalog(limit=0):
            text = f"{entry['summary']} {entry['description']}"
            if not text.strip():
                score = 0
            else:
                score = sum(1 for w in words if w in text) if words else 0
            if not words or score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda pair: (-pair[0], -pair[1]["uses"]))
        result = [entry for _, entry in scored[:max(0, limit)]]
        if not result:
            # 无描述/无词命中时回退热门库存——否则没有 vision 描述的库 pick 恒空，
            # 模型以为"没货"，是从不主动发表情的一环（实录）。
            result = self.catalog(limit=limit)
        return result

    def resolve(self, sticker_id: str) -> StickerRecord | None:
        """Resolve a sticker id (sha256 or its prefix) to a stored record."""
        wanted = str(sticker_id or "").strip().lower()
        if not wanted:
            return None
        for path in self._files():
            digest = path.stem.lower()
            if digest == wanted or digest.startswith(wanted) or wanted.startswith(digest):
                extension = path.suffix.lower().lstrip(".")
                media_type = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif",
                              "webp": "image/webp"}.get(extension, "application/octet-stream")
                return StickerRecord(digest, str(path.resolve()), media_type, path.stat().st_size)
        return None

    def _extract_payload(self, result: Any) -> tuple[str, bool]:
        if not isinstance(result, Mapping):
            raise StickerError("get_image result must be a mapping")
        value: Any = result.get("data", result)
        if isinstance(value, Mapping):
            for key in ("base64", "file", "path"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    candidate = candidate.strip()
                    return (f"base64://{candidate}", False) if key == "base64" else (candidate, True)
            if value.get("url"):
                raise StickerError("remote image URLs are not accepted")
        raise StickerError("get_image result has no local path or base64 data")

    def _read_payload(self, payload: str, local_path: bool, trusted_get_image: bool) -> bytes:
        lower = payload.lower()
        if lower.startswith(("http://", "https://", "ftp://", "file://")):
            raise StickerError("URLs are not accepted")
        encoded = payload
        if lower.startswith("base64://"):
            encoded = payload[9:]
            return self._decode_base64(encoded)
        elif lower.startswith("data:"):
            header, separator, encoded = payload.partition(",")
            if not separator or ";base64" not in header.lower():
                raise StickerError("only base64 data URIs are accepted")
            return self._decode_base64(encoded)
        elif local_path:
            raw_path = Path(payload).expanduser()
            if raw_path.is_symlink():
                raise StickerError("symlink image paths are not accepted")
            path = raw_path.resolve()
            if self.require_trusted_source and not trusted_get_image and not self._within_allowed_root(path):
                raise StickerError("local path is not proven to come from trusted get_image")
            try:
                if not path.is_file():
                    raise StickerError("local image path is not a regular file")
                if any(parent.is_symlink() for parent in raw_path.absolute().parents):
                    raise StickerError("symlink image paths are not accepted")
                if path.stat().st_size > self.max_bytes:
                    raise StickerError("image exceeds size limit")
                with path.open("rb") as source:
                    data = source.read(self.max_bytes + 1)
            except OSError as exc:
                raise StickerError(f"cannot read local image: {exc}") from exc
            if len(data) > self.max_bytes:
                raise StickerError("image exceeds size limit")
            return data
        raise StickerError("ambiguous image payload")

    def _within_allowed_root(self, path: Path) -> bool:
        for root in self.allowed_roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _decode_base64(self, encoded: str) -> bytes:
        if len(encoded) > ((self.max_bytes + 2) // 3) * 4 + 8:
            raise StickerError("image exceeds size limit")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise StickerError("invalid base64 image") from exc
        if len(data) > self.max_bytes:
            raise StickerError("image exceeds size limit")
        return data

    @staticmethod
    def _detect(data: bytes) -> tuple[str, str]:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png", "image/png"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "gif", "image/gif"
        if len(data) >= 4 and data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"):
            marker = data[3]
            if marker not in (0x00, 0xFF):
                return "jpg", "image/jpeg"
        if len(data) >= 16 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            declared_size = int.from_bytes(data[4:8], "little") + 8
            if declared_size == len(data) and data[12:16] in {b"VP8 ", b"VP8L", b"VP8X"}:
                return "webp", "image/webp"
        raise StickerError("unsupported or invalid image structure")

    def _store(self, data: bytes, digest: str, extension: str, media_type: str,
               source_ref: str | None) -> StickerRecord:
        self.root.mkdir(parents=True, exist_ok=True)
        destination = (self.root / f"{digest}.{extension}").resolve()
        if destination.parent != self.root:
            raise StickerError("destination escapes sticker directory")
        if destination.exists():
            os.utime(destination, None)
            return StickerRecord(digest, str(destination), media_type, len(data), source_ref)
        self._evict_for(len(data))
        count = len(self._files())
        if count >= self.max_count:
            raise StickerError("sticker count limit reached")
        fd, temporary = tempfile.mkstemp(prefix=".sticker-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return StickerRecord(digest, str(destination), media_type, len(data), source_ref)

    def _files(self) -> list[Path]:
        if not self.root.exists():
            return []
        return [item for item in self.root.iterdir()
                if item.is_file() and not item.is_symlink()
                and not item.name.startswith(".") and item.name != "index.json"]

    def _evict_for(self, incoming_bytes: int) -> None:
        if self.capacity_bytes is None:
            return
        files = self._files()
        total = sum(item.stat().st_size for item in files)
        for victim in sorted(files, key=lambda item: (item.stat().st_mtime_ns, item.name)):
            if total + incoming_bytes <= self.capacity_bytes:
                break
            size = victim.stat().st_size
            victim.unlink()
            total -= size
        if total + incoming_bytes > self.capacity_bytes:
            raise StickerError("sticker capacity limit reached")

    def stats(self) -> StickerStats:
        files = self._files()
        return StickerStats(
            count=len(files), total_bytes=sum(item.stat().st_size for item in files),
            max_count=self.max_count, max_bytes=self.max_bytes, capacity_bytes=self.capacity_bytes,
        )

    def select(self, *, limit: int = 1) -> list[StickerRecord]:
        if limit < 0:
            raise ValueError("limit cannot be negative")
        selected = sorted(self._files(), key=lambda item: (-item.stat().st_mtime_ns, item.name))[:limit]
        records = []
        for path in selected:
            digest, extension = path.stem, path.suffix.lower().lstrip(".")
            media_type = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif",
                          "webp": "image/webp"}.get(extension, "application/octet-stream")
            records.append(StickerRecord(digest, str(path.resolve()), media_type, path.stat().st_size))
        return records

    def send_path(self, record: "StickerRecord", *, max_edge: int = 256) -> str:
        """表情包**发送用**的路径：缩到表情包规格（默认最长边 256px）。

        实录："bot 发表情包时会直接发送图片，导致比例非常大"——原图常常是
        1000px+ 的梗图，QQ 按原尺寸铺满聊天框。真人群友发的表情包是小图，
        所以发送前统一缩放（按 sha256 缓存副本，只算一次；动图与缩放失败
        一律回退原图，绝不因为缩放把表情发丢了）。
        """
        path = Path(str(record.path))
        suffix = path.suffix.lower()
        if suffix == ".gif":  # 动图不缩（缩放会丢帧）
            return str(path)
        cache_dir = path.parent / "_send"
        # 缓存名带 v2：老版本的副本可能本身就是大图（那时还没按像素尺寸判断），
        # 换名字直接绕开历史遗留，别让一次旧错误永久生效。
        target = cache_dir / f"{path.stem}_{max_edge}_v2{suffix or '.png'}"
        try:
            if target.is_file() and target.stat().st_size > 0 \
                    and self._cache_size_ok(target, max_edge):
                return str(target)
        except OSError:
            return str(path)
        try:
            from PIL import Image

            cache_dir.mkdir(parents=True, exist_ok=True)
            with Image.open(path) as image:
                # 判据是**像素尺寸**而不是文件大小——纯色大图压缩后可能只有几 KB，
                # 按大小当"小图"会漏掉真正需要缩放的那些（第一次就是这么错的）
                if max(image.size) <= max_edge:
                    return str(path)
                image.load()
                ratio = max_edge / float(max(image.size))
                size = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
                converted = image.convert("RGBA") if image.mode in ("P", "LA") else image
                resized = converted.resize(size, Image.LANCZOS)
                save_kwargs = {}
                if suffix in (".jpg", ".jpeg"):
                    resized = resized.convert("RGB")
                    save_kwargs = {"quality": 88}
                resized.save(target, **save_kwargs)
            return str(target)
        except ImportError as error:
            self._warn_no_pil(error)
            return str(path)
        except Exception as error:
            logger.info("长程记忆：表情缩放失败（按原图发）：%s", str(error)[:120])
            return str(path)

    @staticmethod
    def _cache_size_ok(target: Path, max_edge: int) -> bool:
        """缓存副本必须真的是"小图"。

        实录（用户）：表情包还是发很大的图，gif/动图却正常——因为 gif 走早返回、
        不进这个缓存，而静态图的缓存副本**只要存在就被无条件信任**：历史遗留的大图
        副本于是被永久复用（那版还没按像素尺寸判断，或尺寸上限不同）。
        """
        try:
            from PIL import Image

            with Image.open(target) as cached:
                return max(cached.size) <= int(max_edge) + 2
        except Exception:
            return False          # 打不开或没法判断 → 当作不可用，重新生成

    def _warn_no_pil(self, error: Exception) -> None:
        """缩放需要 Pillow；没有它就只能发原图（大图），必须说出来而不是悄悄退化。"""
        if getattr(self, "_pil_warned", False):
            return
        self._pil_warned = True
        logger.warning("长程记忆：表情缩放不可用（Pillow 缺失或损坏：%s），"
                       "这张会按原尺寸发出去——装一下 Pillow 就好", str(error)[:120])

    def delete(self, sha256: str) -> bool:
        if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256.lower()):
            raise StickerError("invalid sticker sha256")
        for path in self._files():
            if path.stem == sha256.lower():
                path.unlink()
                return True
        return False

    def clear(self) -> int:
        """Wipe every sticker file plus the semantic index; returns file count."""
        removed = 0
        for path in self._files():
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        try:
            self._index_path().unlink()
        except OSError:
            pass
        return removed

    def evict(self, *, target_bytes: int | None = None,
              target_count: int | None = None) -> list[str]:
        """Remove least-recently-used files until requested capacities are met."""
        if target_bytes is not None and target_bytes < 0:
            raise ValueError("target_bytes cannot be negative")
        if target_count is not None and target_count < 0:
            raise ValueError("target_count cannot be negative")
        files = self._files()
        total = sum(path.stat().st_size for path in files)
        removed: list[str] = []
        for victim in sorted(files, key=lambda item: (item.stat().st_mtime_ns, item.name)):
            bytes_ok = target_bytes is None or total <= target_bytes
            count_ok = target_count is None or len(files) - len(removed) <= target_count
            if bytes_ok and count_ok:
                break
            size = victim.stat().st_size
            victim.unlink()
            total -= size
            removed.append(victim.stem)
        return removed

    async def _persist_metadata(self, record: StickerRecord) -> None:
        callback = self.metadata_callback
        if callback is not None:
            result = callback(record)
        elif self.metadata_store is not None:
            callback = self.metadata_store.save_sticker
            fields = asdict(record)
            storage_fields = {
                "sha256": record.sha256, "path": record.path,
                "mime_type": record.media_type, "size_bytes": record.size,
                "metadata": {"source_ref": record.source_ref} if record.source_ref else None,
            }
            try:
                signature = inspect.signature(callback)
                parameters = signature.parameters
                accepts_fields = any(parameter.kind == inspect.Parameter.VAR_KEYWORD
                                     for parameter in parameters.values())
                if accepts_fields or set(fields).issubset(parameters):
                    result = callback(**fields)
                elif set(storage_fields).issubset(parameters):
                    result = callback(**storage_fields)
                else:
                    result = callback(record)
            except (TypeError, ValueError):
                result = callback(record)
        else:
            return
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def metadata(record: StickerRecord) -> dict[str, Any]:
        return asdict(record)
