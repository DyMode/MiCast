"""Bounded, cancellable media downloads shared by raw and processed DLNA output.

Downloads run on the event loop with the application's bounded DNS resolver.
Decoders only see completed local files, never a blocking remote socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from micast.net import new_session
from micast.pyav_compat import DLNA_HEADERS

logger = logging.getLogger(__name__)
BROWSER_AGENT = (
    "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/120.0.0.0 Mobile Safari/537.36"
)
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_CACHE_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 16
DOWNLOAD_TIMEOUT = 120.0


class MediaDownload:
    def __init__(self, cache, owner, uri, identity):
        self.cache, self.owner, self.uri, self.identity = cache, owner, uri, identity
        self.token = secrets.token_urlsafe(12)
        self.path: Path | None = None
        self.size = 0
        self.mime = "application/octet-stream"
        self.error = ""
        self.closed = False
        self.ready = asyncio.Event()
        self.task = asyncio.create_task(self._download(), name=f"dlna-download:{owner}")

    async def _download(self):
        try:
            async with self.cache.slots:
                # Public music CDNs expect a browser; LAN media servers expect
                # DLNA headers. A rejected request gets one alternate fingerprint.
                host = (urlsplit(self.uri).hostname or "").lower()
                cdn = host.endswith(("music.126.net", "music.tc.qq.com", "qqmusic.qq.com"))
                fingerprints = (
                    [{"User-Agent": BROWSER_AGENT}, DLNA_HEADERS]
                    if cdn
                    else [DLNA_HEADERS, {"User-Agent": BROWSER_AGENT}]
                )
                timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT, connect=8, sock_read=20)
                async with new_session(timeout=timeout) as client:
                    for index, headers in enumerate(fingerprints):
                        async with client.get(
                            self.uri, headers=headers, allow_redirects=True
                        ) as response:
                            if response.status in (400, 401, 403, 406) and index == 0:
                                continue
                            response.raise_for_status()
                            self.mime = (
                                response.headers.get("Content-Type", "")
                                .split(";")[0]
                                .strip()
                                .lower()
                                or "application/octet-stream"
                            )
                            if not (
                                self.mime.startswith("audio/")
                                or self.mime in {"application/octet-stream", "binary/octet-stream"}
                            ):
                                raise ValueError("媒体地址返回了非音频内容")
                            if "octet-stream" in self.mime and urlsplit(
                                self.uri
                            ).path.lower().endswith(".mp3"):
                                self.mime = "audio/mpeg"
                            if (
                                response.content_length
                                and response.content_length > self.cache.file_limit
                            ):
                                raise ValueError("媒体文件超过缓存限制")
                            with tempfile.NamedTemporaryFile(
                                prefix="micast-media-", delete=False
                            ) as output:
                                self.path = Path(output.name)
                                async for chunk in response.content.iter_chunked(65536):
                                    if (
                                        self.size + len(chunk) > self.cache.file_limit
                                        or self.cache.bytes + len(chunk) > self.cache.byte_limit
                                    ):
                                        raise ValueError("媒体缓存容量不足")
                                    output.write(chunk)
                                    self.size += len(chunk)
                                    self.cache.bytes += len(chunk)
                            if not self.size:
                                raise ValueError("媒体文件为空")
                            logger.info(
                                "DLNA download ready: receiver=%s bytes=%s type=%s",
                                self.owner,
                                self.size,
                                self.mime,
                            )
                            break
        except asyncio.CancelledError:
            self.error = "媒体下载已取消"
            self._discard()
            raise
        except Exception as exc:
            if isinstance(exc, aiohttp.ClientResponseError):
                reason = f"HTTP {exc.status}"
            elif isinstance(exc, ValueError):
                reason = str(exc)
            else:
                reason = type(exc).__name__
            self.error = f"媒体下载失败：{reason}"
            logger.warning("DLNA download failed: receiver=%s reason=%s", self.owner, self.error)
            self._discard()
        finally:
            self.ready.set()

    def _discard(self):
        if self.path is not None:
            self.path.unlink(missing_ok=True)
            self.path = None
        self.cache.bytes -= self.size
        self.size = 0

    async def wait(self):
        await self.ready.wait()
        if self.closed or self.error or self.path is None:
            raise ValueError(self.error or "媒体下载已取消")
        return self.path

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task
        self._discard()
        self.ready.set()


class MediaCache:
    def __init__(
        self, *, file_limit=MAX_FILE_BYTES, byte_limit=MAX_CACHE_BYTES, entry_limit=MAX_ENTRIES
    ):
        self.file_limit, self.byte_limit, self.entry_limit = file_limit, byte_limit, entry_limit
        self.bytes = 0
        self.entries: dict[str, MediaDownload] = {}
        self.owners: dict[str, MediaDownload] = {}
        self.slots = asyncio.Semaphore(4)
        self._locks: dict[str, asyncio.Lock] = {}

    async def prepare(self, owner, uri, identity):
        async with self._locks.setdefault(owner, asyncio.Lock()):
            entry = self.owners.get(owner)
            if (
                entry
                and entry.uri == uri
                and entry.identity == identity
                and not entry.closed
                and not entry.error
            ):
                return entry
            await self._release(owner)
            if len(self.entries) >= self.entry_limit:
                raise ValueError("同时准备的媒体过多")
            entry = MediaDownload(self, owner, uri, identity)
            self.entries[entry.token] = self.owners[owner] = entry
            return entry

    async def release(self, owner):
        async with self._locks.setdefault(owner, asyncio.Lock()):
            await self._release(owner)

    async def _release(self, owner):
        entry = self.owners.pop(owner, None)
        if entry:
            self.entries.pop(entry.token, None)
            await entry.close()

    def rebind(self, entry, identity, *, token=None):
        """Keep the downloaded file while revoking every old speaker URL."""
        if entry.closed or self.owners.get(entry.owner) is not entry:
            raise ValueError("媒体缓存已失效")
        self.entries.pop(entry.token, None)
        entry.token = token if token is not None else secrets.token_urlsafe(12)
        entry.identity = identity
        self.entries[entry.token] = entry

    async def close(self):
        for owner in list(self.owners):
            await self.release(owner)
