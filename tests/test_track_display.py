"""Track display across DLNA, classic AirPlay and bundled AirPlay 2."""

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from micast.config import settings
from micast.dlna import DlnaService, DlnaTransportState
from micast.fnos_receiver import configuration
from micast.shairport_metadata import ShairportMetadata
from micast.xiaomi.device_manager import DeviceManager


def complete(coro):
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coro.close()
    raise AssertionError("Unexpected I/O")


async def immediate(operation, timeout):
    return await operation


def state():
    return DlnaTransportState(
        uri="http://media/song.mp3",
        session_id="new",
        metadata=("<DIDL-Lite><item><title>晴天</title><artist>周杰伦</artist></item></DIDL-Lite>"),
    )


def test_dlna_matches_once_and_owner_preserves_identity(monkeypatch):
    monkeypatch.setattr(settings, "touchscreen_lyrics", True)
    monkeypatch.setattr("micast.dlna.asyncio.wait_for", immediate)
    service = object.__new__(DlnaService)
    service.device_manager = SimpleNamespace(
        search_track=AsyncMock(return_value={"audio_id": "123"})
    )
    track = state()
    complete(service.match_track_display("r1", track))
    complete(service.match_track_display("r1", track))
    service.device_manager.search_track.assert_awaited_once_with(
        "晴天", "周杰伦", fuzzy_fallback=False
    )
    manager = object.__new__(DeviceManager)
    manager.bridge = SimpleNamespace(dlna_service=SimpleNamespace(states={"r1": track}))
    assert manager._display_audio_id("dlna:r1", SimpleNamespace(identity="new")) == "123"
    assert manager._display_audio_id("dlna:r1", SimpleNamespace(identity="old")) is None
    assert manager._display_audio_id("dlna:r2", None) is None


def test_dlna_stale_match_cannot_overwrite_next_track(monkeypatch):
    monkeypatch.setattr(settings, "touchscreen_lyrics", True)
    monkeypatch.setattr("micast.dlna.asyncio.wait_for", immediate)
    track = state()

    async def search(*args, **kwargs):
        track.session_id = "replacement"
        return {"audio_id": "old-song"}

    service = object.__new__(DlnaService)
    service.device_manager = SimpleNamespace(search_track=search)
    complete(service.match_track_display("r1", track))
    assert track.display_audio_id == ""


def test_dlna_disabled_or_missing_match_does_not_break_audio(monkeypatch):
    monkeypatch.setattr("micast.dlna.asyncio.wait_for", immediate)
    service = object.__new__(DlnaService)
    service.device_manager = SimpleNamespace(search_track=AsyncMock(return_value=None))
    track = state()
    monkeypatch.setattr(settings, "touchscreen_lyrics", True)
    complete(service.match_track_display("r1", track))
    assert track.display_audio_id == ""
    track.display_audio_id = "previous"
    monkeypatch.setattr(settings, "touchscreen_lyrics", False)
    complete(service.match_track_display("r1", track))
    assert track.display_audio_id == ""


def packet(kind, code, value=b""):
    return (
        f"<item><type>{kind.encode().hex()}</type><code>{code.encode().hex()}</code>"
        f'<length>{len(value)}</length><data encoding="base64">'
        f"{base64.b64encode(value).decode()}</data></item>"
    ).encode()


def test_shairport_fragmented_metadata_and_artwork():
    source = ShairportMetadata()
    data = (
        packet("ssnc", "mdst")
        + packet("core", "minm", "晴天".encode())
        + packet("core", "asar", "周杰伦".encode())
        + packet("ssnc", "mden")
    )
    for index in range(0, len(data), 13):
        source.feed(data[index : index + 13])
    assert source.daap_meta["title"] == "晴天"
    assert source.daap_meta["artist"] == "周杰伦"
    assert len(source.daap_events) == 1
    source.feed(packet("ssnc", "PICT", b"\x89PNGcover"))
    assert source.artwork_content_type == "image/png"
    assert source.artwork_rev == 1
    source.feed(packet("ssnc", "mdst") + packet("core", "minm", b"Next") + packet("ssnc", "mden"))
    assert source.daap_meta["title"] == "Next"
    assert source.daap_meta["artist"] == ""


def test_shairport_bad_metadata_is_bounded():
    source = ShairportMetadata()
    source.feed(b"<item><type>not-hex</type></item>")
    source.feed(b"x" * (4 * 1024 * 1024 + 1))
    assert source._buffer == b""
    assert source.daap_events == []


def test_shairport_configuration_uses_private_pipe(monkeypatch):
    monkeypatch.setenv("MICAST_METADATA_PIPE", "/tmp/private/metadata")
    config = configuration(7000, "MiCast", "/callback", "/python", "airplay2")
    assert 'pipe_name = "/tmp/private/metadata"' in config
    assert 'include_cover_art = "yes"' in config
    assert 'output_backend = "stdout"' in config


def test_actual_speaker_play_and_recovery_keep_audio_id(monkeypatch):
    from micast.playback_sessions import PlaybackSessions

    monkeypatch.setattr(settings, "touchscreen_lyrics", True)
    manager = DeviceManager(SimpleNamespace())
    manager.refresh_service = AsyncMock(return_value=True)
    api = SimpleNamespace(play_music_url=AsyncMock(), play_url=AsyncMock())
    manager.cloud_api = lambda did: api
    manager._start_watchdog = lambda did: None
    manager.sessions = PlaybackSessions(lambda: 60)
    manager.sessions.begin("dlna:r1", "dlna", "new")
    manager.bridge = SimpleNamespace(dlna_service=SimpleNamespace(states={"r1": state()}))
    manager.bridge.dlna_service.states["r1"].display_audio_id = "matched-song"
    for url in ("http://local/dlna-file/token", "http://local/stream/dlna:r1-q1"):
        assert complete(manager.play_stream("speaker", url, owner="dlna:r1", force=True))
        assert api.play_music_url.await_args.kwargs["audio_id"] == "matched-song"
    manager.bridge.lyrics_matched = {"airplay2": "ap2-song"}
    assert complete(
        manager.play_stream("speaker", "http://local/ap2", owner="airplay2", force=True)
    )
    assert api.play_music_url.await_args.kwargs["audio_id"] == "ap2-song"


def test_airplay2_metadata_is_in_status_and_metadata_source():
    from micast.audio_bridge import AudioBridge

    bridge = object.__new__(AudioBridge)
    bridge._local_provider = SimpleNamespace(receivers={})
    source = ShairportMetadata()
    source.note("ssnc", "mdst", b"")
    source.note("core", "minm", b"Song")
    source.note("ssnc", "mden", b"")
    source.note("ssnc", "PICT", b"jpeg")
    bridge._airplay2_sources = {"airplay2": SimpleNamespace(metadata=source)}
    assert bridge.local_server("airplay2") is None
    assert bridge.metadata_source("airplay2") is source
    track = bridge._now_playing()["airplay2"]
    assert track["title"] == "Song"
    assert track["cover"]["url"] == "api/playback/cover/airplay2"


def test_next_dlna_track_without_metadata_clears_previous_display(monkeypatch):
    monkeypatch.setattr(settings, "touchscreen_lyrics", True)
    track = state()
    track.display_audio_id = "old-song"
    track.display_cover_url = "http://old/cover"
    track.display_match_key = ("http://old/song", "Old", "Artist")
    track.metadata = ""
    service = object.__new__(DlnaService)
    service.device_manager = SimpleNamespace(search_track=AsyncMock())
    complete(service.match_track_display("r1", track))
    assert track.display_audio_id == ""
    assert track.display_cover_url == ""
    service.device_manager.search_track.assert_not_awaited()


def test_dlna_normalizes_title_artist_suffix(monkeypatch):
    monkeypatch.setattr(settings, "touchscreen_lyrics", True)
    monkeypatch.setattr("micast.dlna.asyncio.wait_for", immediate)
    track = state()
    track.metadata = track.metadata.replace("晴天</title>", "晴天 - 周杰伦</title>")
    service = object.__new__(DlnaService)
    service.device_manager = SimpleNamespace(
        search_track=AsyncMock(return_value={"audio_id": "123"})
    )
    complete(service.match_track_display("r1", track))
    service.device_manager.search_track.assert_awaited_once_with(
        "晴天", "周杰伦", fuzzy_fallback=False
    )


@pytest.mark.parametrize("processed", [False, True])
def test_dlna_matches_before_cached_or_dsp_output(monkeypatch, processed):
    from micast.dlna_delivery import DlnaDelivery

    monkeypatch.setattr(settings, "touchscreen_lyrics", True)
    monkeypatch.setattr("micast.dlna.asyncio.wait_for", immediate)
    monkeypatch.setattr("micast.dlna_delivery.asyncio.current_task", lambda: object())

    class Timeout:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("micast.dlna_delivery.asyncio.timeout", lambda seconds: Timeout())
    track = state()
    service = object.__new__(DlnaService)
    service._positions = {}
    service.device_manager = SimpleNamespace(
        search_track=AsyncMock(return_value={"audio_id": "123"})
    )
    entry = SimpleNamespace(wait=AsyncMock(return_value="downloaded.mp3"))
    service.cache = SimpleNamespace(owners={}, prepare=AsyncMock(return_value=entry))
    service.sessions = SimpleNamespace(current=lambda owner: None)
    served = []

    async def output(*args, **kwargs):
        served.append(track.display_audio_id)

    service.media = SimpleNamespace(play=AsyncMock(side_effect=output))
    delivery = DlnaDelivery(service)
    delivery.needs_processing = lambda rid, state: processed
    delivery._start_proxy = AsyncMock(side_effect=output)
    complete(delivery.play("r1", track))
    assert served == ["123"]
    assert service.media.play.await_count == int(processed)
    assert delivery._start_proxy.await_count == int(not processed)
