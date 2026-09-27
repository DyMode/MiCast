"""Play URLs must name a stream that is actually registered.

Field failure (0.3.4, AirPlay 2 to an EQ'd speaker 四楼小爱): the supervisor's
play re-issue took the entry's FIRST registered stream (``airplay2-q1``, the EQ
variant is published first) and appended the sink's own suffix (``-q1``),
asking the speaker for ``/stream/airplay2-q1-q1`` — a 404. The speaker reported
"playing" but pulled nothing, and the Xiaomi watchdog then replayed that same
dead URL every 30s, so AirPlay 2 stayed silent no matter what else was fixed.
"""

from unittest.mock import AsyncMock

import pytest

import micast.audio_bridge as bridge_module
from micast.audio_bridge import AudioBridge
from micast.config import AirPlay2InstanceConfig, ReceiverConfig, Settings, SpeakerConfig


@pytest.fixture(autouse=True)
def _no_persistence(monkeypatch):
    """Never touch the real config file from tests."""
    monkeypatch.setattr(Settings, "save_to_file", lambda self: None)


def _settings_with_eq_speaker() -> Settings:
    """One receiver / one AirPlay 2 instance targeting an EQ'd speaker."""
    cfg = Settings()
    cfg.speakers = [
        SpeakerConfig(
            did="spk",
            alias="四楼小爱",
            enabled=False,
            eq_enabled=True,
            eq_points=[
                {"freq": 31.0, "gain_db": -2.0},
                {"freq": 1000.0, "gain_db": 3.0},
            ],
        )
    ]
    cfg.receivers = [ReceiverConfig(id="r1", name="经典", target_type="speaker", target_id="spk")]
    cfg.airplay2_instances = [
        AirPlay2InstanceConfig(id="airplay2", name="MiCast", target_type="speaker", target_id="spk")
    ]
    return cfg


def test_stream_id_for_carries_the_sink_variant_once():
    cfg = _settings_with_eq_speaker()

    # The EQ'd speaker owns one non-flat variant: the base stream plus -q1.
    assert [v["suffix"] for v in cfg.receiver_stream_variants("airplay2")] == ["-q1", ""]
    assert cfg.stream_id_for("airplay2", "spk") == "airplay2-q1"
    assert cfg.stream_id_for("airplay2") == "airplay2"
    assert cfg.stream_url_for("airplay2", "spk").endswith("/stream/airplay2-q1")


def test_entry_id_of_stream_maps_variants_back_to_the_entry():
    cfg = _settings_with_eq_speaker()

    assert cfg.entry_id_of_stream("airplay2-q1") == "airplay2"
    assert cfg.entry_id_of_stream("airplay2") == "airplay2"
    assert cfg.entry_id_of_stream("r1-Lq1") == "r1"
    assert cfg.entry_id_of_stream("unknown-q1") is None


def _bridge(monkeypatch, registered: list[str]) -> tuple[AudioBridge, AsyncMock]:
    cfg = _settings_with_eq_speaker()
    monkeypatch.setattr(bridge_module, "settings", cfg)
    bridge = object.__new__(AudioBridge)
    bridge._device_manager = AsyncMock()
    bridge._device_manager.owner_of = lambda did: None
    bridge._stream_server = AsyncMock()
    bridge._stream_server.stream_ids = lambda: list(registered)
    return bridge, bridge._device_manager


@pytest.mark.asyncio
async def test_reissue_uses_the_sinks_own_variant(monkeypatch):
    bridge, device_manager = _bridge(monkeypatch, ["airplay2-q1", "airplay2"])

    await bridge.reissue_entry_play("airplay2")

    url = device_manager.play_stream.await_args.args[1]
    assert "/stream/airplay2-q1/for/airplay2/spk?" in url
    assert "-q1-q1" not in url


@pytest.mark.asyncio
async def test_reissue_falls_back_to_the_base_stream_when_variant_is_missing(monkeypatch):
    """A variant the plan did not publish (e.g. mid topo change) must not be
    guessed at: the base stream keeps the speaker playing."""
    bridge, device_manager = _bridge(monkeypatch, ["airplay2"])

    await bridge.reissue_entry_play("airplay2")

    url = device_manager.play_stream.await_args.args[1]
    assert "/stream/airplay2/for/airplay2/spk?" in url


@pytest.mark.asyncio
async def test_reissue_without_streams_plays_nothing(monkeypatch):
    bridge, device_manager = _bridge(monkeypatch, [])

    await bridge.reissue_entry_play("airplay2")

    device_manager.play_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_reissue_never_steals_a_speaker_owned_elsewhere(monkeypatch):
    bridge, device_manager = _bridge(monkeypatch, ["airplay2-q1", "airplay2"])
    device_manager.owner_of = lambda did: "r1"

    await bridge.reissue_entry_play("airplay2")

    device_manager.play_stream.assert_not_awaited()
