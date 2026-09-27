"""Unified encoder-output chunking and cross-format drop accounting.

The muxer flushes one encoded frame as many small writes (measured: a flac
frame can arrive as dozens of tiny control writes plus the frame body; wav
adds a ~78-byte header write). ``_EncodedReader.read(n)`` coalesces every
immediately-pending write into at most n bytes so ALL encoded formats
(flac/mp3/wav) reach the stream server at one uniform granularity — without
it each write becomes one per-client queue item and drop counters mean
different durations per format.
"""

import asyncio
import time

import pytest

from micast.audio_encoder import AudioEncoder, StreamFormat, _EncodedReader
from micast.stream_server import StreamServer


class _FakeEncoderConfig:
    def __init__(self, fmt="flac"):
        self.format = fmt
        self.bitrate = "320k"
        self.sample_rate = 48000


def _reader_with(chunks: list[bytes | None]) -> _EncodedReader:
    q: asyncio.Queue = asyncio.Queue()
    for chunk in chunks:
        q.put_nowait(chunk)
    return _EncodedReader(q)


def test_read_coalesces_pending_writes_up_to_n():
    small = [b"\xAA" * 640, b"\xBB" * 512, b"\xCC" * 2048] + [b"\xDD" * 100] * 50
    reader = _reader_with([*small, None])

    async def run():
        first = await reader.read(32768)
        assert first == b"".join(small)  # 53 writes travel as ONE chunk
        assert await reader.read(32768) == b""  # EOF already consumed

    asyncio.run(run())


def test_read_returns_oversized_item_whole():
    frame = b"\xAB" * 40000  # larger than n: returned unsplit
    reader = _reader_with([frame, None])

    async def run():
        assert await reader.read(32768) == frame
        assert await reader.read(32768) == b""

    asyncio.run(run())


def test_read_defers_eof_when_consumed_mid_aggregation():
    reader = _reader_with([b"\x01" * 100, None])

    async def run():
        # The EOF sentinel sits pending right after the data; aggregation
        # must not swallow it into the data chunk.
        assert await reader.read(32768) == b"\x01" * 100
        assert await reader.read(32768) == b""

    asyncio.run(run())


def test_read_nowait_drains_without_blocking():
    reader = _reader_with([b"\x01" * 10])
    assert reader.read_nowait() == b"\x01" * 10
    assert reader.read_nowait() == b""  # empty, non-blocking


@pytest.mark.asyncio
async def test_encoder_drop_stats_count_input_and_output_loss():
    """A stalled encoder input (full queue) and a stalled output both surface
    drop counts — PCM-layer loss becomes as visible as stream-server drops.

    Input drops are measured with the worker not started (deterministic fill);
    silence frames need 4096 samples, so the 65 tiny pre-start writes can
    never make the started worker emit before the output queue is pre-filled.
    """
    enc = AudioEncoder(_FakeEncoderConfig(), 48000)
    for _ in range(64):  # fill the input queue (maxsize=64)
        enc.stdin.write(b"\x00" * 100)
    enc.stdin.write(b"\x00" * 100)  # this one must evict the oldest
    assert enc.drop_stats()["in"] == 1

    await enc.start()
    for _ in range(64):  # pre-fill the output queue (exactly capacity)
        enc._loop.call_soon_threadsafe(enc._out.put_nowait, b"\xff" * 50)
    await asyncio.sleep(0.2)
    enc.stdin.write(b"\x00" * 32768)  # 8192 samples → 2 flac frames emitted
    await asyncio.sleep(0.5)
    assert enc.drop_stats()["out"] >= 1
    await enc.stop()


def test_drop_metrics_are_cross_format_comparable():
    """Same lost audio expressed in chunks means wildly different durations;
    dropped_bytes and estimated_ms must agree across formats."""
    server = StreamServer()
    # mp3: nominal byte_rate 40000 (320k). flac: no nominal rate — the
    # observed rate from broadcast() stands in so both produce a ms estimate.
    server.register_stream("mp3", StreamFormat("audio/mpeg", "mp3", 40000))
    server.register_stream("flac", StreamFormat("audio/flac", "flac", None))

    async def run():
        # Drive the flac observed rate with a steady 16 kB/s so its rate is
        # known. The rate is bytes over the sample window's span, so the
        # broadcasts have to be spread over real time rather than written back
        # to back — a burst describes the burst, not the stream.
        for _ in range(14):
            await server.broadcast("flac", b"\x00" * 800)
            await asyncio.sleep(0.05)
        flac_rate = server._stream_byte_rate("flac")
        assert 12000 < flac_rate < 20000

        mp3_q: asyncio.Queue = asyncio.Queue(maxsize=1)
        flac_q: asyncio.Queue = asyncio.Queue(maxsize=1)
        server._clients["mp3"].add(mp3_q)
        server._clients["flac"].add(flac_q)
        server._client_delay[mp3_q] = {"last_get_at": time.monotonic()}
        server._client_delay[flac_q] = {"last_get_at": time.monotonic()}
        mp3_q.put_nowait(b"\x00" * 4000)  # 100ms of mp3 audio
        flac_q.put_nowait(b"\x00" * 1600)  # 100ms of flac audio
        server._broadcast_to("mp3", b"\x01" * 4000)
        server._broadcast_to("flac", b"\x01" * 1600)

        mp3_metrics = server.drop_metrics("mp3")
        flac_metrics = server.drop_metrics("flac")
        assert mp3_metrics["chunks"] == flac_metrics["chunks"] == 1
        # Same 100ms of lost audio on both formats → same ms, comparable count.
        assert mp3_metrics["estimated_ms"] == 100
        # flac's figure comes from the observed rate, so compare against that
        # rate rather than a hard-coded number.
        assert flac_metrics["estimated_ms"] == round(1600 / flac_rate * 1000)
        assert abs(flac_metrics["estimated_ms"] - 100) <= 35

    asyncio.run(run())


@pytest.mark.asyncio
async def test_idle_gap_between_sessions_is_not_an_encoder_stall(monkeypatch):
    """An AirPlay 1 <-> 2 switch leaves the session latch set across the idle
    minute, so the pump's inter-chunk gap spans the whole idle period.

    Field data (0.3.6): those gaps were recorded as encoder stalls of 4835ms,
    11.3s, 28.7s and 125.7s right next to real 200ms hiccups — the loudest row
    on the diagnostics page, and pure fiction. Past the ceiling the interval is
    rebased instead.
    """
    import micast.speaker_pipeline as pipeline_module
    from micast.audio_metrics import metrics
    from micast.speaker_pipeline import SpeakerPipeline

    class _SlowReader:
        def __init__(self, gap: float):
            self._gap = gap
            self._calls = 0

        async def read(self, n: int = -1) -> bytes:
            self._calls += 1
            if self._calls > 2:
                await asyncio.sleep(5)  # hold the pump until the test ends
                return b""
            if self._calls == 2:
                await asyncio.sleep(self._gap)
            return b"\x22" * 1024

    async def run(gap: float, ceiling: float) -> dict:
        metrics.reset()
        monkeypatch.setattr(pipeline_module, "ENCODER_STALL_CEILING_MS", ceiling)
        pipeline = object.__new__(SpeakerPipeline)
        pipeline._running = True
        pipeline._stream_id = "airplay2"
        pipeline._status = "running"
        pipeline._session_active = lambda: True
        server = StreamServer()
        server.register_stream("airplay2", StreamFormat("audio/flac", "flac", None))
        pipeline._stream_server = server

        task = asyncio.create_task(pipeline._pump_encoder_to_stream(_SlowReader(gap)))
        await asyncio.sleep(0.4)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return metrics.snapshot()["encode"]

    idle = await run(0.15, 50.0)
    assert idle["chunks"] == 0  # the 150ms idle gap is rebased, not measured
    assert idle["stalls"] == 0

    # Control: with the ceiling out of the way the same gap IS measured (and is
    # still not a stall — it is below ENCODER_STALL_MS).
    measured = await run(0.15, 1000.0)
    assert measured["chunks"] == 1
    assert measured["stalls"] == 0
