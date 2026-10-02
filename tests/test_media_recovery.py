import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from support.playback import device_manager, registry

from micast.recovery import RecoveryCoordinator


async def test_replay_ownership_is_checked_inside_the_device_lock():
    sessions, _ = registry()
    manager, api = device_manager(sessions)
    sessions.begin("old", "airplay")
    sessions.begin("new", "dlna")
    await manager.play_stream("a", "http://new", owner="new")
    assert not await manager.play_stream("a", "http://old", owner="old", force=True, steal=False)
    api.play_music_url.assert_awaited_once_with("http://new", audio_id=None)
    assert manager.owner_of("a") == "new"
    await sessions.close_all()


async def test_recovery_cannot_recreate_a_closed_input_session():
    sessions, _ = registry()
    manager, api = device_manager(sessions)
    assert not await manager.play_stream("a", "http://old", owner="old", force=True, steal=False)
    api.play_music_url.assert_not_awaited()
    assert not sessions.snapshot()


async def test_finite_common_media_recovers_speaker_interruption(monkeypatch):
    monkeypatch.setattr("micast.xiaomi.device_manager.STATUS_CHECK_INTERVAL_SECONDS", 0.001)
    sessions, _ = registry()
    manager, api = device_manager(sessions)
    sessions.begin("dlna:r1", "dlna", "track")
    await manager.play_stream("a", "http://stream", owner="dlna:r1")
    manager.bridge = SimpleNamespace(media_playback=SimpleNamespace(sources={"dlna:r1": object()}))
    api.get_status.return_value = {"status": 0}

    async def recover(*args, **kwargs):
        manager._playing.discard("a")

    manager.recover_play_stream = AsyncMock(side_effect=recover)
    await asyncio.wait_for(manager._watchdog_loop("a"), 1)
    manager.recover_play_stream.assert_awaited_once_with(
        "a", "http://stream", owner="dlna:r1", force=True
    )
    assert sessions.current("dlna:r1").state.value == "active"
    await sessions.close_all()


async def test_finite_eof_drain_is_not_ended_early_by_speaker_watchdog(monkeypatch):
    monkeypatch.setattr("micast.xiaomi.device_manager.STATUS_CHECK_INTERVAL_SECONDS", 0.001)
    sessions, _ = registry()
    manager, api = device_manager(sessions)
    lease = sessions.begin("dlna:r1", "dlna", "track")
    await manager.play_stream("a", "http://stream", owner="dlna:r1")
    manager.bridge = SimpleNamespace(media_playback=SimpleNamespace(sources={"dlna:r1": object()}))
    manager.recovery = RecoveryCoordinator(sessions)
    sessions.quiet(lease.token, "media_finished", grace=20)
    api.get_status.return_value = {"status": 0}
    task = asyncio.create_task(manager._watchdog_loop("a"))
    try:

        async def enough_observations():
            while api.get_status.await_count < 5:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(enough_observations(), 1)
        assert manager.owner_of("a") == "dlna:r1"
        assert sessions.current("dlna:r1").state.value == "quiet"
        api.play_music_url.assert_awaited_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.recovery.close()
        await sessions.close_all()
