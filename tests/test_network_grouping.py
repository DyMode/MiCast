"""Group network-target delays, add_group targets, pump pre-buffer, play-error retry."""

import asyncio
from array import array

import pytest

from micast.airplay_targets import AirPlayTargetManager, extract_channel
from micast.config import Settings, SpeakerGroupConfig
from micast.xiaomi.device_manager import DeviceManager


def test_extract_channel_duplicates_side():
    # Interleaved stereo: L=1,2,3 R=10,20,30
    chunk = array("h", [1, 10, 2, 20, 3, 30]).tobytes()
    left = array("h")
    left.frombytes(extract_channel(chunk, "left"))
    assert list(left) == [1, 1, 2, 2, 3, 3]
    right = array("h")
    right.frombytes(extract_channel(chunk, "right"))
    assert list(right) == [10, 10, 20, 20, 30, 30]


def test_update_group_sanitizes_network_channels(monkeypatch):
    monkeypatch.setattr(Settings, "save_to_file", lambda self: None)
    cfg = Settings()
    cfg.groups = [SpeakerGroupConfig(id="g1", name="全屋", speaker_ids=["a", "b"])]
    group = cfg.update_group(
        "g1", network_channels={"aabbccddeeff": "left", "uuid:x": "right", "y": "up"}
    )
    assert group.network_channels == {"aabbccddeeff": "left", "uuid:x": "right"}
    assert cfg.receiver_network_channels("nope") == {}


def test_channel_variant_suffix_prefers_plain(monkeypatch):
    monkeypatch.setattr(Settings, "save_to_file", lambda self: None)
    cfg = Settings()
    cfg.groups = [
        SpeakerGroupConfig(
            id="g1",
            name="立体声",
            speaker_ids=["a", "b"],
            mode="stereo",
            channels={"a": "left", "b": "right"},
        )
    ]
    cfg.add_receiver("立体声", "group", "g1")
    rid = cfg.receivers[0].id
    assert cfg.receiver_channel_variant_suffix(rid, "left") == "-L"
    assert cfg.receiver_channel_variant_suffix(rid, "right") == "-R"
    # Mirror group has no per-channel variants → "" (plain base stream).
    cfg.groups[0].mode = "mirror"
    assert cfg.receiver_channel_variant_suffix(rid, "left") == ""


def test_delay_holds_normalizes_to_most_ahead_member(monkeypatch):
    """The one shared normalization: the most-ahead member holds 0 and the rest
    pad after it — signed offsets become non-negative holds used by both the
    Xiaomi pull path (stream server sink buffer) and the AirPlay push path.
    DLNA renderers have no delay path and are excluded."""
    monkeypatch.setattr(Settings, "save_to_file", lambda self: None)
    group = SpeakerGroupConfig(
        id="g1",
        name="全屋",
        speaker_ids=["a", "b", "c"],
        delays_ms={"a": -100, "b": 0, "c": 0},
        anchor_did="b",
    )
    assert group.delay_holds() == {"a": 0, "b": 100, "c": 100}

    # A negative offset (pull earlier) is expressed by padding the siblings.
    group = SpeakerGroupConfig(
        id="g2",
        name="全屋",
        speaker_ids=["a", "b"],
        delays_ms={"b": -200},
        anchor_did="a",
    )
    assert group.delay_holds() == {"a": 200, "b": 0}


def test_add_group_with_network_targets(monkeypatch):
    monkeypatch.setattr(Settings, "save_to_file", lambda self: None)
    cfg = Settings()
    group = cfg.add_group(
        "客厅",
        ["a", "b"],
        airplay_targets=["AABBCCDDEEFF", "junk"],
        dlna_targets=["uuid:x", " uuid:x ", ""],
    )
    assert group.airplay_targets == ["aabbccddeeff"]
    assert group.dlna_targets == ["uuid:x"]


def test_receiver_airplay_delays(monkeypatch):
    monkeypatch.setattr(Settings, "save_to_file", lambda self: None)
    cfg = Settings()
    cfg.groups = [
        SpeakerGroupConfig(
            id="g1",
            name="全屋",
            speaker_ids=["a"],
            airplay_targets=["aabbccddeeff"],
            delays_ms={"aabbccddeeff": 300},
        )
    ]
    cfg.add_receiver("全屋", "group", "g1")
    receiver_id = cfg.receivers[0].id
    assert cfg.receiver_airplay_delays(receiver_id) == {"aabbccddeeff": 300}
    assert cfg.receiver_airplay_delays("nope") == {}


def test_speaker_channel_both_pulls_base_stream(monkeypatch):
    """A stereo-group speaker assigned "both" gets the full mix: no channel
    suffix, so it pulls the plain base stream."""
    monkeypatch.setattr(Settings, "save_to_file", lambda self: None)
    cfg = Settings()
    cfg.groups = [
        SpeakerGroupConfig(
            id="g1",
            name="立体声",
            speaker_ids=["a", "b", "c"],
            mode="stereo",
            channels={"a": "left", "b": "right", "c": "both"},
        )
    ]
    cfg.add_receiver("立体声", "group", "g1")
    rid = cfg.receivers[0].id
    # "both" survives an update_group round-trip (not reassigned to a side).
    group = cfg.update_group("g1", channels={"a": "left", "b": "right", "c": "both"})
    assert group.channels["c"] == "both"
    assert cfg.stream_suffix(rid, "c") == ""
    assert cfg.stream_suffix(rid, "a") == "-L"


@pytest.mark.asyncio
async def test_prebuffer_holds_back_delay():
    reader = asyncio.StreamReader()
    reader.feed_data(b"x" * 5000)
    reader.feed_eof()
    # 10ms at 176.4 B/ms ≈ 1764 bytes (frame-aligned); nothing is sent before
    # the buffer fills — the reader must yield exactly the wanted amount.
    buffered = await AirPlayTargetManager._prebuffer(reader, 10)
    assert buffered is not None
    assert 1760 <= len(buffered) <= 1768
    # Zero delay passes through immediately with an empty buffer.
    reader2 = asyncio.StreamReader()
    reader2.feed_eof()
    assert await AirPlayTargetManager._prebuffer(reader2, 0) == b""


@pytest.mark.asyncio
async def test_prebuffer_eof_returns_none():
    reader = asyncio.StreamReader()
    reader.feed_data(b"short")
    reader.feed_eof()
    assert await AirPlayTargetManager._prebuffer(reader, 1000) is None


async def test_short_finite_clip_survives_a_longer_output_delay():
    reader = asyncio.StreamReader()
    pcm = b"\x00\x10\x00\x10" * 441
    reader.feed_data(pcm)
    reader.feed_eof()
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await AirPlayTargetManager._prebuffer(reader, 40) == pcm
    assert loop.time() - started >= 0.035


async def test_external_airplay_loudness_tracks_source_level(monkeypatch):
    from micast.airplay_targets import _Hub, _TargetRuntime
    from micast.config import SpeakerConfig, settings

    monkeypatch.setattr(
        settings, "speakers", [SpeakerConfig(did="airplay:a", loudness_comp_enabled=True)]
    )
    manager = AirPlayTargetManager(None)
    hub = _Hub(asyncio.StreamReader())
    runtime = _TargetRuntime("a", "a")
    hub.targets["a"] = runtime
    manager._hubs["r"] = hub
    manager.set_loudness_level("r", 100)
    high = runtime.audio_filter
    manager.set_loudness_level("r", 25)
    assert runtime.audio_filter and runtime.audio_filter != high


@pytest.mark.asyncio
async def test_play_error_retry_loop(monkeypatch):
    manager = DeviceManager.__new__(DeviceManager)
    manager._play_errors = {}
    manager._stream_urls = {"did1": "http://x/stream/1"}
    manager._owners = {"did1": "rcv1"}
    manager._error_retry_task = None

    monkeypatch.setattr("micast.xiaomi.device_manager.PLAY_ERROR_RETRY_SECONDS", 0.01)
    attempts: list[tuple[str, str]] = []

    async def fake_play_stream(did, url, owner=None, force=False, steal=True):
        attempts.append((did, url))
        if len(attempts) < 2:
            raise RuntimeError("offline")
        return True

    manager.play_stream = fake_play_stream
    manager.note_play_error("did1", "rcv1", "boom", "http://x/stream/1")
    assert manager.play_errors() == {"did1": "boom"}
    for _ in range(50):
        await asyncio.sleep(0.02)
        if not manager._play_errors:
            break
    assert manager._play_errors == {}
    assert attempts[-1] == ("did1", "http://x/stream/1")
    if manager._error_retry_task:
        await manager._error_retry_task


@pytest.mark.asyncio
async def test_play_error_retry_loop_gives_up_after_cap(monkeypatch):
    """A speaker that keeps rejecting the play command (removed from the
    account, powered off for good) must not be retried forever: after
    PLAY_ERROR_MAX_ATTEMPTS the error is dropped and the loop goes idle."""
    manager = DeviceManager.__new__(DeviceManager)
    manager._play_errors = {}
    manager._play_error_attempts = {}
    manager._stream_urls = {"did1": "http://x/stream/1"}
    manager._owners = {"did1": "rcv1"}
    manager._error_retry_task = None

    monkeypatch.setattr("micast.xiaomi.device_manager.PLAY_ERROR_RETRY_SECONDS", 0.01)
    monkeypatch.setattr("micast.xiaomi.device_manager.PLAY_ERROR_MAX_ATTEMPTS", 3)
    attempts: list[str] = []

    async def failing_play_stream(did, url, owner=None, force=False, steal=True):
        attempts.append(did)
        raise RuntimeError("gone")

    manager.play_stream = failing_play_stream
    manager.note_play_error("did1", "rcv1", "boom", "http://x/stream/1")
    if manager._error_retry_task:
        await manager._error_retry_task

    assert manager._play_errors == {}
    assert manager._play_error_attempts == {}
    assert len(attempts) == 3  # capped, not infinite
