from unittest.mock import AsyncMock

from support.playback import device_manager, registry

from micast.xiaomi.device_manager import status_check_delay


def test_poll_schedule_is_bounded():
    assert status_check_delay(initial=True) == 3
    assert status_check_delay(healthy=True) == 30
    assert status_check_delay() == 5
    assert [status_check_delay(failures=n) for n in (1, 2, 3, 100)] == [30, 60, 120, 120]


async def test_healthy_polling_and_failure_backoff(monkeypatch):
    sessions, _ = registry()
    manager, api = device_manager(sessions)
    manager._playing.add("a")
    api.get_status = AsyncMock(side_effect=[{"status": 1, "volume": 42}, RuntimeError("offline"), {"status": 1}])
    delays = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 4:
            manager._playing.clear()

    monkeypatch.setattr("micast.xiaomi.device_manager.asyncio.sleep", sleep)
    await manager._watchdog_loop("a")
    assert delays == [3, 30, 30, 30]
    assert manager._volumes["a"] == 42
    assert manager.player_observation("a")["fresh"]


async def test_unknown_status_never_triggers_recovery(monkeypatch):
    sessions, _ = registry()
    manager, api = device_manager(sessions)
    manager._playing.add("a")
    api.get_status.return_value = {"code": 0}
    manager.recover_play_stream = AsyncMock()
    delays = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 5:
            manager._playing.clear()

    monkeypatch.setattr("micast.xiaomi.device_manager.asyncio.sleep", sleep)
    await manager._watchdog_loop("a")
    assert delays == [3, 30, 60, 120, 120]
    manager.recover_play_stream.assert_not_awaited()
    assert manager.player_observation("a")["status"] is None


async def test_idle_confirmation_queries_once(monkeypatch):
    sessions, _ = registry()
    manager, api = device_manager(sessions)
    api.get_status.return_value = {"status": 0}
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("micast.xiaomi.device_manager.asyncio.sleep", sleep)
    manager._confirm_idle_status("a")
    await manager._status_confirm_tasks["a"]
    assert delays == [2]
    api.get_status.assert_awaited_once()
    assert manager.player_observation("a")["status"] == 0


def test_cached_status_expires_without_cloud_request(monkeypatch):
    sessions, _ = registry()
    manager, api = device_manager(sessions)
    clock = [100.0]
    monkeypatch.setattr("micast.xiaomi.device_manager.time.monotonic", lambda: clock[0])
    manager._remember_player_status("a", {"status": 1})
    assert manager.player_observation("a")["fresh"]
    clock[0] += 46
    assert not manager.player_observation("a")["fresh"]
    api.get_status.assert_not_awaited()


async def test_idle_result_cannot_override_resumed_playback(monkeypatch):
    sessions, _ = registry()
    manager, api = device_manager(sessions)

    async def sleep(delay):
        pass

    async def resume_during_query():
        manager._playing.add("a")
        return {"status": 0}

    api.get_status.side_effect = resume_during_query
    monkeypatch.setattr("micast.xiaomi.device_manager.asyncio.sleep", sleep)
    manager._confirm_idle_status("a")
    await manager._status_confirm_tasks["a"]
    assert manager.player_observation("a")["status"] is None


async def test_observation_is_discarded_after_query_failure(monkeypatch):
    sessions, _ = registry()
    manager, api = device_manager(sessions)
    manager._playing.add("a")
    manager._remember_player_status("a", {"status": 1})
    api.get_status.side_effect = RuntimeError("offline")
    delays = []

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 2:
            manager._playing.clear()

    monkeypatch.setattr("micast.xiaomi.device_manager.asyncio.sleep", sleep)
    await manager._watchdog_loop("a")
    assert delays == [3, 30]
    assert manager.player_observation("a") == {
        "status": None, "checked_at": None, "fresh": False,
    }
