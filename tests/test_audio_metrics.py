"""Audio-path black box: cumulative counters and the rolling event log.

Connection-scoped counters reset on every speaker reconnect, so a periodic
stutter can be invisible in the live status. These tests pin the cumulative
behaviour that makes one occurrence explainable after the fact.
"""

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from micast.audio_metrics import LOOP_LAG_WARN_MS, WINDOW_SECONDS, AudioMetrics
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

    drops = snap["drops"]
    assert drops["tee"] == 2
    assert drops["encoder_in"] == 1
    assert drops["encoder_out"] == 3






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


def test_runtime_sample_averages_cpu_over_the_window():
    """CPU is a windowed average, not a one-second sample."""
    m = AudioMetrics()
    m.note_runtime_sample(wall=100.0, cpu=10.0, loop_lag_ms=5.0)
    # A burst of CPU inside the window must be averaged against the span.
    m.note_runtime_sample(wall=105.0, cpu=12.5, loop_lag_ms=20.0)
    assert m.cpu_percent == pytest.approx(50.0)
    # A spike long ago must not keep the reading high: samples older than the
    # window are dropped, and the next sample re-averages the fresh ones only.
    m.note_runtime_sample(wall=111.0, cpu=12.6, loop_lag_ms=1.0)
    m.note_runtime_sample(wall=112.0, cpu=12.7, loop_lag_ms=1.0)
    assert m.cpu_percent < 20.0


def test_loop_lag_is_reported_and_logged_when_it_is_not_jitter():
    """Long loop lag is the one cause no drop counter can see: every pipeline
    stalls at the same instant because the loop serves all of them."""
    m = AudioMetrics()
    m.note_runtime_sample(wall=1.0, cpu=1.0, loop_lag_ms=12.0)
    assert m.snapshot()["runtime"] == {
        "cpu_percent": 0.0,
        "cores": 0,
        "loop_lag_ms": 12.0,
        "loop_lag_max_ms": 12.0,
    }

    m.note_runtime_sample(wall=2.0, cpu=1.1, loop_lag_ms=LOOP_LAG_WARN_MS + 1)
    snapshot = m.snapshot()
    assert snapshot["runtime"]["loop_lag_max_ms"] == LOOP_LAG_WARN_MS + 1
    assert [event["kind"] for event in snapshot["events"]] == ["loop_lag"]
    # ... and a small lag never logs (it is normal scheduling jitter).
    m.note_runtime_sample(wall=3.0, cpu=1.2, loop_lag_ms=1.0)
    assert len(m.snapshot()["events"]) == 1


@pytest.mark.asyncio
async def test_runtime_monitor_samples_the_running_loop():
    """The monitor owns the low-level sampling so no caller can get it wrong."""
    m = AudioMetrics()
    monkeypatch = pytest.MonkeyPatch()
    import micast.audio_metrics as module

    monkeypatch.setattr(module, "metrics", m)
    task = asyncio.create_task(module.run_runtime_monitor(interval=0.02))
    await asyncio.sleep(0.15)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    monkeypatch.undo()

    runtime = m.snapshot()["runtime"]
    assert runtime["cores"] >= 1
    assert runtime["loop_lag_max_ms"] >= 0.0


def test_window_counts_kinds_weights_entries_and_ms():
    """The 60s fold: per-kind counts (weight = packets/chunks), worst ms,
    per-entry split — the only numbers the live verdicts may reason from."""
    m = AudioMetrics()
    m.note_link_skip(7)
    m.note_link_skip(3)
    m.note_link_resend()
    m.note_lag_skip(1000, 45.0, entry="speaker-1")
    m.note_lag_skip(500, 12.0, entry="speaker-2")
    m.note_silence_fill("speaker-1")
    m.note_tee_drop(4, entry="stream-a")
    m.note_encoder_drop("in", 2)
    m.note_queue_drops(3, 900.0)

    win = m.snapshot()["window"]
    assert win["link_skip"]["count"] == 10  # weights sum, not occurrences
    assert win["link_resend"]["count"] == 1
    assert win["lag_skip"]["count"] == 2
    assert win["lag_skip"]["ms_max"] == 45.0
    assert win["lag_skip"]["by_entry"] == {"speaker-1": 1, "speaker-2": 1}
    assert win["silence_fill"]["by_entry"] == {"speaker-1": 1}
    assert win["tee_drop"]["count"] == 4
    assert win["encoder_drop"]["count"] == 2
    assert win["queue_drop"]["count"] == 3


def test_window_drops_entries_older_than_sixty_seconds():
    m = AudioMetrics()
    now = time.time()
    m.note_link_skip(5)
    # A stale entry must not keep the page red after the minute has passed.
    m._window.append((now - WINDOW_SECONDS - 1, "link_skip", None, None, 100))

    win = m.snapshot()["window"]
    assert win["link_skip"]["count"] == 5


def test_silence_fill_records_event_with_label():
    m = AudioMetrics()
    m.note_silence_fill("speaker-1")
    snap = m.snapshot()

    assert snap["source"]["silence_fills"] == 1
    assert snap["events"][-1]["kind"] == "silence_fill"
    assert snap["events"][-1]["label"] == "补静音"
    assert snap["events"][-1]["entry"] == "speaker-1"


def test_stall_padding_counts_but_stays_out_of_the_window():
    """Source-stall padding is a sender-side symptom: it keeps the cumulative
    counter (history unchanged) but must not feed the speaker-side verdict."""
    m = AudioMetrics()
    m.note_silence_fill(windowed=False)
    m.note_silence_fill("speaker-1")
    snap = m.snapshot()

    assert snap["source"]["silence_fills"] == 2
    assert snap["window"]["silence_fill"]["count"] == 1
    assert snap["window"]["silence_fill"]["by_entry"] == {"speaker-1": 1}


def test_link_health_notes_feed_the_window_only():
    """RAOP link counters stay connection-scoped; the window is the verdict's view."""
    m = AudioMetrics()
    m.note_link_skip(12)
    m.note_link_resend()
    m.note_link_decode_error()
    snap = m.snapshot()

    win = snap["window"]
    assert win["link_skip"]["count"] == 12
    assert win["link_resend"]["count"] == 1
    assert win["link_decode_error"]["count"] == 1
    # No event-ring noise for burst-prone link counters.
    assert snap["events"] == []
