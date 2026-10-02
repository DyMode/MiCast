"""Finite media URI decoder feeding the same PCM pipeline as live protocols."""

import asyncio
import contextlib
import queue
import threading
import time

import av

from micast.pcm_format import PCMFormat
from micast.pcm_source import PCMSource


class MediaPCMSource(PCMSource):
    def __init__(self, url: str, seek_seconds: float = 0, deferred: bool = False):
        self.url = url
        self.seek_seconds = max(0, seek_seconds)
        self._stop = threading.Event()
        self._queue = queue.Queue(maxsize=8)
        self._done = threading.Event()
        self._ready = threading.Event()
        self._task = None
        self._thread = None
        self.error = None
        self.samples = 0
        self.ended = False
        self.on_end = None
        self._active = asyncio.Event()
        if not deferred:
            self._active.set()

    def activate(self):
        self._active.set()

    @property
    def pcm_format(self):
        return PCMFormat(44100)

    @property
    def position(self):
        return self.seek_seconds + self.samples / self.pcm_format.sample_rate

    def _emit(self, frame):
        data = bytes(frame.planes[0])[: frame.samples * 4]
        while not self._stop.is_set():
            try:
                self._queue.put(data, timeout=0.05)
                self._ready.set()
                return
            except queue.Full:
                continue

    def _decode(self):
        try:
            with av.open(
                self.url,
                options={"rw_timeout": "15000000", "user_agent": "Mozilla/5.0 MiCast"},
                metadata_errors="ignore",
            ) as container:
                if self.seek_seconds:
                    container.seek(int(self.seek_seconds * 1_000_000), backward=True)
                audio = container.streams.audio[0]
                resampler = av.AudioResampler(format="s16", layout="stereo", rate=44100)
                for frame in container.decode(audio):
                    if self._stop.is_set():
                        return
                    for output in resampler.resample(frame):
                        # A backwards seek lands on a keyframe; trim before the requested time.
                        timestamp = (
                            float(output.pts * output.time_base) if output.pts is not None else None
                        )
                        if (
                            timestamp is not None
                            and timestamp + output.samples / 44100 <= self.seek_seconds
                        ):
                            continue
                        if timestamp is not None and timestamp < self.seek_seconds:
                            skip = min(output.samples, int((self.seek_seconds - timestamp) * 44100))
                            data = bytes(output.planes[0])[skip * 4 : output.samples * 4]
                            trimmed = av.AudioFrame(
                                format="s16", layout="stereo", samples=len(data) // 4
                            )
                            trimmed.planes[0].update(data)
                            self._emit(trimmed)
                        else:
                            self._emit(output)
                for frame in resampler.resample(None):
                    self._emit(frame)
        except Exception as exc:
            self.error = exc
        finally:
            self._done.set()

    async def start(self):
        reader = asyncio.StreamReader()
        self._thread = threading.Thread(target=self._decode, name="media-pcm", daemon=True)
        self._thread.start()
        try:
            await asyncio.wait_for(self._wait_ready(), 16)
        except BaseException:
            await self.stop()
            raise
        self._task = asyncio.create_task(self._pump(reader))
        return reader

    async def _wait_ready(self):
        while not self._ready.is_set():
            if self._stop.is_set():
                raise ValueError("媒体准备已取消")
            if self._done.is_set():
                raise ValueError("无法读取媒体音频") from self.error
            await asyncio.sleep(0.01)

    async def _pump(self, reader):
        await self._active.wait()
        started = time.monotonic()
        try:
            while not self._stop.is_set():
                try:
                    chunk = self._queue.get_nowait()
                except queue.Empty:
                    if self._done.is_set():
                        self.ended = True
                        break
                    await asyncio.sleep(0.01)
                    continue
                wait = self.samples / 44100 - (time.monotonic() - started) - 0.15
                if wait > 0:
                    await asyncio.sleep(wait)
                self.samples += len(chunk) // 4
                reader.feed_data(chunk)
        finally:
            reader.feed_eof()
            if self.ended and self.on_end:
                self.on_end(self.error)

    async def stop(self):
        self._stop.set()
        if self._task and self._task is not asyncio.current_task():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._thread:
            await asyncio.to_thread(self._thread.join, 0.2)
