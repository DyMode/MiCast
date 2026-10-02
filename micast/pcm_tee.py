"""Fan out one PCM stream to multiple readers (stereo pair channel split)."""

import asyncio
import contextlib
import logging

from .audio_metrics import metrics

logger = logging.getLogger(__name__)

# How much audio a branch may buffer before the oldest chunk is dropped.
#
# This used to be a chunk COUNT (64), which is not a duration: a source that
# hands over small fragments made 64 entries worth a few hundred milliseconds,
# so a paced pump (one that holds itself to realtime) overflowed it constantly.
# Field data (0.3.3): 56 drops in ~20s, both branches dropping in lockstep,
# while the source itself never stalled — the surplus that should have become
# the listener's jitter budget was simply thrown away. Bounding by TIME keeps
# the window meaningful whatever the fragment size is.
BRANCH_BUFFER_SECONDS = 4.0
DEFAULT_BYTES_PER_SECOND = 48000 * 4  # s16 stereo


class PCMTee:
    """Copies every PCM chunk from one source reader into N output readers.

    Used by stereo-pair receivers: the RAOP session decodes one stereo PCM
    stream, and each channel pipeline (L/R) consumes its own copy.
    """

    def __init__(self, source: asyncio.StreamReader, outputs: int = 2, sample_rate: int = 48000):
        self._source = source
        self.outputs = [BoundedPCMReader(bytes_per_second=sample_rate * 4) for _ in range(outputs)]
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    @property
    def dropped_chunks(self) -> int:
        """PCM chunks lost because a branch could not keep up with realtime."""
        return sum(out.dropped_chunks for out in self.outputs)

    def depth_ms(self) -> float:
        """Deepest branch buffer, in milliseconds of audio."""
        return max((out.depth_ms() for out in self.outputs), default=0.0)

    def capacity_ms(self) -> float:
        return max((out.capacity_ms() for out in self.outputs), default=0.0)

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
    A dead branch would therefore retain audio forever; this reader keeps only
    a bounded live window and drops the oldest chunk when full. The window is
    sized in TIME (see BRANCH_BUFFER_SECONDS) so it means the same thing for a
    source that delivers large blocks and one that delivers fragments.
    """

    def __init__(
        self,
        max_chunks: int = 256,
        *,
        max_seconds: float = BRANCH_BUFFER_SECONDS,
        bytes_per_second: int = DEFAULT_BYTES_PER_SECOND,
    ):
        # Set by the owner (bridge) to the stream id this branch feeds, so a
        # drop can be attributed instead of only counted globally.
        self.name = ""
        self._max_seconds = max_seconds
        self._bytes_per_second = bytes_per_second
        self._max_bytes = int(max_seconds * bytes_per_second)
        self._queued_bytes = 0
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=max_chunks)
        self._eof = False
        # Drops at this layer mean a downstream encoder/branch fell behind
        # realtime; surfaced via drop_stats so PCM-layer loss is as visible
        # as stream-server dropped_chunks.
        self.dropped_chunks = 0

    def depth_ms(self) -> float:
        return self._queued_bytes / self._bytes_per_second * 1000

    def capacity_ms(self) -> float:
        return self._max_seconds * 1000

    def feed_data(self, data: bytes) -> None:
        if self._eof:
            return
        self._queued_bytes += len(data)
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            self._drop_oldest()
            self._queue.put_nowait(data)
        self._enforce_time_window()

    def _drop_oldest(self) -> None:
        with contextlib.suppress(asyncio.QueueEmpty):
            dropped = self._queue.get_nowait()
            if isinstance(dropped, bytes):
                self._queued_bytes -= len(dropped)
        self.dropped_chunks += 1
        metrics.note_tee_drop(entry=self.name or None)

    def _enforce_time_window(self) -> None:
        """Trim past the time budget, keeping the queue's count cap as a hard backstop."""
        while self._queued_bytes > self._max_bytes and not self._queue.empty():
            self._drop_oldest()

    def feed_eof(self) -> None:
        if self._eof:
            return
        self._eof = True
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            self._drop_oldest()
            self._queue.put_nowait(None)

    def at_eof(self) -> bool:
        return self._eof and self._queue.empty()

    async def read(self, n: int = -1) -> bytes:
        item = await self._queue.get()
        if item:
            self._queued_bytes = max(0, self._queued_bytes - len(item))
            return item
        return b""
