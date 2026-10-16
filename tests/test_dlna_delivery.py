"""Unified downloaded-source delivery, protocol state and cancellation."""

import asyncio
from types import SimpleNamespace

import pytest

from micast.config import ReceiverConfig, SpeakerGroupConfig, settings
from micast.dlna import DlnaService
from micast.media_cache import MediaDownload

DLNA_MARKERS = ("transferMode.dlna.org: Streaming", "getContentFeatures.dlna.org: 1")


@pytest.fixture(autouse=True)
def downloaded_media(monkeypatch, tmp_path):
    async def download(entry):
        entry.path = tmp_path / entry.token
        entry.path.write_bytes(b"fake-audio")
        entry.size = 10
        entry.cache.bytes += entry.size
        entry.mime = "audio/mpeg"
        entry.ready.set()

    monkeypatch.setattr(MediaDownload, "_download", download)


class FakeDeviceManager:
    def __init__(self):
        self.calls = []
        self._owners = {}
        self.reject = False

    async def play_stream(self, did, url, owner=None, force=False):
        self.calls.append(("play", did, url, owner, force))
        if self.reject:
            return False
        if owner is not None:
            self._owners[did] = owner
        return True

    async def stop(self, did, owner=None):
        self.calls.append(("pause", did, owner))

    async def stop_playback(self, did, owner=None):
        self.calls.append(("stop", did, owner))

    async def set_volume(self, did, volume):
        self.calls.append(("volume", did, volume))
        return volume

    def owner_of(self, did):
        return self._owners.get(did)

    def owned_targets(self, receiver_id, owner):
        return [
            did for did in settings.receiver_targets(receiver_id) if self._owners.get(did) == owner
        ]


class FakeMedia:
    """Stands in for MediaPlayback on the local-decode path."""

    def __init__(self):
        self.bridge = SimpleNamespace()
        self.plays = []
        self.seeks = []
        self.fail = False

    async def play(self, receiver_id, state, seek_seconds=0, resume=False):
        if self.fail:
            raise ValueError("无法读取媒体音频")
        self.plays.append((receiver_id, state.uri, seek_seconds, resume))

    async def seek_position(self, receiver_id, state, seconds):
        self.seeks.append((receiver_id, seconds))

    async def cancel_pending(self, receiver_id):
        pass

    def position(self, receiver_id):
        return 0

    def set_position(self, receiver_id, seconds):
        pass

    def set_volume(self, receiver_id, state):
        pass


def configure(monkeypatch, delivery="auto"):
    monkeypatch.setattr(settings, "sender_volume_mode", "linked")
    monkeypatch.setattr(settings, "default_volume_enabled", False)
    receivers = [
        ReceiverConfig(
            id="living", name="客厅", target_type="speaker", target_id="a", dlna_delivery=delivery
        ),
    ]
    monkeypatch.setattr(settings, "receivers", receivers)
    monkeypatch.setattr(settings, "groups", [SpeakerGroupConfig(id="all", name="全屋")])
    monkeypatch.setattr(settings, "dlna_enabled", True)
    monkeypatch.setattr(settings, "sync_groups_enabled", True)


ART_DIDL = """
    <DIDL-Lite><item><title>谁明浪子心</title><artist>王杰</artist>
    <albumArtURI>http://192.168.0.13:10246/MDEServer/art.jpg</albumArtURI>
    <res duration="0:04:27">http://192.168.0.13:10246/MDEServer/1000.mp3</res></item></DIDL-Lite>
"""


def test_decoder_open_uses_hard_open_timeout(monkeypatch):
    import micast.media_source as media_source

    calls = []

    def fake_open(url, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("boom")

    monkeypatch.setattr(media_source.av, "open", fake_open)
    source = media_source.MediaPCMSource("http://media.local/song.mp3")
    source._decode()
    assert calls and calls[0]["timeout"] == (
        media_source.OPEN_TIMEOUT_SECONDS,
        None,
    )
    assert source.error is not None


async def test_dlna_cover_uses_micast_endpoint_contract(monkeypatch):
    configure(monkeypatch)
    service = DlnaService(FakeDeviceManager())
    await service.set_uri("living", "http://192.168.0.13:10246/MDEServer/1000.mp3", ART_DIDL)
    lease = service.sessions.begin("dlna:living", "dlna", service.state_for("living").session_id)

    track = service.now_playing()["dlna:living"]
    assert track["cover"] == {
        "url": "api/playback/cover/dlna:living",
        "rev": service.state_for("living").session_id,
    }
    assert service.cover_artwork("living") == "http://192.168.0.13:10246/MDEServer/art.jpg"
    assert service.cover_artwork("other") == ""
    await service.sessions.close(lease.token)


async def test_cover_endpoint_proxies_controller_artwork(monkeypatch):
    from types import SimpleNamespace

    from micast.routes import playback as playback_routes

    configure(monkeypatch)
    service = DlnaService(FakeDeviceManager())
    await service.set_uri("living", "http://192.168.0.13:10246/MDEServer/1000.mp3", ART_DIDL)
    bridge = SimpleNamespace(
        local_server=lambda receiver_id: None,
        track_metadata=None,
        dlna_service=service,
    )

    fetched = {}

    async def fake_get(self, url, headers=None, **kwargs):
        fetched["url"] = url
        fetched["ua"] = (headers or {}).get("User-Agent")

        class Result:
            status_code = 200
            content = b"JPEG-BYTES"
            headers = {"content-type": "image/jpeg"}

        return Result()

    monkeypatch.setattr(playback_routes.httpx.AsyncClient, "get", fake_get)
    # Same idiom as test_mute: grab the freshly installed endpoint from the
    # shared router and call it directly (a second FastAPI app would see the
    # first install's closures instead).
    old_routes = list(playback_routes.router.routes)
    try:
        playback_routes.install(bridge, FakeDeviceManager())
        endpoint = [
            route.endpoint
            for route in playback_routes.router.routes
            if route.path == "/api/playback/cover/{receiver_id}"
        ][-1]
        ok = await endpoint("dlna:living")
        assert ok.status_code == 200
        assert ok.body == b"JPEG-BYTES"
        assert ok.media_type == "image/jpeg"
        assert fetched["url"] == "http://192.168.0.13:10246/MDEServer/art.jpg"
        assert "MiCast" in fetched["ua"]
        with pytest.raises(Exception) as missing:
            await endpoint("dlna:unknown")
        assert getattr(missing.value, "status_code", None) == 404
    finally:
        playback_routes.router.routes[:] = old_routes


def test_decoder_sends_dlna_fingerprint(monkeypatch):
    import micast.media_source as media_source

    calls = []

    def fake_open(url, **kwargs):
        calls.append(kwargs)
        if "metadata_errors" in kwargs:
            raise TypeError("open() got an unexpected keyword argument 'metadata_errors'")

        class Boom:
            def __enter__(self):
                raise RuntimeError("boom")

            def __exit__(self, *args):
                return False

        return Boom()

    monkeypatch.setattr(media_source.av, "open", fake_open)
    source = media_source.MediaPCMSource("http://192.168.0.13:10246/MDEServer/x/1000.mp3")
    source._decode()
    options = calls[-1]["options"]
    assert options["user_agent"] == "Windows Media Player/12.0.7601.17514"
    headers = options["headers"]
    for marker in DLNA_MARKERS:
        assert marker in headers, f"missing {marker!r} in {headers!r}"


def test_proxy_pump_sends_dlna_fingerprint(monkeypatch):
    import micast.audio_encoder as audio_encoder

    captured = {}

    def fake_open_input(url, *, options=None, timeout=None):
        captured["options"] = options
        raise RuntimeError("boom")

    monkeypatch.setattr(audio_encoder, "open_input", fake_open_input)
    errors = []
    audio_encoder.stream_media_as_mp3(
        "http://192.168.0.13:10246/MDEServer/x/1000.mp3",
        0,
        lambda chunk: None,
        __import__("threading").Event(),
        "unused-ua",
        errors=errors,
    )
    assert captured["options"]["user_agent"] == "Windows Media Player/12.0.7601.17514"
    headers = captured["options"]["headers"]
    for marker in DLNA_MARKERS:
        assert marker in headers, f"missing {marker!r} in {headers!r}"
    assert errors, "open failure must be recorded in errors"


@pytest.mark.parametrize("preference", ["auto", "direct"])
async def test_plain_mp3_uses_local_short_address_without_probe(monkeypatch, preference):
    configure(monkeypatch, preference)
    manager = FakeDeviceManager()
    service = DlnaService(manager, media=FakeMedia())
    try:
        uri = "http://media.local/song.mp3?signature=" + "x" * 500
        await service.set_uri("living", uri)
        await service.play("living")
        plays = [c for c in manager.calls if c[0] == "play"]
        assert len(plays) == 1 and "/dlna-file/" in plays[0][2]
        assert "signature" not in plays[0][2] and "media.local" not in plays[0][2]
        assert service.delivery.modes["living"] == "proxy"
    finally:
        await service.stop()
    assert service.cache.bytes == 0 and not service.cache.entries


@pytest.mark.parametrize("volume_mode", ["independent", "linked"])
async def test_processing_reads_shared_file_and_keeps_volume_live(monkeypatch, volume_mode):
    configure(monkeypatch, "local")
    monkeypatch.setattr(settings, "sender_volume_mode", volume_mode)
    media = FakeMedia()
    service = DlnaService(FakeDeviceManager(), media=media)
    await service.set_uri("living", "http://media.local/song.mp3")
    entry = service.cache.owners["living"]
    try:
        await service.play("living")
        assert service.state_for("living").source_path == str(entry.path)
        assert service.delivery.modes["living"] == "local"
        assert len(media.plays) == 1
        await service.set_volume("living", 21)
        assert service.state_for("living").volume == 21
    finally:
        await service.stop()
    assert not entry.path


async def test_decode_failure_never_sends_remote_url(monkeypatch):
    configure(monkeypatch, "local")
    media = FakeMedia()
    media.fail = True
    manager = FakeDeviceManager()
    service = DlnaService(manager, media=media)
    try:
        await service.set_uri("living", "http://media.local/song.mp3")
        with pytest.raises(ValueError, match="无法读取"):
            await service.play("living")
        assert not any(c[0] == "play" for c in manager.calls)
        assert service.state_for("living").state == "STOPPED"
    finally:
        await service.stop()


@pytest.mark.parametrize("action", ["stop_playback", "pause", "set_uri", "stop"])
async def test_slow_download_play_acknowledges_and_cancels_without_resurrection(
    monkeypatch, action
):
    from micast.routes.dlna import _dispatch

    configure(monkeypatch, "local")
    gate = asyncio.Event()
    original = MediaDownload._download

    async def delayed(entry):
        await gate.wait()
        await original(entry)

    monkeypatch.setattr(MediaDownload, "_download", delayed)
    manager = FakeDeviceManager()
    service = DlnaService(manager, media=FakeMedia())
    await service.set_uri("living", "http://media.local/old.mp3")
    await asyncio.wait_for(_dispatch(service, "living", "AVTransport", "Play", b""), 0.2)
    assert service.state_for("living").state == "TRANSITIONING"
    task = service._play_tasks["living"]
    await asyncio.sleep(0)
    if action == "set_uri":
        await service.set_uri("living", "http://media.local/new.mp3")
    elif action == "stop":
        await service.stop()
    else:
        await getattr(service, action)("living")
    gate.set()
    await asyncio.gather(task, return_exceptions=True)
    assert not any(c[0] == "play" for c in manager.calls)
    assert service.state_for("living").state != "PLAYING"
    assert not service._play_tasks
    await service.stop()


async def test_seek_reuses_download_and_enables_processing(monkeypatch):
    configure(monkeypatch)
    media = FakeMedia()
    service = DlnaService(FakeDeviceManager(), media=media)
    try:
        await service.set_uri("living", "http://media.local/song.mp3")
        await service.play("living")
        entry = service.cache.owners["living"]
        await entry.wait()
        path = entry.path
        await service.seek("living", 42)
        assert service.cache.owners["living"] is entry and entry.path == path
        assert media.plays[-1][2:] == (42, True)
        assert service.delivery.modes["living"] == "local"
    finally:
        await service.stop()


async def test_failed_seek_preserves_previous_speaker_url(monkeypatch):
    configure(monkeypatch)
    media = FakeMedia()
    service = DlnaService(FakeDeviceManager(), media=media)
    try:
        await service.set_uri("living", "http://media.local/song.mp3")
        await service.play("living")
        entry = service.cache.owners["living"]
        token, identity = entry.token, entry.identity
        media.fail = True
        with pytest.raises(ValueError):
            await service.seek("living", 42)
        assert service.cache.entries.get(token) is entry
        assert entry.token == token and entry.identity == identity
        assert service.state_for("living").session_id == identity
        assert service.delivery.modes["living"] == "proxy"
    finally:
        await service.stop()


async def test_repeated_play_while_preparing_creates_one_task(monkeypatch):
    configure(monkeypatch, "local")
    service = DlnaService(FakeDeviceManager(), media=FakeMedia())
    try:
        await service.set_uri("living", "http://media.local/song.mp3")
        service.request_play("living")
        task = service._play_tasks["living"]
        service.request_play("living")
        assert service._play_tasks["living"] is task
        await task
        assert len(service.media.plays) == 1
    finally:
        await service.stop()


async def test_replacement_during_seek_cannot_restore_old_identity_or_token(monkeypatch):
    configure(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    class SeekingMedia(FakeMedia):
        async def play(self, *args, **kwargs):
            entered.set()
            await release.wait()

    service = DlnaService(FakeDeviceManager(), media=SeekingMedia())
    try:
        await service.set_uri("living", "http://media.local/old.mp3")
        await service.play("living")
        old_entry = service.cache.owners["living"]
        seek = asyncio.create_task(service.seek("living", 42))
        await asyncio.wait_for(entered.wait(), 1)
        await service.set_uri("living", "http://media.local/new.mp3")
        new_identity = service.state_for("living").session_id
        release.set()
        with pytest.raises(ValueError, match="定位请求已被替换"):
            await seek
        assert service.state_for("living").session_id == new_identity
        assert old_entry.closed and old_entry.token not in service.cache.entries
        assert len(service.cache.entries) == 1
    finally:
        release.set()
        await service.stop()


async def test_removing_receiver_cancels_pending_download_and_play(monkeypatch):
    from unittest.mock import AsyncMock

    configure(monkeypatch, "local")
    gate = asyncio.Event()
    original = MediaDownload._download

    async def pending(entry):
        await gate.wait()
        await original(entry)

    monkeypatch.setattr(MediaDownload, "_download", pending)
    service = DlnaService(FakeDeviceManager(), media=FakeMedia())
    try:
        await service.set_uri("living", "http://media.local/song.mp3")
        service.request_play("living")
        await asyncio.sleep(0)
        service._transport = object()
        service.announce = AsyncMock()
        monkeypatch.setattr(settings, "receivers", [])
        await service.reconcile()
        assert not service.cache.entries and not service._play_tasks and not service.states
    finally:
        service._transport = None
        gate.set()
        await service.stop()
