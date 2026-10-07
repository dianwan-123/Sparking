from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import inspect
import ipaddress
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urlsplit

MAX_PATH = 260

_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


@dataclass(frozen=True, slots=True)
class ArchiveRecord:
    item_id: str
    scope_id: str
    kind: str
    status: str
    path: str = ""
    url: str = ""
    size: int = 0
    sha256: str = ""
    mime: str = ""
    source_message_id: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.item_id, "kind": self.kind, "status": self.status,
            "path": self.path, "url": self.url, "size": self.size,
            "sha256": self.sha256, "mime": self.mime,
            "message_id": self.source_message_id, "note": self.note,
        }


class MediaArchiveError(RuntimeError):
    pass


def is_blocked_host(host: str) -> bool:
    """True when a hostname resolves to loopback/private/link-local/metadata space."""
    normalized = (host or "").strip().rstrip(".").casefold()
    if not normalized or normalized in {"localhost", "metadata.google.internal"}:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        address = None
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    if address is not None:
        candidates.append(address)
    else:
        try:
            infos = socket.getaddrinfo(normalized, None)
        except OSError:
            return True
        for info in infos:
            try:
                candidates.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                return True
    return any(not item.is_global for item in candidates)


class MediaArchive:
    """Bounded local archiving of message media and user-shared links."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        db_path: str | os.PathLike[str] | None = None,
        max_file_bytes: int = 15 * 1024 * 1024,
        max_total_bytes: int = 1024 * 1024 * 1024,
        fetch_stream: Callable[[str, int], AsyncIterator[tuple[bytes, int | None]]] | None = None,
        get_image_callback: Callable[[str], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        if max_file_bytes <= 0 or max_total_bytes <= 0:
            raise ValueError("limits must be positive")
        self.root = Path(root).resolve()
        self.db_path = Path(db_path) if db_path else self.root / "media.db"
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self._fetch_stream = fetch_stream
        self._get_image = get_image_callback
        self._lock = asyncio.Lock()
        self._db: Any = None
        self._ready = False

    async def open(self) -> "MediaArchive":
        if self._ready and self._db is not None:
            return self                      # 幂等：重复调用不该开第二个连接
        self.root.mkdir(parents=True, exist_ok=True)
        import aiosqlite

        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS media(item_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL,"
            "kind TEXT NOT NULL,status TEXT NOT NULL,path TEXT NOT NULL DEFAULT '',url TEXT NOT NULL DEFAULT '',"
            "size INTEGER NOT NULL DEFAULT 0,sha256 TEXT NOT NULL DEFAULT '',mime TEXT NOT NULL DEFAULT '',"
            "source_message_id TEXT NOT NULL DEFAULT '',note TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL)"
        )
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_media_scope ON media(scope_id,created_at)")
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_media_hash ON media(sha256)")
        await self._db.commit()
        self._ready = True
        return self

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
            self._ready = False

    @property
    def is_open(self) -> bool:
        return bool(self._ready and self._db is not None)

    async def ensure_open(self) -> None:
        """按需重开。

        实录（用户日志）：插件重载后，上一实例里仍在跑的 agent 工具循环继续调本插件的
        工具，而它持有的归档连接已被 terminate 关掉 → 连环 `media archive is not open`
        （看图/发图/媒体列表全废，bot 看起来像傻了）。真正要用连接前自动重开一次，
        幂等且走已有的锁，代价极小。
        """
        if self.is_open:
            return
        async with self._lock:
            if not self.is_open:
                await self.open()

    def _conn(self) -> Any:
        if not self._ready or self._db is None:
            raise MediaArchiveError("media archive is not open")
        return self._db

    async def _record(self, record: ArchiveRecord, created_at: str) -> None:
        async with self._lock:
            await self._conn().execute(
                "INSERT INTO media(item_id,scope_id,kind,status,path,url,size,sha256,mime,source_message_id,note,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (record.item_id, record.scope_id, record.kind, record.status, record.path,
                 record.url, record.size, record.sha256, record.mime, record.source_message_id,
                 record.note, created_at),
            )
            await self._conn().commit()

    async def total_bytes(self) -> int:
        row = await self._conn().execute("SELECT COALESCE(SUM(size),0) AS n FROM media WHERE status='saved'")
        data = await row.fetchone()
        await row.close()
        return int(data["n"]) if data else 0

    def _destination(self, digest: str, suffix: str) -> Path:
        target = (self.root / f"{digest}{suffix}").resolve()
        if self.root != target and self.root not in target.parents:
            raise MediaArchiveError("destination escapes archive root")
        if len(str(target)) >= MAX_PATH:
            raise MediaArchiveError("destination path is too long")
        return target

    async def save_bytes(
        self,
        data: bytes,
        *,
        scope_id: str,
        kind: str,
        source_message_id: str = "",
        mime: str = "",
        note: str = "",
    ) -> ArchiveRecord:
        if len(data) > self.max_file_bytes:
            return await self._record_skip(scope_id, kind, "exceeds per-file limit", source_message_id, note)
        digest = hashlib.sha256(data).hexdigest()
        suffix = _suffix_for(mime, data)
        target = self._destination(digest, suffix)
        async with self._lock:
            row_cursor = await self._conn().execute(
                "SELECT item_id,path FROM media WHERE sha256=? AND status='saved'", (digest,))
            row = await row_cursor.fetchone()
            await row_cursor.close()
        if row is not None:
            stored_path = str(row["path"] or "")
            on_disk = bool(stored_path) and await asyncio.to_thread(os.path.exists, stored_path)
            if on_disk:
                return ArchiveRecord(str(row["item_id"]), scope_id, kind, "duplicate",
                                     path=stored_path, sha256=digest, size=len(data),
                                     source_message_id=source_message_id)
            # The DB says this content is saved but the file is gone (plugin
            # reinstall wiped media/, manual cleanup, AV quarantine, …). Since we
            # hold the bytes and files are content-addressed, re-materialize it so
            # a later send_image/get_path doesn't hand NapCat a dead path (ENOENT).
            await asyncio.to_thread(self._write_atomic, target, data)
            async with self._lock:
                await self._conn().execute(
                    "UPDATE media SET path=?,size=?,status='saved' WHERE item_id=?",
                    (str(target), len(data), str(row["item_id"])))
                await self._conn().commit()
            return ArchiveRecord(str(row["item_id"]), scope_id, kind, "saved", str(target),
                                 "", len(data), digest, mime or _magic_mime(data),
                                 source_message_id, note)
        if await self.total_bytes() + len(data) > self.max_total_bytes:
            return await self._record_skip(scope_id, kind, "exceeds archive capacity", source_message_id, note)
        await asyncio.to_thread(self._write_atomic, target, data)
        record = ArchiveRecord(
            uuid.uuid4().hex, scope_id, kind, "saved", str(target), "", len(data),
            digest, mime or _magic_mime(data), source_message_id, note,
        )
        await self._record(record, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        return record

    @staticmethod
    def _write_atomic(target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{uuid.uuid4().hex}.part")
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)

    async def _record_skip(
        self, scope_id: str, kind: str, reason: str, source_message_id: str, note: str
    ) -> ArchiveRecord:
        record = ArchiveRecord(uuid.uuid4().hex, scope_id, kind, "skipped", note=note or reason,
                               source_message_id=source_message_id)
        await self._record(record, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        return record

    async def record_url(
        self, url: str, *, scope_id: str, source_message_id: str = "", note: str = ""
    ) -> ArchiveRecord:
        status = "pending" if self._safe_url(url) else "blocked"
        record = ArchiveRecord(uuid.uuid4().hex, scope_id, "link", status, url=url,
                               source_message_id=source_message_id, note=note)
        await self._record(record, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        return record

    @staticmethod
    def _safe_url(url: str) -> bool:
        return safe_url(url)

    async def ingest_onebot_image(self, payload: dict[str, Any], *, scope_id: str, source_message_id: str) -> ArchiveRecord:
        data: bytes | None = None
        if isinstance(payload, dict):
            inner = payload.get("data", payload)
            if isinstance(inner, dict):
                encoded = inner.get("base64")
                if isinstance(encoded, str) and encoded.strip():
                    try:
                        data = base64.b64decode(encoded.split(",", 1)[-1], validate=True)
                    except (binascii.Error, ValueError):
                        data = None
                if data is None and self._get_image is not None and inner.get("file"):
                    result = await self._get_image(str(inner["file"]))
                    nested = result.get("data", result) if isinstance(result, dict) else {}
                    if isinstance(nested, dict):
                        encoded = nested.get("base64")
                        if isinstance(encoded, str) and encoded.strip():
                            try:
                                data = base64.b64decode(encoded.split(",", 1)[-1], validate=True)
                            except (binascii.Error, ValueError):
                                data = None
        if data is None:
            return await self._record_skip(scope_id, "image", "no trusted local content", source_message_id, "")
        mime = _magic_mime(data)
        if mime == "application/octet-stream":
            return await self._record_skip(scope_id, "image", "not a supported image", source_message_id, "")
        return await self.save_bytes(data, scope_id=scope_id, kind="image",
                                     source_message_id=source_message_id, mime=mime, note="onebot image")

    async def ingest_url(
        self,
        url: str,
        *,
        scope_id: str,
        source_message_id: str = "",
        note: str = "user link",
    ) -> ArchiveRecord:
        """Curiosity download: only when the caller explicitly chooses to fetch."""
        if not self._safe_url(url):
            return await self._record_skip(scope_id, "link", "unsafe or private URL", source_message_id, url)
        if self._fetch_stream is None:
            return await self.record_url(url, scope_id=scope_id, source_message_id=source_message_id, note=note)
        chunks: list[bytes] = []
        total = 0
        declared: int | None = None
        try:
            async for chunk, content_length in self._fetch_stream(url, self.max_file_bytes):
                if declared is None and content_length is not None:
                    declared = content_length
                    if content_length > self.max_file_bytes:
                        return await self._record_skip(scope_id, "link", "content larger than limit", source_message_id, url)
                total += len(chunk)
                if total > self.max_file_bytes:
                    return await self._record_skip(scope_id, "link", "download exceeded limit", source_message_id, url)
                chunks.append(chunk)
        except Exception as error:
            return await self._record_skip(scope_id, "link", f"fetch failed: {type(error).__name__}", source_message_id, url)
        if not chunks:
            return await self._record_skip(scope_id, "link", "empty response", source_message_id, url)
        data = b"".join(chunks)
        return await self.save_bytes(data, scope_id=scope_id, kind="download",
                                     source_message_id=source_message_id, mime=_magic_mime(data), note=url)

    async def recent(self, scope_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        await self.ensure_open()
        limit = min(max(int(limit), 1), 100)
        if scope_id:
            cursor = await self._conn().execute(
                "SELECT * FROM media WHERE scope_id=? ORDER BY created_at DESC LIMIT ?", (scope_id, limit))
        else:
            cursor = await self._conn().execute("SELECT * FROM media ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            {
                "id": str(row["item_id"]), "scope": str(row["scope_id"]), "kind": str(row["kind"]),
                "status": str(row["status"]), "path": str(row["path"]), "url": str(row["url"]),
                "size": int(row["size"]), "mime": str(row["mime"]), "note": str(row["note"]),
            }
            for row in rows
        ]

    async def delete_scope(self, scope_id: str) -> int:
        """Delete every archived item of one scope: rows AND files."""
        return await self._purge(
            "SELECT item_id, path FROM media WHERE scope_id=?", (scope_id,),
            "DELETE FROM media WHERE scope_id=?", (scope_id,),
        )

    async def delete_all(self) -> int:
        """Wipe the whole archive: rows AND files."""
        return await self._purge(
            "SELECT item_id, path FROM media", (),
            "DELETE FROM media", (),
        )

    async def _purge(self, select_sql: str, select_args: tuple,
                     delete_sql: str, delete_args: tuple) -> int:
        async with self._lock:
            cursor = await self._conn().execute(select_sql, select_args)
            rows = await cursor.fetchall()
            await cursor.close()
            removed = 0
            for row in rows:
                path = str(row["path"] or "")
                if path:
                    try:
                        target = Path(path).resolve()
                        if self.root in target.parents:
                            target.unlink(missing_ok=True)
                            removed += 1
                    except OSError:
                        continue
            await self._conn().execute(delete_sql, delete_args)
            await self._conn().commit()
            return removed

    async def get_path(self, item_id: str) -> str:
        await self.ensure_open()
        cursor = await self._conn().execute(
            "SELECT path,status FROM media WHERE item_id=?", (item_id,))
        row = await cursor.fetchone()
        await cursor.close()
        if row is None or row["status"] != "saved" or not row["path"]:
            raise MediaArchiveError("item is not available")
        path = Path(str(row["path"])).resolve()
        if self.root not in path.parents:
            raise MediaArchiveError("stored path escaped archive root")
        if not await asyncio.to_thread(path.exists):
            # a 'saved' row whose file vanished — fail clearly instead of handing
            # a dead path to the sender (NapCat would raise ENOENT). Re-taking the
            # screenshot/re-saving the bytes self-heals it via save_bytes().
            raise MediaArchiveError("item file is missing on disk")
        return str(path)

    async def find_by_message(self, scope_id: str, message_id: str, kind: str | None = None) -> list[ArchiveRecord]:
        await self.ensure_open()
        cursor = await self._conn().execute(
            "SELECT * FROM media WHERE scope_id=? AND source_message_id=? ORDER BY created_at ASC",
            (scope_id, message_id),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        records = [
            ArchiveRecord(
                str(row["item_id"]), str(row["scope_id"]), str(row["kind"]),
                str(row["status"]), str(row["path"]), str(row["url"]),
                int(row["size"]), str(row["sha256"]), str(row["mime"]),
                str(row["source_message_id"]), str(row["note"]),
            )
            for row in rows
        ]
        if kind:
            records = [r for r in records if r.kind == kind]
        return records

    async def get_base64(self, item_id: str) -> tuple[str, str]:
        """Return (mime, base64) for a saved item."""
        await self.ensure_open()
        path = await self.get_path(item_id)
        cursor = await self._conn().execute("SELECT mime FROM media WHERE item_id=?", (item_id,))
        row = await cursor.fetchone()
        await cursor.close()
        mime = str(row["mime"]) if row and row["mime"] else "image/png"
        import base64 as _b64

        data = await asyncio.to_thread(Path(path).read_bytes)
        return mime, _b64.b64encode(data).decode("ascii")

    async def stats(self) -> dict[str, Any]:
        cursor = await self._conn().execute(
            "SELECT status,COUNT(*) AS n,COALESCE(SUM(size),0) AS bytes FROM media GROUP BY status")
        rows = await cursor.fetchall()
        await cursor.close()
        return {
            "total_bytes": await self.total_bytes(),
            "max_file_bytes": self.max_file_bytes,
            "max_total_bytes": self.max_total_bytes,
            "by_status": {str(row["status"]): {"count": int(row["n"]), "bytes": int(row["bytes"])} for row in rows},
        }


_MAGIC: tuple[tuple[bytes, str], ...] = _IMAGE_SIGNATURES + (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"RIFF", "application/octet-stream"),
    (b"%PDF", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
    (b"\x00\x00\x00\x18ftyp", "video/mp4"),
    (b"\x00\x00\x00\x20ftyp", "video/mp4"),
    (b"ID3", "audio/mpeg"),
    (b"OggS", "audio/ogg"),
)


def _magic_mime(data: bytes) -> str:
    for signature, mime in _MAGIC:
        if data.startswith(signature):
            if mime == "application/octet-stream" and data[:4] == b"RIFF":
                return "image/webp" if data[8:12] == b"WEBP" else mime
            if mime == "video/mp4" and b"ftyp" not in data[:12]:
                continue
            return mime
    if data[:2] == b"\xff\xfb" or data[:2] == b"\xff\xf3":
        return "audio/mpeg"
    try:
        text = data[:512].decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    lowered = text.lstrip().lower()
    if lowered.startswith("<!doctype html") or lowered.startswith("<html"):
        return "text/html"
    if lowered.startswith("{") or lowered.startswith("["):
        return "application/json"
    return "text/plain"


def _suffix_for(mime: str, data: bytes) -> str:
    mapping = {
        "image/png": ".png", "image/gif": ".gif", "image/jpeg": ".jpg",
        "image/webp": ".webp", "application/pdf": ".pdf", "application/zip": ".zip",
        "application/gzip": ".gz", "video/mp4": ".mp4", "audio/mpeg": ".mp3",
        "audio/ogg": ".ogg", "text/html": ".html", "text/plain": ".txt",
        "application/json": ".json",
    }
    if mime and mime in mapping:
        return mapping[mime]
    detected = _magic_mime(data)
    return mapping.get(detected, ".bin")


def safe_url(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    return not is_blocked_host(parsed.hostname)


async def fetch_bounded(
    url: str, max_bytes: int, *, fetcher: Callable[[str, int], Any] | None = None
) -> bytes:
    """Download a public URL with hard size limits; returns raw bytes."""
    if not safe_url(url):
        raise MediaArchiveError("unsafe or private URL")
    iterator = (fetcher or default_aiohttp_fetch)(url, max_bytes)
    if inspect.isawaitable(iterator):
        iterator = await iterator
    chunks: list[bytes] = []
    total = 0
    async for chunk, _declared in iterator:
        total += len(chunk)
        if total > max_bytes:
            raise MediaArchiveError("download exceeded limit")
        if chunk:
            chunks.append(chunk)
    if not chunks:
        raise MediaArchiveError("empty response")
    return b"".join(chunks)


def extract_urls(text: str) -> list[str]:
    return re.findall(r"https?://[^\s<>\"'）)\]]+", text or "")


def _stdlib_fetch(url: str, max_bytes: int) -> AsyncIterator[tuple[bytes, int | None]]:
    """urllib-based streaming fallback (no third-party dependency)."""
    async def _stream() -> AsyncIterator[tuple[bytes, int | None]]:
        import urllib.request

        def _open():
            request = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; AstrBotAgent)",
            })
            return urllib.request.urlopen(request, timeout=20)

        response = await asyncio.to_thread(_open)
        try:
            status = int(getattr(response, "status", 200) or 200)
            if status >= 300:
                raise MediaArchiveError(f"HTTP {status}")
            declared = response.headers.get("Content-Length")
            yield b"", int(declared) if declared and declared.isdigit() else None
            received = 0
            while True:
                chunk = await asyncio.to_thread(response.read, 64 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > max_bytes:
                    raise MediaArchiveError("download exceeded limit")
                yield chunk, None
        finally:
            try:
                response.close()
            except Exception:
                pass

    return _stream()


def default_aiohttp_fetch(url: str, max_bytes: int) -> AsyncIterator[tuple[bytes, int | None]]:
    """Preferred aiohttp downloader; falls back to stdlib when unavailable."""
    try:
        import aiohttp  # noqa: F401
    except Exception:
        return _stdlib_fetch(url, max_bytes)

    async def _stream() -> AsyncIterator[tuple[bytes, int | None]]:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=20, connect=8)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, allow_redirects=False) as response:
                    if response.status >= 300:
                        raise MediaArchiveError(f"HTTP {response.status}")
                    declared = response.headers.get("Content-Length")
                    yield b"", int(declared) if declared and declared.isdigit() else None
                    received = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        received += len(chunk)
                        if received > max_bytes:
                            raise MediaArchiveError("download exceeded limit")
                        yield chunk, None
                    return
        except MediaArchiveError:
            raise
        except Exception:
            # network/transport differences: fall back rather than fail the fetch
            pass

        async for item in _stdlib_fetch(url, max_bytes):
            yield item

    return _stream()
