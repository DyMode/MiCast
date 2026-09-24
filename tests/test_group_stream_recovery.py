import asyncio

import pytest
from starlette.requests import Request

from micast.audio_encoder import StreamFormat
from micast.stream_server import StreamServer


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "client": ("192.168.0.20", 12345),
        }
    )


@pytest.mark.asyncio
async def test_group_recovery_releases_members_on_one_future_boundary():
    server = StreamServer()
    stream_format = StreamFormat("audio/mpeg", "mp3", 40_000)
    server.register_stream("group-L", stream_format)
    server.register_stream("group-R", stream_format)
    server.begin_group_recovery("group", ["speaker-a", "speaker-b"])

    response_a = await server._serve_stream(_request(), "group-L", "group", "speaker-a")
    first_a = asyncio.create_task(anext(response_a.body_iterator))
    await server.broadcast("group-L", b"old-a" * 4000)
    await asyncio.sleep(0)
    assert not first_a.done()

    response_b = await server._serve_stream(_request(), "group-R", "group", "speaker-b")
    first_b = asyncio.create_task(anext(response_b.body_iterator))
    await server.broadcast("group-L", b"A" * 20_000)
    await server.broadcast("group-R", b"B" * 20_000)
    # Release is chunk-granular (every byte a client reads stays frame-aligned),
    # so one more chunk is needed before the reserve lets the first one out.
    await server.broadcast("group-L", b"A" * 20_000)
    await server.broadcast("group-R", b"B" * 20_000)

    assert await asyncio.wait_for(first_a, 1) == b"A" * 20_000
    assert await asyncio.wait_for(first_b, 1) == b"B" * 20_000
    await response_a.body_iterator.aclose()
    await response_b.body_iterator.aclose()


def test_aborting_group_recovery_releases_waiters():
    server = StreamServer()
    server.begin_group_recovery("group", ["speaker-a", "speaker-b"])
    event = server._group_recoveries["group"]["event"]

    server.abort_group_recovery("group")

    assert event.is_set()
    assert "group" not in server._group_recoveries


@pytest.mark.asyncio
async def test_sink_connected_tracks_replacement_client():
    server = StreamServer()
    server.register_stream("group-L", StreamFormat("audio/mpeg", "mp3", 40_000))
    response = await server._serve_stream(_request(), "group-L", "group", "speaker-a")

    assert server.sink_connected("group", "speaker-a")
    assert not server.sink_connected("group", "speaker-b")

    first = asyncio.create_task(anext(response.body_iterator))
    await server.broadcast("group-L", b"A" * 20_000)
    await server.broadcast("group-L", b"A" * 20_000)
    await asyncio.wait_for(first, 1)
    await response.body_iterator.aclose()
    assert not server.sink_connected("group", "speaker-a")
