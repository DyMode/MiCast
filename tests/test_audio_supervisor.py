"""The audio supervisor: one state machine, one ladder, verified afterwards.

These pin the behaviour the previous six independent observers got wrong:
idle entries stay untouched, our-side faults are rebuilt, speaker-side faults
are re-issued then kicked, escalations are rate-limited, and a failure that
survives every level is surfaced instead of spinning forever.
"""

from types import SimpleNamespace

import pytest

from micast.audio_metrics import AudioMetrics, metrics
from micast.audio_supervisor import (
    LEVEL_KICK,
    LEVEL_REBUILD,
    LEVEL_REISSUE,
    LEVEL_SOURCE,
    SOURCE_PAUSE_GRACE_SECONDS,
    STATE_BURSTY,
    STATE_DEGRADED_OUR_SIDE,
    STATE_DEGRADED_SPEAKER,
    STATE_HEALTHY,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_QUIET,
    STATE_STARTING,
    STATE_UNHEALTHY,
    VERIFY_SECONDS,
    AudioSupervisor,
)


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeBridge:
    """Records the primitives the supervisor is allowed to drive."""

    def __init__(self, signals: dict, consumer_lagging: bool = True):
        self.signals = signals
        self.consumer_lagging = consumer_lagging
        self.calls: list[str] = []
        self.buffers: list[tuple[str, float | None]] = []
        self.targets = ["did-1"]

    # signals
    # Tests that retire a stream override this; a fresh list each call, so the
    # default ("one live entry") keeps every other test unchanged.
    live_entries: list[str] | None = None

    def entry_ids(self):
        return list(self.live_entries) if self.live_entries is not None else ["entry"]

    def entry_stream_ids(self, entry_id):
        return ["entry", "entry-q1"]

    def entry_targets(self, entry_id):
        return self.targets

    def is_session_active(self, entry_id):
        return self.signals["session_active"]

    def entry_source_idle_ms(self, entry_id):
        return self.signals.get("source_idle_ms")

    def entry_pipeline_usable(self, entry_id):
        return self.signals["pipeline_usable"]

    def entry_source_bursty(self, entry_id):
        return self.signals.get("source_bursty", False)

    def stream_client_count(self, stream_id):
        return 1 if self.signals["served"] else 0

    def stream_served(self, stream_id):
        return self.signals["served"]

    # primitives
    async def rebuild_entry(self, entry_id):
        self.calls.append(f"{LEVEL_REBUILD}:{entry_id}")

    async def recover_source(self, entry_id):
        self.calls.append(f"{LEVEL_SOURCE}:{entry_id}")

    async def kick_entry_clients(self, entry_id):
        self.calls.append(f"{LEVEL_KICK}:{entry_id}")

    async def reissue_entry_play(self, entry_id):
        self.calls.append(f"{LEVEL_REISSUE}:{entry_id}")

    def set_entry_buffer(self, entry_id, seconds):
        self.buffers.append((entry_id, seconds))

    def entry_consumer_lagging(self, entry_id):
        return self.consumer_lagging


def _signals(**overrides) -> dict:
    base = {
        "session_active": False,
        "source_idle_ms": 0.0,
        "pipeline_usable": True,
        "served": False,
        "source_bursty": False,
    }
    base.update(overrides)
    return base


def _supervisor(signals: dict, clock: FakeClock | None = None) -> AudioSupervisor:
    clock = clock or FakeClock()
    return AudioSupervisor(FakeBridge(signals), object(), clock=clock)


async def _tick_after_grace(supervisor: AudioSupervisor, clock: FakeClock) -> None:
    """Tick twice, letting the session grace period elapse in between.

    The first tick stamps the session; the grace window exists so a phone that
    is still doing SETUP is never "recovered" at.
    """
    await supervisor.tick()
    clock.advance(10)
    await supervisor.tick()


def test_no_session_classifies_idle_and_takes_no_action():
    supervisor = _supervisor(_signals())
    assert supervisor.classify("entry") == STATE_IDLE


def test_first_seconds_of_a_session_are_a_grace_period():
    supervisor = _supervisor(_signals(session_active=True, pipeline_usable=False))
    assert supervisor.classify("entry") == STATE_STARTING


def test_served_stream_is_healthy_unless_the_source_is_bursty():
    supervisor = _supervisor(_signals(session_active=True, served=True))
    assert supervisor.classify("entry") == STATE_HEALTHY

    bursty = _supervisor(_signals(session_active=True, served=True, source_bursty=True))
    assert bursty.classify("entry") == STATE_BURSTY


def test_our_side_fault_is_classified_by_pipeline_then_source():
    clock = FakeClock()
    dead = _supervisor(_signals(session_active=True, pipeline_usable=False), clock)
    dead.classify("entry")  # stamps the session start
    clock.advance(10)
    assert dead.classify("entry") == STATE_DEGRADED_OUR_SIDE

    clock = FakeClock()
    silent = _supervisor(_signals(session_active=True, source_idle_ms=9000.0), clock)
    silent.classify("entry")
    clock.advance(10)
    assert silent.classify("entry") == STATE_DEGRADED_OUR_SIDE


def test_speaker_side_fault_when_we_have_audio_but_nobody_pulls():
    clock = FakeClock()
    supervisor = _supervisor(
        _signals(session_active=True, served=False, source_idle_ms=10.0), clock
    )
    supervisor.classify("entry")
    clock.advance(10)
    assert supervisor.classify("entry") == STATE_DEGRADED_SPEAKER


@pytest.mark.asyncio
async def test_paused_sender_is_quiet_not_a_fault(monkeypatch):
    """A session that HAS delivered audio and then goes quiet is a pause, not a
    fault. Acting on it is destructive: restoring the source restarts the
    receiver, and for AirPlay 2 that is shairport-sync — the phone's session
    dies with it. Field data: this fired ~8s into such a silence.
    """
    clock = FakeClock()
    bridge = FakeBridge(_signals(session_active=True, served=True))
    supervisor = AudioSupervisor(bridge, object(), clock=clock)
    await supervisor.tick()  # healthy run latches "this session delivered audio"

    bridge.signals = _signals(session_active=True, source_idle_ms=9000.0)
    clock.advance(10)
    assert supervisor.classify("entry") == STATE_QUIET
    await supervisor.tick()
    assert bridge.calls == []
    assert supervisor.health("entry").state == STATE_QUIET

    # Past the pause grace the silence is a fault again, and the ladder acts.
    bridge.signals = _signals(
        session_active=True,
        source_idle_ms=(SOURCE_PAUSE_GRACE_SECONDS + 5) * 1000,
    )
    clock.advance(VERIFY_SECONDS + 1)
    await supervisor.tick()
    assert bridge.calls == [f"{LEVEL_SOURCE}:entry"]


@pytest.mark.asyncio
async def test_new_session_after_a_pause_is_judged_from_scratch():
    """The delivered-audio latch belongs to one session: a fresh session gets
    the ordinary grace, not the pause grace."""
    clock = FakeClock()
    bridge = FakeBridge(_signals(session_active=True, served=True))
    supervisor = AudioSupervisor(bridge, object(), clock=clock)
    await supervisor.tick()

    bridge.signals = _signals()  # session ended
    clock.advance(10)
    await supervisor.tick()
    assert supervisor.health("entry").state == STATE_IDLE

    bridge.signals = _signals(session_active=True, source_idle_ms=9000.0)
    clock.advance(10)
    await supervisor.tick()  # stamps the fresh session: still inside the grace
    clock.advance(10)
    assert supervisor.classify("entry") == STATE_DEGRADED_OUR_SIDE


@pytest.mark.asyncio
async def test_idle_entry_never_triggers_a_recovery():
    bridge = FakeBridge(_signals())
    supervisor = AudioSupervisor(bridge, object(), clock=FakeClock())
    for _ in range(5):
        await supervisor.tick()
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_unusable_pipeline_is_rebuilt_once_then_verified():
    clock = FakeClock()
    bridge = FakeBridge(_signals(session_active=True, pipeline_usable=False))
    supervisor = AudioSupervisor(bridge, object(), clock=clock)
    await _tick_after_grace(supervisor, clock)
    assert bridge.calls == [f"{LEVEL_REBUILD}:entry"]

    # Verification window: no second action until it elapses.
    clock.advance(1)
    await supervisor.tick()
    assert bridge.calls == [f"{LEVEL_REBUILD}:entry"]


@pytest.mark.asyncio
async def test_source_is_restarted_when_the_pipeline_is_alive_but_silent():
    clock = FakeClock()
    bridge = FakeBridge(_signals(session_active=True, source_idle_ms=9000.0))
    supervisor = AudioSupervisor(bridge, object(), clock=clock)
    await _tick_after_grace(supervisor, clock)
    assert bridge.calls == [f"{LEVEL_SOURCE}:entry"]


@pytest.mark.asyncio
async def test_speaker_fault_reissues_then_kicks():
    clock = FakeClock()
    bridge = FakeBridge(_signals(session_active=True, source_idle_ms=10.0))
    supervisor = AudioSupervisor(bridge, object(), clock=clock)
    await _tick_after_grace(supervisor, clock)
    assert bridge.calls == [f"{LEVEL_REISSUE}:entry"]

    clock.advance(VERIFY_SECONDS + 1)
    await supervisor.tick()
    assert bridge.calls == [f"{LEVEL_REISSUE}:entry", f"{LEVEL_KICK}:entry"]


@pytest.mark.asyncio
async def test_repeated_failure_ends_unhealthy_instead_of_spinning():
    clock = FakeClock()
    bridge = FakeBridge(_signals(session_active=True, source_idle_ms=10.0))
    supervisor = AudioSupervisor(bridge, object(), clock=clock)
    await _tick_after_grace(supervisor, clock)
    for _ in range(2):
        clock.advance(VERIFY_SECONDS + 1)
        await supervisor.tick()

    entry = supervisor.health("entry")
    assert entry.state == STATE_UNHEALTHY
    assert entry.escalations >= 3
    actions = len(bridge.calls)

    # Unhealthy entries back off: no further hammering in the next minute.
    await supervisor.tick()
    assert len(bridge.calls) == actions


@pytest.mark.asyncio
async def test_recovery_stands_down_once_healthy_again():
    clock = FakeClock()
    bridge = FakeBridge(_signals(session_active=True, pipeline_usable=False))
    supervisor = AudioSupervisor(bridge, object(), clock=clock)
    await _tick_after_grace(supervisor, clock)
    assert supervisor.health("entry").escalations == 1

    bridge.signals = _signals(session_active=True, served=True)
    clock.advance(VERIFY_SECONDS + 1)
    await supervisor.tick()
    entry = supervisor.health("entry")
    assert entry.state == STATE_HEALTHY
    assert entry.escalations == 0
    assert entry.last_action_ok is True


@pytest.mark.asyncio
async def test_bursty_consumer_is_given_more_buffer():
    """A speaker that cannot keep up IS helped by a wider reserve."""
    clock = FakeClock()
    bridge = FakeBridge(
        _signals(session_active=True, served=True, source_bursty=True),
        consumer_lagging=True,
    )
    supervisor = AudioSupervisor(bridge, object(), clock=clock)
    await supervisor.tick()  # marks burstiness start
    clock.advance(31)
    await supervisor.tick()
    assert bridge.buffers[-1] == ("entry", 0.5)

    bridge.signals = _signals(session_active=True, served=True)
    clock.advance(5)
    await supervisor.tick()
    assert bridge.buffers[-1] == ("entry", None)


@pytest.mark.asyncio
async def test_bursty_source_without_a_slow_consumer_is_not_buffer_widened():
    """Holding audio back does not smooth a lumpy source — only report it."""
    clock = FakeClock()
    bridge = FakeBridge(
        _signals(session_active=True, served=True, source_bursty=True),
        consumer_lagging=False,
    )
    supervisor = AudioSupervisor(bridge, object(), clock=clock)
    await supervisor.tick()
    clock.advance(70)
    await supervisor.tick()
    assert bridge.buffers == []


@pytest.mark.asyncio
async def test_watchdog_defers_while_the_supervisor_recovers():
    clock = FakeClock()
    bridge = FakeBridge(_signals(session_active=True, source_idle_ms=10.0))
    supervisor = AudioSupervisor(bridge, object(), clock=clock)
    await _tick_after_grace(supervisor, clock)

    assert supervisor.recovery_in_progress("did-1") is True
    assert supervisor.recovery_in_progress("other-did") is False


@pytest.mark.asyncio
async def test_snapshot_exposes_state_for_diagnostics():
    clock = FakeClock()
    supervisor = _supervisor(_signals(session_active=True, served=True), clock)
    await supervisor.tick()
    snap = supervisor.snapshot()
    assert snap["entry"]["state"] == STATE_HEALTHY
    assert "reason" in snap["entry"]


@pytest.mark.asyncio
async def test_metrics_records_health_transitions(monkeypatch):
    recorder = AudioMetrics(max_events=10)
    monkeypatch.setattr("micast.audio_supervisor.metrics", recorder)
    clock = FakeClock()
    supervisor = _supervisor(_signals(session_active=True, served=True), clock)
    await supervisor.tick()
    kinds = [event["kind"] for event in recorder.snapshot()["events"]]
    assert "health" in kinds


def test_speaker_paused_from_the_ui_is_not_a_fault():
    """Our own pause must never be answered with a play re-issue.

    Field failure (0.4.0): the UI paused the speaker, the supervisor then saw
    "clients connected but taking nothing" (or none at all) and re-issued the
    play command ~8 s later, which resumed the speaker behind the user's back
    and cleared the paused flag — "暂停后自己又播放了，控制就乱了".
    """
    clock = FakeClock()
    bridge = FakeBridge(_signals(session_active=True, served=False, source_idle_ms=10.0))
    manager = SimpleNamespace(is_paused=lambda did: True)
    supervisor = AudioSupervisor(bridge, manager, clock=clock)

    supervisor.classify("entry")
    clock.advance(10)
    assert supervisor.classify("entry") == STATE_PAUSED


@pytest.mark.asyncio
async def test_paused_entry_takes_no_action():
    clock = FakeClock()
    bridge = FakeBridge(_signals(session_active=True, served=False, source_idle_ms=10.0))
    supervisor = AudioSupervisor(bridge, SimpleNamespace(is_paused=lambda did: True), clock=clock)
    await _tick_after_grace(supervisor, clock)
    clock.advance(VERIFY_SECONDS + 1)
    await supervisor.tick()
    assert bridge.calls == []
    entry = supervisor.health("entry")
    assert entry.state == STATE_PAUSED
    assert entry.escalations == 0

    # Resuming from the UI puts the entry back in play.
    supervisor._device_manager = SimpleNamespace(is_paused=lambda did: False)
    clock.advance(10)
    await supervisor.tick()
    assert supervisor.health("entry").state in (STATE_HEALTHY, STATE_DEGRADED_SPEAKER)


@pytest.mark.asyncio
async def test_recovered_entry_stops_announcing_itself_every_tick():
    """The recovery line must be logged once, not on every 2s tick.

    Field data (0.4.0): one play_reissue left ``last_action`` set, so a healthy
    entry logged "recovered (was healthy)" every two seconds — 500+ identical
    lines that buried the real events in the report.
    """
    clock = FakeClock()
    bridge = FakeBridge(_signals(session_active=True, served=False, source_idle_ms=10.0))
    supervisor = AudioSupervisor(bridge, object(), clock=clock)
    await _tick_after_grace(supervisor, clock)  # play_reissue escalation
    assert bridge.calls == [f"{LEVEL_REISSUE}:entry"]

    bridge.signals = _signals(session_active=True, served=True)
    clock.advance(VERIFY_SECONDS + 1)
    await supervisor.tick()
    entry = supervisor.health("entry")
    assert entry.state == STATE_HEALTHY
    assert entry.last_action == ""

    # Ticking on must not log or count another recovery.
    events_before = len(metrics.snapshot()["events"])
    for _ in range(3):
        clock.advance(2)
        await supervisor.tick()
    assert supervisor.health("entry").escalations == 0
    assert len(metrics.snapshot()["events"]) == events_before


@pytest.mark.asyncio
async def test_entries_for_retired_streams_are_forgotten():
    """A stream the plan stopped publishing must leave the health panel.

    Field data (0.5.1, stereo pair): the panel showed six entries for three
    real streams, so the retired variants read like extra pipelines nobody had
    explained — and photos of that panel drove a wrong diagnosis.
    """
    clock = FakeClock()
    bridge = FakeBridge(_signals())
    supervisor = AudioSupervisor(bridge, object(), clock=clock)
    supervisor.health("entry")  # a stream that exists right now
    supervisor.health("entry-q1")  # ... and one that was retired since

    bridge.live_entries = ["entry"]
    clock.advance(10)
    await supervisor.tick()

    assert set(supervisor.snapshot()) == {"entry"}
