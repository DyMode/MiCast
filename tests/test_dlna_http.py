from unittest.mock import AsyncMock

import httpx
from fastapi.responses import Response

from micast.audio_encoder import StreamFormat
from micast.stream_server import StreamServer


async def test_dlna_head_reports_type_without_subscribing():
    server = StreamServer()
    server.register_stream("r", StreamFormat("audio/mpeg", "mp3", None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server._app),
                                 base_url="http://test") as client:
        response = await client.head("/stream/r/for/r/uuid%3Atv/audio.mp3")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["transfermode.dlna.org"] == "Streaming"
    assert "content-length" not in response.headers
    assert not any(server._clients.values())
    assert not server._client_delay


async def test_dlna_extension_keeps_receiver_and_sink_identity():
    server = StreamServer()
    server._serve_stream = AsyncMock(return_value=Response(b"mp3", media_type="audio/mpeg"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server._app),
                                 base_url="http://test") as client:
        response = await client.get("/stream/r-L/for/r/uuid%3Atv/audio.mp3")
    assert response.status_code == 200
    args = server._serve_stream.await_args.args
    assert args[1:] == ("r-L", "r", "uuid:tv")
