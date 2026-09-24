"""Per-speaker audio pipeline: PCM source → in-process encoder → stream server."""

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable

from micast.audio_encoder import AudioEncoder, firequalizer_available, raw_pcm_format, wav_header
from micast.audio_metrics import metrics
from micast.config import settings
from micast.curve_fit import (
    add_curve,
    equalizer_chain,
    firequalizer_args,
    gain_table,
    loudness_band,
    loudness_curve,
)
from micast.pcm_source import PCMSource
from micast.spectrum import SpectrumAnalyzer, spectrum_wanted
from micast.stream_server import StreamServer

logger = logging.getLogger(__name__)

# A PCM source (TCP socket, receiver stdout) can die silently: the read hangs
# forever, the encoder never exits, and no error is logged anywhere — the only
# symptom is speakers reconnecting to a byteless stream every ~2 seconds. While
# a sender session is live the source must produce continuously; this timeout
# is the signal that it wedged.
SOURCE_STALL_TIMEOUT_SECONDS = 8.0
SOURCE_STALL_CHECK_SECONDS = 2.0
# When the PCM source stops delivering while HTTP clients are connected, the
# pumps synthesize zero-PCM chunks after roughly one chunk period of stall.
# FLAC has no precomputable silence frame (unlike mp3), so the running
# encoder must be fed zero PCM to keep the stream's frame sequence unbroken —
# a starved Xiaomi pull player abandons the response in ~2s. Stall watchdog
# timestamps are NOT advanced by synthesized chunks: a genuinely wedged
# source still trips the restart watchdog above.
SOURCE_SILENCE_CHUNK_BYTES = 32768
# Grace window before synthesizing: a source read gets this many consecutive
# chunk periods (~0.5s at 48k) of zero bytes before the first silence chunk.
# shairport's pipe writes are jittery — one late chunk (a read window without
# bytes) must NOT inject 170ms of digital silence into a healthy stream:
# measured on a 50-300ms-jitter source, the single-timeout trigger synthesized
# 3.5-4.6s of silence per 6s (audible stutter, invisible to every diagnostic
# counter). Still far below the speaker's ~2s abandonment threshold.
SOURCE_SILENCE_GRACE_PERIODS = 3
# A wait longer than this for real source bytes (while a session is live) means
# the sender itself delivered in lumps rather than a steady stream — the
# "accumulate then hiccup" shape listeners report. Chunk cadence is ~170ms, so
# the threshold sits comfortably above normal jitter.
SOURCE_GAP_MS = 150.0
# Encoded output is expected at roughly the audio duration of the previous
# chunk; a longer gap means the encoder thread stalled (nothing lost, just
# delayed) — the one hiccup cause no drop counter can see.
ENCODER_GAP_MS = 150.0
# A source that was idle longer than this starts a fresh pacing baseline, so
# diagnostics never report idle time as input starvation.
INPUT_IDLE_RESET_SECONDS = 1.0


class SpeakerPipeline:
    """Handles one AirPlay receiver's audio pipeline.

    A stereo-pair receiver runs two of these (one per channel); ``group_id`` +
    ``channel`` bind the pipeline to one side of the pair. The audio filter is
    resolved from the live group config: the channel itself decides the pan,
    and the loudness trim follows whichever speaker currently holds that
    channel — so swapping channels needs only an encoder restart. (Delay
    alignment lives downstream in the stream server, keyed per speaker.)
    """

    def __init__(
        self,
        device_id: str,
        alias: str,
        pcm_source: PCMSource,
        stream_server: StreamServer,
        on_session_start: Callable[[str], Awaitable[None]] | None = None,
        input_sample_rate: int | None = None,
        stream_id: str | None = None,
        group_id: str | None = None,
        channel: str | None = None,
        eq_curve: list[tuple[float, float]] | None = None,
        loudness: bool = False,
        pace_source: bool = True,
        session_active: Callable[[], bool] | None = None,
        on_source_stall: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.device_id = device_id
        self.alias = alias
        self._pcm_source = pcm_source
        self._stream_server = stream_server
        self._on_session_start = on_session_start
        self._input_sample_rate = input_sample_rate
        self._stream_id = stream_id or device_id
        self._group_id = group_id
        self._channel = channel
        self._eq_curve = list(eq_curve) if eq_curve else None
        self._loudness = loudness
        self._pace_source = pace_source
        # Ground truth for the stall watchdog: True while a sender session is
        # live on this pipeline's receiver. None disables stall detection.
        self._session_active = session_active
        self._on_source_stall = on_source_stall
        self._stall_task: asyncio.Task | None = None
        self._recovery_task: asyncio.Task | None = None
        self._aux_tasks: set[asyncio.Task] = set()
        self._generation = 0
        self._source_restart_lock = asyncio.Lock()
        self._last_feed_at = 0.0
        # Armed only while a newly-started sender session has not produced its
        # first bytes. Once audio has flowed, later silence may be a pause or a
        # track transition and must not restart the receiver underneath it.
        self._stall_armed = True
        self._encoder: AudioEncoder | None = None
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._status = "idle"
        # AirPlay sender volume is stream gain, independent from the physical
        # speaker volume controlled by the Web UI.
        self._input_volume = 100
        # Equal-loudness state: the listening level drives the compensation
        # curve (in both volume modes it is the sender volume), quantized so a
        # nudge inside a band never rebuilds the encoder.
        self._loudness_level = 100
        self._loudness_band = loudness_band(self._loudness_level)
        self._loudness_restarting = False
        # Live-spectrum tap for the tuning page: raw post-gain PCM in, FFT
        # only when the UI polls — see micast.spectrum.
        self._spectrum = SpectrumAnalyzer(self._input_sample_rate or 44100)
        # Input-side pacing: how far the fed audio leads (positive) or trails
        # (negative) real time. Positive means the source is ahead of the
        # playback clock (input buffer), negative means it starved. Surfaced in
        # diagnostics so AirPlay 2 (which has no RAOP input_buffer_ms) can
        # report an equivalent figure.
        self._input_ahead_ms = 0.0
        self._input_fed_bytes = 0
        # Timestamp of the last real byte delivered by the PCM source, used to
        # measure how long the source leaves us waiting (see SOURCE_GAP_MS).
        self._last_source_read_at = 0.0

    def spectrum_bands(self) -> list[float] | None:
        """Latest spectrum bands (0..1), or None when the pipeline is idle."""
        if not self._running:
            return None
        return self._spectrum.bands()

    def drop_stats(self) -> dict[str, int]:
        """Encoder-stage loss counters (input/output), for end-to-end drop
        diagnostics alongside stream-server dropped_chunks and PCM-tee drops.
        Empty when no encoder is running (raw bypass never drops here)."""
        if self._encoder is None:
            return {"in": 0, "out": 0}
        return self._encoder.drop_stats()

    def input_stats(self) -> dict[str, float]:
        """Input-side pacing for this pipeline.

        ``ahead_ms`` > 0 means the fed audio leads the wall clock (buffered
        input); < 0 means the source trailed real time (starvation). AirPlay 2
        has no RAOP session counters, so this is what stands in for
        ``input_buffer_ms`` there.
        """
        return {
            "ahead_ms": round(self._input_ahead_ms, 1),
            "buffered_ms": round(max(0.0, self._input_ahead_ms), 1),
            "starved_ms": round(max(0.0, -self._input_ahead_ms), 1),
        }

    @property
    def status(self) -> str:
        return self._status

    @property
    def running(self) -> bool:
        """True while the pump/encoder tasks are live. A clean encoder exit
        (source EOF between sessions) leaves this False until the next session."""
        return self._running

    @property
    def stream_url(self) -> str:
        return f"http://{settings.effective_stream_host}:{settings.stream_port}/stream/{self._stream_id}"

    def _build_audio_filter(self) -> list[tuple[str, str]] | None:
        """Audio filter chain: per-speaker EQ curve first, then stereo shaping.

        Returns structured ``(filter_name, args)`` links consumed by
        micast.audio_encoder._build_filter_graph. The drawn EQ curve renders
        as one firequalizer gain table, or a multi-band equalizer chain when
        the bundled libavfilter lacks firequalizer.
        """
        parts: list[tuple[str, str]] = []
        # The effective curve is the drawn EQ plus the equal-loudness shelf; a
        # loudness-only speaker (flat EQ) still gets a non-empty curve so it is
        # encoded rather than bypassed raw.
        curve = self._eq_curve
        if self._loudness:
            shelf = loudness_curve(self._loudness_level)
            if shelf:
                curve = add_curve(curve or [], shelf)
        if curve:
            table = gain_table(curve)
            if firequalizer_available():
                parts.append(("firequalizer", firequalizer_args(table)))
            else:
                for link in equalizer_chain(table):
                    name, _, args = link.partition("=")
                    parts.append((name, args))
        group = next((g for g in settings.groups if g.id == self._group_id), None)
        stereo = group is not None and group.mode == "stereo" and self._channel in ("left", "right")
        if not stereo:
            return parts or None
        # Keep the stream stereo (duplicate the picked channel to both sides):
        # byte-rate pacing and speaker decoders all assume two channels.
        side = "FL" if self._channel == "left" else "FR"
        parts.append(("pan", f"stereo|c0={side}|c1={side}"))
        # Trims are configured per speaker; follow whoever holds this channel.
        holder = self._channel_holder()
        if holder:
            gain = float(group.gains_db.get(holder, 0.0))
            if gain:
                parts.append(("volume", f"{gain}dB"))
        return parts

    def _channel_holder(self) -> str | None:
        """The speaker currently assigned to this pipeline's stereo channel."""
        group = next((g for g in settings.groups if g.id == self._group_id), None)
        if group is None or group.mode != "stereo" or self._channel not in ("left", "right"):
            return None
        return next((did for did, ch in group.channels.items() if ch == self._channel), None)

    def _audio_config(self):
        """Encoding config: filtered streams always go through the encoder, so
        the raw-PCM bypass becomes a WAV encode at the input rate (no resample)."""
        if settings.audio.auto_transcode:
            return settings.audio
        return settings.audio.model_copy(
            update={"format": "wav", "sample_rate": self._input_sample_rate or 44100}
        )

    def set_input_volume(self, percent: int) -> None:
        self._input_volume = max(0, min(100, int(percent)))

    def set_audio_character(
        self,
        *,
        eq_curve: list[tuple[float, float]] | tuple[tuple[float, float], ...] | None,
        loudness: bool,
    ) -> None:
        """Update the filter inputs consumed by the next encoder start.

        Pipelines are intentionally long-lived across ordinary EQ edits. The
        curve therefore has to be refreshed explicitly before restarting the
        encoder; otherwise it keeps rendering the constructor-time curve even
        though the saved stream plan has moved on.
        """
        self._eq_curve = list(eq_curve) if eq_curve else None
        self._loudness = bool(loudness)
        self._loudness_band = loudness_band(self._loudness_level)

    def set_loudness_level(self, percent: int) -> None:
        """Update the listening level driving equal-loudness compensation.

        The curve is quantized into bands, so only a band crossing rebuilds the
        encoder (and even then just the encoder, never the stream/session).
        """
        percent = max(0, min(100, int(percent)))
        self._loudness_level = percent
        if not self._loudness:
            return
        band = loudness_band(percent)
        if band == self._loudness_band or not self._running:
            self._loudness_band = band
            return
        self._loudness_band = band
        if not self._loudness_restarting:
            self._spawn_aux(self._rebuild_loudness(), "loudness-rebuild")

    def _spawn_aux(self, coroutine, label: str) -> asyncio.Task:
        """Own a short-lived helper task and consume its failures."""
        task = asyncio.create_task(coroutine, name=f"pipeline:{self._stream_id}:{label}")
        self._aux_tasks.add(task)

        def done(finished: asyncio.Task) -> None:
            self._aux_tasks.discard(finished)
            if finished.cancelled():
                return
            error = finished.exception()
            if error:
                logger.error(
                    "Pipeline helper %s failed",
                    finished.get_name(),
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(done)
        return task

    async def _rebuild_loudness(self) -> None:
        """Rebuild only the encoder after a loudness band crossing."""
        if self._loudness_restarting:
            return
        self._loudness_restarting = True
        try:
            await self.restart_encoder()
        finally:
            self._loudness_restarting = False

    def _apply_input_gain(self, chunk: bytes) -> bytes:
        """Apply the sender volume to signed 16-bit little-endian PCM."""
        from micast.volume import apply_pcm_gain

        return apply_pcm_gain(chunk, self._input_volume)

    @property
    def source_silence_seconds(self) -> float | None:
        """Seconds since the PCM source last produced bytes (None when idle)."""
        if not self._running or not self._last_feed_at:
            return None
        return time.monotonic() - self._last_feed_at

    def register_stream(self) -> None:
        stream_format = (
            AudioEncoder(self._audio_config(), self._input_sample_rate).format
            if settings.audio.auto_transcode or self._build_audio_filter()
            else raw_pcm_format(self._input_sample_rate or 44100)
        )
        self._stream_server.register_stream(self._stream_id, stream_format)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._generation += 1
        self._status = "running"
        self._last_feed_at = time.monotonic()
        self._stall_armed = True
        logger.info("Starting pipeline for %s (%s)", self._stream_id, self.alias)

        self.register_stream()

        try:
            source_reader = await self._pcm_source.start()
            await self._start_encoder(source_reader)
        except Exception:
            logger.exception("Failed to start pipeline for %s", self._stream_id)
            self._status = "error"
            raise
        if self._session_active is not None and self._stall_task is None:
            self._stall_task = asyncio.create_task(self._watch_source_stall())

    async def _start_encoder(self, source_reader: asyncio.StreamReader) -> None:
        audio_filter = self._build_audio_filter()
        if settings.audio.auto_transcode or audio_filter:
            self._encoder = AudioEncoder(
                self._audio_config(), self._input_sample_rate, audio_filter=audio_filter
            )
            await self._encoder.start()
            self._tasks = [
                asyncio.create_task(
                    self._pump_source_to_encoder(source_reader, self._encoder.stdin)
                ),
                asyncio.create_task(self._pump_encoder_to_stream(self._encoder.stdout)),
                asyncio.create_task(self._watch_encoder()),
            ]
        else:
            self._encoder = None
            self._tasks = [
                asyncio.create_task(self._pump_source_to_stream(source_reader)),
            ]

    async def restart_encoder(self) -> None:
        """Rebuild only the encoder stage with current audio settings.

        The PCM source (AirPlay session) and the speaker's stream endpoint stay
        up, so a format change costs a sub-second encoder gap instead of a full
        engine restart that drops every connection.
        """
        if not self._running:
            return
        logger.info("Restarting encoder for %s (%s)", self._stream_id, self.alias)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._encoder:
            await self._encoder.stop()
            self._encoder = None

        self.register_stream()
        source_reader = await self._pcm_source.start()
        await self._start_encoder(source_reader)
        self._last_feed_at = time.monotonic()

    async def stop(self, keep_stream: bool = False) -> None:
        if not self._running:
            return
        self._running = False
        self._generation += 1
        self._status = "stopping"
        logger.info("Stopping pipeline for %s", self._stream_id)

        if self._stall_task and not self._stall_task.done():
            self._stall_task.cancel()
            await asyncio.gather(self._stall_task, return_exceptions=True)
        self._stall_task = None

        current = asyncio.current_task()
        if self._recovery_task and self._recovery_task is not current:
            self._recovery_task.cancel()
            await asyncio.gather(self._recovery_task, return_exceptions=True)
        if self._recovery_task is not current:
            self._recovery_task = None
        # The stall-recovery aux task runs INSIDE this pipeline and reaches
        # stop() via bridge._recover_stalled_source -> rebuild. Cancelling and
        # gathering the current task here makes Task.cancel recurse into its
        # own gather child (~1000 frames, RecursionError on py3.14) and wedges
        # the stop forever — the exact "AirPlay 2 dead after PCM stall" crash.
        # Skip the current task like _recovery_task above; it unwinds on its
        # own once stop() returns.
        for task in list(self._aux_tasks):
            if task is not current:
                task.cancel()
        others = [task for task in self._aux_tasks if task is not current]
        if others:
            await asyncio.gather(*others, return_exceptions=True)
        self._aux_tasks.clear()

        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        await self._pcm_source.stop()
        if self._encoder:
            await self._encoder.stop()

        # keep_stream: a rebuilt pipeline re-registers the same stream id, so
        # connected speakers survive a topology change without reconnecting.
        if not keep_stream:
            self._stream_server.unregister_stream(self._stream_id)
        self._status = "idle"

    async def session_start(self) -> None:
        logger.info("AirPlay session started on receiver %s", self.device_id)
        # Pipelines are long-lived. Without resetting this timestamp, a fresh
        # connection inherits all idle time since startup and can be declared
        # stalled before its first packet arrives.
        self._last_feed_at = time.monotonic()
        self._stall_armed = True
        if self._on_session_start:
            try:
                await self._on_session_start(self.device_id)
            except Exception:
                logger.exception("session_start hook failed for %s", self.device_id)

    def _note_source_bytes(self, chunk: bytes) -> None:
        if chunk:
            self._last_feed_at = time.monotonic()
            self._stall_armed = False

    async def _watch_source_stall(self) -> None:
        """Restart the PCM source when it stops producing during a live session.

        The reads above have no timeout, so a wedged TCP socket or a dead
        receiver process that never closes its pipe stalls the encoder forever
        without a single error. Speakers then reconnect to a silent stream in a
        tight loop. Rebuilding the source (reconnect/respawn) is the only cure.
        """
        try:
            while self._running:
                await asyncio.sleep(SOURCE_STALL_CHECK_SECONDS)
                if not self._session_active or not self._session_active():
                    self._stall_armed = False
                    continue
                if self._source_restart_lock.locked():
                    continue
                silence = self.source_silence_seconds
                if silence is None or silence < SOURCE_STALL_TIMEOUT_SECONDS:
                    continue
                if not self._stall_armed:
                    continue
                self._stall_armed = False
                logger.warning(
                    "PCM source for %s produced nothing for %.0fs during a live "
                    "session; restarting the source",
                    self._stream_id,
                    silence,
                )
                if self._on_source_stall is not None:
                    # A ReaderPCMSource cannot repair its upstream RAOP/TCP
                    # producer by restarting around the same dead reader.
                    self._spawn_aux(
                        self._on_source_stall(self._stream_id), "source-stall-recovery"
                    )
                    return
                await self._restart_source()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Source stall watchdog failed for %s", self._stream_id)

    async def _restart_source(self) -> None:
        """Rebuild source + encoder after a stall. The stream endpoint stays
        registered, so connected speakers rejoin the moment bytes flow again."""
        async with self._source_restart_lock:
            if not self._running:
                return
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
            if self._encoder:
                await self._encoder.stop()
                self._encoder = None
            await self._pcm_source.stop()
            self._last_feed_at = time.monotonic()
            self.register_stream()
            try:
                source_reader = await self._pcm_source.start()
                await self._start_encoder(source_reader)
            except Exception:
                logger.exception("Failed to restart stalled source for %s", self._stream_id)
                self._status = "error"

    async def _read_source_chunk(self, reader: asyncio.StreamReader) -> tuple[bytes, bool]:
        """Read one source chunk, synthesizing silence on source stalls.

        Returns (b"", False) only at EOF. While HTTP clients are connected, a
        source that stops delivering for ~one chunk period yields a zero-PCM
        chunk (synthesized=True) instead of starving the encoder — see
        SOURCE_SILENCE_CHUNK_BYTES. With no clients connected this degenerates
        to a plain blocking read: silence would be pure CPU burn with nobody
        listening. The flag lets callers leave stall-watchdog timestamps
        untouched for synthesized chunks: a genuinely wedged source must still
        trip the restart watchdog.
        """
        chunk_seconds = SOURCE_SILENCE_CHUNK_BYTES / (4 * (self._input_sample_rate or 44100))
        if not self._stream_server.client_count(self._stream_id):
            # Nobody is listening: silence would be pure CPU burn. Plain
            # blocking read, exactly like the pre-keepalive behaviour.
            return await reader.read(SOURCE_SILENCE_CHUNK_BYTES), False
        # Grace: the encoder tolerates a sub-grace gap on its own (the speaker
        # buffers seconds); only a sustained zero-byte run synthesizes. This
        # keeps bursty shairport pipe writes from becoming digital-silence
        # gaps while still answering a real stall in ~0.5s, well under the
        # speaker's ~2s abandonment. Late bytes within the grace window are
        # returned immediately — no silence, no extra beat of delay.
        started = asyncio.get_running_loop().time()
        for _ in range(SOURCE_SILENCE_GRACE_PERIODS):
            try:
                chunk = await asyncio.wait_for(
                    reader.read(SOURCE_SILENCE_CHUNK_BYTES), chunk_seconds
                )
                # How long the source made us wait for real bytes: a hole well
                # above the chunk cadence means the SENDER delivers in bursts
                # (the audio the listener hears arrive in lumps). Measured here
                # rather than downstream so it cannot be confused with encoder
                # or delay-line behaviour.
                now = asyncio.get_running_loop().time()
                previous_read = getattr(self, "_last_source_read_at", 0.0)
                if chunk and previous_read:
                    waited_ms = (now - previous_read) * 1000
                    session_active = getattr(self, "_session_active", None)
                    in_session = session_active is None or session_active()
                    if waited_ms > SOURCE_GAP_MS and in_session:
                        metrics.note_source_gap(waited_ms)
                if chunk:
                    self._last_source_read_at = now
                return chunk, False
            except TimeoutError:
                continue
        logger.debug(
            "Source stalled for %s; feeding silence to keep clients alive",
            self._stream_id,
        )
        # Report the silence the source actually produced: the grace window is
        # a constant, so quoting it back hid whether a stall was 0.5s or 30s.
        metrics.note_source_stall((asyncio.get_running_loop().time() - started) * 1000)
        metrics.note_silence_fill()
        return b"\x00" * SOURCE_SILENCE_CHUNK_BYTES, True

    async def _pump_source_to_encoder(self, reader: asyncio.StreamReader, writer) -> None:
        try:
            rate = self._input_sample_rate or 44100
            byte_rate = rate * 4  # s16 stereo
            loop = asyncio.get_running_loop()
            started_at = loop.time()
            fed_bytes = 0
            last_real_at = started_at
            while self._running:
                chunk, synthesized = await self._read_source_chunk(reader)
                if not chunk:
                    break
                if not synthesized:
                    self._note_source_bytes(chunk)
                    # Idle time is not "behind realtime": restart the pacing
                    # baseline after a gap, otherwise a pipeline that sat idle
                    # for minutes reports a three-minute starvation figure.
                    if loop.time() - last_real_at > INPUT_IDLE_RESET_SECONDS:
                        started_at = loop.time()
                        fed_bytes = 0
                    last_real_at = loop.time()
                gained = self._apply_input_gain(chunk)
                if spectrum_wanted():
                    self._spectrum.feed(gained)
                writer.write(gained)
                fed_bytes += len(chunk)
                # Never run ahead of real time: a tee backlog (restart window)
                # must drain at 1x, not burst into the encoder and overflow
                # client queues.
                ahead = fed_bytes / byte_rate - (loop.time() - started_at)
                self._input_ahead_ms = ahead * 1000
                self._input_fed_bytes = fed_bytes
                if self._pace_source and ahead > 0:
                    await asyncio.sleep(ahead)
                await writer.drain()
            writer.write_eof()
            await writer.drain()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error pumping PCM to encoder for %s", self._stream_id)
            self._status = "error"

    async def _pump_encoder_to_stream(self, reader) -> None:
        try:
            loop = asyncio.get_running_loop()
            last_at = loop.time()
            seen = 0
            intervals: deque[float] = deque(maxlen=60)
            while self._running:
                # read(32768) coalesces every immediately-pending muxer write
                # into one ≤32KB chunk (see _EncodedReader), so all encoded
                # formats broadcast at a uniform granularity.
                chunk = await reader.read(32768)
                if not chunk:
                    break
                now = loop.time()
                gap_ms = (now - last_at) * 1000
                last_at = now
                seen += 1
                # Only a gap while a sender session is live is a stall: the
                # pipeline legitimately produces nothing between sessions, and
                # counting that reported an idle afternoon as a 272s stall.
                session_active = getattr(self, "_session_active", None)
                in_session = session_active is None or session_active()
                if seen > 1 and in_session:
                    metrics.note_encode(gap_ms)
                    # The baseline is this pipeline's OWN recent cadence. The
                    # first cut of this metric derived it from the previous
                    # chunk's byte length over the PCM byte rate — but these
                    # are ENCODED bytes, so the "expected" value was far too
                    # small and ordinary jitter looked like a stall (96 false
                    # gaps in five minutes). A real encoder stall is a gap
                    # several times the median interval.
                    if len(intervals) >= 10:
                        typical = sorted(intervals)[len(intervals) // 2]
                        if gap_ms > max(typical * 2.5, typical + ENCODER_GAP_MS):
                            metrics.note_encoder_gap(gap_ms, typical)
                    intervals.append(gap_ms)
                await self._stream_server.broadcast(self._stream_id, chunk)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error pumping encoder to stream for %s", self._stream_id)
            self._status = "error"

    async def _pump_source_to_stream(self, reader: asyncio.StreamReader) -> None:
        try:
            rate = self._input_sample_rate or 44100
            byte_rate = rate * 4
            loop = asyncio.get_running_loop()
            started_at = loop.time()
            fed_bytes = 0
            last_real_at = started_at
            if self._running:
                # Raw PCM bypass: prepend a streaming WAV header so the stream
                # is a valid container (and gets cached as the join prefix).
                await self._stream_server.broadcast(self._stream_id, wav_header(rate))
            while self._running:
                chunk, synthesized = await self._read_source_chunk(reader)
                if not chunk:
                    break
                if not synthesized:
                    self._note_source_bytes(chunk)
                    # See _pump_source_to_encoder: idle time is not starvation.
                    if loop.time() - last_real_at > INPUT_IDLE_RESET_SECONDS:
                        started_at = loop.time()
                        fed_bytes = 0
                    last_real_at = loop.time()
                gained = self._apply_input_gain(chunk)
                if spectrum_wanted():
                    self._spectrum.feed(gained)
                await self._stream_server.broadcast(self._stream_id, gained)
                fed_bytes += len(chunk)
                ahead = fed_bytes / byte_rate - (loop.time() - started_at)
                self._input_ahead_ms = ahead * 1000
                self._input_fed_bytes = fed_bytes
                if self._pace_source and ahead > 0:
                    await asyncio.sleep(ahead)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error pumping PCM to stream for %s", self._stream_id)
            self._status = "error"

    async def _watch_encoder(self) -> None:
        if not self._encoder:
            return
        try:
            await self._encoder.wait()
            if self._running:
                code = self._encoder.returncode
                if code == 0:
                    # Clean EOF: the sender session ended and closed the PCM
                    # source. Restarting here would spin start→EOF→restart every
                    # 3s for every idle receiver (no audio, but a full encoder
                    # teardown repeatedly). Go idle instead; the next session
                    # starts the pipeline again (session_start / ensure_running).
                    logger.info(
                        "Encoder finished for %s; idle until the next session",
                        self._stream_id,
                    )
                    self._status = "idle"
                    return
                logger.warning(
                    "Encoder exited for %s with code %s",
                    self._stream_id,
                    code,
                )
                self._status = "restarting"
                if self._recovery_task is None or self._recovery_task.done():
                    generation = self._generation
                    self._recovery_task = asyncio.create_task(self._delayed_restart(generation))
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error watching encoder for %s", self._stream_id)

    async def _delayed_restart(self, generation: int) -> None:
        try:
            await asyncio.sleep(3)
            # A manual/config-driven rebuild supersedes this recovery. Without
            # the generation check an old encoder failure can stop a healthy
            # replacement pipeline several seconds later.
            if self._running and self._generation == generation:
                await self.stop()
                await self.start()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Delayed pipeline recovery failed for %s", self._stream_id)
            self._status = "error"
        finally:
            if self._recovery_task is asyncio.current_task():
                self._recovery_task = None
