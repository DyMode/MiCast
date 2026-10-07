"""Concurrent local output, cleanup failure and discovery shutdown regressions."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from micast.dlna_client import DlnaDevice, DlnaDiscovery, DlnaTargetManager
from micast.playback_sessions import PlaybackSessions


def output():
    discovery = DlnaDiscovery()
    discovery._devices["d"] = DlnaDevice(
        "d", "音箱", control_url="http://d/control", last_seen=time.monotonic()
    )
    sessions = PlaybackSessions(lambda: 120)
    lease = sessions.begin("r", "airplay")
    manager = DlnaTargetManager(
        discovery,
        sessions,
        stream_metrics=SimpleNamespace(sink_last_byte_at=lambda *_: 0),
        source_active=lambda _: False,
    )
    manager._soap = AsyncMock()
    return manager, sessions, lease


async def test_concurrent_starts_and_reconcile_keep_one_play_and_monitor():
    manager, sessions, lease = output()
    try:
        await asyncio.gather(
            *(manager.play_targets("r", ["d"], "http://m/stream/r") for _ in range(3))
        )
        runtime = manager._targets["r"]["d"]
        await manager.reconcile("r", ["d"])
        assert manager._targets["r"]["d"] is runtime
        assert len(manager._monitors) == 1
        assert [call.args[1] for call in manager._soap.await_args_list] == [
            "SetAVTransportURI",
            "Play",
        ]
        await manager.stop_targets("r")
        assert runtime.monitor.done()
        assert not sessions.targets.owns("dlna-target:d", lease.token)
        assert runtime.status == "idle"
    finally:
        await manager.close()


async def test_url_replacement_cancels_old_monitor_and_stops_before_play():
    manager, _, _ = output()
    try:
        await manager.play_targets("r", ["d"], "http://m/stream/r")
        old = manager._targets["r"]["d"]
        await manager.play_targets("r", ["d"], "http://m/stream/replacement")
        assert old.monitor.done()
        assert [call.args[1] for call in manager._soap.await_args_list] == [
            "SetAVTransportURI",
            "Play",
            "Stop",
            "SetAVTransportURI",
            "Play",
        ]
        assert len(manager._monitors) == 1
    finally:
        await manager.close()


async def test_removing_every_target_stops_playback_and_later_add_can_resume():
    manager, sessions, lease = output()
    try:
        await manager.play_targets("r", ["d"], "http://m/stream/r")
        old = manager._targets["r"]["d"]
        await manager.reconcile("r", [])
        assert not manager._targets["r"]
        assert old.monitor.done()
        assert not sessions.targets.owns("dlna-target:d", lease.token)
        await manager.reconcile("r", ["d"])
        assert manager._targets["r"]["d"].status == "connecting"
        assert sessions.targets.owns("dlna-target:d", lease.token)
    finally:
        await manager.close()


async def test_failed_stop_keeps_original_ownership_and_never_starts_replacement():
    manager, sessions, lease = output()
    try:
        await manager.play_targets("r", ["d"], "http://m/stream/r")
        old = manager._targets["r"]["d"]
        manager._soap.side_effect = httpx.ReadTimeout("Stop timeout")
        await manager.play_targets("r", ["d"], "http://m/stream/replacement")
        assert manager._targets["r"]["d"] is old
        assert old.failure_stage == "stop_failed"
        assert old.command_sent
        assert sessions.targets.owns("dlna-target:d", lease.token)
        assert len(manager._soap.await_args_list) == 3
        manager._soap.side_effect = None
        await manager.stop_targets("r")
        assert not sessions.targets.owns("dlna-target:d", lease.token)
    finally:
        await manager.close()


async def test_stop_waits_for_slow_start_and_leaves_no_monitor():
    manager, sessions, lease = output()
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def soap(device, action, arguments):
        calls.append(action)
        if action == "SetAVTransportURI":
            entered.set()
            await finish.wait()

    manager._soap = soap
    try:
        start = asyncio.create_task(manager.play_targets("r", ["d"], "http://m/stream/r"))
        await asyncio.wait_for(entered.wait(), 1)
        stop = asyncio.create_task(manager.stop_targets("r"))
        await asyncio.sleep(0)
        finish.set()
        await asyncio.wait_for(asyncio.gather(start, stop), 1)
        assert calls == ["SetAVTransportURI", "Play", "Stop"]
        assert not manager._monitors
        assert not sessions.targets.owns("dlna-target:d", lease.token)
    finally:
        await manager.close()


async def test_discovery_stop_awaits_worker_cleanup():
    discovery = DlnaDiscovery()
    ready, closed = asyncio.Event(), asyncio.Event()

    async def worker():
        try:
            ready.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            closed.set()

    discovery._task = asyncio.create_task(worker())
    await ready.wait()
    await discovery.stop()
    assert closed.is_set()
    assert discovery._task is None


async def test_rescan_does_not_fetch_more_descriptions_after_discovery_is_disabled():
    discovery = DlnaDiscovery()
    sent = asyncio.Event()
    discovery._transport = SimpleNamespace(sendto=lambda *_: sent.set(), close=lambda: None)
    discovery._fetch_pending = AsyncMock()
    request = asyncio.create_task(discovery.rescan())
    await asyncio.wait_for(sent.wait(), 1)
    await discovery.stop()
    await asyncio.wait_for(request, 4)
    discovery._fetch_pending.assert_not_awaited()


async def test_discovery_does_not_publish_description_after_shutdown(monkeypatch):
    discovery = DlnaDiscovery()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def get(self, url):
        entered.set()
        await finish.wait()
        return httpx.Response(
            200,
            text="<root><device><deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType><UDN>uuid:late</UDN></device></root>",
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    fetch = asyncio.create_task(discovery._fetch_description("http://d/device.xml"))
    await asyncio.wait_for(entered.wait(), 1)
    await discovery.stop()
    finish.set()
    await fetch
    assert not discovery.devices()


async def test_command_acceptance_does_not_claim_audio_is_flowing():
    manager, sessions, lease = output()
    try:
        await manager.play_targets("r", ["d"], "http://m/stream/r")
        runtime = manager._targets["r"]["d"]
        assert runtime.status == "connecting"
        assert runtime.timings.get("first_audio_byte") is None
    finally:
        await manager.close()
