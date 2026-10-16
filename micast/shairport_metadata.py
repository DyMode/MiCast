"""Bounded, nonblocking reader for Shairport's XML metadata FIFO."""

import asyncio
import base64
import os
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET


class ShairportMetadata:
    def __init__(self):
        self.daap_meta = {}
        self.daap_events = []
        self.lyric_lines = []
        self.artwork_bytes = b""
        self.artwork_rev = 0
        self.artwork_content_type = ""
        self._seq = 0
        self._buffer = b""
        self._tags = {}
        self._directory = None
        self._fd = None
        self._task = None

    def start(self):
        self._directory = tempfile.TemporaryDirectory(prefix="micast-metadata-")
        path = Path(self._directory.name) / "metadata"
        os.mkfifo(path, 0o600)
        self._fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self._task = asyncio.create_task(self._read())
        return str(path)

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._directory:
            self._directory.cleanup()
            self._directory = None

    async def _read(self):
        while True:
            try:
                chunk = os.read(self._fd, 65536)
            except BlockingIOError:
                chunk = b""
            if chunk:
                self.feed(chunk)
            else:
                await asyncio.sleep(0.1)

    def feed(self, chunk):
        self._buffer += chunk
        if len(self._buffer) > 4 * 1024 * 1024:
            self._buffer = b""
            return
        while b"</item>" in self._buffer:
            item, self._buffer = self._buffer.split(b"</item>", 1)
            start = item.find(b"<item>")
            if start < 0:
                continue
            try:
                node = ET.fromstring(item[start:] + b"</item>")
                kind = bytes.fromhex(node.findtext("type", "")).decode("ascii")
                code = bytes.fromhex(node.findtext("code", "")).decode("ascii")
                value = base64.b64decode(node.findtext("data", ""), validate=False)
                self.note(kind, code, value)
            except (ValueError, UnicodeError, ET.ParseError):
                continue

    def note(self, kind, code, value):
        from micast.raop.dmap import track_meta

        if kind == "ssnc" and code == "pend":
            self.daap_meta = {}
            self.daap_events = []
            self.lyric_lines = []
            self.artwork_bytes = b""
            self._tags = {}
        elif kind == "ssnc" and code == "mdst":
            self._tags = {}
        elif kind == "core" and code in ("minm", "asar", "asal"):
            self._tags[code] = value
        elif kind == "ssnc" and code == "mden":
            body = b"".join(
                k.encode() + len(v).to_bytes(4, "big") + v for k, v in self._tags.items()
            )
            meta = track_meta(body)
            if meta.get("title"):
                self.daap_meta = meta
                self._seq += 1
                self.daap_events.append((self._seq, dict(meta)))
                self.daap_events = self.daap_events[-128:]
                self.lyric_lines = [meta["lyric_line"]] if meta.get("lyric_line") else []
        elif kind == "ssnc" and code == "PICT" and len(value) <= 2 * 1024 * 1024:
            self.artwork_bytes = value
            self.artwork_rev += 1
            self.artwork_content_type = (
                "image/png" if value.startswith(b"\x89PNG") else "image/jpeg"
            )
