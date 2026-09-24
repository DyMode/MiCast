"""In-process audio transcoding via PyAV (libavcodec/libavfilter).

Replaces the ffmpeg subprocess: PCM s16le stereo goes in, an MP3/FLAC/WAV
byte stream comes out. PyAV releases the GIL inside libavcodec, so a worker
thread keeps encoding off the event loop. The bundled av.libs already carry
every codec needed — no external ffmpeg binary ships with the app.

The same module also powers the two one-shot transcodes that used to shell
out: the DLNA seekable-media proxy and the debug-page file preview.
"""

import asyncio
import contextlib
import io
import logging
import queue
import struct
import threading
import time
from dataclasses import dataclass
from functools import lru_cache

import av
from av.filter import Graph

from micast.audio_metrics import metrics
from micast.config import settings

logger = logging.getLogger(__name__)

# Push/pull are drained until these surface: EAGAIN means "no frame ready
# yet", EOFError means the graph is done after the closing push(None).
_PULL_DONE = (EOFError, av.error.BlockingIOError)


@dataclass(frozen=True)
class StreamFormat:
    """Output stream media type and file extension."""

    content_type: str
    extension: str
    byte_rate: int | None = None


_FORMATS: dict[str, StreamFormat] = {
    "mp3": StreamFormat("audio/mpeg", "mp3"),
    "flac": StreamFormat("audio/flac", "flac"),
    "wav": StreamFormat("audio/wav", "wav"),
}

_CODECS = {"mp3": "libmp3lame", "flac": "flac", "wav": "pcm_s16le"}


def raw_pcm_format(sample_rate: int = 44100) -> StreamFormat:
    """Stream format when the encoder is bypassed: raw PCM wrapped as streaming WAV."""
    return StreamFormat("audio/wav", "wav", sample_rate * 2 * 2)


def wav_header(
    sample_rate: int,
    channels: int = 2,
    bits: int = 16,
    data_bytes: int | None = None,
) -> bytes:
    """Minimal WAV header prepended to raw PCM.

    Xiaomi speakers cannot parse headerless L16; wrapping PCM in WAV keeps the
    bypass path playable without running an encoder. A known data size is used
    for finite streams; the default preserves the live-stream sentinel.
    """
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    data_size = 0xFFFFFFFF if data_bytes is None else data_bytes
    riff_size = 0xFFFFFFFF if data_bytes is None else 36 + data_bytes
    return (
        b"RIFF"
        + struct.pack("<I", riff_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits)
        + b"data"
        + struct.pack("<I", data_size)
    )


@lru_cache(maxsize=8)
def mp3_silence(sample_rate: int, bitrate: str, seconds: int = 5) -> bytes:
    """Reusable CBR silence that keeps pull players alive while delay fills."""
    buffer = io.BytesIO()
    container = av.open(buffer, mode="w", format="mp3")
    stream = _open_encoder(container, "mp3", bitrate, sample_rate)
    frame_samples = 960
    frames = max(1, sample_rate * seconds // frame_samples)
    for _ in range(frames):
        frame = av.AudioFrame(format="s16", layout="stereo", samples=frame_samples)
        frame.sample_rate = sample_rate
        frame.planes[0].update(bytes(frame_samples * 4))
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    return buffer.getvalue()


class _StreamSink(io.RawIOBase):
    """File-like the muxer writes into; each write becomes one output chunk."""

    def __init__(self, emit):
        self._emit = emit

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def write(self, data) -> int:
        if data:
            self._emit(bytes(data))
        return len(data)


def _open_encoder(container, fmt: str, bitrate: str, sample_rate: int):
    """Add the output audio stream for one of our three formats."""
    stream = container.add_stream(_CODECS[fmt], rate=sample_rate)
    stream.layout = "stereo"
    if fmt == "mp3":
        stream.bit_rate = int(bitrate.removesuffix("k")) * 1000
    return stream


@lru_cache(maxsize=1)
def firequalizer_available() -> bool:
    """Whether the bundled libavfilter can configure firequalizer with a real
    gain table. Probed once with a dense, non-flat curve: the filter can add
    successfully yet fail at configure with a large gain_entry (ENOMEM on the
    installed Windows build), so a trivial one-entry probe is not enough."""
    try:
        graph = Graph()
        src = graph.add_abuffer(format="s16", layout="stereo", sample_rate=48000)
        # 120 log-spaced entries matching curve_fit.gain_table, with a non-flat
        # gain slope (+6 dB → −6 dB) so the probe exercises interpolation too.
        entries = ";".join(
            f"entry({20.0 * (20000.0 / 20.0) ** (i / 119):.1f},{6.0 - 12.0 * i / 119:.2f})"
            for i in range(120)
        )
        node = graph.add("firequalizer", f"gain_entry='{entries}'")
        sink = graph.add("abuffersink")
        graph.link_nodes(src, node, sink).configure()
        return True
    except Exception:
        logger.info("firequalizer unavailable; EQ curves fall back to equalizer chain")
        return False


def _build_filter_graph(
    audio_filter: str | list[tuple[str, str]] | None,
    input_rate: int,
    output_rate: int,
) -> Graph:
    """abuffer → [speaker EQ/pan/gain/delay chain] → aresample → s16 stereo out.

    ``audio_filter`` is normally a structured list of (name, args) links —
    firequalizer gain tables contain commas and semicolons, so the old
    comma-joined mini-language string is accepted only for compatibility.
    """
    if isinstance(audio_filter, str):
        links = []
        for link in audio_filter.split(","):
            name, _, args = link.partition("=")
            links.append((name.strip(), args.strip()))
        audio_filter = links
    graph = Graph()
    nodes = [
        graph.add_abuffer(format="s16", layout="stereo", sample_rate=input_rate),
    ]
    if audio_filter:
        for name, args in audio_filter:
            nodes.append(graph.add(name, args))
    if output_rate != input_rate:
        nodes.append(graph.add("aresample", str(output_rate)))
    nodes.append(graph.add("aformat", "sample_fmts=s16:channel_layouts=stereo"))
    nodes.append(graph.add("abuffersink"))
    graph.link_nodes(*nodes).configure()
    return graph


def _drain_graph(graph, stream, container) -> None:
    while True:
        try:
            frame = graph.pull()
        except _PULL_DONE:
            return
        for packet in stream.encode(frame):
            container.mux(packet)


class AudioEncoder:
    """One receiver's PCM → MP3/FLAC/WAV encoder, interface-compatible with
    the old ffmpeg subprocess wrapper (stdin/stdout/start/stop/wait)."""

    def __init__(
        self,
        config,
        input_sample_rate: int | None = None,
        audio_filter: str | list[tuple[str, str]] | None = None,
    ):
        self.config = config
        self.input_sample_rate = input_sample_rate or settings.pcm_sample_rate
        self.audio_filter = audio_filter
        # Realtime audio must never accumulate without limit. At 44.1 kHz,
        # unbounded PCM grows by ~635 MB/hour when an encoder stalls.
        self._in: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
        self._out: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)
        # Loss counters for the drop-oldest overflow paths (see _put_latest_*):
        # input drops mean the pipeline fed PCM faster than this encoder could
        # consume; output drops mean the event loop did not drain encoded
        # chunks fast enough. Both are realtime losses, surfaced via
        # SpeakerPipeline.drop_stats() alongside stream-server dropped_chunks.
        self._drop_counts = {"in": 0, "out": 0}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._exit_code: int | None = None
        self.stdin = _PCMWriter(self._in, self._drop_counts, "in")

    def drop_stats(self) -> dict[str, int]:
        return dict(self._drop_counts)

    @property
    def format(self) -> StreamFormat:
        stream_format = _FORMATS[self.config.format]
        if self.config.format == "mp3":
            bits_per_second = int(self.config.bitrate.removesuffix("k")) * 1000
            return StreamFormat(
                stream_format.content_type,
                stream_format.extension,
                bits_per_second // 8,
            )
        if self.config.format == "wav":
            return StreamFormat(
                stream_format.content_type,
                stream_format.extension,
                self.config.sample_rate * 2 * 2,
            )
        return stream_format

    async def start(self) -> "AudioEncoder":
        self._loop = asyncio.get_running_loop()
        chain = (
            ",".join(f"{name}={args}" for name, args in self.audio_filter)
            if isinstance(self.audio_filter, list)
            else self.audio_filter
        )
        logger.info(
            "Starting encoder: s16le/%dHz → %s/%dHz%s",
            self.input_sample_rate,
            self.config.format,
            self.config.sample_rate,
            f" [{chain}]" if chain else "",
        )
        self._thread = threading.Thread(
            target=self._run, name=f"encoder-{self.config.format}", daemon=True
        )
        self._thread.start()
        return self

    async def stop(self) -> None:
        if self._thread is None or self._exit_code is not None:
            return
        with contextlib.suppress(Exception):
            self._in.put_nowait(None)  # close the PCM feed → flush + EOF
        try:
            await asyncio.wait_for(asyncio.to_thread(self._thread.join), timeout=3.0)
        except TimeoutError:
            logger.warning("encoder thread did not finish within 3s")

    async def wait(self) -> int | None:
        """Wait for the worker thread to finish; returns a process-like exit code."""
        if self._thread is None:
            return None
        await asyncio.to_thread(self._thread.join)
        return self._exit_code

    @property
    def stdout(self) -> "_EncodedReader | None":
        return _EncodedReader(self._out) if self._thread else None

    @property
    def returncode(self) -> int | None:
        return self._exit_code

    # ---- worker thread ----

    def _run(self) -> None:
        try:
            self._transcode()
            self._exit_code = 0
        except Exception:
            logger.exception("Encoder thread failed")
            self._exit_code = 1
        finally:
            # EOF marker unblocks the pipeline's stdout pump.
            self._loop.call_soon_threadsafe(_put_latest_async, self._out, None)

    def _transcode(self) -> None:
        assert self._loop is not None
        out = self._out
        loop = self._loop

        def emit(chunk: bytes) -> None:
            def put() -> None:
                if _put_latest_async(out, chunk):
                    self._drop_counts["out"] += 1
                    metrics.note_encoder_drop("out")

            loop.call_soon_threadsafe(put)

        container = av.open(_StreamSink(emit), mode="w", format=self.config.format)
        stream = _open_encoder(
            container, self.config.format, self.config.bitrate, self.config.sample_rate
        )
        graph = _build_filter_graph(
            self.audio_filter, self.input_sample_rate, self.config.sample_rate
        )
        rate = self.input_sample_rate
        while True:
            chunk = self._in.get()
            if chunk is None:
                break
            if not chunk:
                continue
            frame = av.AudioFrame(format="s16", layout="stereo", samples=len(chunk) // 4)
            frame.sample_rate = rate
            frame.planes[0].update(chunk)
            graph.push(frame)
            _drain_graph(graph, stream, container)
        graph.push(None)
        _drain_graph(graph, stream, container)
        for packet in stream.encode(None):
            container.mux(packet)
        container.close()


class _PCMWriter:
    """Sync facade over the input queue; matches StreamWriter's used surface."""

    def __init__(self, pcm_queue: queue.Queue, drop_counts: dict, drop_key: str):
        self._queue = pcm_queue
        self._drop_counts = drop_counts
        self._drop_key = drop_key

    def write(self, data: bytes) -> None:
        if _put_latest_sync(self._queue, data):
            self._drop_counts[self._drop_key] += 1
            metrics.note_encoder_drop(self._drop_key)

    def write_eof(self) -> None:
        _put_latest_sync(self._queue, None)

    async def drain(self) -> None:
        # libavcodec encodes several times faster than realtime; an unbounded
        # queue therefore stays near-empty and no backpressure is needed.
        return


def _put_latest_sync(target: queue.Queue, item: bytes | None) -> bool:
    """Bound latency by discarding the oldest realtime chunk on overflow.

    Returns True when a chunk was discarded."""
    try:
        target.put_nowait(item)
        return False
    except queue.Full:
        with contextlib.suppress(queue.Empty):
            target.get_nowait()
        target.put_nowait(item)
        return True


def _put_latest_async(target: asyncio.Queue, item: bytes | None) -> bool:
    """Async counterpart of _put_latest_sync; True when a chunk was discarded."""
    try:
        target.put_nowait(item)
        return False
    except asyncio.QueueFull:
        with contextlib.suppress(asyncio.QueueEmpty):
            target.get_nowait()
        target.put_nowait(item)
        return True


class _EncodedReader:
    """Async read(n) facade over the output queue; matches StreamReader.

    ``read(n)`` returns AT MOST n bytes, coalescing every chunk that is
    immediately pending — the muxer flushes one encoded frame as many small
    writes (a FLAC frame can be hundreds of tiny sink writes), and without
    aggregation each write would become one broadcast and one per-client
    queue item, making queue accounting meaningless and multiplying drop
    counters by an arbitrary factor. Items are never split; a single item
    larger than n is returned whole. This is the ONE chunker every encoded
    format (flac/mp3/wav) travels through, so all formats present the same
    granularity downstream regardless of muxer write patterns.
    """

    def __init__(self, out_queue: asyncio.Queue):
        self._queue = out_queue
        self._saw_eof = False

    async def read(self, n: int = -1) -> bytes:
        if self._saw_eof:
            return b""
        while True:
            item = await self._queue.get()
            if item is None:
                self._saw_eof = True
                return b""
            if n <= 0 or len(item) >= n:
                return item
            parts = [item]
            total = len(item)
            while total < n:
                try:
                    extra = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if extra is None:
                    # EOF sentinel consumed mid-aggregation: the queued audio
                    # still belongs to this run; report EOF on the next call.
                    self._saw_eof = True
                    break
                parts.append(extra)
                total += len(extra)
            return b"".join(parts)

    def read_nowait(self) -> bytes:
        """Drain one immediately-available chunk (b"" when none)."""
        if self._saw_eof:
            return b""
        try:
            item = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return b""
        if item is None:
            self._saw_eof = True
            return b""
        return item


# ---------------------------------------------------------------------------
# One-shot transcodes (DLNA media proxy, debug-page preview)
# ---------------------------------------------------------------------------


def transcode_file_to_wav(path) -> bytes:
    """Decode a local media file and return it as a 44.1kHz stereo WAV.

    Seekable BytesIO output, so sizes in the header are real — this is a
    finite preview, not a stream.
    """
    resampler = av.audio.resampler.AudioResampler(format="s16", layout="stereo", rate=44100)
    buffer = io.BytesIO()
    out = av.open(buffer, mode="w", format="wav")
    stream = _open_encoder(out, "wav", "", 44100)
    with av.open(str(path)) as source:
        audio = source.streams.audio[0]
        for frame in source.decode(audio):
            for resampled in resampler.resample(frame):
                for packet in stream.encode(resampled):
                    out.mux(packet)
        for resampled in resampler.resample(None):
            for packet in stream.encode(resampled):
                out.mux(packet)
    for packet in stream.encode(None):
        out.mux(packet)
    out.close()
    return buffer.getvalue()


def stream_media_as_mp3(
    url: str,
    seek_seconds: float,
    emit,
    stop: threading.Event,
    user_agent: str,
    volume_provider=None,
) -> None:
    """Decode a remote URL from `seek_seconds` and emit MP3 (192k) chunks.

    Runs on a worker thread; `emit(chunk)` hands bytes to the async side and
    `stop` aborts promptly when the HTTP client goes away mid-stream.
    """
    container = None
    out = None
    try:
        # Media from DLNA control points may carry non-UTF8 (e.g. GBK) tags;
        # PyAV decodes container metadata strictly by default and av.open
        # would raise UnicodeDecodeError before we see a single frame.
        container = av.open(
            url,
            options={"user_agent": user_agent},
            metadata_errors="ignore",
        )
        if seek_seconds > 0:
            container.seek(int(seek_seconds * 1_000_000), backward=True)
        audio = container.streams.audio[0]
        resampler = av.audio.resampler.AudioResampler(format="s16", layout="stereo", rate=44100)
        out = av.open(_StreamSink(emit), mode="w", format="mp3")
        stream = _open_encoder(out, "mp3", "192k", 44100)
        started = time.monotonic()
        samples_sent = 0
        for frame in container.decode(audio):
            if stop.is_set():
                return
            for resampled in resampler.resample(frame):
                if volume_provider is not None:
                    # Don't encode/download minutes ahead of a live volume
                    # change. The small lead accommodates decoder startup.
                    wait = samples_sent / 44100 - (time.monotonic() - started) - 0.15
                    if wait > 0 and stop.wait(wait):
                        return
                    from micast.volume import apply_pcm_gain

                    plane = resampled.planes[0]
                    plane.update(apply_pcm_gain(bytes(plane), volume_provider()))
                    samples_sent += resampled.samples
                for packet in stream.encode(resampled):
                    out.mux(packet)
        if stop.is_set():
            return
        for resampled in resampler.resample(None):
            for packet in stream.encode(resampled):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    except Exception:
        logger.exception("Media proxy transcode failed for %s", url)
    finally:
        with contextlib.suppress(Exception):
            if out is not None:
                out.close()
        with contextlib.suppress(Exception):
            if container is not None:
                container.close()


class MediaProxyPump:
    """Async-side harness for stream_media_as_mp3: bounded queue, prompt abort."""

    def __init__(self, url: str, seek_seconds: float, user_agent: str, volume_provider=None):
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=8)
        self._done = threading.Event()
        self._volume_provider = volume_provider
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._args = (url, seek_seconds, user_agent)

    def start(self) -> "MediaProxyPump":
        self._loop = asyncio.get_running_loop()
        url, seek_seconds, user_agent = self._args

        def emit(chunk: bytes) -> None:
            # Bounded queue = real backpressure; the short spin lets a dead
            # client's stop event break a full queue within ~100ms.
            while not self._stop.is_set():
                try:
                    self._queue.put(chunk, timeout=0.05)
                    return
                except queue.Full:
                    continue

        def work() -> None:
            try:
                stream_media_as_mp3(
                    url, seek_seconds, emit, self._stop, user_agent, self._volume_provider
                )
            finally:
                self._done.set()

        self._thread = threading.Thread(target=work, name="media-proxy", daemon=True)
        self._thread.start()
        return self

    async def read(self) -> bytes:
        while not self._stop.is_set():
            try:
                return self._queue.get_nowait()
            except queue.Empty:
                if self._done.is_set():
                    return b""
                await asyncio.sleep(0.01)
        return b""

    def abort(self) -> None:
        self._stop.set()
