"""An unregistered stream id must serve the receiver's stream, not a 404.

A 404 here is not a neutral error: the speaker's player gives up on the URL,
and the codec-capability prober behind ``no_stream_pull`` records "this device
cannot play this format" — so one URL built with the wrong suffix would
blacklist a format permanently. A URL naming an unregistered variant (the
channel-only ``/stream/{entry}-L`` after the entry went EQ-split) must
therefore land on the stream the receiver does publish.
"""

import pytest
from fastapi import HTTPException, Request

from micast.audio_encoder import StreamFormat
from micast.stream_server import StreamServer


def _request(stream_id: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/stream/{stream_id}",
            "query_string": b"",
            "headers": [],
            "client": ("192.168.0.128", 51000),
        }
    )


def _server(*stream_ids: str) -> StreamServer:
    server = StreamServer()
    for stream_id in stream_ids:
        server.register_stream(stream_id, StreamFormat("audio/mpeg", "mp3", None))
    return server


@pytest.mark.asyncio
async def test_unregistered_variant_serves_the_receivers_stream():
    server = _server("4d1353bfc104-q1")

    response = await server._serve_stream(_request("4d1353bfc104-L"), "4d1353bfc104-L")

    assert response.media_type == "audio/mpeg"
    # The client must be registered on the stream that exists, or the
    # broadcaster never feeds it.
    assert server.client_count("4d1353bfc104-q1") == 1
    assert server.client_count("4d1353bfc104-L") == 0


@pytest.mark.asyncio
async def test_unregistered_variant_prefers_the_receivers_plain_stream():
    server = _server("4d1353bfc104", "4d1353bfc104-q1")

    await server._serve_stream(_request("4d1353bfc104-L-q1"), "4d1353bfc104-L-q1")

    assert server.client_count("4d1353bfc104") == 1
    assert server.client_count("4d1353bfc104-q1") == 0


@pytest.mark.asyncio
async def test_completely_unknown_id_is_still_a_404():
    server = _server("4d1353bfc104-q1")

    with pytest.raises(HTTPException) as excinfo:
        await server._serve_stream(_request("nobody"), "nobody")

    assert excinfo.value.status_code == 404
