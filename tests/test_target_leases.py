import asyncio

from micast.playback_sessions import PlaybackSessions


async def test_takeover_and_late_cleanup_never_stop_new_target():
    sessions = PlaybackSessions(lambda: 120)
    a = sessions.begin("a", "airplay")
    b = sessions.begin("b", "dlna")
    stops = []

    async def stop_a():
        stops.append("a")

    async def stop_b():
        stops.append("b")

    assert await sessions.targets.acquire("dlna-target:speaker", a.token, stop_a)
    assert await sessions.targets.acquire("dlna-target:speaker", b.token, stop_b)
    await sessions.targets.release("dlna-target:speaker", a.token)
    assert stops == ["a"]
    assert sessions.targets.owns("dlna-target:speaker", b.token)
    await sessions.close(b.token)
    assert stops == ["a", "b"]


async def test_concurrent_claims_are_serialized_and_closed_owner_cannot_reclaim():
    sessions = PlaybackSessions(lambda: 120)
    a = sessions.begin("a", "airplay2")
    b = sessions.begin("b", "dlna")

    async def release():
        await asyncio.sleep(0)

    await asyncio.gather(
        sessions.targets.acquire("airplay:speaker", a.token, release),
        sessions.targets.acquire("airplay:speaker", b.token, release),
    )
    assert sessions.targets.owns("airplay:speaker", b.token)
    assert not await sessions.targets.acquire("airplay:speaker", a.token, release)
