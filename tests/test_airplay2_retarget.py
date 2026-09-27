"""A retarget must not restart the receiver (that kills the sender's session).

Field data (0.3.6): changing an AirPlay 2 instance's target speaker ran
``airplay2-rebuild``, which stopped the local shairport process and spawned a
new one — the phone's live AirPlay 2 session died with it, even though nothing
about the INGRESS had changed. Only name/port/command are baked into shairport;
the target speaker, EQ and delay are egress concerns of our own pipelines.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import micast.audio_bridge as bridge_module
from micast.audio_bridge import AudioBridge
from micast.config import AirPlay2InstanceConfig, Settings


@pytest.fixture(autouse=True)
def _no_persistence(monkeypatch):
    monkeypatch.setattr(Settings, "save_to_file", lambda self: None)


def _bridge(monkeypatch, instance: AirPlay2InstanceConfig) -> AudioBridge:
    cfg = Settings()
    cfg.airplay2_instances = [instance]
    monkeypatch.setattr(bridge_module, "settings", cfg)
    bridge = object.__new__(AudioBridge)
    bridge._airplay2_runtime = {instance.id: {"id": instance.id, "status": "running", "detail": ""}}
    bridge._airplay2_sources = {}
    bridge._airplay2_readers = {}
    bridge._airplay2_source_keys = {}
    bridge._airplay2_tees = {}
    bridge._airplay2_pipelines = {}
    bridge._target_taps = {}
    bridge._airplay_targets = None
    bridge._dlna_targets = None
    bridge._stream_server = SimpleNamespace(kick_clients=lambda *a, **k: 0, unregister_stream=lambda *a: None)
    bridge._airplay2_targets = {}
    return bridge


@pytest.mark.asyncio
async def test_stop_keeps_a_live_receiver_and_its_reader(monkeypatch):
    bridge = _bridge(
        monkeypatch,
        AirPlay2InstanceConfig(id="ap2", name="MiCast", target_type="speaker", target_id="a"),
    )
    source = SimpleNamespace(alive=True, stop=AsyncMock())
    bridge._airplay2_sources["ap2"] = source
    bridge._airplay2_readers["ap2"] = object()
    bridge._airplay2_source_keys["ap2"] = ("MiCast", "", "local:run-shairport")

    await bridge._stop_airplay2_pipeline("ap2", keep_source=True)

    source.stop.assert_not_awaited()
    assert bridge._airplay2_sources["ap2"] is source
    assert "ap2" in bridge._airplay2_readers

    # Without the flag the receiver really stops (a full engine stop must not
    # leave a stale shairport behind for a later reuse).
    await bridge._stop_airplay2_pipeline("ap2")
    source.stop.assert_awaited_once()
    assert "ap2" not in bridge._airplay2_sources
    assert "ap2" not in bridge._airplay2_readers
    assert "ap2" not in bridge._airplay2_source_keys


def test_ingress_key_ignores_egress_settings(monkeypatch):
    instance = AirPlay2InstanceConfig(
        id="ap2", name="MiCast", target_type="speaker", target_id="a"
    )
    bridge = _bridge(monkeypatch, instance)
    bridge._airplay2_source_keys["ap2"] = bridge._airplay2_source_key(instance)
    assert bridge._airplay2_ingress_unchanged(instance)

    # Retargeting keeps the receiver: only the egress changes.
    retargeted = AirPlay2InstanceConfig(
        id="ap2", name="MiCast", target_type="speaker", target_id="b"
    )
    assert bridge._airplay2_ingress_unchanged(retargeted)

    # A renamed receiver is a NEW mDNS service: it must be restarted.
    renamed = AirPlay2InstanceConfig(
        id="ap2", name="书房", target_type="speaker", target_id="b"
    )
    assert not bridge._airplay2_ingress_unchanged(renamed)

    # So must a different preferred port (shairport binds it).
    bridge_module.settings.airplay2_port = 7100
    assert not bridge._airplay2_ingress_unchanged(retargeted)


@pytest.mark.asyncio
async def test_plan_rebuild_reuses_the_receiver_instead_of_respawning(monkeypatch):
    instance = AirPlay2InstanceConfig(
        id="ap2", name="MiCast", target_type="speaker", target_id="a"
    )
    bridge = _bridge(monkeypatch, instance)
    source = SimpleNamespace(alive=True, stop=AsyncMock(), start=AsyncMock())
    reader = object()
    bridge._airplay2_sources["ap2"] = source
    bridge._airplay2_readers["ap2"] = reader
    bridge._airplay2_source_keys["ap2"] = bridge._airplay2_source_key(instance)
    started = []

    async def start_variant(inst, group, stereo, variants, got_reader, **kwargs):
        started.append(got_reader)

    monkeypatch.setattr(bridge, "_start_airplay2_variant_pipelines", start_variant)
    monkeypatch.setattr(bridge, "_resolve_airplay2_targets", lambda: {})
    monkeypatch.setattr(bridge, "_release_retargeted_speakers", AsyncMock())
    monkeypatch.setattr(bridge_module, "airplay2_mode", lambda: "single")

    await bridge._rebuild_airplay2_instances_locked({"ap2"})

    source.stop.assert_not_awaited()
    source.start.assert_not_awaited()
    assert started == [reader]  # the SAME reader re-attached


@pytest.mark.asyncio
async def test_retarget_plays_the_new_speaker_while_the_session_lives(monkeypatch):
    """A retarget mid-song must actually start the new speaker.

    Field failure (0.4.0): keeping shairport alive (correct — the phone's
    session must survive) also removed the accidental re-play the older code
    got from the session restart, so the new speaker was never told to play and
    the app looked stuck.
    """
    instance = AirPlay2InstanceConfig(
        id="ap2", name="MiCast", target_type="speaker", target_id="new-speaker"
    )
    bridge = _bridge(monkeypatch, instance)
    bridge._active_sessions = {"ap2"}
    bridge._stream_server = SimpleNamespace(
        stream_ids=lambda: ["ap2"],
        client_count=lambda *a: 0,
        kick_clients=lambda *a, **k: 0,
        unregister_stream=lambda *a: None,
    )
    bridge._device_manager = SimpleNamespace(
        owner_of=lambda did: None,
        play_stream=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(bridge, "play_stream_id", lambda entry, did: "ap2-q1")

    await bridge._start_retargeted_speakers({"ap2"}, {"ap2": "old-speaker"})

    assert bridge._device_manager.play_stream.await_count == 1
    url = bridge._device_manager.play_stream.await_args.args[1]
    assert "/stream/ap2-q1/for/ap2/new-speaker?" in url

    # A retarget while nothing is playing must NOT start playback on its own.
    bridge._active_sessions = set()
    bridge._device_manager.play_stream.reset_mock()
    await bridge._start_retargeted_speakers({"ap2"}, {"ap2": "old-speaker"})
    bridge._device_manager.play_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_retarget_leaves_a_speaker_owned_by_another_receiver_alone(monkeypatch):
    instance = AirPlay2InstanceConfig(
        id="ap2", name="MiCast", target_type="speaker", target_id="new-speaker"
    )
    bridge = _bridge(monkeypatch, instance)
    bridge._active_sessions = {"ap2"}
    bridge._stream_server = SimpleNamespace(
        stream_ids=lambda: ["ap2"],
        client_count=lambda *a: 0,
        kick_clients=lambda *a, **k: 0,
        unregister_stream=lambda *a: None,
    )
    bridge._device_manager = SimpleNamespace(
        owner_of=lambda did: "classic-receiver",
        play_stream=AsyncMock(return_value=True),
    )

    await bridge._start_retargeted_speakers({"ap2"}, {"ap2": "old-speaker"})

    bridge._device_manager.play_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_retarget_play_goes_through_the_verified_orchestrator_path(monkeypatch):
    """The retarget play must use the retry+verify path, not a bare one-shot.

    Field failure (0.4.1): the Xiaomi cloud answered a single play command with
    "ubus server internal error ... Timed out waiting 2000.00ms". A bare play
    only logged it and the new speaker stayed silent for good.
    """
    instance = AirPlay2InstanceConfig(
        id="ap2", name="MiCast", target_type="speaker", target_id="new-speaker"
    )
    bridge = _bridge(monkeypatch, instance)
    bridge._active_sessions = {"ap2"}
    calls: list[tuple] = []

    async def play_receiver(receiver_id: str, url: str, steal: bool = True) -> None:
        calls.append((receiver_id, url, steal))

    bridge.on_local_stream = play_receiver
    monkeypatch.setattr(bridge, "_device_manager", SimpleNamespace(), raising=False)

    await bridge._start_retargeted_speakers({"ap2"}, {"ap2": "old-speaker"})

    assert len(calls) == 1
    receiver_id, url, steal = calls[0]
    assert receiver_id == "ap2"
    assert url.endswith("/stream/ap2")
    assert steal is False  # never steal a speaker another receiver owns
