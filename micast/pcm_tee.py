"""Fan out one PCM stream to multiple readers (stereo pair channel split)."""

import asyncio
import contextlib
import logging

from .audio_metrics import metrics

logger = logging.getLogger(__name__)


class PCMTee:
    """Copies every PCM chunk from one source reader into N output readers.

    Used by stereo-pair receivers: the RAOP session decodes one stereo PCM
    stream, and each channel pipeline (L/R) consumes its own copy.
    """

    def __init__(self, source: asyncio.StreamReader, outputs: int = 2):
        self._source = source
        self.outputs = [BoundedPCMReader() for _ in range(outputs)]
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    @property
    def dropped_chunks(self) -> int:
        """PCM chunks lost because a branch could not keep up with realtime."""
        return sum(out.dropped_chunks for out in self.outputs)

    async def _run(self) -> None:
        try:
            while True:
                chunk = await self._source.read(32768)
                if not chunk:
                    break
                for out in self.outputs:
                    out.feed_data(chunk)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("PCM tee failed")
        finally:
            for out in self.outputs:
                with contextlib.suppress(Exception):
                    out.feed_eof()

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


class BoundedPCMReader:
    """Small StreamReader-compatible realtime PCM sink.

    ``asyncio.StreamReader.feed_data`` has an unbounded internal byte buffer.
    A dead stereo branch would therefore retain audio forever; this reader
    keeps only a short live window and drops the oldest chunk when full.
    """

    def __init__(self, max_chunks: int = 64):
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=max_chunks)
        self._eof = False
        # Drops at this layer mean a downstream encoder/branch fell behind
        # realtime; surfaced via drop_stats so PCM-layer loss is as visible
        # as stream-server dropped_chunks.
        self.dropped_chunks = 0

    def feed_data(self, data: bytes) -> None:
        if self._eof:
            return
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self.dropped_chunks += 1
            metrics.note_tee_drop()
            self._queue.put_nowait(data)

    def feed_eof(self) -> None:
        if self._eof:
            return
        self._eof = True
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self.dropped_chunks += 1
            metrics.note_tee_drop()
            self._queue.put_nowait(None)

    def at_eof(self) -> bool:
        return self._eof and self._queue.empty()

    async def read(self, n: int = -1) -> bytes:
        item = await self._queue.get()
        return item or b""
