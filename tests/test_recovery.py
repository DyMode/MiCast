import asyncio

import pytest

from micast.playback_sessions import PlaybackSessions
from micast.recovery import RecoveryCoordinator


async def test_competing_recoveries_coalesce_and_pause_prevents_recovery():
    sessions = PlaybackSessions(lambda: 120)
    lease = sessions.begin("receiver", "airplay")
    coordinator = RecoveryCoordinator(sessions)
    called = []
    gate = asyncio.Event()

    async def work():
        called.append(True)
        await gate.wait()

    a = asyncio.create_task(coordinator.run("receiver", "rebuild", work))
    await asyncio.sleep(0)
    b = asyncio.create_task(coordinator.run("receiver", "source", work))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(a, b)
    assert len(called) == 1
    sessions.pause(lease.token)
    await coordinator.run("receiver", "reissue", work)
    assert len(called) == 1
    await coordinator.close()


@pytest.mark.parametrize("replace", [False, True])
async def test_pause_or_replacement_cancels_inflight_recovery(replace):
    sessions = PlaybackSessions(lambda: 120)
    lease = sessions.begin("receiver", "airplay", "first")
    coordinator = RecoveryCoordinator(sessions)
    entered = asyncio.Event()
    gate = asyncio.Event()
    writes = []

    async def recover():
        entered.set()
        await gate.wait()
        writes.append("stale")

    task = asyncio.create_task(coordinator.run("receiver", "source", recover))
    await entered.wait()
    if replace:
        sessions.begin("receiver", "airplay2", "second")
    else:
        sessions.pause(lease.token)
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not writes
    await coordinator.close()
