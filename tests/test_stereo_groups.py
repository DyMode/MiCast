"""Stereo pair groups: config validation, channel suffixes, ffmpeg filters."""

import asyncio
import struct

import pytest

from micast.config import ReceiverConfig, Settings, SpeakerGroupConfig
from micast.pcm_source import ReaderPCMSource
from micast.speaker_pipeline import SpeakerPipeline
from micast.stream_server import StreamServer


@pytest.fixture(autouse=True)
def _no_persistence(monkeypatch):
    """Never touch the real config file from tests."""
    monkeypatch.setattr(Settings, "save_to_file", lambda self: None)
    # StreamReader() wants a current loop even outside async code.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    loop.close()


def _settings_with_stereo_group() -> Settings:
    settings = Settings()
    settings.groups = [SpeakerGroupConfig(id="g1", name="组播", speaker_ids=["didA", "didB"])]
    settings.receivers = [ReceiverConfig(id="r1", name="组播", target_type="group", target_id="g1")]
    return settings


def test_stereo_mode_requires_at_least_two_members():
    settings = _settings_with_stereo_group()
    settings.groups[0].speaker_ids = ["didA"]
    with pytest.raises(ValueError, match="至少需要两个成员"):
        settings.update_group("g1", mode="stereo")


def test_stereo_mode_allows_network_only_group():
    """A stereo group can be two network devices with no Xiaomi speakers."""
    settings = _settings_with_stereo_group()
    settings.groups[0].speaker_ids = []
    group = settings.update_group(
        "g1", mode="stereo", airplay_targets=["aabbccddeeff", "001122334455"]
    )
    assert group.mode == "stereo"
    variants = settings.receiver_stream_variants("r1")
    # No speaker channels: only the always-present base stream.
    assert [v["suffix"] for v in variants] == [""]
    # Assigning network channels creates channel streams for DLNA to pull.
    settings.update_group("g1", network_channels={"aabbccddeeff": "left", "001122334455": "right"})
    variants = settings.receiver_stream_variants("r1")
    # Network members without a channel assignment still need the base mix, so
    # it stays published alongside the two channel streams.
    assert [v["suffix"] for v in variants] == ["-L", "-R", ""]
    assert settings.receiver_channel_variant_suffix("r1", "left") == "-L"


def test_stereo_mode_allows_many_speakers_sharing_a_channel():
    settings = _settings_with_stereo_group()
    settings.groups[0].speaker_ids = ["didA", "didB", "didC"]
    group = settings.update_group(
        "g1",
        mode="stereo",
        channels={"didA": "right", "didB": "right", "didC": "right"},
    )
    assert group.channels == {"didA": "right", "didB": "right", "didC": "right"}
    assert settings.channel_suffix("r1", "didC") == "-R"


def test_stereo_mode_defaults_channels_first_left_second_right():
    settings = _settings_with_stereo_group()
    group = settings.update_group("g1", mode="stereo")
    assert group.channels == {"didA": "left", "didB": "right"}
    assert settings.receiver_channel("r1", "didA") == "left"
    assert settings.channel_suffix("r1", "didB") == "-R"
    # Mirror groups never get a suffix.
    assert settings.channel_suffix("r1", "didA") == "-L"
    other = Settings()
    other.groups = [SpeakerGroupConfig(id="g2", name="m", speaker_ids=["didA"])]
    other.receivers = [ReceiverConfig(id="r2", name="m", target_type="group", target_id="g2")]
    assert other.channel_suffix("r2", "didA") == ""


def test_swapping_channels_keeps_one_per_side():
    settings = _settings_with_stereo_group()
    settings.update_group("g1", mode="stereo")
    group = settings.update_group("g1", channels={"didA": "right", "didB": "left"})
    assert group.channels == {"didA": "right", "didB": "left"}
    assert settings.channel_suffix("r1", "didA") == "-R"


def test_gains_and_delays_are_clamped():
    settings = _settings_with_stereo_group()
    group = settings.update_group("g1", gains_db={"didA": 99.0}, delays_ms={"didB": 99999})
    assert group.gains_db["didA"] == 12.0
    assert group.delays_ms["didB"] == 5000
    # Signed offsets clamp at the lower bound too.
    group = settings.update_group("g1", delays_ms={"didB": -99999})
    assert group.delays_ms["didB"] == -5000

    settings.set_large_delay_enabled(True)
    group = settings.update_group("g1", delays_ms={"didB": 99999})
    assert group.delays_ms["didB"] == 15000

    settings.set_large_delay_enabled(False)
    group = settings.groups[0]
    assert group.delays_ms["didB"] == 5000


def test_sink_hold_is_scoped_to_receiver_group():
    settings = Settings()
    settings.groups = [
        SpeakerGroupConfig(
            id="g1",
            name="first",
            speaker_ids=["shared", "a"],
            delays_ms={"shared": 100},
            anchor_did="a",
        ),
        SpeakerGroupConfig(
            id="g2",
            name="playing",
            speaker_ids=["shared", "b"],
            delays_ms={"shared": 900},
            anchor_did="b",
        ),
    ]
    settings.receivers = [
        ReceiverConfig(id="r1", name="first", target_type="group", target_id="g1"),
        ReceiverConfig(id="r2", name="playing", target_type="group", target_id="g2"),
    ]

    assert settings.sink_hold_ms("r1", "shared") == 100
    assert settings.sink_hold_ms("r2", "shared") == 900
    assert settings.sink_hold_ms("r2", "a") == 0


def test_hidden_startup_sync_can_be_cleared():
    server = StreamServer()
    first = asyncio.Queue()
    second = asyncio.Queue()
    server._client_delay = {
        first: {
            "receiver": "r1",
            "sink": "didA",
            "manual_ms": 0,
            "startup_ms": 0,
            "ready_at": 100.0,
            "calibrated": False,
        },
        second: {
            "receiver": "r1",
            "sink": "didB",
            "manual_ms": 1000,
            "startup_ms": 0,
            "ready_at": 101.2,
            "calibrated": False,
        },
    }

    server._client_delay[first]["startup_ms"] = 900
    server._client_delay[second]["startup_ms"] = -2000
    server.clear_startup_sync("r1")

    assert server._client_delay[first]["startup_ms"] == 0
    assert server._client_delay[second]["startup_ms"] == 0
    assert server._client_delay[first]["calibrated"] is True
    assert server._client_delay[second]["calibrated"] is True


def test_reanchor_preserves_physical_holds():
    settings = _settings_with_stereo_group()
    settings.groups[0].anchor_did = "didA"
    settings.groups[0].delays_ms = {"didB": 3300}
    before = settings.groups[0].delay_holds()

    group = settings.update_group("g1", anchor_did="didB")

    assert group.anchor_did == "didB"
    assert group.delays_ms == {"didA": -3300}
    assert group.delay_holds() == before


def _pipeline(monkeypatch, group) -> SpeakerPipeline:
    import micast.speaker_pipeline as sp

    monkeypatch.setattr(sp.settings, "groups", [group], raising=False)
    return SpeakerPipeline(
        device_id="r1",
        alias="组播 (左声道)",
        pcm_source=ReaderPCMSource(asyncio.StreamReader()),
        stream_server=StreamServer(),
        stream_id="r1-L",
        group_id=group.id,
        channel="left",
    )


def test_pipeline_builds_channel_filter(monkeypatch):
    group = SpeakerGroupConfig(
        id="g1",
        name="组播",
        speaker_ids=["didA", "didB"],
        mode="stereo",
        channels={"didA": "left", "didB": "right"},
        gains_db={"didA": -3.0},
    )
    pipeline = _pipeline(monkeypatch, group)
    assert pipeline._build_audio_filter() == [
        ("pan", "stereo|c0=FL|c1=FL"),
        ("volume", "-3.0dB"),
    ]
    assert pipeline.stream_url.endswith("/stream/r1-L")


def test_pipeline_without_stereo_group_has_no_filter(monkeypatch):
    group = SpeakerGroupConfig(id="g1", name="组播", speaker_ids=["didA", "didB"])
    pipeline = _pipeline(monkeypatch, group)
    assert pipeline._build_audio_filter() is None


def test_airplay_volume_applies_stream_gain_without_touching_speaker(monkeypatch):
    group = SpeakerGroupConfig(id="g1", name="组播", speaker_ids=["didA", "didB"])
    pipeline = _pipeline(monkeypatch, group)
    samples = struct.pack("<hhhh", 10000, -10000, 20000, -20000)

    pipeline.set_input_volume(100)
    assert pipeline._apply_input_gain(samples) == samples

    pipeline.set_input_volume(0)
    assert pipeline._apply_input_gain(samples) == bytes(len(samples))

    pipeline.set_input_volume(50)
    reduced = struct.unpack("<hhhh", pipeline._apply_input_gain(samples))
    assert 1700 < reduced[0] < 1850
    assert -1850 < reduced[1] < -1700


def test_filter_follows_channel_holder_after_swap(monkeypatch):
    """After speakers swap channels, the -L stream must still carry FL content
    with the trim of whichever speaker now holds 'left'."""
    group = SpeakerGroupConfig(
        id="g1",
        name="组播",
        speaker_ids=["didA", "didB"],
        mode="stereo",
        channels={"didA": "right", "didB": "left"},  # swapped
        gains_db={"didB": -6.0},
    )
    pipeline = _pipeline(monkeypatch, group)  # the -L pipeline
    assert pipeline._build_audio_filter() == [
        ("pan", "stereo|c0=FL|c1=FL"),
        ("volume", "-6.0dB"),
    ]


def test_plain_base_is_published_only_when_something_consumes_it():
    """A channel-split group must not run a third encoder for nobody.

    Field data (0.5.x, stereo pair): the group published base + -L-q1 + -R, but
    every sink resolves to its own channel variant, so the base stream had no
    possible consumer — one wasted encoder (and tee branch) on a box where the
    branches that matter were competing for CPU.
    """
    settings = _settings_with_stereo_group()
    group = settings.groups[0]
    group.mode = "stereo"
    group.channels = {"didA": "left", "didB": "right"}

    # Both members own a channel, nothing external: the base mix is unreachable.
    assert not settings.needs_plain_base("r1")
    assert [v["suffix"] for v in settings.receiver_stream_variants("r1")] == ["-L", "-R"]

    # A member without a channel plays the mix, so the base comes back.
    group.channels = {"didA": "left"}
    assert settings.needs_plain_base("r1")
    assert "" in [v["suffix"] for v in settings.receiver_stream_variants("r1")]

    # ... and so does an external consumer (DLNA renderer pulls /stream/{id}).
    group.channels = {"didA": "left", "didB": "right"}
    group.dlna_targets = ["udn-1"]
    assert settings.needs_plain_base("r1")
    assert "" in [v["suffix"] for v in settings.receiver_stream_variants("r1")]

    # A mirror group serves everyone from the base mix.
    group.mode = "mirror"
    group.dlna_targets = []
    assert settings.needs_plain_base("r1")


def test_plain_base_is_kept_for_external_targets_and_channelless_members():
    """Anything that can ask for the mix keeps it published."""
    settings = _settings_with_stereo_group()
    group = settings.groups[0]
    group.mode = "stereo"
    group.channels = {"didA": "left", "didB": "right"}

    # An external AirPlay target taps the base mix.
    group.airplay_targets = ["aabbccddeeff"]
    assert settings.needs_plain_base("r1")
    assert "" in [v["suffix"] for v in settings.receiver_stream_variants("r1")]

    # A DLNA renderer attached to the group pulls the bare entry.
    group.airplay_targets = []
    group.dlna_targets = ["udn-1"]
    assert settings.needs_plain_base("r1")

    # A member that owns no channel plays the mix.
    group.dlna_targets = []
    group.channels = {"didA": "left"}
    assert settings.needs_plain_base("r1")

    # Every member on a channel and nothing external: no consumer is left.
    group.channels = {"didA": "left", "didB": "right"}
    assert not settings.needs_plain_base("r1")
