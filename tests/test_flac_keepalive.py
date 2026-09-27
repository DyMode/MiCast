"""Source-stall silence feed and FLAC delay-line activation.

Plan item 1 (encoder-side keepalive): when the PCM source stops delivering
while HTTP clients are connected, the pipeline pumps synthesize zero-PCM
chunks so the running encoder keeps producing valid frames (flac has no
precomputable silence; a starved Xiaomi pull player drops the response in
~2s). Synthesized chunks advance the pacing clock but NOT the stall-watchdog
timestamp, so a genuinely wedged source still gets restarted. Resumed source
data flows at the pacing clock — no burst.

Plan item 2 (flac delay line): clients of a format without a nominal byte
rate get the reserve/lag delay line only once the broadcast-observed rate EMA
is well-sampled and above an absolute floor; otherwise they stay on the
transparent passthrough.
"""

import asyncio
import time

import pytest
from fastapi import Request

import micast.speaker_pipeline as pipeline_module
import micast.stream_server as stream_server_module
from micast.audio_encoder import StreamFormat, encoder_silence_chunks, raw_pcm_format
from micast.config import settings
from micast.speaker_pipeline import (
    INPUT_MAX_SLEEP_SECONDS,
    SOURCE_SILENCE_CHUNK_BYTES,
    SpeakerPipeline,
)
from micast.stream_server import (
    DELAY_LINE_MIN_BYTES_PER_SECOND,
    DELAY_LINE_MIN_SAMPLES,
    StreamServer,
)

# A smaller synthetic chunk scales the grace window down for fast tests.
SMALL_CHUNK = 4096


class _RecordingWriter:
    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.chunks.append(bytes(data))

    def write_eof(self) -> None:
        pass

    async def drain(self) -> None:
        return None


class _FakeStreamServer:
    def __init__(self, clients: int, backlog: int = 0):
        self._clients = clients
        self.backlog = backlog
        self.broadcasts: list[bytes] = []

    def client_count(self, stream_id: str) -> int:
        return self._clients

    def client_backlog_chunks(self, stream_id: str) -> int:
        return self.backlog

    async def broadcast(self, stream_id: str, chunk: bytes) -> None:
        self.broadcasts.append(chunk)


def _pipeline(client_count: int) -> tuple[SpeakerPipeline, _RecordingWriter, _FakeStreamServer]:
    pipeline = object.__new__(SpeakerPipeline)
    pipeline._running = True
    pipeline._stream_id = "airplay2"
    pipeline._input_sample_rate = 48000
    pipeline._pace_source = False  # no pacing sleeps: the stall timeout paces
    pipeline._input_volume = 100
    pipeline._status = "running"
    pipeline._last_feed_at = 0.0
    pipeline._stall_armed = True
    pipeline._spectrum = None
    pipeline._stream_server = _FakeStreamServer(client_count)
    writer = _RecordingWriter()
    return pipeline, writer, pipeline._stream_server


@pytest.mark.asyncio
async def test_encoder_pump_feeds_silence_while_clients_connected():
    pipeline, writer, _ = _pipeline(client_count=1)
    reader = asyncio.StreamReader()
    reader.feed_data(b"\x7f" * SOURCE_SILENCE_CHUNK_BYTES)  # one real chunk

    task = asyncio.create_task(pipeline._pump_source_to_encoder(reader, writer))
    # One real chunk, then synthesized silence every ~0.17s of stall.
    deadline = asyncio.get_running_loop().time() + 5
    while len(writer.chunks) < 4 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)

    assert len(writer.chunks) >= 4
    assert writer.chunks[0] == b"\x7f" * SOURCE_SILENCE_CHUNK_BYTES
    for silence in writer.chunks[1:]:
        assert silence == b"\x00" * SOURCE_SILENCE_CHUNK_BYTES
    # Synthesized silence must NOT advance the stall-watchdog timestamp: a
    # genuinely wedged source still needs the restart path to fire (~8s of
    # no REAL bytes).
    import time

    assert time.monotonic() - pipeline._last_feed_at > 0.3

    # Source resumes: the real chunk is delivered after the silence, no burst
    # (each synthesized chunk was already paced at one chunk period).
    reader.feed_data(b"\x55" * SOURCE_SILENCE_CHUNK_BYTES)
    deadline = asyncio.get_running_loop().time() + 3
    while writer.chunks[-1] != b"\x55" * SOURCE_SILENCE_CHUNK_BYTES and (
        asyncio.get_running_loop().time() < deadline
    ):
        await asyncio.sleep(0.02)
    assert writer.chunks[-1] == b"\x55" * SOURCE_SILENCE_CHUNK_BYTES

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_encoder_pump_stays_quiet_without_clients():
    pipeline, writer, _ = _pipeline(client_count=0)
    reader = asyncio.StreamReader()
    reader.feed_data(b"\x7f" * SOURCE_SILENCE_CHUNK_BYTES)

    task = asyncio.create_task(pipeline._pump_source_to_encoder(reader, writer))
    await asyncio.sleep(0.7)  # > one chunk period: no silence may be fed
    assert len(writer.chunks) == 1  # only the real chunk

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_raw_pump_broadcasts_silence_while_clients_connected():
    pipeline, _, server = _pipeline(client_count=1)
    reader = asyncio.StreamReader()
    reader.feed_data(b"\x7f" * SOURCE_SILENCE_CHUNK_BYTES)

    task = asyncio.create_task(pipeline._pump_source_to_stream(reader))
    deadline = asyncio.get_running_loop().time() + 5
    while len(server.broadcasts) < 5 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)

    assert len(server.broadcasts) >= 5  # header + real + ≥3 silence
    assert server.broadcasts[0].startswith(b"RIFF")  # streaming WAV header
    assert server.broadcasts[1] == b"\x7f" * SOURCE_SILENCE_CHUNK_BYTES
    for silence in server.broadcasts[2:]:
        assert silence == b"\x00" * SOURCE_SILENCE_CHUNK_BYTES

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/stream/airplay2",
            "query_string": b"",
            "headers": [],
            "client": ("192.168.0.128", 9),
        }
    )


def _flac_server() -> StreamServer:
    server = StreamServer()
    server.register_stream("airplay2", StreamFormat("audio/flac", "flac", None))
    return server


@pytest.mark.asyncio
async def test_flac_delay_line_activates_with_stable_observed_rate():
    """With a trustworthy observed rate the flac delay line paces the client
    through whole chunks and keeps the reserve back."""
    server = _flac_server()
    rate = 100_000
    server._observed_byte_rate["airplay2"] = float(rate)
    server._rate_samples["airplay2"] = DELAY_LINE_MIN_SAMPLES

    response = await server._serve_stream(_request(), "airplay2")
    iterator = response.body_iterator
    state = next(iter(server._client_delay.values()))

    chunks = [bytes([index]) * 20_000 for index in range(1, 20)]
    for chunk in chunks:
        server._broadcast_to("airplay2", chunk)

    received = [await asyncio.wait_for(anext(iterator), timeout=0.5)]
    # A client whose reserve is not whole yet is fed silence *in its own
    # format*. For flac there used to be nothing to send, so the socket simply
    # went empty — which is what lets a speaker decide the stream died while a
    # delay is filling.
    silence = encoder_silence_chunks("audio/flac", settings.audio.sample_rate, settings.audio.bitrate)
    assert received[0] in silence
    # ... and the real audio stays in the delay line meanwhile: this buffer is
    # the reserve the delay line exists to hold (alignment offset plus a margin
    # for the release rule).
    received.append(await asyncio.wait_for(anext(iterator), timeout=0.5))
    assert state["buffer_ms"] > 0

    for _ in range(1000):
        try:
            received.append(await asyncio.wait_for(anext(iterator), timeout=0.5))
        except TimeoutError:
            break

    audio = [item for item in received if item in chunks]
    assert audio  # real audio does flow once the reserve is whole
    for payload in received:
        # Whole chunks only: never a frame-splitting slice, and never audio
        # dressed up as something else.
        assert payload in chunks or payload in silence
    # Nothing is lost on the way: what the reserve held is bridged to the client
    # during the arrival gaps instead of being withheld (see the arrival-gap
    # test below), and the silence only ever adds to it.
    assert sum(len(item) for item in audio) == sum(len(item) for item in chunks)
    await iterator.aclose()


@pytest.mark.asyncio
async def test_wav_client_is_fed_silence_while_its_delay_fills():
    """The passthrough (raw PCM as streaming WAV) had no silence to send either.

    Same gap as flac, and this is the format a speaker gets when transcoding is
    off: a delay increase used to leave its socket empty.
    """
    server = StreamServer()
    server.register_stream("airplay2", raw_pcm_format(48000))

    response = await server._serve_stream(_request(), "airplay2")
    iterator = response.body_iterator

    for index in range(1, 4):
        server._broadcast_to("airplay2", bytes([index]) * 20_000)

    received = [await asyncio.wait_for(anext(iterator), timeout=0.5)]

    expected = encoder_silence_chunks(
        "audio/wav", settings.audio.sample_rate, settings.audio.bitrate
    )[0]
    assert received[0] == expected
    assert not any(received[0])  # zero samples: silence, not a byte pattern
    await iterator.aclose()


@pytest.mark.asyncio
async def test_flac_passthrough_during_rate_warmup():
    """Before the EMA has enough samples the client stays transparent: a
    burst passes through whole, unthrottled."""
    server = _flac_server()
    server._observed_byte_rate["airplay2"] = 100_000.0
    server._rate_samples["airplay2"] = DELAY_LINE_MIN_SAMPLES - 1

    response = await server._serve_stream(_request(), "airplay2")
    iterator = response.body_iterator
    state = next(iter(server._client_delay.values()))

    burst = b"\x02" * 500_000
    server._broadcast_to("airplay2", burst)
    received = bytearray()
    for _ in range(10):
        try:
            received.extend(await asyncio.wait_for(anext(iterator), timeout=0.5))
        except TimeoutError:
            break

    assert bytes(received) == burst  # nothing held back, nothing dropped
    assert state["lag_drops"] == 0
    await iterator.aclose()


@pytest.mark.asyncio
async def test_flac_passthrough_when_observed_rate_below_floor():
    """A near-silence EMA (below the absolute floor) must not throttle."""
    server = _flac_server()
    server._observed_byte_rate["airplay2"] = float(DELAY_LINE_MIN_BYTES_PER_SECOND - 1)
    server._rate_samples["airplay2"] = DELAY_LINE_MIN_SAMPLES * 5

    response = await server._serve_stream(_request(), "airplay2")
    iterator = response.body_iterator
    state = next(iter(server._client_delay.values()))

    burst = b"\x03" * 200_000
    server._broadcast_to("airplay2", burst)
    received = bytearray()
    for _ in range(10):
        try:
            received.extend(await asyncio.wait_for(anext(iterator), timeout=0.5))
        except TimeoutError:
            break

    assert bytes(received) == burst
    assert state["lag_drops"] == 0
    await iterator.aclose()


@pytest.mark.asyncio
async def test_broadcast_accumulates_rate_samples_for_delay_line(monkeypatch):
    # The gate is "enough samples AND a span long enough to mean something";
    # shorten the span so the cadence below can be short too.
    monkeypatch.setattr(stream_server_module, "RATE_MIN_WINDOW_SECONDS", 0.2)
    server = _flac_server()
    assert server._delay_line_byte_rate("airplay2") is None
    for _ in range(DELAY_LINE_MIN_SAMPLES):
        await server.broadcast("airplay2", b"\x00" * 20_000)
        await asyncio.sleep(0.02)  # ~1 MB/s observed over the shortened window
    rate = server._delay_line_byte_rate("airplay2")
    assert rate is not None and rate >= DELAY_LINE_MIN_BYTES_PER_SECOND
    # Re-registration resets the sample count: warmup starts over.
    server.register_stream("airplay2", StreamFormat("audio/flac", "flac", None))
    assert server._delay_line_byte_rate("airplay2") is None


# --- Regression: grace window against jitter-induced silence injection ---

class _WriterAdapter:
    """Adapt the _RecordingWriter list-consumption to the raw pipeline."""

    def __init__(self, writer):
        self._writer = writer

    def write(self, data: bytes) -> None:
        self._writer.write(data)

    def write_eof(self) -> None:
        pass

    async def drain(self) -> None:
        return None




class _PipeSource:
    """Producer-driven source like a process stdout pipe: a background task
    pushes chunks on its own jittery schedule; reads only consume what has
    arrived (cancellation-safe via StreamReader's internal buffer)."""

    def __init__(self, delays: list[float]):
        self._delays = delays
        self._reader = asyncio.StreamReader()
        self._producer: asyncio.Task | None = None

    def start(self) -> None:
        async def produce() -> None:
            index = 0
            while True:
                await asyncio.sleep(self._delays[index % len(self._delays)])
                index += 1
                self._reader.feed_data(b"\x7f" * SOURCE_SILENCE_CHUNK_BYTES)

        self._producer = asyncio.create_task(produce())

    async def read(self, n: int = -1) -> bytes:
        return await self._reader.read(n)

    async def stop(self) -> None:
        if self._producer:
            self._producer.cancel()
            await asyncio.gather(self._producer, return_exceptions=True)


def _zero(chunk: bytes, size: int = SOURCE_SILENCE_CHUNK_BYTES) -> bool:
    return chunk == b"\x00" * size


@pytest.fixture
def fast_silence(monkeypatch) -> int:
    """Shrink the synthesized chunk period; returns the new chunk size.

    The grace window is SOURCE_SILENCE_GRACE_PERIODS chunk periods, and a
    period is SOURCE_SILENCE_CHUNK_BYTES of PCM — a smaller chunk scales the
    whole timing behaviour down without changing the code path under test
    (~0.1s grace instead of ~0.9s).
    """
    monkeypatch.setattr(pipeline_module, "SOURCE_SILENCE_CHUNK_BYTES", SMALL_CHUNK)
    return SMALL_CHUNK


@pytest.mark.asyncio
async def test_jittery_pipe_source_never_gets_synthesis():
    """A shairport-like pipe with 50-300ms write jitter must produce ZERO
    synthesized silence (the single-timeout trigger injected 3.5-4.6s of
    digital silence per 6s — audible stutter, invisible to diagnostics)."""
    pipeline, writer, _ = _pipeline(client_count=1)
    source = _PipeSource([0.05, 0.30, 0.10, 0.26, 0.17, 0.30])
    source.start()
    task = asyncio.create_task(pipeline._pump_source_to_encoder(source._reader, _WriterAdapter(writer)))
    await asyncio.sleep(5)
    task.cancel()
    await source.stop()
    await asyncio.gather(task, return_exceptions=True)

    assert writer.chunks, "the source did feed anything"
    assert all(not _zero(chunk) for chunk in writer.chunks)


@pytest.mark.asyncio
async def test_real_stall_starts_silence_within_grace():
    """A genuinely dead source must still be kept alive — the first silence
    chunk arrives after ~GRACE x chunk period (~0.5s), far below the speaker's
    ~2s abandonment threshold."""
    pipeline, writer, _ = _pipeline(client_count=1)
    reader = asyncio.StreamReader()
    reader.feed_data(b"\x7f" * SOURCE_SILENCE_CHUNK_BYTES)  # one real chunk, then dead

    task = asyncio.create_task(
        pipeline._pump_source_to_encoder(reader, _WriterAdapter(writer))
    )
    started = time.monotonic()
    while len(writer.chunks) < 2 and time.monotonic() - started < 3:
        await asyncio.sleep(0.01)
    first_silence_at = time.monotonic() - started

    assert len(writer.chunks) >= 2
    assert _zero(writer.chunks[-1])
    # ~0.51s grace at 48kHz; generous bounds for CI scheduling jitter.
    assert 0.35 < first_silence_at < 1.2
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_late_data_within_grace_is_fed_without_extra_beat():
    """Data arriving during the grace window is consumed immediately (no
    silence inserted, no additional chunk-period delay), and the grace budget
    resets — repeated sub-grace gaps never synthesize."""
    pipeline, writer, _ = _pipeline(client_count=1)
    source = _PipeSource([0.05, 0.34, 0.05, 0.34, 0.05])  # sub-grace gaps
    source.start()
    task = asyncio.create_task(
        pipeline._pump_source_to_encoder(source._reader, _WriterAdapter(writer))
    )
    await asyncio.sleep(3.5)
    task.cancel()
    await source.stop()
    await asyncio.gather(task, return_exceptions=True)

    assert writer.chunks
    assert all(not _zero(chunk) for chunk in writer.chunks)


# --- Pacing: the source's lead, never one branch's consumer ----------------

@pytest.mark.asyncio
async def test_the_sender_lookahead_is_banked_not_slept_away():
    """A modest lead must reach the speaker, not be paced away.

    Field data (0.3.5): the pump paced against the wall-clock lead, so the
    sender's 525ms look-ahead (shairport's decoded buffer, or a phone's AirPlay
    buffer) was held at exactly that lead and the speaker ended up with no
    buffer of its own — every 200-400ms source hole was then audible, with all
    drop counters at zero.
    """
    pipeline, writer, _ = _pipeline(client_count=1)
    pipeline._input_sample_rate = 8000  # one 32 KiB chunk ≈ 1s of audio
    pipeline._pace_source = True
    sleeps: list[float] = []

    async def spy(seconds: float, loop) -> None:
        sleeps.append(seconds)

    pipeline._pace_sleep = spy

    reader = asyncio.StreamReader()
    reader.feed_data(b"\x11" * SOURCE_SILENCE_CHUNK_BYTES)  # ahead ≈ 2.05s

    task = asyncio.create_task(pipeline._pump_source_to_encoder(reader, writer))
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert pipeline._input_ahead_ms > 500  # the lead was real
    assert sleeps == []  # ... and inside the allowance: handed over untouched


@pytest.mark.asyncio
async def test_pacing_follows_the_source_lead_not_one_branchs_consumer():
    """The pacing signal must be shared by every branch of one source.

    Field failure (0.4.2, stereo pair): each branch paced on its OWN consumer,
    so the right channel ran 2.1s ahead while the left sat 16.9s behind — and a
    single half-open client (whose queue nobody ever drains) stalled the left
    branch for good, overflowing the upstream tee by 2507 chunks.
    """
    pipeline, writer, server = _pipeline(client_count=1)
    pipeline._input_sample_rate = 4000
    pipeline._pace_source = True
    sleeps: list[float] = []

    async def spy(seconds: float, loop) -> None:
        sleeps.append(seconds)

    pipeline._pace_sleep = spy
    server.backlog = 40  # a very "behind" consumer must change nothing

    reader = asyncio.StreamReader()
    reader.feed_data(b"\x11" * (4 * SOURCE_SILENCE_CHUNK_BYTES))  # ~8s of lead

    task = asyncio.create_task(pipeline._pump_source_to_encoder(reader, writer))
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert sleeps, "a lead past the allowance must still hold the pump back"
    assert max(sleeps) <= INPUT_MAX_SLEEP_SECONDS


# --- Source filler: silence is not lead, and holes are bridged -------------

@pytest.mark.asyncio
async def test_synthesized_silence_is_not_counted_as_source_lead(fast_silence):
    """Silence we inject must not advance the pacing clock.

    Field failure (0.3.4): every silence chunk counted as fed audio, so ~25 of
    them (all around session transitions) added ~4.2s to the "source lead". The
    pump then slept that phantom lead away — a 5.1s pacing sleep measured
    against a 5.4s encoder gap in the same second — which starved the speaker's
    HTTP feed and made it drop the connection.
    """
    size = fast_silence
    pipeline, writer, _ = _pipeline(client_count=1)
    reader = asyncio.StreamReader()
    real = 2 * size
    reader.feed_data(b"\x7f" * real)  # two real chunks, then the source is dead

    task = asyncio.create_task(pipeline._pump_source_to_encoder(reader, writer))
    await asyncio.sleep(0.5)  # a few grace windows of the shrunk chunk

    silences = [chunk for chunk in writer.chunks if _zero(chunk, size)]
    assert silences, "the dead source should still be padded"
    assert pipeline._input_fed_bytes == real  # only real audio counts as lead

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_arrival_gap_is_bridged_from_the_delay_line(monkeypatch):
    """A source hole must not become a hole in the speaker's byte stream.

    Field data (every report, 0.3.4-0.4.x): the source leaves 150-400ms holes
    with every drop counter at zero — the audio is late, not lost. The
    generator only released bytes when a NEW chunk arrived, so during a hole it
    held the reserve and sent nothing, and the listener heard each one. Now it
    serves the held audio instead.
    """
    from micast.stream_server import DELAY_LINE_MIN_SAMPLES

    server = _flac_server()
    rate = 100_000
    server._observed_byte_rate["airplay2"] = float(rate)
    server._rate_samples["airplay2"] = DELAY_LINE_MIN_SAMPLES
    server.set_buffer_override("airplay2", 0.25)  # 25 kB reserve

    response = await server._serve_stream(_request(), "airplay2")
    iterator = response.body_iterator
    state = next(iter(server._client_delay.values()))

    for index in range(3):
        server._broadcast_to("airplay2", bytes([index + 1]) * 20_000)
        await asyncio.sleep(0.01)
    await asyncio.wait_for(anext(iterator), timeout=0.5)
    await asyncio.wait_for(anext(iterator), timeout=0.5)
    assert state["buffer_ms"] > 0, "the delay line should be holding audio"

    bridged = await asyncio.wait_for(anext(iterator), timeout=0.6)
    assert len(bridged) == 20_000
    assert state["bridges"] >= 1
    assert state["last_get_at"] > 0  # still a live client, not a ghost

    # Counterfactual: waiting for the next broadcast (the pre-0.4 rule) would
    # simply have left the speaker with nothing.
    monkeypatch.setattr(stream_server_module, "CLIENT_BRIDGE_GAP_SECONDS", 30.0)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(iterator), timeout=0.4)

    await iterator.aclose()
