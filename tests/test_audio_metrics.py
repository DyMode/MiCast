"""Audio-path black box: cumulative counters and the rolling event log.

Connection-scoped counters reset on every speaker reconnect, so a periodic
stutter can be invisible in the live status. These tests pin the cumulative
behaviour that makes one occurrence explainable after the fact.
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from micast.audio_bridge import AudioBridge
from micast.audio_metrics import AudioMetrics
from micast.config import settings


def test_snapshot_tracks_encode_stalls_and_percentiles():
    m = AudioMetrics(max_events=4, sample_window=10)
    for ms in (10.0, 20.0, 30.0):
        m.note_encode(ms)
    m.note_encode(500.0)  # stall

    snap = m.snapshot()
    assert snap["encode"]["chunks"] == 4
    assert snap["encode"]["stalls"] == 1
    assert snap["encode"]["stall_max_ms"] == 500.0
    assert snap["encode"]["max_ms"] == 500.0
    assert snap["encode"]["p95_ms"] == 500.0
    assert [e["kind"] for e in snap["events"]] == ["encoder_stall"]


def test_event_log_is_bounded_and_records_values():
    m = AudioMetrics(max_events=3)
    for i in range(5):
        m.note_lag_skip(1000 * (i + 1), float(i + 1))
    snap = m.snapshot()

    assert snap["client"]["lag_skips"] == 5
    assert snap["client"]["lag_skip_bytes"] == 1000 * (1 + 2 + 3 + 4 + 5)
    assert len(snap["events"]) == 3  # ring buffer keeps the newest
    assert snap["events"][-1]["ms"] == 5.0
    assert snap["events"][-1]["kind"] == "lag_skip"


def test_client_reconnect_is_counted_separately():
    m = AudioMetrics()
    m.note_client_connect("r1", replacing=False)
    m.note_client_connect("r1", replacing=True)
    snap = m.snapshot()

    assert snap["client"]["connects"] == 2
    assert snap["client"]["reconnects"] == 1
    assert [e["kind"] for e in snap["events"]] == ["client_reconnect"]


def test_drop_layers_accumulate():
    m = AudioMetrics()
    m.note_tee_drop(2)
    m.note_encoder_drop("in")
    m.note_encoder_drop("out", 3)
    snap = m.snapshot()

    assert snap["drops"] == {"tee": 2, "encoder_in": 1, "encoder_out": 3}


@pytest.mark.asyncio
async def test_retarget_releases_old_speaker(monkeypatch):
    """A retargeted AirPlay 2 instance must unload the speaker it left behind,
    otherwise both speakers pull the instance's stream side by side."""
    bridge = object.__new__(AudioBridge)
    bridge._stream_server = MagicMock()
    bridge._stream_server.stream_ids.return_value = ["ap2", "ap2-q1"]
    # _start_airplay2_pipelines already refreshed the snapshot to the new
    # target; the previous map is what the rebuild captured beforehand.
    bridge._airplay2_targets = {"ap2": "new-did"}
    hook = AsyncMock()
    bridge.on_airplay2_retarget = hook

    monkeypatch.setattr(
        "micast.audio_bridge.settings",
        SimpleNamespace(
            airplay2_instances=[SimpleNamespace(id="ap2", target_id="new-did")]
        ),
    )

    await AudioBridge._release_retargeted_speakers(bridge, {"ap2"}, {"ap2": "old-did"})

    # The leftover speaker's client is dropped on every stream of the instance,
    # and the orchestrator is asked to stop its playback.
    assert bridge._stream_server.kick_clients.call_args_list == [
        call("ap2", sink="old-did"),
        call("ap2-q1", sink="old-did"),
    ]
    assert hook.await_args.args == ("old-did", "ap2")


@pytest.mark.asyncio
async def test_retarget_without_change_keeps_speaker(monkeypatch):
    bridge = object.__new__(AudioBridge)
    bridge._stream_server = MagicMock()
    bridge._stream_server.stream_ids.return_value = ["ap2"]
    bridge._airplay2_targets = {"ap2": "same-did"}
    hook = AsyncMock()
    bridge.on_airplay2_retarget = hook

    monkeypatch.setattr(
        "micast.audio_bridge.settings",
        SimpleNamespace(
            airplay2_instances=[SimpleNamespace(id="ap2", target_id="same-did")]
        ),
    )

    await AudioBridge._release_retargeted_speakers(bridge, {"ap2"}, {"ap2": "same-did"})

    bridge._stream_server.kick_clients.assert_not_called()
    hook.assert_not_awaited()


@pytest.mark.asyncio
async def test_kick_clients_can_target_one_sink():
    from micast.stream_server import StreamServer

    server = StreamServer()
    server.register_stream("ap2", MagicMock())
    queue_old: asyncio.Queue = asyncio.Queue(maxsize=4)
    queue_new: asyncio.Queue = asyncio.Queue(maxsize=4)
    server._clients["ap2"] = {queue_old, queue_new}
    now = time.monotonic()
    server._client_delay[queue_old] = {"sink": "old-did", "last_get_at": now}
    server._client_delay[queue_new] = {"sink": "new-did", "last_get_at": now}

    kicked = server.kick_clients("ap2", sink="old-did")

    assert kicked == 1
    assert queue_old.get_nowait() is None
    assert queue_new.empty()
    assert queue_new in server._clients["ap2"]


def test_client_max_lag_is_configurable(monkeypatch):
    # The setter persists, so stub save_to_file: a unit test must not rewrite
    # the developer's real config file.
    monkeypatch.setattr(type(settings), "save_to_file", lambda *a, **k: None)
    original = settings.client_max_lag_seconds
    try:
        settings.set_client_max_lag_seconds(6)
        assert settings.client_max_lag_seconds == 6.0
        with pytest.raises(ValueError):
            settings.set_client_max_lag_seconds(0.1)
        with pytest.raises(ValueError):
            settings.set_client_max_lag_seconds(99)
    finally:
        settings.client_max_lag_seconds = original
