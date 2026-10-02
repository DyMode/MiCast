"""apply_config_change regression tests: one per lifecycle defect found on fnOS.

The plan fingerprint itself is covered by test_stream_plan.py; here we verify
the bridge actually dispatches each diff class to the right rebuild path, and
that the AirPlay 2 session callback starts a mapped group's network members.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from micast.audio_bridge import AudioBridge
from micast.config import (
    AirPlay2InstanceConfig,
    ReceiverConfig,
    Settings,
    SpeakerConfig,
    SpeakerGroupConfig,
)
from micast.stream_plan import compute_plan


@pytest.fixture(autouse=True)
def _no_persistence(monkeypatch):
    monkeypatch.setattr(Settings, "save_to_file", lambda self: None)


def _settings(**overrides) -> Settings:
    s = Settings()
    s.airplay_engine = "local"
    s.sync_groups_enabled = True
    s.airplay2_enabled = True
    s.groups = [SpeakerGroupConfig(id="g1", name="全屋", speaker_ids=["a", "b"], anchor_did="a")]
    s.receivers = [
        ReceiverConfig(id="r1", name="全屋", target_type="group", target_id="g1", enabled=True),
    ]
    s.airplay2_instances = [
        AirPlay2InstanceConfig(id="ap2", name="客厅 AP2", target_type="group", target_id="g1")
    ]
    for key, value in overrides.items():
        setattr(s, key, value)
    return s


def test_audio_config_update_is_validated_atomically():
    s = _settings()
    before = s.audio.model_dump()

    with pytest.raises(ValueError):
        s.update_audio(format="opus", sample_rate=12345)

    assert s.audio.model_dump() == before


def _bare_bridge(monkeypatch, s: Settings) -> AudioBridge:
    """An AudioBridge with every subsystem stubbed — only the diff dispatch runs."""
    bridge = object.__new__(AudioBridge)
    bridge._running = True
    bridge._restart_lock = asyncio.Lock()
    bridge._plan_update_requested = False
    bridge._plan = None
    bridge._pipelines = {}
    bridge._airplay2_pipelines = {}
    bridge._tees = {}
    bridge._error_count = 0
    bridge.on_group_membership_changed = None
    bridge.on_audio_restarted = None
    bridge._restart_engine_locked = AsyncMock()
    bridge._rebuild_pipelines_locked = AsyncMock()
    bridge._rebuild_classic_entries_locked = AsyncMock()
    bridge._reconcile_classic_entries_locked = AsyncMock()
    bridge._start_airplay2_pipelines = AsyncMock()
    bridge._stop_airplay2_pipelines = AsyncMock()
    bridge._rebuild_airplay2_instances_locked = AsyncMock()
    bridge._reconcile_entry_airplay_targets = AsyncMock()
    bridge._reconcile_entry_dlna_targets = AsyncMock()
    monkeypatch.setattr("micast.audio_bridge.settings", s)
    return bridge


@pytest.mark.asyncio
async def test_audio_format_change_restarts_encoders_on_both_engines(monkeypatch):
    """Defect 2: POST /audio used to leave AirPlay 2 pipelines on the old codec.
    A format swap is encoder-level: neither classic pipelines nor the AirPlay 2
    PCM source (a live session!) is torn down."""
    s = _settings()
    bridge = _bare_bridge(monkeypatch, s)
    classic = MagicMock()
    classic.restart_encoder = AsyncMock()
    airplay2 = MagicMock()
    airplay2.restart_encoder = AsyncMock()
    bridge._pipelines = {"r1": classic}
    bridge._airplay2_pipelines = {"ap2": airplay2}
    bridge._plan = compute_plan(s)

    s.audio.format = "flac"
    await bridge.apply_config_change()

    classic.restart_encoder.assert_awaited_once()
    airplay2.restart_encoder.assert_awaited_once()
    bridge._rebuild_airplay2_instances_locked.assert_not_called()
    bridge._restart_engine_locked.assert_not_called()
    assert bridge._plan == compute_plan(s)


@pytest.mark.asyncio
async def test_eq_edit_refreshes_pipeline_curve_before_encoder_restart(monkeypatch):
    s = _settings()
    s.set_speaker_eq_curve("a", enabled=True, points=[(100, 2), (1000, -1)])
    bridge = _bare_bridge(monkeypatch, s)
    pipeline = MagicMock()
    pipeline.restart_encoder = AsyncMock()
    bridge._pipelines = {"r1-q1": pipeline}
    bridge._plan = compute_plan(s)
    monkeypatch.setattr("micast.audio_bridge.asyncio.sleep", AsyncMock())

    s.set_speaker_eq_curve("a", enabled=True, points=[(100, -3), (1000, 4)])
    await bridge.apply_config_change()

    pipeline.set_audio_character.assert_called_once_with(
        eq_curve=((100.0, -3.0), (1000.0, 4.0)), loudness=False
    )
    pipeline.restart_encoder.assert_awaited_once()
    assert bridge._plan == compute_plan(s)


@pytest.mark.asyncio
async def test_config_change_waits_for_busy_restart_lock(monkeypatch):
    s = _settings()
    bridge = _bare_bridge(monkeypatch, s)
    bridge._plan = compute_plan(s)
    s.audio.format = "flac"
    pipeline = MagicMock()
    pipeline.restart_encoder = AsyncMock()
    bridge._pipelines = {"r1": pipeline}

    await bridge._restart_lock.acquire()
    task = asyncio.create_task(bridge.apply_config_change())
    await asyncio.sleep(0)
    assert not task.done()
    bridge._restart_lock.release()
    await task

    pipeline.restart_encoder.assert_awaited_once()
    assert bridge._plan == compute_plan(s)


@pytest.mark.asyncio
async def test_full_restart_request_is_not_lost_while_lock_is_busy(monkeypatch):
    s = _settings()
    bridge = _bare_bridge(monkeypatch, s)

    await bridge._restart_lock.acquire()
    task = asyncio.create_task(bridge.restart())
    await asyncio.sleep(0)
    assert not task.done()
    bridge._restart_lock.release()
    await task

    bridge._restart_engine_locked.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_encoder_restart_does_not_claim_plan_was_applied(monkeypatch):
    s = _settings()
    bridge = _bare_bridge(monkeypatch, s)
    old_plan = compute_plan(s)
    bridge._plan = old_plan
    s.audio.format = "flac"
    pipeline = MagicMock()
    pipeline.restart_encoder = AsyncMock(side_effect=RuntimeError("encoder failed"))
    bridge._pipelines = {"r1": pipeline}

    with pytest.raises(RuntimeError, match="暂未完全生效"):
        await bridge.apply_config_change()

    # The failed entry keeps its old fingerprint so the next apply re-diffs it.
    assert bridge._plan["entries"]["r1"] == old_plan["entries"]["r1"]
    assert bridge._plan != compute_plan(s)


@pytest.mark.asyncio
async def test_partial_encoder_restart_failure_retries_only_failed_entry(monkeypatch):
    """One entry's encoder restart fails while another succeeds: the plan must
    advance for the succeeded entry only, so a retry re-applies just the failed
    one instead of re-gapping the healthy speaker or forgetting the failure."""
    s = _settings()
    bridge = _bare_bridge(monkeypatch, s)
    old_plan = compute_plan(s)
    bridge._plan = old_plan
    s.audio.format = "flac"
    failing = MagicMock()
    failing.restart_encoder = AsyncMock(side_effect=RuntimeError("encoder failed"))
    healthy = MagicMock()
    healthy.restart_encoder = AsyncMock()
    bridge._pipelines = {"r1": failing}
    bridge._airplay2_pipelines = {"ap2": healthy}

    with pytest.raises(RuntimeError, match="暂未完全生效"):
        await bridge.apply_config_change()

    # r1 keeps its old fingerprint (will re-diff), ap2 is advanced.
    assert bridge._plan["entries"]["r1"] == old_plan["entries"]["r1"]
    assert bridge._plan["entries"]["ap2"] == compute_plan(s)["entries"]["ap2"]

    # Retry after the failure is fixed: only r1's encoder restarts again.
    failing.restart_encoder = AsyncMock()
    await bridge.apply_config_change()

    assert failing.restart_encoder.await_count == 1
    assert healthy.restart_encoder.await_count == 1
    assert bridge._plan == compute_plan(s)


@pytest.mark.asyncio
async def test_failed_audio_restarted_hook_keeps_touched_entries_unapplied(monkeypatch):
    """A hook failure re-points speakers globally, so every entry the diff
    touched must stay un-advanced — not just the one whose encoder failed."""
    s = _settings()
    bridge = _bare_bridge(monkeypatch, s)
    old_plan = compute_plan(s)
    bridge._plan = old_plan
    s.audio.format = "flac"
    pipeline = MagicMock()
    pipeline.restart_encoder = AsyncMock()
    bridge._pipelines = {"r1": pipeline}
    bridge.on_audio_restarted = AsyncMock(side_effect=RuntimeError("reconnect failed"))

    with pytest.raises(RuntimeError, match="暂未完全生效"):
        await bridge.apply_config_change()

    assert bridge._plan == old_plan


@pytest.mark.asyncio
async def test_airplay2_retarget_rebuilds_only_that_instance(monkeypatch):
    """Defect 1: same stream suffix set, different target — must rebuild."""
    s = _settings()
    bridge = _bare_bridge(monkeypatch, s)
    bridge._plan = compute_plan(s)

    s.airplay2_instances[0].target_type = "speaker"
    s.airplay2_instances[0].target_id = "a"
    await bridge.apply_config_change()

    bridge._rebuild_airplay2_instances_locked.assert_awaited_once_with({"ap2"})
    bridge._rebuild_pipelines_locked.assert_not_called()


@pytest.mark.asyncio
async def test_delay_only_change_touches_no_pipeline(monkeypatch):
    s = _settings()
    s.groups[0].delays_ms = {"b": 500}
    bridge = _bare_bridge(monkeypatch, s)
    bridge._plan = compute_plan(s)

    s.groups[0].delays_ms = {"b": 1800}
    await bridge.apply_config_change()

    bridge._restart_engine_locked.assert_not_called()
    bridge._rebuild_pipelines_locked.assert_not_called()
    bridge._rebuild_airplay2_instances_locked.assert_not_called()
    for pipeline in bridge._pipelines.values():
        pipeline.restart_encoder.assert_not_called()


@pytest.mark.asyncio
async def test_external_delay_change_reconciles_every_entry_of_group(monkeypatch):
    """Defect 4: the AirPlay 2 entry's external targets reconnect too."""
    s = _settings()
    s.groups[0].airplay_targets = ["apdev"]
    s.groups[0].delays_ms = {"apdev": 300}
    bridge = _bare_bridge(monkeypatch, s)
    bridge._plan = compute_plan(s)

    s.groups[0].delays_ms = {"apdev": 900}
    await bridge.apply_config_change()

    calls = {call.args[0] for call in bridge._reconcile_entry_airplay_targets.await_args_list}
    assert calls == {"r1", "ap2"}
    bridge._rebuild_airplay2_instances_locked.assert_not_called()


@pytest.mark.asyncio
async def test_membership_change_fires_the_group_hook(monkeypatch):
    s = _settings()
    bridge = _bare_bridge(monkeypatch, s)
    hook = AsyncMock()
    bridge.on_group_membership_changed = hook
    bridge._plan = compute_plan(s)

    s.groups[0].speaker_ids = ["a"]
    await bridge.apply_config_change()

    hook.assert_awaited_once_with("g1", ["b"])
    bridge._rebuild_classic_entries_locked.assert_awaited_once_with({"r1"})


@pytest.mark.asyncio
async def test_first_application_after_start_restarts(monkeypatch):
    """No snapshot (legacy start path) → full restart, then the plan is set."""
    s = _settings()
    bridge = _bare_bridge(monkeypatch, s)
    await bridge.apply_config_change()
    bridge._restart_engine_locked.assert_awaited_once()


def test_merge_marks_reference_rewrite_for_rebuild():
    """Defect 6: a device-id migration must surface so the caller rebuilds."""
    s = _settings()
    s.speakers = [SpeakerConfig(did="old-did", miot_did="m1", alias="客厅")]
    s.groups[0].speaker_ids = ["old-did", "b"]
    s.groups[0].delays_ms = {"old-did": 300}
    s.groups[0].anchor_did = "old-did"
    s.receivers.append(
        ReceiverConfig(
            id="speaker-old-did", name="客厅", target_type="speaker", target_id="old-did"
        )
    )
    s.airplay2_instances.append(
        AirPlay2InstanceConfig(id="ap2b", name="直推", target_type="speaker", target_id="old-did")
    )

    assert not s.consume_merge_rewrite()
    s.merge_speakers([{"deviceID": "new-did", "miotDID": "m1", "name": "客厅"}])

    assert s.consume_merge_rewrite()
    assert not s.consume_merge_rewrite()  # one-shot
    assert s.groups[0].speaker_ids == ["new-did", "b"]
    assert s.groups[0].anchor_did == "new-did"
    assert s.groups[0].delays_ms == {"new-did": 300}
    assert s.receivers[1].target_id == "new-did"
    assert s.airplay2_instances[1].target_id == "new-did"


@pytest.mark.asyncio
async def test_airplay2_session_start_starts_group_network_members(monkeypatch):
    """Defect 5: the AirPlay 2 session callback must start external AirPlay and
    DLNA members of the mapped group, like the classic session path does."""
    s = _settings()
    s.groups[0].airplay_targets = ["apdev"]
    s.groups[0].dlna_targets = ["urn:dlna-1"]
    monkeypatch.setattr("micast.audio_bridge.settings", s)

    bridge = object.__new__(AudioBridge)
    bridge._pipelines = {}
    bridge._active_sessions = set()
    bridge._volume_modes = {}
    bridge._sender_volumes = {}
    bridge._target_taps = {"ap2": object()}
    pipeline = AsyncMock()
    bridge._airplay2_pipelines = {"ap2": pipeline}
    airplay_targets = AsyncMock()
    dlna_targets = AsyncMock()
    bridge._airplay_targets = airplay_targets
    bridge._dlna_targets = dlna_targets

    await bridge.session_start("ap2")

    pipeline.session_start.assert_awaited_once()
    start = airplay_targets.start_targets.await_args
    assert start.args[0] == "ap2"
    assert start.args[1] == ["apdev"]
    play = dlna_targets.play_targets.await_args
    assert play.args[0] == "ap2"
    assert play.args[1] == ["urn:dlna-1"]
    assert "ap2" in bridge._active_sessions


def test_delete_group_conflict_lists_airplay2_references(monkeypatch):
    """Defect 3 (route level): deleting an AirPlay 2-referenced group → 409."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from micast.routes import receivers

    s = _settings()
    monkeypatch.setattr("micast.routes.receivers.settings", s)
    bridge = AsyncMock()
    app = FastAPI()
    app.include_router(receivers.install(bridge))
    client = TestClient(app)

    response = client.delete("/api/receivers/groups/g1")
    assert response.status_code == 409
    assert "客厅 AP2" in response.json()["detail"]
    assert s.groups  # untouched

    # Remove the AirPlay 2 reference: the delete then cascades classic
    # receivers and funnels through apply_config_change.
    s.airplay2_instances = []
    response = client.delete("/api/receivers/groups/g1")
    assert response.status_code == 200
    assert not s.groups
    assert not s.receivers
    bridge.apply_config_change.assert_awaited_once()


@pytest.mark.asyncio
async def test_rebuild_airplay2_kicks_and_unregisters_retired_endpoints(monkeypatch):
    """Retarget mid-playback: the old variant plan's stream ids (e.g. ap2-q1)
    disappear, so speakers attached to them must be kicked and the endpoints
    unregistered — otherwise they pull silence forever."""
    s = _settings()
    bridge = _bare_bridge(monkeypatch, s)
    stream_server = MagicMock()
    bridge._stream_server = stream_server
    bridge._airplay2_pipelines = {"ap2": AsyncMock(), "ap2-q1": AsyncMock()}
    bridge._pipelines = {"r1": AsyncMock()}
    bridge._stop_airplay2_pipeline = AsyncMock()

    async def _restart():
        # The new plan keeps only the base pipeline.
        bridge._airplay2_pipelines = {"ap2": AsyncMock()}

    bridge._start_airplay2_pipelines = AsyncMock(side_effect=_restart)

    await AudioBridge._rebuild_airplay2_instances_locked(bridge, {"ap2"})

    stream_server.kick_clients.assert_called_once_with("ap2-q1")
    stream_server.unregister_stream.assert_called_once_with("ap2-q1")


@pytest.mark.asyncio
async def test_airplay2_retarget_fires_on_audio_restarted(monkeypatch):
    """The retarget crash: speakers keep playing their old URLs unless the
    audio-restarted hook re-points them after the rebuild."""
    s = _settings()
    bridge = _bare_bridge(monkeypatch, s)
    bridge._plan = compute_plan(s)
    restarted = AsyncMock()
    bridge.on_audio_restarted = restarted

    s.airplay2_instances[0].target_type = "speaker"
    s.airplay2_instances[0].target_id = "a"
    await bridge.apply_config_change()

    restarted.assert_awaited_once()
