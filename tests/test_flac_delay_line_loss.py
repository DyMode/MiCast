"""The flac delay line must never cost audio: a wrong rate estimate may only
cost latency.

Field report (0.3.3, second AirPlay session of a bridge run): the speaker's
HTTP client received ~1% of the stream — 11 kB out of 1.3 MB in 12 s — while
"1 台音箱取流" and every drop counter read healthy, and the listener heard
stuttering.

Cause: the observed byte rate was an EMA of ``len(chunk) / (now - last)``.
A realtime encoder emits in bursts (two muxer writes land a few ms apart
inside one paced PCM burst), so that divisor is ~0 for one of the pair and the
estimate came out ~900x high (92 MB/s against a true 104 kB/s). The delay
line's reserve is ``rate * stream_buffer_seconds``, so the "250 ms" reserve
became ~250 seconds: no client could ever hold it, ``held <= reserve`` stayed
true forever, and the keepalive branch — which is documented as *releasing*
below the reserve — popped the buffered chunks, counted them as consumed and
threw them away. The player then got nothing.
"""

import asyncio

import pytest
from fastapi import Request

import micast.stream_server as stream_server_module
from micast.audio_encoder import StreamFormat
from micast.config import settings
from micast.stream_server import (
    DELAY_LINE_MIN_SAMPLES,
    STREAM_PREFIX_BYTES,
    StreamServer,
)

CHUNK = b"\x11" * 6000
CYCLE_SECONDS = 0.05


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


async def _broadcast_cycles(server: StreamServer, cycles: int) -> None:
    """One cycle = the pair of muxer writes inside a single paced PCM burst."""
    for _ in range(cycles):
        await server.broadcast("airplay2", CHUNK)
        await server.broadcast("airplay2", CHUNK)
        await asyncio.sleep(CYCLE_SECONDS)


async def _consume(iterator, received: list[int]) -> None:
    async for payload in iterator:
        received[0] += len(payload)


@pytest.mark.asyncio
async def test_bursty_cadence_yields_a_truthful_rate(monkeypatch):
    """Bytes over the window's span, not bytes over the gap since the last write."""
    # Same relationship, 4x faster: the rate window is a span in seconds, so it
    # is scaled with the broadcast cadence instead of the wall clock.
    monkeypatch.setattr(stream_server_module, "RATE_MIN_WINDOW_SECONDS", 0.2)
    server = _flac_server()
    await _broadcast_cycles(server, DELAY_LINE_MIN_SAMPLES // 2 + 2)

    true_rate = 2 * len(CHUNK) / CYCLE_SECONDS
    rate = server._observed_byte_rate["airplay2"]
    assert 0.75 * true_rate < rate < 1.3 * true_rate
    assert server._delay_line_byte_rate("airplay2") is not None


@pytest.mark.asyncio
async def test_bursty_cadence_delivers_the_audio_and_holds_a_real_reserve(monkeypatch):
    monkeypatch.setattr(stream_server_module, "RATE_MIN_WINDOW_SECONDS", 0.2)
    server = _flac_server()
    await _broadcast_cycles(server, DELAY_LINE_MIN_SAMPLES // 2 + 2)

    response = await server._serve_stream(_request(), "airplay2")
    state = next(iter(server._client_delay.values()))
    received = [0]
    consumer = asyncio.create_task(_consume(response.body_iterator, received))

    await _broadcast_cycles(server, 10)
    await asyncio.sleep(0.2)
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)

    session_bytes = 2 * len(CHUNK) * 10
    reserve_ms = state["buffer_ms"]
    assert reserve_ms, "the delay line reported no reserve at all"
    # The reserve is the configured 250 ms; anything in the same order of
    # magnitude means the rate behind it is trustworthy.
    assert 100 <= reserve_ms <= 400
    # Everything sent during the session, minus what the reserve legitimately
    # holds back (its size in bytes follows the observed rate, not the cadence
    # this test happens to use), has to reach the client.
    observed = server._delay_line_byte_rate("airplay2")
    reserve_bytes = observed * settings.stream_buffer_seconds
    assert received[0] >= session_bytes - reserve_bytes - 2 * len(CHUNK)
    await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_absurd_reserve_never_silences_the_client(monkeypatch):
    """A high rate estimate may cost latency; it must never cost the audio.

    Regression: with a ~900x rate the reserve was ~250 s, the normal release
    path could never fire, and the keepalive branch discarded the client's
    buffered audio once a second — the speaker received nothing.
    """
    # The keepalive window is what the assertion waits for; shrink both.
    keepalive = 0.2
    monkeypatch.setattr(stream_server_module, "CLIENT_KEEPALIVE_SECONDS", keepalive)
    server = _flac_server()
    server._observed_byte_rate["airplay2"] = 92_000_000.0
    server._rate_samples["airplay2"] = DELAY_LINE_MIN_SAMPLES * 10

    response = await server._serve_stream(_request(), "airplay2")
    received = [0]
    consumer = asyncio.create_task(_consume(response.body_iterator, received))

    for _ in range(3):
        await server.broadcast("airplay2", CHUNK)
    await asyncio.sleep(keepalive + 0.2)
    await server.broadcast("airplay2", CHUNK)
    await asyncio.sleep(0.2)
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)

    assert received[0] >= STREAM_PREFIX_BYTES + len(CHUNK), (
        f"client starved beyond the 16 KiB join prefix: {received[0]} bytes"
    )
    await response.body_iterator.aclose()
