"""Client drift handling: lag ceiling skips to live, slow queues are not kicked."""

import asyncio
from collections import deque

import pytest
from fastapi import Request

from micast.audio_encoder import StreamFormat
from micast.config import settings
from micast.stream_server import (
    StreamServer,
    _drop_whole_chunks,
)


def test_delay_cuts_drop_whole_chunks_only():
    """Drops must stay on chunk boundaries: a byte-offset cut hands the decoder
    a partial frame, which is how a delivered gap becomes lasting distortion."""
    buffer = deque([b"a" * 100, b"b" * 100, b"c" * 100])
    # 90 bytes requested: the first whole chunk goes (100), never 90 bytes of it.
    assert _drop_whole_chunks(buffer, 90) == 100
    assert list(buffer) == [b"b" * 100, b"c" * 100]
    # Overshoot is bounded by one chunk.
    assert _drop_whole_chunks(buffer, 250) == 200
    assert buffer == deque()
    assert _drop_whole_chunks(buffer, 10) == 0


def _mp3_format(byte_rate: int = 40000) -> StreamFormat:
    return StreamFormat("audio/mpeg", "mp3", byte_rate)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/stream/r1",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 9),
        }
    )


def test_laggy_client_is_skipped_to_live_not_kicked():
    """A full queue drops its oldest chunk and keeps the connection."""
    server = StreamServer()
    server.register_stream("r1", _mp3_format())
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    server._clients["r1"].add(queue)
    queue.put_nowait(b"old-1")
    queue.put_nowait(b"old-2")

    server._broadcast_to("r1", b"new")

    assert queue in server._clients["r1"]  # still connected
    assert queue.get_nowait() == b"old-2"  # oldest dropped
    assert queue.get_nowait() == b"new"
    assert server.dropped_chunks["r1"] == 1


def test_kick_sentinel_still_replaces_queued_data():
    """None (kick/EOF) must reach a full queue too, just one chunk later."""
    server = StreamServer()
    server.register_stream("r1", _mp3_format())
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    server._clients["r1"].add(queue)
    queue.put_nowait(b"old")

    server._broadcast_to("r1", None)

    assert queue in server._clients["r1"]  # connection kept
    assert queue.get_nowait() is None  # sentinel replaced the dropped chunk


@pytest.mark.asyncio
async def test_delay_line_releases_whole_chunks_only():
    """Every payload a client reads must be a whole broadcast chunk.

    Chunks are encoder write runs, so whole-chunk release keeps the stream
    frame-aligned. The old 1 024-byte paced slicing cut frames in half, which
    is how a delivered gap turned into lasting distortion on players that do
    not resync (FLAC/WAV in particular)."""
    byte_rate = 40000
    server = StreamServer()
    server.register_stream("r1", _mp3_format(byte_rate))
    response = await server._serve_stream(_request(), "r1")
    iterator = response.body_iterator

    chunks = [bytes([index]) * 20_000 for index in range(1, 20)]
    for chunk in chunks:
        server._broadcast_to("r1", chunk)

    received = []
    for _ in range(1000):  # drain everything releasable from this burst
        try:
            received.append(await asyncio.wait_for(anext(iterator), timeout=0.5))
        except TimeoutError:
            break

    assert received  # the client is fed
    for payload in received:
        assert payload in chunks  # whole chunks only, never a slice
    # The reserve still holds audio back (that is the delay line's job).
    assert sum(len(item) for item in received) < sum(len(item) for item in chunks)

    await iterator.aclose()


@pytest.mark.asyncio
async def test_hold_increase_mid_stream_refills_with_silence(monkeypatch):
    """Raising the delay while playing must withhold real audio until the
    larger reserve has accumulated — otherwise the keepalive path drains the
    buffer every second and the new delay never becomes audible."""
    byte_rate = 40000
    monkeypatch.setattr(settings, "stream_buffer_seconds", 0.05)  # 2 000 B reserve
    server = StreamServer()
    server.register_stream("r1", _mp3_format(byte_rate))
    hold = {"ms": 0}
    monkeypatch.setattr(type(settings), "sink_hold_ms", lambda _self, _rid, _sink: hold["ms"])
    response = await server._serve_stream(_request(), "r1", receiver_id="r1", sink="didB")
    iterator = response.body_iterator
    state = next(iter(server._client_delay.values()))

    async def read_until(marker: bytes, max_chunks: int = 500) -> None:
        for _ in range(max_chunks):
            if marker in await anext(iterator):
                return
        raise AssertionError(f"marker {marker!r} not seen within {max_chunks} chunks")

    live_a = b"\xde\xad\xbe\xef" * 2000
    live_b = b"\xca\xfe\xba\xbe" * 2000
    server._broadcast_to("r1", live_a)
    # Release is chunk-granular: the first chunk stays buffered because the
    # 2 000 B reserve cannot be given up, and the next one frees the first.
    server._broadcast_to("r1", live_b)
    first = await anext(iterator)
    assert b"\xde\xad\xbe\xef" in first and b"\xca\xfe\xba\xbe" not in first

    hold["ms"] = 2000  # reserve grows to 80 000 B mid-stream
    server._broadcast_to("r1", live_a)
    # The refill must keep the player alive with silence instead of draining
    # the withheld audio. No wait_for timeout here: cancelling anext kills
    # the generator (see the note in test_delay_line_caps_lag).
    refill = await anext(iterator)
    assert b"\xca\xfe\xba\xbe" not in refill  # real audio withheld while refilling
    assert refill  # silence frame kept the connection alive
    assert state["needs_fill"]

    server._broadcast_to("r1", b"\x03" * 90000)
    await read_until(b"\xca\xfe\xba\xbe")  # reserve reached: delayed audio flows
    assert not state["needs_fill"]

    await iterator.aclose()
