"""Real HTTP origin -> shared download -> speaker-facing Range endpoint."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from aiohttp import web

from micast.config import ReceiverConfig, settings
from micast.dlna import DlnaService
from micast.media_cache import MediaCache
from micast.routes.dlna import _dispatch
from micast.stream_server import StreamServer


@asynccontextmanager
async def origin(handler):
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


def setup_service(monkeypatch):
    monkeypatch.setattr(
        settings,
        "receivers",
        [ReceiverConfig(id="r", name="r", target_type="speaker", target_id="s")],
    )
    monkeypatch.setattr(settings, "groups", [])
    monkeypatch.setattr(settings, "speakers", [])
    monkeypatch.setattr(settings, "dlna_enabled", True)
    monkeypatch.setattr(settings, "sender_volume_mode", "linked")
    monkeypatch.setattr(settings, "default_volume_enabled", False)
    manager = SimpleNamespace(
        play_stream=AsyncMock(return_value=True),
        stop_playback=AsyncMock(),
        stop=AsyncMock(),
        owner_of=lambda did: "dlna:r",
    )
    service = DlnaService(manager)
    server = StreamServer()
    server.media_cache = service.cache
    server.media_session = service.media_token
    server.sessions = service.sessions
    service.stream_server = server
    return service, server, manager


async def test_download_once_serves_head_ranges_and_revokes_on_stop(monkeypatch):
    requests = []
    payload = b"ID3" + bytes(range(256)) * 20

    async def serve(request):
        requests.append((request.method, request.path, dict(request.headers)))
        if request.path == "/redirect.mp3":
            raise web.HTTPFound("/song.mp3")
        return web.Response(body=payload, content_type="audio/mpeg")

    service, server, manager = setup_service(monkeypatch)
    async with origin(serve) as base:
        try:
            await service.set_uri("r", base + "/redirect.mp3?private=secret")
            entry = service.cache.owners["r"]
            await entry.wait()  # starts before Play
            await service.play("r")
            address = manager.play_stream.call_args.args[1]
            assert "/dlna-file/" in address and base not in address
            path = "/dlna-file/" + entry.token
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=server._app), base_url="http://speaker"
            ) as client:
                result = await client.get(path, headers={"Range": "bytes=2-10"})
                assert result.status_code == 206 and result.content == payload[2:11]
                assert result.headers["content-range"] == f"bytes 2-10/{len(payload)}"
                tail = await client.get(path, headers={"Range": "bytes=-5"})
                assert tail.content == payload[-5:]
                head = await client.head(path)
                assert head.status_code == 200 and not head.content
                assert int(head.headers["content-length"]) == len(payload)
                invalid = await client.get(path, headers={"Range": "bytes=999999-"})
                assert invalid.status_code == 416
                assert (await client.get(path)).content == payload
                await service.stop_playback("r")
                assert (await client.get(path)).status_code == 404
            assert len(requests) == 2 and all(r[0] == "GET" for r in requests)
            assert not entry.path and service.cache.bytes == 0
        finally:
            await service.stop()


async def test_slow_origin_does_not_block_soap_or_report_stopped(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()

    async def serve(request):
        entered.set()
        await release.wait()
        return web.Response(body=b"ID3music", content_type="audio/mpeg")

    service, server, manager = setup_service(monkeypatch)
    async with origin(serve) as base:
        try:
            await service.set_uri("r", base + "/song.mp3")
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(_dispatch(service, "r", "AVTransport", "Play", b""), 0.2)
            task = service._play_tasks["r"]
            info = await _dispatch(service, "r", "AVTransport", "GetTransportInfo", b"")
            assert info["CurrentTransportState"] == "TRANSITIONING"
            await asyncio.sleep(0.02)
            assert not task.done()
            await service.stop_playback("r")
            release.set()
            await task
            assert service.state_for("r").state == "STOPPED"
            assert not service.cache.entries and service.cache.bytes == 0
            assert all("/dlna-file/" in c.args[1] for c in manager.play_stream.call_args_list)
        finally:
            release.set()
            await service.stop()


@pytest.mark.parametrize("mode", ["rejected", "oversize", "html", "empty", "truncated"])
async def test_download_failure_is_explicit_and_cleans_cache(monkeypatch, mode):
    count = 0

    async def serve(request):
        nonlocal count
        count += 1
        if mode == "rejected":
            return web.Response(status=403)
        if mode == "html":
            return web.Response(text="<html>login</html>", content_type="text/html")
        if mode == "oversize":
            return web.Response(body=b"x" * 20, content_type="audio/mpeg")
        if mode == "empty":
            return web.Response(body=b"", content_type="audio/mpeg")
        response = web.StreamResponse(
            headers={"Content-Type": "audio/mpeg", "Content-Length": "100"}
        )
        await response.prepare(request)
        await response.write(b"short")
        request.transport.close()
        return response

    cache = MediaCache(file_limit=10)
    async with origin(serve) as base:
        try:
            entry = await cache.prepare("r", base + "/song.mp3", "one")
            with pytest.raises(ValueError, match="下载失败"):
                await asyncio.wait_for(entry.wait(), 2)
            assert entry.error and entry.path is None and cache.bytes == 0
            assert count == (2 if mode == "rejected" else 1)
        finally:
            await cache.close()


async def test_same_track_is_reused_replacement_revokes_old_and_capacity_is_bounded():
    gate = asyncio.Event()

    async def serve(request):
        await gate.wait()
        return web.Response(body=b"ID3music", content_type="audio/mpeg")

    cache = MediaCache(entry_limit=1)
    async with origin(serve) as base:
        try:
            first = await cache.prepare("r", base + "/one.mp3", "one")
            assert await cache.prepare("r", base + "/one.mp3", "one") is first
            with pytest.raises(ValueError, match="过多"):
                await cache.prepare("other", base + "/other.mp3", "other")
            second = await cache.prepare("r", base + "/two.mp3", "two")
            assert first.closed and first.task.done() and first.token not in cache.entries
            gate.set()
            await second.wait()
            assert len(cache.entries) == 1 and cache.bytes == 8
        finally:
            gate.set()
            await cache.close()
    assert not cache.entries and cache.bytes == 0


@pytest.mark.parametrize("format", ["wav", "mp3", "flac"])
async def test_real_http_file_enters_existing_pcm_pipeline(monkeypatch, tmp_path, format):
    from micast.media_cache import MediaDownload
    from micast.media_source import MediaPCMSource
    from tests.test_media_playback import setup_media, wait_until

    original_download = MediaDownload._download
    media, bridge, manager, state = setup_media(monkeypatch, tmp_path, seconds=0.4)
    manager.stop = AsyncMock()
    monkeypatch.setattr(MediaDownload, "_download", original_download)
    monkeypatch.setattr("micast.media_playback.MediaPCMSource", MediaPCMSource)
    monkeypatch.setattr(settings, "dlna_enabled", True)
    monkeypatch.setattr(settings, "default_volume_enabled", False)
    payload = (tmp_path / "source.wav").read_bytes()
    if format != "wav":
        import io

        import av

        converted = io.BytesIO()
        with (
            av.open(io.BytesIO(payload)) as source,
            av.open(converted, "w", format=format) as output,
        ):
            stream = output.add_stream("libmp3lame" if format == "mp3" else "flac", rate=48000)
            stream.layout = "stereo"
            for frame in source.decode(audio=0):
                for packet in stream.encode(frame):
                    output.mux(packet)
            for packet in stream.encode(None):
                output.mux(packet)
        payload = converted.getvalue()
    requests = []

    async def serve(request):
        requests.append(request.path)
        return web.Response(body=payload, content_type=f"audio/{format}")

    service = DlnaService(manager, bridge.sessions, media)
    async with origin(serve) as base:
        try:
            await service.set_uri("r", base + f"/source.{format}")
            await service.play("r")
            entry = service.cache.owners["r"]
            source = media.sources["dlna:r"]
            assert source.url == str(entry.path) and not source.url.startswith("http")
            assert service.delivery.modes["r"] == "local"
            await wait_until(lambda: source.position > 0.1)
            await service.pause("r")
            await service.seek("r", 0.2)
            await service.play("r")
            assert len(requests) == 1  # resume/seek never re-downloads
            assert media.sources["dlna:r"].seek_seconds == 0.2
        finally:
            await service.stop()
    assert not service.cache.entries and not media.sources and not bridge._pipelines


async def test_rejected_fingerprint_retries_same_download_without_remote_delivery():
    agents = []

    async def serve(request):
        agents.append(request.headers.get("User-Agent"))
        if len(agents) == 1:
            return web.Response(status=406)
        return web.Response(body=b"ID3music", content_type="audio/mpeg")

    cache = MediaCache()
    async with origin(serve) as base:
        try:
            entry = await cache.prepare("r", base + "/song.mp3", "one")
            await entry.wait()
            assert len(agents) == 2 and agents[0] != agents[1]
            assert entry.path.read_bytes() == b"ID3music"
        finally:
            await cache.close()


async def test_stop_cancels_speaker_http_wait_and_download(monkeypatch):
    service, server, manager = setup_service(monkeypatch)
    gate = asyncio.Event()

    async def serve(request):
        await gate.wait()
        return web.Response(body=b"ID3audio", content_type="audio/mpeg")

    async with origin(serve) as base:
        try:
            await service.set_uri("r", base + "/song.mp3")
            service.request_play("r")
            entry = service.cache.owners["r"]
            while not manager.play_stream.await_count:
                await asyncio.sleep(0)
            lease = service.sessions.current("dlna:r")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=server._app), base_url="http://speaker"
            ) as client:
                waiting = asyncio.create_task(client.get("/dlna-file/" + entry.token))
                async with asyncio.timeout(1):
                    while not any(k.startswith("cached-request:") for k in lease.resources):
                        await asyncio.sleep(0)
                await asyncio.wait_for(service.stop_playback("r"), 1)
                result = await asyncio.gather(waiting, return_exceptions=True)
                assert isinstance(result[0], asyncio.CancelledError)
                assert entry.task.done() and not service.cache.entries
                assert not lease.resources
        finally:
            gate.set()
            await service.stop()
