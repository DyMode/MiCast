"""Stall-recovery regressions for the 0.3.1 "AirPlay 2 completely silent" crash.

Field timeline: PCM stall watchdog fired -> bridge._recover_stalled_source ->
_rebuild_airplay2_instances -> _stop_airplay2_pipeline -> pipeline.stop().
stop() ran INSIDE the pipeline's own stall-recovery aux task and cancelled +
gathered the current task: Task.cancel recursed into its own gather child
(~1000 frames -> RecursionError on py3.14), wedging the stop forever. The
pipelines were already popped from the bridge dict, so every later
session_start logged "unknown receiver" and playback never came back.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import micast.audio_bridge as bridge_module
from micast.audio_bridge import AudioBridge
from micast.pcm_source import MockPCMSource
from micast.speaker_pipeline import SpeakerPipeline
from micast.stream_server import StreamServer


@pytest.mark.asyncio
async def test_stop_from_own_aux_task_completes_without_recursion():
    """The exact 0.3.1 crash: the stall-recovery aux task calls stop() on its
    own pipeline. stop() must skip (not cancel+gather) the current task."""
    server = StreamServer()
    pipeline = SpeakerPipeline(
        "ap2", "ap2", MockPCMSource(), server, input_sample_rate=48000
    )
    await pipeline.start()

    async def recovery():
        await pipeline.stop()

    aux = pipeline._spawn_aux(recovery(), "source-stall-recovery")
    await asyncio.wait_for(aux, timeout=10)  # RecursionError wedged this await

    assert not pipeline._running
    assert pipeline.status == "idle"
    await pipeline._pcm_source.stop()


@pytest.mark.asyncio
async def test_restart_source_recovers_pipeline():
    """_restart_source (the no-hook fallback) tears down and rebuilds cleanly,
    and the pipeline broadcasts again afterwards."""
    server = StreamServer()
    pipeline = SpeakerPipeline(
        "ap2", "ap2", MockPCMSource(), server, input_sample_rate=48000
    )
    await pipeline.start()
    await asyncio.sleep(1.1)  # let the mock source feed at least one chunk

    await pipeline._restart_source()

    assert pipeline._running
    assert pipeline.status == "running"
    await asyncio.sleep(0.6)
    assert server.total_bytes_sent.get("ap2", 0) > 0
    await pipeline.stop()


def _bare_bridge(monkeypatch) -> AudioBridge:
    bridge = object.__new__(AudioBridge)
    bridge._stall_recovery_requested = False
    bridge._maintenance_sessions = set()
    bridge._active_sessions = {"ap2"}
    monkeypatch.setattr(
        bridge_module,
        "settings",
        SimpleNamespace(
            airplay2_instances=[SimpleNamespace(id="ap2", enabled=True)],
        ),
    )
    return bridge


def _bridge_with_entries(monkeypatch) -> AudioBridge:
    """A bridge whose settings know both a classic receiver and an instance."""
    bridge = object.__new__(AudioBridge)
    bridge._stall_recovery_requested = False
    bridge._maintenance_sessions = set()
    bridge._active_sessions = {"r1", "ap2"}
    entries = [SimpleNamespace(id="r1"), SimpleNamespace(id="ap2")]
    monkeypatch.setattr(
        bridge_module,
        "settings",
        SimpleNamespace(
            airplay2_instances=[SimpleNamespace(id="ap2", enabled=True)],
            receivers=[entries[0]],
            audio_entry_ids=lambda: [item.id for item in entries],
            entry_id_of_stream=lambda stream_id: max(
                (
                    item.id
                    for item in entries
                    if stream_id == item.id or stream_id.startswith(f"{item.id}-")
                ),
                key=len,
                default=None,
            ),
        ),
    )
    return bridge


@pytest.mark.asyncio
async def test_classic_stall_rebuilds_only_its_own_entry(monkeypatch):
    """A PCM stall on a classic (AirPlay 1) receiver must rebuild that entry,
    never restart the whole app.

    Field failure (0.3.4): ``_recover_stalled_source`` fell back to
    ``self.restart`` for every stream that was not an AirPlay 2 instance, so one
    AirPlay 1 hiccup tore the engine down — every speaker disconnected, the
    shairport instance was killed, zeroconf re-registered — and, since a full
    restart cannot repair a stalled reader either, it repeated every ~10s. That
    is the "everything keeps getting kicked" the user reported.
    """
    bridge = _bridge_with_entries(monkeypatch)
    bridge.rebuild_entry = AsyncMock()
    bridge.restart = AsyncMock()
    monkeypatch.setattr(bridge_module.asyncio, "sleep", AsyncMock())

    await bridge._recover_stalled_source("r1-q1")

    bridge.restart.assert_not_awaited()
    bridge.rebuild_entry.assert_awaited_once_with("r1")
    # The sender session is still live: the latch must survive the rebuild.
    assert "r1" in bridge._active_sessions
    assert bridge._stall_recovery_requested is False


@pytest.mark.asyncio
async def test_airplay2_stall_never_restarts_the_engine(monkeypatch):
    bridge = _bridge_with_entries(monkeypatch)
    rebuild = AsyncMock()
    bridge._rebuild_airplay2_instances = rebuild
    bridge.restart = AsyncMock()
    monkeypatch.setattr(bridge_module.asyncio, "sleep", AsyncMock())

    await bridge._recover_stalled_source("ap2-q1")

    bridge.restart.assert_not_awaited()
    rebuild.assert_awaited_once_with({"ap2"})
    assert "ap2" not in bridge._active_sessions
    assert "ap2" not in bridge._maintenance_sessions


@pytest.mark.asyncio
async def test_unattributable_stall_leaves_running_sessions_alone(monkeypatch):
    """A stream no configured entry owns (a config change raced the watchdog)
    is logged, not answered with a full restart."""
    bridge = _bridge_with_entries(monkeypatch)
    bridge.rebuild_entry = AsyncMock()
    bridge.restart = AsyncMock()

    await bridge._recover_stalled_source("gone-q1")

    bridge.restart.assert_not_awaited()
    bridge.rebuild_entry.assert_not_awaited()


@pytest.mark.asyncio
async def test_recover_stalled_source_retries_failed_rebuild(monkeypatch):
    """A rebuild that throws once (the post-RecursionError half-stopped state
    class) is retried after a backoff instead of abandoning the entry."""
    bridge = _bare_bridge(monkeypatch)
    rebuild = AsyncMock(side_effect=[RuntimeError("wedged"), None])
    bridge._rebuild_airplay2_instances = rebuild
    monkeypatch.setattr(bridge_module.asyncio, "sleep", AsyncMock())

    await bridge._recover_stalled_source("ap2-q1")

    assert rebuild.await_count == 2
    assert "ap2" not in bridge._maintenance_sessions
    assert bridge._stall_recovery_requested is False


@pytest.mark.asyncio
async def test_recover_stalled_source_exhausted_retry_releases_latches(monkeypatch):
    """Both attempts failing propagates (logged by the task's done callback),
    but the re-entry latch and the maintenance flag MUST be released so a
    later stall or restart gets a clean shot."""
    bridge = _bare_bridge(monkeypatch)
    rebuild = AsyncMock(side_effect=[RuntimeError("a"), RuntimeError("b")])
    bridge._rebuild_airplay2_instances = rebuild
    monkeypatch.setattr(bridge_module.asyncio, "sleep", AsyncMock())

    with pytest.raises(RuntimeError, match="b"):
        await bridge._recover_stalled_source("ap2")

    assert rebuild.await_count == 2
    assert "ap2" not in bridge._maintenance_sessions
    assert bridge._stall_recovery_requested is False


@pytest.mark.asyncio
async def test_orchestrator_replays_after_maintenance_style_state_loss(monkeypatch):
    """Maintenance rebuild discards bridge._active_sessions WITHOUT firing
    on_session_stop, so the orchestrator's served-session latch can be stale
    while the bridge reports the session inactive. The next play-begins must
    replay the cloud play (skip condition requires BOTH latch and active)."""

    import micast.playback_orchestrator as orchestrator_module
    from micast.playback_orchestrator import PlaybackOrchestrator

    class _FakeSettings:
        audio = SimpleNamespace(auto_transcode=True, format="mp3")
        effective_stream_host = "h"
        stream_port = 8080
        touchscreen_lyrics = False
        default_volume_enabled = False

        def receiver_targets(self, receiver_id):
            return ["d1"]

        def stream_suffix(self, receiver_id, did):
            return ""

    monkeypatch.setattr(orchestrator_module, "settings", _FakeSettings())
    active: set[str] = set()  # maintenance discarded the session
    bridge = SimpleNamespace(
        is_session_active=lambda rid: rid in active,
        _volume_modes={},
        _sender_volumes={},
        lyrics_matched={},
        local_server=lambda rid: None,
    )
    device_manager = SimpleNamespace(
        play_stream=AsyncMock(return_value=True),
        stop_playback=AsyncMock(),
        stop=AsyncMock(),
        note_codec_capability=AsyncMock(),
        note_play_error=AsyncMock(),
        clear_play_error=AsyncMock(),
        owner_of=lambda did: None,
        stream_url_of=lambda did: None,
        playing_ids=lambda: [],
        owned_targets=lambda rid, owner: [],
    )
    tasks: set[asyncio.Task] = set()

    def start_background(coro, name):
        task = asyncio.create_task(coro, name=name)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    orch = PlaybackOrchestrator(bridge, device_manager, start_background)
    orch._started_sessions.add("ap2")  # stale latch from before maintenance

    await orch.on_session_start("ap2")

    # Bridge said inactive -> the stale latch alone must not skip the replay.
    assert device_manager.play_stream.await_count == 1
    for task in list(tasks):
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
