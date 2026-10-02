from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter

from micast.config import ReceiverConfig, SpeakerGroupConfig, settings
from micast.playback_sessions import PlaybackSessions
from micast.routes import playback
from micast.runtime_snapshot import RuntimeSnapshot


def test_health_uses_one_ingress_owner_for_all_dsp_variants():
    from micast.audio_bridge import AudioBridge

    bridge = object.__new__(AudioBridge)
    bridge.sessions = PlaybackSessions(lambda: 120)
    lease = bridge.sessions.begin("dlna:r", "dlna")
    bridge._pipelines = {"dlna:r-L-q1": object(), "dlna:r-R": object()}
    bridge._airplay2_pipelines = {}
    assert bridge.entry_ids() == ["dlna:r"]
    assert bridge.is_session_active("dlna:r")
    bridge.sessions.pause(lease.token)
    assert not bridge.is_session_active("dlna:r")


def test_dlna_topology_follows_pcm_pipeline_instead_of_uri_control(monkeypatch):
    from micast.topology import build_topology

    monkeypatch.setattr(settings, "dlna_enabled", True)
    monkeypatch.setattr(
        settings,
        "receivers",
        [ReceiverConfig(id="r", name="r", target_type="speaker", target_id="s")],
    )
    monkeypatch.setattr(settings, "groups", [])
    monkeypatch.setattr(settings, "airplay2_instances", [])
    bridge = SimpleNamespace(
        diagnostics={"streams": {"dlna:r": {"flowing": True, "clients": 1}}},
        status={
            "status": "running",
            "runtime": {"sessions": [{"owner": "dlna:r", "state": "active"}]},
        },
    )
    manager = SimpleNamespace(
        owner_of=lambda did: "dlna:r",
        is_playing=lambda did: True,
        is_paused=lambda did: False,
        get_alias=lambda did: did,
        is_enabled=lambda did: True,
    )
    snapshot = build_topology(bridge, manager)
    ids = {node["id"] for node in snapshot["nodes"]}
    assert {"src:dlna:r", "engine:dlna:r", "stream:dlna:r", "spk:s"} <= ids
    assert any(
        edge["from"] == "stream:dlna:r" and edge["to"] == "spk:s" and edge["active"]
        for edge in snapshot["edges"]
    )
    assert not any(
        edge["from"] == "src:dlna:r" and edge["to"] == "cloud:xiaomi" for edge in snapshot["edges"]
    )


@pytest.mark.parametrize("protocol", ["airplay", "airplay2", "dlna"])
@pytest.mark.parametrize("target", ["speaker:s", "airplay:s", "dlna-target:s"])
async def test_each_input_and_output_obeys_generation_and_takeover(protocol, target):
    sessions = PlaybackSessions(lambda: 120)
    former = sessions.begin("former", protocol)
    replacement = sessions.begin("replacement", "dlna" if protocol != "dlna" else "airplay2")
    released = []

    async def release(owner):
        released.append(owner)

    assert await sessions.targets.acquire(target, former.token, lambda: release("former"))
    assert await sessions.targets.acquire(target, replacement.token, lambda: release("replacement"))
    assert not sessions.valid(former.token)
    await sessions.close(former.token)
    assert released == ["former"]
    assert sessions.targets.owns(target, replacement.token)
    await sessions.close(replacement.token)
    assert released == ["former", "replacement"]


@pytest.mark.parametrize("protocol", ["airplay", "airplay2"])
async def test_global_pause_and_resume_controls_network_outputs(monkeypatch, protocol):
    sessions = PlaybackSessions(lambda: 120)
    lease = sessions.begin("r", protocol)
    released = AsyncMock()
    sessions.register(lease.token, "external:airplay", released)
    bridge = SimpleNamespace(
        sessions=sessions, reissue_entry_play=AsyncMock(), _start_entry_targets=AsyncMock()
    )
    manager = SimpleNamespace(get_control_targets=lambda: [], _volume_locks={})
    monkeypatch.setattr(playback, "router", APIRouter())
    routes = playback.install(bridge, manager).routes

    def endpoint(suffix):
        return next(route.endpoint for route in routes if route.path == suffix)

    await endpoint("/pause")()
    assert sessions.current("r").state.value == "paused"
    released.assert_awaited_once()
    result = await endpoint("/play")()
    assert result["resumed"] == ["r"]
    assert sessions.valid(lease.token)
    bridge.reissue_entry_play.assert_awaited_once_with("r")
    bridge._start_entry_targets.assert_awaited_once_with("r", resume=True)


def test_runtime_versions_track_ownership_and_backend_epoch():
    sessions = PlaybackSessions(lambda: 120)
    runtime = RuntimeSnapshot()
    first = runtime.project(sessions)
    assert runtime.project(sessions)["revision"] == first["revision"]
    sessions.begin("r", "airplay2")
    second = runtime.project(sessions)
    assert second["revision"] > first["revision"]
    assert second["sessions"][0]["capabilities"]["eq"]
    assert not second["sessions"][0]["capabilities"]["seek"]
    assert RuntimeSnapshot().epoch != runtime.epoch


async def test_paused_network_output_remains_visible_and_new_owner_wins(monkeypatch):
    monkeypatch.setattr(
        settings,
        "receivers",
        [ReceiverConfig(id="r", name="r", target_type="group", target_id="g")],
    )
    monkeypatch.setattr(
        settings, "groups", [SpeakerGroupConfig(id="g", name="g", airplay_targets=["s"])]
    )
    sessions = PlaybackSessions(lambda: 120)
    paused = sessions.begin("r", "airplay")
    sessions.pause(paused.token)
    adapter = SimpleNamespace(statuses=lambda: {}, get_volume=AsyncMock(return_value=42))
    manager = SimpleNamespace(
        get_control_targets=lambda: [],
        is_muted=lambda did: False,
        bridge=SimpleNamespace(sessions=sessions, _airplay_targets=adapter),
    )
    state = await playback.build_playback_state(manager)
    assert state["paused"] and state["devices"][0]["did"] == "airplay:s"
    newer = sessions.begin("new", "dlna")
    await sessions.targets.acquire("airplay:s", newer.token, AsyncMock())
    state = await playback.build_playback_state(manager)
    assert not state["paused"] and not state["devices"]
    await sessions.close_all()
