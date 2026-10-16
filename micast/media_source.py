"""Finite media URI decoder feeding the same PCM pipeline as live protocols."""

import asyncio
import contextlib
import logging
import queue
import threading
import time

import av

from micast.pcm_format import PCMFormat
from micast.pcm_source import PCMSource
from micast.pyav_compat import DLNA_USER_AGENT, dlna_headers_string, open_input

logger = logging.getLogger(__name__)

# Hard deadline (seconds) for ffmpeg's open + stream-info probe. Socket-level
# rw_timeout does not cover a peer that answers one byte at a time.
OPEN_TIMEOUT_SECONDS = 10.0


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
        # Controller-hosted media servers (WMP MDEServer and friends) answer
        # a bare GET with a 4XX unless the request carries a DLNA client
        # fingerprint; the Referer inside covers controllers (NetEase and
        # similar) that 403 without it. The user_agent OPTION matters: ffmpeg
        # writes it as the request's primary User-Agent header, whereas a
        # User-Agent line in the custom headers string is overridden by
        # ffmpeg's Lavf default — which WMP's media sharing rejects.
        options = {
            "rw_timeout": "15000000",
            "user_agent": DLNA_USER_AGENT,
            "headers": dlna_headers_string(self.url),
        }
        try:
            # timeout=(open, read) is PyAV's interrupt-callback deadline
            # (seconds) for avformat_open_input AND avformat_find_stream_info.
            # rw_timeout only guards individual socket reads, so a server that
            # dribbles bytes (WMP MDEServer) could otherwise keep the probe
            # phase alive indefinitely; the interrupt callback cannot be
            # dribbled past. Reads stay on the socket-level rw_timeout.
            # open_input applies metadata_errors only where the installed
            # PyAV still accepts it (removed in PyAV 19).
            with contextlib.ExitStack() as stack:

                def open_container():
                    return stack.enter_context(
                        open_input(self.url, options=options, timeout=(OPEN_TIMEOUT_SECONDS, None))
                    )

                container = open_container()
                scan_from_start = False
                if self.seek_seconds:
                    try:
                        container.seek(int(self.seek_seconds * 1_000_000), backward=True)
                    except (av.error.FFmpegError, ValueError):
                        # Some valid FLAC files have no usable seek table.
                        # Reopen the local file and discard decoded frames up
                        # to the requested time; never re-fetch the origin.
                        container.close()
                        container = open_container()
                        scan_from_start = True
                audio = container.streams.audio[0]
                resampler = av.AudioResampler(format="s16", layout="stereo", rate=44100)
                decoded_samples = 0

                def outputs():
                    for frame in container.decode(audio):
                        if self._stop.is_set():
                            return
                        yield from resampler.resample(frame)
                    yield from resampler.resample(None)

                for output in outputs():
                    if self._stop.is_set():
                        return
                    # A backwards seek lands on a keyframe; trim before the requested time.
                    timestamp = (
                        float(output.pts * output.time_base) if output.pts is not None else None
                    )
                    if timestamp is None and scan_from_start:
                        timestamp = decoded_samples / 44100
                    decoded_samples += output.samples
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
            # Never enqueue cleanup behind DNS/other work in the default
            # executor. Waiting for a thread's completion flag is nonblocking.
            deadline = asyncio.get_running_loop().time() + 0.2
            while self._thread.is_alive() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            if self._thread.is_alive():
                # A blocked ffmpeg call (or its interrupt callback not firing)
                # cannot be killed from Python; the daemon thread is leaked on
                # purpose so playback state stays consistent. open_timeout
                # bounds this to seconds for network URIs.
                logger.warning("Media decode thread did not exit for %s; leaking it", self.url)
