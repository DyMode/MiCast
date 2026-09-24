"""HTTP audio stream server supporting multiple named streams on one port."""

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from uvicorn import Config, Server

from micast.audio_encoder import MediaProxyPump, StreamFormat, mp3_silence, wav_header
from micast.audio_metrics import metrics
from micast.config import settings
from micast.test_tone import test_tone_wav

logger = logging.getLogger(__name__)

# Hard ceiling on a client's delay line. Producer (sender clock) and consumer
# (speaker clock) always drift a little; a speaker that trails accumulates
# backlog without bound until its queue overflows. Capping the lag skips it to
# live once instead of kicking the connection — a kick reconnects and refills
# on a fixed period, turning one drift event into a permanent rhythmic stutter.
CLIENT_MAX_LAG_SECONDS = 4.0
# A Xiaomi pull player abandons an HTTP response that stays silent for ~2s;
# keepalive yields must stay well under that.
CLIENT_KEEPALIVE_SECONDS = 1.0
# A client whose queue stays full this long without the reader taking a single
# chunk is a ghost: the speaker opened a replacement connection and the old
# socket died half-open. Reaping it (instead of dropping chunks forever) stops
# the dropped-chunks counter from growing one per broadcast — and the speaker's
# own retry lands on a fresh queue at the live edge. Xiaomi players read in
# bursts but always drain pending data within a second or two; ten seconds of
# zero reads with a full queue means the reader is gone.
CLIENT_UNDRAINED_SECONDS = 10.0
# First bytes of an encoder run are cached and replayed to late-joining
# clients: WAV/FLAC decoders need the stream header, MP3 just skips it.
STREAM_PREFIX_BYTES = 16384
# Delay-line trust thresholds for formats without a nominal byte rate
# (flac): the EMA must be based on at least this many broadcast samples
# (~2-4s of streaming) and exceed an absolute floor — below ~64 kbps the
# "stream" is silence/underrun, not audio worth throttling. Flac has no
# nominal expectation to take a percentage of, so the floor is absolute;
# a wrong rate here would mis-throttle healthy clients, so stay conservative.
DELAY_LINE_MIN_SAMPLES = 20
DELAY_LINE_MIN_BYTES_PER_SECOND = 8000

_MEDIA_UA = (
    "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)


def _drop_whole_chunks(buffer: deque[bytes], requested: int) -> int:
    """Drop the oldest whole chunks covering ``requested`` bytes.

    Returns the bytes actually dropped. Chunks are encoder write runs, so
    dropping whole ones keeps every scrap of audio the client sees
    frame-aligned — a byte-offset cut leaves a partial frame at the head of
    the stream, which is how a delivered gap turns into lasting distortion on
    players that do not resync.
    """
    dropped = 0
    while buffer and dropped < requested:
        dropped += len(buffer.popleft())
    return dropped




async def _serve_seekable_media(url: str, ss: float, volume_provider=None) -> StreamingResponse:
    """In-process transcode of a remote URL streamed back as MP3. Seeking the
    input container is fast when the origin supports Range requests (music
    CDNs do)."""
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="invalid url")
    pump = MediaProxyPump(url, ss, _MEDIA_UA, volume_provider).start()

    async def generator():
        try:
            while True:
                chunk = await pump.read()
                if not chunk:
                    break
                yield chunk
        finally:
            pump.abort()

    return StreamingResponse(generator(), media_type="audio/mpeg")


class StreamServer:
    """Broadcast multiple audio streams to HTTP clients on /stream/{device_id}."""

    def __init__(self):
        self._streams: dict[str, StreamFormat] = {}
        self._clients: dict[str, set[asyncio.Queue[bytes | None]]] = {}
        self._client_delay: dict[asyncio.Queue, dict[str, int | str | None]] = {}
        self._prefixes: dict[str, bytearray] = {}
        self._app = FastAPI()
        self._server: Server | None = None
        self._task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self.total_bytes_sent: dict[str, int] = {}
        self.dropped_chunks: dict[str, int] = {}
        self._last_broadcast: dict[str, float] = {}
        self._calibration_sessions: dict[str, dict] = {}
        self._diagnostic_media: dict[str, tuple[Path, str]] = {}
        self._diagnostic_hits: dict[str, int] = {}
        # Byte-accurate drop accounting: chunk counts are NOT comparable
        # across formats (a wav chunk carries ~100x the audio of an mp3
        # frame), so drops are also tracked in bytes and converted to an
        # estimated duration via the stream's byte rate.
        self._buffer_overrides: dict[str, float] = {}
        self.dropped_bytes: dict[str, int] = {}
        # Observed output byte rate per stream (EMA), used to express drops in
        # milliseconds for formats whose StreamFormat has no nominal byte_rate
        # (flac). Nominal byte_rate wins when present.
        self._observed_byte_rate: dict[str, float] = {}
        # Broadcast-sample count feeding the EMA; the flac delay line only
        # trusts the EMA after DELAY_LINE_MIN_SAMPLES samples.
        self._rate_samples: dict[str, int] = {}
        # One-shot rendezvous used when a grouped speaker disconnects while
        # its AirPlay session is still live. New HTTP clients wait here until
        # every group member has arrived, then start on the same future chunk.
        self._group_recoveries: dict[str, dict[str, Any]] = {}
        self.on_client_disconnected: Callable[[str, str], Awaitable[None]] | None = None
        self.media_volume = None
        self._setup_routes()

    def _setup_routes(self) -> None:
        @self._app.get("/stream/{device_id}/for/{receiver_id}/{sink}")
        async def stream_for_sink(request: Request, device_id: str, receiver_id: str, sink: str):
            # Xiaomi players may discard a URL's query string before pulling
            # it. Keep routing identity in the path so per-speaker delay still
            # survives the cloud/player hand-off.
            return await self._serve_stream(request, device_id, receiver_id, sink)

        @self._app.get("/stream/{device_id}")
        async def stream(request: Request, device_id: str):
            return await self._serve_stream(request, device_id)

        @self._app.get("/stream/{device_id}.{ext}")
        async def stream_ext(request: Request, device_id: str, ext: str):
            return await self._serve_stream(request, device_id)

        @self._app.get("/calibration/{token}/{sink}.wav")
        async def calibration_tone(token: str, sink: str):
            """Finite local tone on the speaker-facing, known-reachable port.

            Identity lives in the path because Xiaomi players may remove the
            query string before fetching a cloud-issued URL.
            """
            session = self._calibration_sessions.get(token)
            if session is None or sink not in session["expected"]:
                raise HTTPException(status_code=404, detail="Calibration ended")
            if session is not None and sink in session["expected"]:
                session["arrivals"].setdefault(sink, time.monotonic())
                if session["expected"].issubset(session["arrivals"]):
                    session["start_at"] = time.monotonic() + 0.3
                    session["ready"].set()
                logger.info(
                    "Calibration %s connected: %s (%d/%d)",
                    token,
                    sink,
                    len(session["arrivals"]),
                    len(session["expected"]),
                )

            if not session.get("group_id"):
                pcm = session.get("pcm")
                data = (
                    test_tone_wav() if pcm is None else wav_header(44100, data_bytes=len(pcm)) + pcm
                )
                return Response(
                    data,
                    media_type="audio/wav",
                    headers={"Cache-Control": "no-store"},
                )

            async def tone_stream():
                duration_seconds = 600
                data_bytes = 44100 * 4 * duration_seconds
                yield wav_header(44100, data_bytes=data_bytes)
                pcm = session.get("pcm") or test_tone_wav()[44:]
                # Xiaomi speakers consume HTTP audio in comparatively large
                # bursts. 20 ms writes left no scheduling margin on Windows and
                # produced audible starvation, so keep a modest 200 ms cadence.
                chunk_seconds = 0.2
                chunk_bytes = int(44100 * 4 * chunk_seconds)
                position = 0
                applied_hold = 0
                primed = False
                sent = 0
                while not session["stopped"].is_set() and sent < data_bytes:
                    # Send valid audio immediately. Some players do not open a
                    # second URL while the first response is waiting for data,
                    # so an all-clients barrier here can deadlock calibration.
                    start_at = session.get("start_at")
                    if start_at is None or time.monotonic() < start_at:
                        chunk = bytes(min(chunk_bytes, data_bytes - sent))
                        sent += len(chunk)
                        yield chunk
                        await asyncio.sleep(chunk_seconds)
                        continue
                    if not primed:
                        # One second of queued material absorbs normal event-loop
                        # and Wi-Fi jitter without building an ever-growing lag.
                        primed = True
                        for _ in range(5):
                            if sent >= data_bytes:
                                break
                            end = position + chunk_bytes
                            chunk = pcm[position:end]
                            if len(chunk) < chunk_bytes:
                                chunk += pcm[: chunk_bytes - len(chunk)]
                            position = end % len(pcm)
                            chunk = chunk[: data_bytes - sent]
                            sent += len(chunk)
                            yield chunk
                    group = next(
                        (item for item in settings.groups if item.id == session.get("group_id")),
                        None,
                    )
                    hold = group.delay_holds().get(sink, 0) if group else 0
                    if hold > applied_hold:
                        await asyncio.sleep((hold - applied_hold) / 1000)
                    elif hold < applied_hold:
                        position = (position + int((applied_hold - hold) * 176.4)) % len(pcm)
                    applied_hold = hold
                    end = position + chunk_bytes
                    chunk = pcm[position:end]
                    if len(chunk) < chunk_bytes:
                        chunk += pcm[: chunk_bytes - len(chunk)]
                    position = end % len(pcm)
                    chunk = chunk[: data_bytes - sent]
                    sent += len(chunk)
                    yield chunk
                    await asyncio.sleep(chunk_seconds)

            return StreamingResponse(
                tone_stream(),
                media_type="audio/wav",
                headers={
                    "Cache-Control": "no-store",
                    "Content-Length": str(44 + 44100 * 4 * 600),
                },
            )

        @self._app.get("/dlna-media")
        async def dlna_media(url: str, ss: float = 0.0, receiver: str = "", session: str = ""):
            """Transcode proxy for DLNA casting: lets speakers play remote media
            from an arbitrary position (DLNA Seek) — the input container is
            seeked directly and re-encoded to MP3. `ss` is seconds."""
            provider = (
                (lambda: self.media_volume(receiver, session))
                if receiver and self.media_volume
                else None
            )
            return await _serve_seekable_media(url, max(0.0, ss), provider)

        @self._app.get("/diagnostic/builtin.wav")
        async def diagnostic_builtin():
            """Finite built-in test tone on the universal speaker-facing port."""
            return Response(
                test_tone_wav(),
                media_type="audio/wav",
                headers={"Cache-Control": "no-store"},
            )

        @self._app.get("/diagnostic/media/{token}")
        async def diagnostic_upload(token: str):
            media = self._diagnostic_media.get(token)
            if media is None or not media[0].is_file():
                raise HTTPException(status_code=404, detail="Diagnostic media expired")
            self._diagnostic_hits[token] = self._diagnostic_hits.get(token, 0) + 1
            return FileResponse(
                media[0],
                media_type=media[1],
                headers={"Cache-Control": "no-store"},
            )

    def register_stream(self, device_id: str, stream_format: StreamFormat) -> None:
        """Register a new stream endpoint."""
        self._streams[device_id] = stream_format
        self._clients.setdefault(device_id, set())
        self._prefixes[device_id] = bytearray()
        self.total_bytes_sent.setdefault(device_id, 0)
        self.dropped_chunks.setdefault(device_id, 0)
        self.dropped_bytes.setdefault(device_id, 0)
        # A re-registration (format change) invalidates the previous run's
        # observed rate; the EMA must rebuild before the flac delay line
        # may use it.
        self._observed_byte_rate.pop(device_id, None)
        self._rate_samples[device_id] = 0
        logger.info("Registered stream /stream/%s (%s)", device_id, stream_format.content_type)

    def register_diagnostic_media(self, token: str, path: Path, media_type: str) -> None:
        """Expose uploaded diagnostic audio on the same port as live streams."""
        self._diagnostic_media[token] = (path, media_type)
        self._diagnostic_hits[token] = 0

    def diagnostic_hits(self, token: str) -> int:
        return self._diagnostic_hits.get(token, 0)

    def unregister_diagnostic_media(self, token: str) -> None:
        self._diagnostic_media.pop(token, None)
        self._diagnostic_hits.pop(token, None)

    def unregister_stream(self, device_id: str) -> None:
        """Remove a stream endpoint and disconnect clients."""
        self._broadcast_to(device_id, None)
        self._streams.pop(device_id, None)
        self._clients.pop(device_id, None)
        self._prefixes.pop(device_id, None)
        self.total_bytes_sent.pop(device_id, None)
        self.dropped_chunks.pop(device_id, None)
        self.dropped_bytes.pop(device_id, None)
        self._observed_byte_rate.pop(device_id, None)
        self._rate_samples.pop(device_id, None)

    def stream_ids(self) -> list[str]:
        return list(self._streams.keys())

    def client_delay_states(self, device_id: str) -> list[dict]:
        """Per-client delay-line state for one stream (consumer health)."""
        return [
            self._client_delay.get(queue, {})
            for queue in self._clients.get(device_id, set())
        ]

    def set_buffer_override(self, device_id: str, seconds: float | None) -> None:
        """Per-stream delay-line reserve override (see the audio supervisor).

        A bursty upstream is the one stall cause no recovery action can fix;
        extra slack in this stream's buffer is the remedy, and scoping it per
        stream keeps every other speaker at its configured latency.
        """
        if seconds is None:
            self._buffer_overrides.pop(device_id, None)
        else:
            self._buffer_overrides[device_id] = float(seconds)

    def client_count(self, device_id: str) -> int:
        return len(self._clients.get(device_id, set()))

    def _max_lag_seconds(self) -> float:
        """How far a client may lead before we trim it to live.

        Configurable because it doubles as the tolerance for speaker clock
        drift: a larger value absorbs more drift but grows latency.
        """
        try:
            return float(settings.client_max_lag_seconds)
        except AttributeError:  # settings fixture without the field
            return CLIENT_MAX_LAG_SECONDS

    def total_clients(self) -> int:
        return sum(len(clients) for clients in self._clients.values())

    def total_flowing_clients(self) -> int:
        """HTTP consumers currently receiving fresh audio, excluding sockets
        left open by paused or stopped speakers."""
        return sum(
            len(clients)
            for stream_id, clients in self._clients.items()
            if self.is_flowing(stream_id)
        )

    def sink_connected(self, receiver_id: str, sink: str) -> bool:
        """Whether a current HTTP client already serves this grouped sink."""
        return any(
            state.get("receiver") == receiver_id and state.get("sink") == sink
            for queue, state in self._client_delay.items()
            if any(queue in clients for clients in self._clients.values())
        )

    def total_bytes(self) -> int:
        return sum(self.total_bytes_sent.values())

    def latency_metrics(self, device_id: str) -> dict[str, int | None]:
        """Estimate buffering between the encoder output and HTTP clients."""
        stream_format = self._streams.get(device_id)
        clients = self._clients.get(device_id, set())
        byte_rate = stream_format.byte_rate if stream_format else None
        queued_bytes = 0
        for queue in clients:
            queued_bytes = max(
                queued_bytes,
                sum(len(chunk) for chunk in queue._queue if isinstance(chunk, bytes)),
            )
        send_queue_ms = round(queued_bytes / byte_rate * 1000) if byte_rate else 0
        delay_states = [self._client_delay.get(queue, {}) for queue in clients]
        target_delay_ms = max(
            (int(item.get("target_ms") or 0) for item in delay_states),
            default=0,
        )
        retained_buffer_ms = max(
            (int(item.get("buffer_ms") or 0) for item in delay_states),
            default=0,
        )
        stream_buffer_ms = round(settings.stream_buffer_seconds * 1000)
        encoding_ms = _encoding_frame_ms()
        return {
            "encoding_ms": encoding_ms,
            "stream_buffer_ms": stream_buffer_ms,
            "send_queue_ms": send_queue_ms,
            "target_delay_ms": target_delay_ms,
            "retained_buffer_ms": retained_buffer_ms,
            "estimated_ms": encoding_ms + stream_buffer_ms + send_queue_ms,
        }

    async def _serve_stream(
        self,
        request: Request,
        device_id: str,
        receiver_id: str | None = None,
        sink: str | None = None,
    ) -> StreamingResponse:
        stream_format = self._streams.get(device_id)
        if stream_format is None:
            raise HTTPException(status_code=404, detail="Stream not found")

        sink = sink or request.query_params.get("sink") or None
        receiver_id = receiver_id or request.query_params.get("receiver") or None
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)
        metrics.note_client_connect(device_id, replacing=bool(self._clients.get(device_id)))
        self._clients.setdefault(device_id, set()).add(queue)
        self._client_delay[queue] = {
            "receiver": receiver_id,
            "sink": sink,
            "manual_ms": 0,
            "startup_ms": 0,
            "target_ms": 0,
            "buffer_ms": 0,
            "ready_at": None,
            "calibrated": False,
            "skip_ms": 0,
            "intentional_close": False,
            # Last time the reader took a chunk from the queue. A full queue
            # with a stale timestamp is a dead (half-open) connection; see
            # CLIENT_UNDRAINED_SECONDS in _broadcast_to.
            "last_get_at": time.monotonic(),
            # Drift counters: how often this client was skipped to live
            # (lag_drops/queue_drops) or needed injected silence (underruns).
            "lag_drops": 0,
            "queue_drops": 0,
            "silence_fills": 0,
        }
        recovery = self._group_recoveries.get(receiver_id) if receiver_id else None
        recovery_event: asyncio.Event | None = None
        if recovery is not None and sink in recovery["expected"]:
            recovery["clients"][sink] = queue
            recovery_event = recovery["event"]
            if recovery["expected"].issubset(recovery["clients"]):
                # Drop everything accumulated while waiting. All clients will
                # consume only chunks broadcast after this common boundary.
                for waiting in recovery["clients"].values():
                    while not waiting.empty():
                        with contextlib.suppress(asyncio.QueueEmpty):
                            waiting.get_nowait()
                recovery_event.set()
                self._group_recoveries.pop(receiver_id, None)
                logger.info("Group stream recovery ready for %s", receiver_id)
        logger.info(
            "Stream client connected to /stream/%s (receiver=%s, sink=%s): %s",
            device_id,
            receiver_id,
            sink,
            request.client,
        )

        async def generator():
            if recovery_event is not None:
                try:
                    await asyncio.wait_for(recovery_event.wait(), timeout=8.0)
                except TimeoutError:
                    logger.warning("Group stream recovery timed out for %s", receiver_id)
                    self.abort_group_recovery(receiver_id)
            stream_format = self._streams.get(device_id)
            # MP3 is self-synchronising. Replaying the encoder's first 16 KiB
            # on every reconnect injects old programme audio before the live
            # edge and permanently offsets independently reconnecting members.
            prefix = (
                None
                if stream_format and stream_format.content_type == "audio/mpeg"
                else self._prefixes.get(device_id)
            )
            if prefix:
                yield bytes(prefix)
            # The delay line holds WHOLE broadcast chunks, never byte slices:
            # each chunk is a run of complete encoder mux writes, so keeping
            # chunk boundaries means every client always sees frame-aligned
            # data. Slicing at an arbitrary byte offset (the old behaviour)
            # hands a FLAC/WAV decoder a stream cut mid-frame — cheap pull
            # players can then latch onto noise instead of resyncing, which is
            # how a dropout turns into permanent distortion.
            buffer: deque[bytes] = deque()
            held = 0
            # One frame of encoded silence (~20ms) for true-underrun keepalive.
            # seconds=0 still produces one frame plus the encoder flush.
            silence = (
                mp3_silence(settings.audio.sample_rate, settings.audio.bitrate, 0)
                if stream_format and stream_format.content_type == "audio/mpeg"
                else b""
            )
            # Delay-line byte rate: nominal when the format has one (mp3/wav/
            # pcm); for flac the broadcast-observed EMA once it is trustworthy.
            # Until then the client stays on the transparent passthrough.
            byte_rate = self._delay_line_byte_rate(device_id)
            buffer_seconds = self._buffer_overrides.get(
                device_id, settings.stream_buffer_seconds
            )
            initial_buffer = int(byte_rate * buffer_seconds) if byte_rate else 0
            # Delay alignment: extra bytes held back per client so this speaker
            # trails its siblings. Re-read live each chunk — a smaller value
            # drops the staged excess (pull earlier), a larger one stages more.
            hold_bytes = 0
            hold_ms = 0
            last_yield_at = time.monotonic()
            try:
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        break
                    self._note_client_read(queue)
                    if not byte_rate:
                        yield chunk
                        continue

                    buffer.append(chunk)
                    held += len(chunk)

                    manual_ms = (
                        settings.sink_hold_ms(receiver_id, sink) if receiver_id and sink else 0
                    )
                    state = self._client_delay.get(queue)
                    if state is not None:
                        state["manual_ms"] = manual_ms
                    startup_ms = int(state.get("startup_ms") or 0) if state else 0
                    hold_ms = max(0, manual_ms + startup_ms)
                    new_hold = int(byte_rate * hold_ms / 1000) if byte_rate else 0
                    skip_ms = int(state.get("skip_ms") or 0) if state else 0
                    if skip_ms:
                        skip_bytes = int(byte_rate * skip_ms / 1000)
                        if skip_bytes:
                            dropped = _drop_whole_chunks(buffer, skip_bytes)
                            held -= dropped
                        state["skip_ms"] = 0
                    if new_hold != hold_bytes:
                        if new_hold < hold_bytes:
                            # Pull earlier: discard the staged excess from the
                            # head (oldest content) so the client skips to live.
                            dropped = _drop_whole_chunks(buffer, hold_bytes - new_hold)
                            held -= dropped
                        elif state is not None:
                            # Hold grew mid-stream: the reserve must be re-filled
                            # byte-by-byte from here on. Mark it so the keepalive
                            # branch below feeds silence instead of draining the
                            # real audio that is supposed to accumulate.
                            state["needs_fill"] = True
                        hold_bytes = new_hold

                    # The encoder already produces data in real time. Treat
                    # this buffer as a delay line and release only the bytes
                    # above its permanent reserve. A second wall-clock pacer
                    # based on nominal MP3 bitrate slowly accumulated several
                    # seconds of queue and eventually caused periodic drops.
                    reserve = initial_buffer + hold_bytes

                    # Drift ceiling: a speaker whose clock trails the encoder
                    # would backlog without bound (eventually overflowing its
                    # queue). Skip the excess to live instead — one small skip
                    # beats a permanent disconnect/reconnect rhythm. The excess
                    # is only ever the growth since the previous chunk (one
                    # chunk at a time), so this stays a small trim; the metrics
                    # event log records how large it actually was.
                    ceiling = reserve + int(byte_rate * self._max_lag_seconds())
                    if held > ceiling:
                        # Never trim below the reserve: dropping the last chunk
                        # would starve the client completely.
                        skipped = 0
                        while buffer and held > ceiling and held - len(buffer[0]) >= reserve:
                            head = buffer.popleft()
                            held -= len(head)
                            skipped += len(head)
                        if skipped:
                            metrics.note_lag_skip(
                            skipped, skipped / byte_rate * 1000, device_id
                        )
                        if skipped and state is not None:
                            state["lag_drops"] = int(state.get("lag_drops") or 0) + 1
                            if state["lag_drops"] == 1 or state["lag_drops"] % 20 == 0:
                                logger.info(
                                    "Client %s on /stream/%s lagged %.1fs behind; "
                                    "skipped %dms to live (%d times)",
                                    request.client,
                                    device_id,
                                    (skipped + ceiling - reserve) / byte_rate,
                                    skipped / byte_rate * 1000,
                                    state["lag_drops"],
                                )

                    if held <= reserve:
                        now = time.monotonic()
                        if state is not None and state.get("ready_at") is None and silence:
                            # Startup fill: the speaker just connected and the
                            # delay line has never reached its reserve. Feed a
                            # frame of silence per incoming chunk so the player
                            # doesn't abandon the response while it fills.
                            yield bytes(silence)
                            last_yield_at = now
                        elif state is not None and state.get("needs_fill") and silence:
                            # Delay increase in progress: hold the real bytes so
                            # the reserve grows, and keep the player alive with
                            # frame-aligned silence — same trick as startup fill.
                            # Draining real audio here would keep the speaker
                            # live and the configured delay would never apply.
                            yield bytes(silence)
                            last_yield_at = now
                        elif buffer and now - last_yield_at >= CLIENT_KEEPALIVE_SECONDS:
                            # Keepalive with real audio: releasing below the
                            # reserve beats injecting a dropout. The delay line
                            # refills by itself once flow normalises.
                            while buffer:
                                held -= len(buffer.popleft())
                            last_yield_at = now
                            if state is not None and state.get("ready_at") is None:
                                # Real bytes flowed: this connection is out of
                                # its startup fill phase for good.
                                state["ready_at"] = now
                        elif not buffer and silence:
                            # True underrun: a single frame-aligned slice of
                            # silence keeps the player from abandoning the
                            # response until real bytes arrive. Never loop
                            # mid-frame — byte-offset slices decode as clicks.
                            yield bytes(silence)
                            last_yield_at = now
                            if state is not None:
                                state["silence_fills"] = int(state.get("silence_fills") or 0) + 1
                                if state["silence_fills"] in (1, 20, 100):
                                    logger.info(
                                        "Client %s on /stream/%s underran; injected "
                                        "silence (%d times)",
                                        request.client,
                                        device_id,
                                        state["silence_fills"],
                                    )
                    if state is not None:
                        state["target_ms"] = hold_ms
                        state["buffer_ms"] = round(held / byte_rate * 1000)
                        if held >= reserve:
                            state["needs_fill"] = None
                        if state.get("ready_at") is None and held >= reserve:
                            state["ready_at"] = time.monotonic()
                        metrics.note_queue_depth(
                            queue.qsize(), float(state["buffer_ms"])
                        )
                    # Release whole chunks, never byte slices: chunk boundaries
                    # are encoder write boundaries, so the client's decoder
                    # always sees complete frames.
                    while buffer and held - len(buffer[0]) >= reserve:
                        head = buffer.popleft()
                        held -= len(head)
                        yield head
                        last_yield_at = time.monotonic()
                    if state is not None:
                        state["buffer_ms"] = round(held / byte_rate * 1000)

                while buffer:
                    yield buffer.popleft()
            finally:
                self._clients.get(device_id, set()).discard(queue)
                state = self._client_delay.pop(queue, None) or {}
                waiting = self._group_recoveries.get(receiver_id) if receiver_id else None
                if waiting is not None and sink and waiting["clients"].get(sink) is queue:
                    waiting["clients"].pop(sink, None)
                logger.info(
                    "Stream client disconnected from /stream/%s: %s",
                    device_id,
                    request.client,
                )
                if (
                    receiver_id
                    and sink
                    and not state.get("intentional_close")
                    and self.on_client_disconnected is not None
                ):
                    await self.on_client_disconnected(receiver_id, sink)

        return StreamingResponse(
            generator(),
            media_type=stream_format.content_type,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    def clear_startup_sync(self, receiver_id: str) -> None:
        """Remove the retired hidden startup correction from live clients.

        Delay is now represented by the group's single persisted value.  The
        old readiness comparison mixed independent encoder/HTTP start times
        and could manufacture multi-second corrections, so it must never be
        applied automatically during ordinary playback.
        """
        for state in self._client_delay.values():
            if state.get("receiver") != receiver_id:
                continue
            state["startup_ms"] = 0
            state["skip_ms"] = 0
            state["calibrated"] = True

    def begin_delay_calibration(
        self,
        token: str,
        sinks: list[str],
        group_id: str | None = None,
        pcm: bytes | None = None,
    ) -> dict:
        session = {
            "expected": set(sinks),
            "arrivals": {},
            "ready": asyncio.Event(),
            "stopped": asyncio.Event(),
            "group_id": group_id,
            "start_at": None,
            "pcm": pcm,
        }
        self._calibration_sessions[token] = session
        return session

    def end_delay_calibration(self, token: str) -> None:
        session = self._calibration_sessions.pop(token, None)
        if session is not None:
            session["stopped"].set()

    def sink_latency_metrics(self) -> dict[str, dict[str, dict[str, int]]]:
        """Current per-speaker delay values for status/UI surfaces."""
        result: dict[str, dict[str, dict[str, int]]] = {}
        for state in self._client_delay.values():
            receiver = str(state.get("receiver") or "")
            sink = str(state.get("sink") or "")
            if not receiver or not sink:
                continue
            result.setdefault(receiver, {})[sink] = {
                "manual_ms": int(state.get("manual_ms") or 0),
                "startup_ms": int(state.get("startup_ms") or 0),
                "effective_ms": int(state.get("target_ms") or 0),
                "buffer_ms": int(state.get("buffer_ms") or 0),
                "silence_fills": int(state.get("silence_fills") or 0),
                "lag_drops": int(state.get("lag_drops") or 0),
                "queue_drops": int(state.get("queue_drops") or 0),
            }
        return result

    async def start(self) -> None:
        # Serialise concurrent starters: two interleaved start() calls would
        # each create a uvicorn Server, and _serve() reads self._server — the
        # orphaned first task would end up driving (or killing) the second
        # server while its own socket leaks.
        async with self._start_lock:
            if (
                self._task
                and not self._task.done()
                and self._server
                and self._server.started
            ):
                return
            config = Config(
                self._app, host=settings.host, port=settings.stream_port, log_level="warning"
            )
            server = Server(config)
            self._server = server
            self._task = asyncio.create_task(self._serve(server))
            deadline = asyncio.get_running_loop().time() + 7.0
            while True:
                if self._task.done():
                    # Retries inside _serve may have swapped in a new Server;
                    # report the original failure and leave no stale state
                    # behind for the next start()/stop() call.
                    error = None if self._task.cancelled() else self._task.exception()
                    self._task = None
                    self._server = None
                    if error is not None:
                        raise RuntimeError("音频流服务启动失败") from error
                    raise RuntimeError("音频流服务启动失败")
                # Read self._server (not the local variable): a bind-retry
                # inside _serve replaces it, and only the CURRENT server's
                # `started` flag proves the port is actually serving.
                current = self._server
                if current is not None and current.started:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    if current is not None:
                        current.should_exit = True
                    await asyncio.gather(self._task, return_exceptions=True)
                    self._task = None
                    self._server = None
                    raise RuntimeError("音频流服务启动超时")
                await asyncio.sleep(0.05)
        logger.info(
            "Stream server started on http://%s:%s/stream/{device_id}",
            settings.host,
            settings.stream_port,
        )

    async def _serve(self, server: Server) -> None:
        """Run uvicorn, containing bind failures instead of killing the process.

        uvicorn calls sys.exit() when startup fails (e.g. port still held by a
        previous instance mid-restart); as a BaseException it would tear down
        the whole event loop. Retry briefly, then give up gracefully. The
        server under serve is threaded through explicitly: retries replace
        ``self._server`` at a single assignment point so start()'s readiness
        check and stop()'s shutdown signal always target the live instance.
        """
        for attempt in range(5):
            try:
                await server.serve()
                return
            except SystemExit:
                logger.error(
                    "Stream server failed to bind port %s (attempt %s/5)",
                    settings.stream_port,
                    attempt + 1,
                )
                if server.should_exit or attempt == 4:
                    return
                await asyncio.sleep(1)
                server = Server(server.config)
                self._server = server

    async def stop(self) -> None:
        for device_id in list(self._streams.keys()):
            self.unregister_stream(device_id)
        if self._server:
            self._server.should_exit = True
            if self._task and not self._task.done():
                try:
                    await asyncio.wait_for(self._task, timeout=5.0)
                except TimeoutError:
                    logger.error("Stream server did not stop within 5s; cancelling task")
                    self._task.cancel()
                    await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self._server = None

    def kick_clients(self, device_id: str, sink: str | None = None) -> int:
        """Close client connections of a stream; returns how many were kicked.

        ``sink`` narrows it to the clients pulled for one speaker, which is how
        a retargeted AirPlay 2 instance drops the speaker it just left behind.
        """
        clients = self._clients.get(device_id, set())
        targets = [
            queue
            for queue in clients
            if sink is None or (self._client_delay.get(queue, {}) or {}).get("sink") == sink
        ]
        count = len(targets)
        if count:
            logger.info(
                "Kicking %d client(s) from /stream/%s%s",
                count,
                device_id,
                f" (sink={sink})" if sink else "",
            )
            for queue in targets:
                state = self._client_delay.get(queue)
                if state is not None:
                    state["intentional_close"] = True
            self._broadcast_to(device_id, None, only=set(targets))
        return count

    def begin_group_recovery(self, receiver_id: str, sinks: list[str]) -> None:
        """Hold replacement clients until every grouped sink has connected."""
        previous = self._group_recoveries.pop(receiver_id, None)
        if previous is not None:
            previous["event"].set()
        self._group_recoveries[receiver_id] = {
            "expected": set(sinks),
            "clients": {},
            "event": asyncio.Event(),
        }
        logger.info("Group stream recovery waiting for %s: %s", receiver_id, sinks)

    def abort_group_recovery(self, receiver_id: str) -> None:
        recovery = self._group_recoveries.pop(receiver_id, None)
        if recovery is not None:
            recovery["event"].set()

    async def broadcast(self, device_id: str, chunk: bytes | None) -> None:
        """Send a chunk to all connected clients for a stream."""
        if chunk:
            now = time.monotonic()
            last = self._last_broadcast.get(device_id, 0.0)
            self.total_bytes_sent[device_id] = self.total_bytes_sent.get(device_id, 0) + len(chunk)
            self._last_broadcast[device_id] = now
            dt = now - last
            if dt > 0:
                instantaneous = len(chunk) / dt
                ema = self._observed_byte_rate.get(device_id)
                self._observed_byte_rate[device_id] = (
                    instantaneous if ema is None else ema * 0.9 + instantaneous * 0.1
                )
                self._rate_samples[device_id] = self._rate_samples.get(device_id, 0) + 1
            prefix = self._prefixes.get(device_id)
            if prefix is not None and len(prefix) < STREAM_PREFIX_BYTES:
                prefix.extend(chunk[: STREAM_PREFIX_BYTES - len(prefix)])
        self._broadcast_to(device_id, chunk)

    def _stream_byte_rate(self, device_id: str) -> float | None:
        """Nominal rate when known (mp3/wav/pcm), observed EMA otherwise (flac)."""
        stream_format = self._streams.get(device_id)
        if stream_format and stream_format.byte_rate:
            return float(stream_format.byte_rate)
        return self._observed_byte_rate.get(device_id)

    def _delay_line_byte_rate(self, device_id: str) -> int | None:
        """Byte rate the per-client delay line may use for this stream.

        Formats with a nominal byte rate always qualify. FLAC has none: only
        a well-sampled EMA above the absolute floor qualifies, so volatile
        warm-up periods and silence keep the transparent passthrough.
        """
        stream_format = self._streams.get(device_id)
        if stream_format and stream_format.byte_rate:
            return int(stream_format.byte_rate)
        if self._rate_samples.get(device_id, 0) < DELAY_LINE_MIN_SAMPLES:
            return None
        rate = self._observed_byte_rate.get(device_id)
        if rate is None or rate < DELAY_LINE_MIN_BYTES_PER_SECOND:
            return None
        return int(rate)

    def drop_metrics(self, device_id: str) -> dict[str, int]:
        """Cross-format-comparable drop accounting for one stream.

        ``chunks`` is the raw counter (format-dependent granularity);
        ``bytes`` is exact; ``estimated_ms`` converts bytes via the stream's
        byte rate so flac and mp3 drops are comparable on one scale.
        """
        chunks = self.dropped_chunks.get(device_id, 0)
        dropped_bytes = self.dropped_bytes.get(device_id, 0)
        rate = self._stream_byte_rate(device_id)
        return {
            "chunks": chunks,
            "bytes": dropped_bytes,
            "estimated_ms": round(dropped_bytes / rate * 1000) if rate and dropped_bytes else 0,
        }

    def is_flowing(self, device_id: str, window: float = 3.0) -> bool:
        """True only while bytes actually move: a paused speaker can hold the
        HTTP connection open indefinitely, so client count alone lies."""
        if not self._clients.get(device_id):
            return False
        last = self._last_broadcast.get(device_id, 0.0)
        return (time.monotonic() - last) < window

    def _note_client_read(self, queue: asyncio.Queue) -> None:
        """Record that the HTTP reader took a chunk (ghost-client detector)."""
        state = self._client_delay.get(queue)
        if state is not None:
            state["last_get_at"] = time.monotonic()

    def _client_is_ghost(self, queue: asyncio.Queue) -> bool:
        """True when the reader has taken nothing for a long while — a
        half-open dead connection (the speaker replaced its pull socket and
        the old one died without a close). Group-recovery waiters are exempt:
        they deliberately hold data unread until every grouped sink has
        connected."""
        state = self._client_delay.get(queue)
        if state is None:
            return False
        receiver_id = state.get("receiver")
        waiting = self._group_recoveries.get(receiver_id) if receiver_id else None
        if (
            waiting is not None
            and not waiting["event"].is_set()
            and state.get("sink") in waiting["expected"]
        ):
            return False
        last_get = state.get("last_get_at") or 0.0
        return (time.monotonic() - float(last_get)) > CLIENT_UNDRAINED_SECONDS

    def reap_ghost_clients(
        self, device_id: str, subset: set[asyncio.Queue] | None = None
    ) -> int:
        """Close dead (half-open) client connections of one stream; returns how
        many were reaped. Mirrors kick_clients' intentional_close semantics so
        group-recovery hooks don't fire for a dead socket. Without this, every
        broadcast to a ghost queue drops one chunk — the dropped-chunks
        counter grows for as long as the session stays active. ``subset``
        limits reaping to those clients, so a targeted kick never touches
        anything outside its scope."""
        clients = self._clients.get(device_id)
        if not clients:
            return 0
        candidates = clients if subset is None else clients & subset
        dead = [queue for queue in candidates if self._client_is_ghost(queue)]
        for queue in dead:
            state = self._client_delay.get(queue)
            if state is not None:
                state["intentional_close"] = True
            clients.discard(queue)
            with contextlib.suppress(Exception):
                queue.put_nowait(None)
        if dead:
            logger.info("Reaped %d ghost client(s) from /stream/%s", len(dead), device_id)
        return len(dead)

    def _broadcast_to(
        self, device_id: str, chunk: bytes | None, only: set[asyncio.Queue] | None = None
    ) -> None:
        clients = self._clients.get(device_id)
        if not clients:
            return
        self.reap_ghost_clients(device_id, only)
        dead: set[asyncio.Queue] = set()
        for queue in clients:
            if only is not None and queue not in only:
                continue
            try:
                queue.put_nowait(chunk)
            except asyncio.QueueFull:
                self.dropped_chunks[device_id] = self.dropped_chunks.get(device_id, 0) + 1
                # Slow consumer: drop its oldest queued chunk so it skips to
                # live. Kicking the connection (the old behaviour) makes the
                # speaker reconnect and refill on a fixed period — one clock
                # drift event becomes a permanent rhythmic stutter.
                try:
                    dropped = queue.get_nowait()
                    if isinstance(dropped, bytes):
                        # Byte-accurate accounting: chunk counts mean different
                        # durations per format; bytes/ms are comparable.
                        self.dropped_bytes[device_id] = (
                            self.dropped_bytes.get(device_id, 0) + len(dropped)
                        )
                    queue.put_nowait(chunk)
                    state = self._client_delay.get(queue)
                    if state is not None:
                        state["queue_drops"] = int(state.get("queue_drops") or 0) + 1
                        metrics.note_queue_drops(
                            1, float(state.get("buffer_ms") or 0)
                        )
                        if state["queue_drops"] in (1, 20, 100):
                            logger.info(
                                "Client queue for /stream/%s overflowed; dropped "
                                "oldest chunk (%d times)",
                                device_id,
                                state["queue_drops"],
                            )
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    dead.add(queue)
        for queue in dead:
            clients.discard(queue)
            with contextlib.suppress(Exception):
                queue.put_nowait(None)


def _encoding_frame_ms() -> int:
    if not settings.audio.auto_transcode or settings.audio.format == "wav":
        return 0
    if settings.audio.format == "mp3":
        return round(1152 / settings.audio.sample_rate * 1000)
    # FLAC block sizes vary; one short block is a useful conservative estimate.
    return 20
