"""Track metadata subsystem: registry, library search, now_playing, cover art."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from micast.audio_bridge import AudioBridge
from micast.playback_orchestrator import PlaybackOrchestrator
from micast.routes import playback as playback_routes
from micast.track_metadata import TrackMetadataRegistry
from micast.xiaomi.mina_api import MinaAPI

# --- TrackMetadataRegistry -------------------------------------------------


def test_registry_set_get_drop_clear():
    reg = TrackMetadataRegistry()
    reg.set_library_match(
        "r1", audio_id="a1", cover_url="http://x/c.jpg", duration=235, updated_at=1.0
    )
    entry = reg.enrichment_for("r1")
    assert entry.audio_id == "a1"
    assert entry.cover_url == "http://x/c.jpg"
    assert entry.duration == 235
    # Unknown receiver reads as empty enrichment, never as an error.
    assert reg.enrichment_for("nope").audio_id == ""
    # Re-match updates fields; an absent duration keeps the previous one.
    reg.set_library_match("r1", audio_id="a2", updated_at=2.0)
    entry = reg.enrichment_for("r1")
    assert entry.audio_id == "a2"
    assert entry.cover_url == "http://x/c.jpg"
    assert entry.duration == 235
    reg.drop("r1")
    assert reg.enrichment_for("r1").audio_id == ""
    reg.set_library_match("r2", audio_id="a3")
    reg.clear()
    assert reg.enrichment_for("r2").audio_id == ""


# --- MinaAPI.search_track --------------------------------------------------


def _song(name, artist, audio_id, cover_url="", duration=0):
    return {
        "name": name,
        "artist": {"name": artist},
        "audioID": audio_id,
        "coverURL": cover_url,
        "duration": duration,
    }


class _FakeMinaService:
    def __init__(self, payload):
        self._payload = payload

    def mina_request(self, uri, data):
        async def _go():
            return self._payload

        return _go()


def _api(payload) -> MinaAPI:
    return MinaAPI(_FakeMinaService(payload), device_id="")


@pytest.mark.asyncio
async def test_search_track_exact_hit_carries_cover_and_duration():
    payload = {
        "data": {
            "songList": [
                _song("晴天", "郭衡 (Guo Heng)", "111", cover_url="http://c1.jpg"),
                _song("晴天", "周杰伦", "222", cover_url="http://c2.jpg", duration=267),
            ]
        }
    }
    hit = await _api(payload).search_track("晴天", "周杰伦")
    assert hit == {"audio_id": "222", "cover_url": "http://c2.jpg", "duration": 267}


@pytest.mark.asyncio
async def test_search_track_fuzzy_fallback_takes_first_entry():
    payload = {"data": {"songList": [_song("别的歌", "别人", "999", cover_url="http://c9.jpg")]}}
    hit = await _api(payload).search_track("不存在的歌", "", fuzzy_fallback=True)
    assert hit["audio_id"] == "999"
    # Strict mode (lyrics watcher) rejects it.
    assert await _api(payload).search_track("不存在的歌", "", fuzzy_fallback=False) is None


@pytest.mark.asyncio
async def test_search_track_no_results_and_error():
    assert await _api({"data": {"songList": []}}).search_track("晴天", "周杰伦") is None
    assert await _api(None).search_track("晴天", "周杰伦") is None

    class _Boom:
        def mina_request(self, uri, data):
            async def _go():
                raise TimeoutError("cloud dead")

            return _go()

    assert await MinaAPI(_Boom(), device_id="").search_track("晴天", "周杰伦") is None


@pytest.mark.asyncio
async def test_search_audio_id_facade_still_returns_str():
    payload = {"data": {"songList": [_song("晴天", "周杰伦", "222", cover_url="http://c2.jpg")]}}
    assert await _api(payload).search_audio_id("晴天", "周杰伦") == "222"
    assert await _api({"data": {"songList": []}}).search_audio_id("晴天", "周杰伦") == ""


# --- AudioBridge._now_playing composition ----------------------------------


def _fake_bridge(servers, registry=None, matched=None):
    receivers = {rid: SimpleNamespace(server=server) for rid, server in servers.items()}
    return SimpleNamespace(
        _local_provider=SimpleNamespace(receivers=receivers),
        track_metadata=registry,
        lyrics_matched=matched if matched is not None else {},
    )


def test_now_playing_merges_sender_metadata_and_enrichment():
    server = SimpleNamespace(
        daap_meta={"title": "晴天", "artist": "周杰伦", "album": "叶惠美", "lyric_line": "故事的小黄花"},
        lyric_lines=["故事的小黄花", "从出生那年就飘着"],
        artwork_bytes=b"",
        artwork_rev=0,
    )
    reg = TrackMetadataRegistry()
    reg.set_library_match("r1", audio_id="222", cover_url="http://c2.jpg", duration=267)
    fake = _fake_bridge({"r1": server}, registry=reg)
    now = AudioBridge._now_playing(fake)  # unbound method on the fake
    assert now["r1"] == {
        "title": "晴天",
        "artist": "周杰伦",
        "album": "叶惠美",
        "lyric_lines": ["故事的小黄花", "从出生那年就飘着"],
        "audio_id": "222",
        "duration": 267,
        "cover": {"url": "api/playback/cover/r1", "rev": "222"},
    }


def test_server_lyric_ring_dedupes_and_caps():
    """The rolling window is maintained incrementally on arrival: consecutive
    duplicates collapse and it caps at 8 — so the per-tick now_playing read
    stays O(1)."""
    import struct

    from micast.raop.server import RaopServer

    def tag(name: str, payload: bytes) -> bytes:
        return name.encode() + struct.pack(">I", len(payload)) + payload

    def body(line: str) -> bytes:
        inner = tag("minm", "风继续吹 - 张国荣".encode()) + tag("asar", line.encode())
        return tag("mlit", inner)

    # Bypass __init__ (its StreamReader needs a running loop); the ring only
    # touches these fields.
    server = object.__new__(RaopServer)
    server.name = "Test"
    server.daap_meta = {}
    server.daap_events = []
    server._daap_seq = 0
    server.lyric_lines = []
    lines = ["一句，", "一句，", "二句，", "三句，", "四句，", "五句，", "六句，", "七句，", "八句，", "九句，", "十句，"]
    for line in lines:
        server._note_dmap_metadata(body(line))
    assert server.lyric_lines == ["三句，", "四句，", "五句，", "六句，", "七句，", "八句，", "九句，", "十句，"]

    fake = _fake_bridge({"r1": server})
    now = AudioBridge._now_playing(fake)
    assert now["r1"]["lyric_lines"][-1] == "十句，"
    server._note_dmap_metadata(body("新歌第一句，").replace("风继续吹".encode(), "风继续听".encode()))
    assert server.lyric_lines == ["新歌第一句，"]



def test_now_playing_sender_artwork_sets_cover_endpoint():
    server = SimpleNamespace(
        daap_meta={},
        artwork_bytes=b"\xff\xd8\xff",
        artwork_rev=3,
    )
    fake = _fake_bridge({"r1": server})
    now = AudioBridge._now_playing(fake)
    assert now["r1"]["cover"] == {"url": "api/playback/cover/r1", "rev": "art3"}
    # No metadata, no match, no artwork -> the receiver stays out entirely.
    empty = SimpleNamespace(daap_meta={}, artwork_bytes=b"", artwork_rev=0)
    assert AudioBridge._now_playing(_fake_bridge({"r2": empty})) == {}


def test_now_playing_no_cover_source_means_none():
    server = SimpleNamespace(
        daap_meta={"title": "晴天"},
        artwork_bytes=b"",
        artwork_rev=0,
    )
    fake = _fake_bridge({"r1": server})
    now = AudioBridge._now_playing(fake)
    assert now["r1"]["cover"] is None
    # lyrics_matched mirror still feeds audio_id for pre-registry bridges.
    fake = _fake_bridge({"r1": server}, matched={"r1": "legacy-audio-id"})
    now = AudioBridge._now_playing(fake)
    assert now["r1"]["audio_id"] == "legacy-audio-id"


# --- cover endpoint ---------------------------------------------------------
#
# Like test_mute/test_volume_control: fetch the freshly installed endpoint
# closure from the global router and call it directly — include_router's
# lazy inclusion makes a TestClient round trip brittle here.


def _cover_endpoint(server, registry):
    bridge = SimpleNamespace(
        local_server=lambda rid: server if rid == "r1" else None,
        track_metadata=registry,
    )
    old_routes = list(playback_routes.router.routes)
    try:
        playback_routes.install(bridge, SimpleNamespace())
        return [
            r.endpoint
            for r in playback_routes.router.routes
            if r.path == "/api/playback/cover/{receiver_id}"
        ][-1]
    finally:
        playback_routes.router.routes[:] = old_routes


def test_cover_endpoint_serves_sender_artwork_bytes():
    server = SimpleNamespace(artwork_bytes=b"\xff\xd8\xff", artwork_content_type="image/jpeg")
    endpoint = _cover_endpoint(server, TrackMetadataRegistry())
    response = asyncio.run(endpoint("r1"))
    assert response.body == b"\xff\xd8\xff"
    assert response.media_type == "image/jpeg"
    assert response.headers["cache-control"] == "no-cache"


def test_cover_endpoint_redirects_to_library_cover():
    server = SimpleNamespace(artwork_bytes=b"", artwork_content_type="")
    reg = TrackMetadataRegistry()
    reg.set_library_match("r1", audio_id="a1", cover_url="http://cdn.example/c.jpg")
    endpoint = _cover_endpoint(server, reg)
    response = asyncio.run(endpoint("r1"))
    assert response.status_code == 302
    assert response.headers["location"] == "http://cdn.example/c.jpg"


def test_cover_endpoint_404_when_no_artwork():
    endpoint = _cover_endpoint(None, TrackMetadataRegistry())
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(endpoint("r1"))
    assert exc_info.value.status_code == 404
    with pytest.raises(HTTPException):
        asyncio.run(endpoint("unknown"))


# --- orchestrator: match lands in the registry ------------------------------


@pytest.mark.asyncio
async def test_resend_with_match_updates_registry_and_replays():
    bridge = SimpleNamespace(
        lyrics_matched={},
        is_session_active=lambda rid: True,
        _volume_modes={},
        _sender_volumes={},
        local_server=lambda rid: None,
    )
    device_manager = SimpleNamespace(
        playing_ids=lambda: ["did-1"],
        owner_of=lambda did: "r1",
        stream_url_of=lambda did: "http://x/stream",
        play_stream=AsyncMock(return_value=True),
    )
    registry = TrackMetadataRegistry()
    orchestrator = PlaybackOrchestrator(
        bridge, device_manager, lambda coro, name: None, track_metadata=registry
    )
    hit = {"audio_id": "222", "cover_url": "http://c2.jpg", "duration": 267}
    await orchestrator.resend_with_match("r1", hit)

    assert bridge.lyrics_matched["r1"] == "222"
    entry = registry.enrichment_for("r1")
    assert entry.audio_id == "222"
    assert entry.cover_url == "http://c2.jpg"
    assert entry.duration == 267
    device_manager.play_stream.assert_awaited_once()
    assert device_manager.play_stream.await_args.kwargs["audio_id"] == "222"

    # Session end drops the enrichment together with the lyrics state.
    registry.drop("r1")
    assert registry.enrichment_for("r1").audio_id == ""
