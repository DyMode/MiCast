"""EQ debounce, latest-state application and tuning transaction regressions."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from micast.audio_bridge import AudioBridge
from micast.config import (
    AirPlay2InstanceConfig,
    ReceiverConfig,
    Settings,
    SpeakerGroupConfig,
)
from micast.stream_plan import compute_plan


@pytest.fixture(autouse=True)
def _no_persistence(monkeypatch):
    monkeypatch.setattr(Settings, "save_to_file", lambda self: None)


def _settings() -> Settings:
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
    return s


def _bare_bridge(monkeypatch, s: Settings) -> AudioBridge:
    """An AudioBridge with every subsystem stubbed — only plan logic runs."""
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
    bridge._settle_deadline = 0.0
    bridge._settle_waiting = False
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


def _pipeline() -> MagicMock:
    pipeline = MagicMock()
    pipeline.restart_encoder = AsyncMock()
    return pipeline


class _GatedSleep:
    """Stand-in for asyncio.sleep that only returns once ``release`` is set."""

    def __init__(self) -> None:
        self.entries = 0
        self.release = asyncio.Event()

    async def __call__(self, _seconds: float) -> None:
        self.entries += 1
        await self.release.wait()


@pytest.mark.asyncio
async def test_rapid_eq_burst_coalesces_into_one_latest_curve_apply(monkeypatch):
    """Two in-flight apply_config_change calls during a drag must produce a
    single encoder restart carrying the newest curve."""
    s = _settings()
    s.set_speaker_eq_curve("a", enabled=True, points=[(100, 1), (1000, -1)])
    bridge = _bare_bridge(monkeypatch, s)
    pipeline = _pipeline()
    bridge._pipelines = {"r1-q1": pipeline}
    bridge._plan = compute_plan(s)
    gate = _GatedSleep()
    real_sleep = asyncio.sleep  # the patch below replaces module-level asyncio.sleep
    monkeypatch.setattr("micast.audio_bridge.asyncio.sleep", gate)

    s.set_speaker_eq_curve("a", enabled=True, points=[(100, 2), (1000, -2)])
    first = asyncio.create_task(bridge.apply_config_change())
    while gate.entries < 1:
        await real_sleep(0)
    s.set_speaker_eq_curve("a", enabled=True, points=[(100, 5), (1000, -5)])
    second = asyncio.create_task(bridge.apply_config_change())
    while gate.entries < 2:
        await real_sleep(0)
    assert not first.done() and not second.done()

    gate.release.set()
    await asyncio.gather(first, second)

    pipeline.set_audio_character.assert_called_once_with(
        eq_curve=((100.0, 5.0), (1000.0, -5.0)), loudness=False
    )
    pipeline.restart_encoder.assert_awaited_once()
    assert bridge._plan == compute_plan(s)


@pytest.mark.asyncio
async def test_debounce_wait_does_not_hold_restart_lock(monkeypatch):
    """Regression: sleeping inside ``_restart_lock`` blocked bridge restarts.
    While a drag settles, another task must take the lock immediately."""
    s = _settings()
    s.set_speaker_eq_curve("a", enabled=True, points=[(100, 1)])
    bridge = _bare_bridge(monkeypatch, s)
    bridge._pipelines = {"r1-q1": _pipeline()}
    bridge._plan = compute_plan(s)
    gate = _GatedSleep()
    real_sleep = asyncio.sleep  # the patch below replaces module-level asyncio.sleep
    monkeypatch.setattr("micast.audio_bridge.asyncio.sleep", gate)

    s.set_speaker_eq_curve("a", enabled=True, points=[(100, 3)])
    pending = asyncio.create_task(bridge.apply_config_change())
    while gate.entries < 1:
        await real_sleep(0)

    async def take_lock() -> bool:
        async with bridge._restart_lock:
            return True

    # Old in-lock debounce: this would block until the debounce released and
    # the wait_for would expire. Lock-free debounce: acquired instantly.
    assert await asyncio.wait_for(take_lock(), timeout=0.2) is True

    gate.release.set()
    await pending
    assert bridge._plan == compute_plan(s)


@pytest.mark.asyncio
async def test_stale_lock_free_diff_is_recomputed_inside_lock(monkeypatch):
    """The outer diff is computed without the lock, so it can go stale while
    the task waits for a busy lock. The in-lock recompute must apply the FINAL
    settings state, never the stale outer diff."""
    s = _settings()
    s.set_speaker_eq_curve("a", enabled=True, points=[(100, 1)])
    bridge = _bare_bridge(monkeypatch, s)
    pipeline = _pipeline()
    bridge._pipelines = {"r1-q1": pipeline}
    bridge._plan = compute_plan(s)
    gate = _GatedSleep()
    real_sleep = asyncio.sleep  # the patch below replaces module-level asyncio.sleep
    monkeypatch.setattr("micast.audio_bridge.asyncio.sleep", gate)

    # Hold the lock so the apply task stalls between its outer diff (curve v1)
    # and the in-lock recompute.
    await bridge._restart_lock.acquire()
    try:
        s.set_speaker_eq_curve("a", enabled=True, points=[(100, 2)])
        pending = asyncio.create_task(bridge.apply_config_change())
        while gate.entries < 1:
            await real_sleep(0)
        gate.release.set()
        for _ in range(20):  # let the task settle past the debounce…
            await real_sleep(0)
        assert not pending.done()  # …and park it on the busy lock

        # Another edit lands while the outer diff (v1) is already computed.
        s.set_speaker_eq_curve("a", enabled=True, points=[(100, 9), (1000, -4)])
    finally:
        bridge._restart_lock.release()
    await pending

    pipeline.set_audio_character.assert_called_once_with(
        eq_curve=((100.0, 9.0), (1000.0, -4.0)), loudness=False
    )
    pipeline.restart_encoder.assert_awaited_once()
    assert bridge._plan == compute_plan(s)


@pytest.mark.asyncio
async def test_tuning_transaction_releases_config_lock_after_cancelled_settle(monkeypatch):
    """Exercise the route and rollback rather than an isolated settle hook.

    The route currently settles inside the transaction. Cancellation must
    release that lock and restore the curve without another debounce wait.
    """
    from micast import config_apply
    from micast.routes import tuning

    s = _settings()
    s.set_speaker_eq_curve("a", enabled=True, points=[(100, 1)])
    before = s.get_speaker("a").model_copy(deep=True)
    bridge = _bare_bridge(monkeypatch, s)
    bridge.apply_config_change = AsyncMock()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def settle():
        entered.set()
        await release.wait()

    bridge.wait_config_settled = settle
    monkeypatch.setattr(tuning, "settings", s)
    monkeypatch.setattr(config_apply, "settings", s)
    monkeypatch.setattr(config_apply, "_lock", asyncio.Lock())
    old_routes = list(tuning.router.routes)
    pending = None
    try:
        router = tuning.install(bridge)
        endpoint = [r.endpoint for r in router.routes if r.path == "/api/tuning/eq"][-1]
        pending = asyncio.create_task(endpoint({
            "did": "a", "enabled": True, "points": [[100, 7]],
        }))
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert config_apply._lock.locked()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=1)
        assert not config_apply._lock.locked()
        assert s.get_speaker("a").model_dump() == before.model_dump()
        bridge.apply_config_change.assert_awaited_once_with(debounce=False)
    finally:
        release.set()
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        tuning.router.routes[:] = old_routes


@pytest.mark.asyncio
async def test_settle_wait_coalesces_rapid_calls(monkeypatch):
    """A burst of wait_config_settled() calls shares ONE timer: the first
    caller sleeps, the rest return immediately without extending the freeze."""
    s = _settings()
    bridge = _bare_bridge(monkeypatch, s)
    gate = _GatedSleep()
    real_sleep = asyncio.sleep
    monkeypatch.setattr("micast.audio_bridge.asyncio.sleep", gate)

    first = asyncio.create_task(bridge.wait_config_settled())
    while gate.entries < 1:
        await real_sleep(0)
    second = asyncio.create_task(bridge.wait_config_settled())
    for _ in range(10):
        await real_sleep(0)

    assert gate.entries == 1  # the rapid joiner did not start its own sleep
    assert not first.done()
    assert second.done()  # joined the in-flight wait instead of sleeping

    gate.release.set()
    await first


@pytest.mark.asyncio
async def test_apply_config_change_without_debounce_never_sleeps(monkeypatch):
    """Rollback/runtime paths inside the config transaction pass
    debounce=False: an EQ-only diff must apply immediately, with no settle
    sleep under the transaction lock."""
    s = _settings()
    s.set_speaker_eq_curve("a", enabled=True, points=[(100, 1)])
    bridge = _bare_bridge(monkeypatch, s)
    bridge._pipelines = {"r1-q1": _pipeline()}
    bridge._plan = compute_plan(s)
    gate = _GatedSleep()
    monkeypatch.setattr("micast.audio_bridge.asyncio.sleep", gate)

    s.set_speaker_eq_curve("a", enabled=True, points=[(100, 7)])
    await bridge.apply_config_change(debounce=False)

    assert gate.entries == 0
    assert bridge._plan == compute_plan(s)
