from types import SimpleNamespace

import pytest

from micast.local_airplay import LocalAirPlayProvider


class FakeServer:
    def __init__(self, hostname, name, zeroconf):
        self.hostname = hostname
        self.name = name
        self.zeroconf = zeroconf
        self.on_play_start = None
        self.on_play_stop = None
        self._stream_server = SimpleNamespace(stream_url=f"http://{hostname}/{name}")
        self.started = 0
        self.stopped = 0

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1


class FakeZeroconf:
    def close(self):
        pass


@pytest.mark.asyncio
async def test_reconcile_keeps_unchanged_receiver_running():
    provider = LocalAirPlayProvider(FakeServer, FakeZeroconf)
    await provider.start([("living", "客厅")], "192.168.0.13", None, None)
    first = provider.receivers["living"].server

    await provider.start([("living", "客厅"), ("kitchen", "厨房")], "192.168.0.13", None, None)

    assert provider.receivers["living"].server is first
    assert first.started == 1
    assert provider.receivers["kitchen"].status == "running"
    await provider.stop()


@pytest.mark.asyncio
async def test_queued_callbacks_cannot_revive_replaced_session_or_receiver():
    import asyncio
    from unittest.mock import AsyncMock

    from micast.playback_sessions import PlaybackSessions

    provider = LocalAirPlayProvider(FakeServer, FakeZeroconf)
    provider.sessions = PlaybackSessions(lambda: 60)
    started, stopped = AsyncMock(), AsyncMock()
    await provider.start([("living", "客厅")], "localhost", started, stopped)
    server = provider.receivers["living"].server
    old = provider.sessions.begin("living", "airplay", "old")
    server.on_play_start(token=old.token)
    server.on_play_stop(token=old.token)
    provider.sessions.begin("living", "airplay", "new")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    started.assert_not_awaited()
    stopped.assert_not_awaited()
    current = provider.sessions.current("living")
    server.on_play_start(token=current.token)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    started.assert_awaited_once_with("living", False)
    server.on_play_start(token=current.token)
    await provider.start([("living", "改名")], "localhost", started, stopped)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert started.await_count == 1
    await provider.stop()
