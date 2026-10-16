"""Startup latency guards.

The fnOS app centre treats a slow "enable" as a failure and can kill the
process, so every step that used to hold startup hostage must either run in the
background or concurrently. These tests pin the expensive ones: mDNS
publication, per-receiver bring-up, and the AirPlay 2 runtime.
"""

import asyncio
import threading
import time

import pytest

import micast.audio_bridge as bridge_module
import micast.local_airplay as local_airplay
import micast.raop.server as server_module
from micast.audio_bridge import AudioBridge
from micast.raop.server import RaopServer


@pytest.mark.asyncio
async def test_receiver_start_returns_before_mdns_announcement_finishes(monkeypatch):
    released = threading.Event()
    announced = []

    class Zeroconf:
        def register_service(self, service):
            # Stands in for the announcement round that blocks 1-5s on a busy LAN.
            released.wait(timeout=5)
            announced.append(service)

        def unregister_service(self, service):
            pass

    class Server:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def fake_rtsp(client):
        return Server(), 45001

    monkeypatch.setattr(server_module, "_start_rtsp_server", fake_rtsp)
    receiver = RaopServer("127.0.0.1", "Latency", Zeroconf())

    started = time.monotonic()
    await receiver.start()
    elapsed = time.monotonic() - started

    # start() must not wait for the announcement thread; the socket is bound.
    assert elapsed < 0.5
    assert announced == []

    released.set()
    await receiver.wait_announced()
    assert announced
    await receiver.stop()


@pytest.mark.asyncio
async def test_local_provider_starts_receivers_concurrently():
    """Four receivers must overlap, not queue up one after another."""
    started: list[str] = []
    active = 0
    peak = 0

    class Server:
        def __init__(self, hostname, name, zeroconf):
            self.name = name

        async def start(self):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.15)
            active -= 1
            started.append(self.name)

        async def stop(self):
            pass

    provider = local_airplay.LocalAirPlayProvider(
        Server, zeroconf_factory=lambda: type("Z", (), {"close": lambda self: None})()
    )
    provider._zeroconf = type("Z", (), {"close": lambda self: None})()

    await provider.start(
        [("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")], "127.0.0.1", None, None
    )

    assert len(started) == 4
    # Sequential start would peak at 1.
    assert peak == 4
    await provider.stop()


def _partial_bridge(monkeypatch, slow: float) -> tuple[AudioBridge, list[str]]:
    """Bridge stub that only carries what the deferred AirPlay 2 start needs."""
    bridge = object.__new__(AudioBridge)
    bridge._airplay2_lock = asyncio.Lock()
    bridge._airplay2_runtime = {}
    bridge._aux_tasks = set()
    bridge._airplay2_startup_task = None
    monkeypatch.setattr(
        bridge_module.settings,
        "airplay2_instances",
        [type("Instance", (), {"id": "ap2", "name": "MiCast", "enabled": True})()],
    )
    runs: list[str] = []

    async def slow_start():
        runs.append("start")
        await asyncio.sleep(slow)

    monkeypatch.setattr(bridge, "_start_airplay2_pipelines", slow_start)
    return bridge, runs


@pytest.mark.asyncio
async def test_airplay2_startup_is_deferred_out_of_the_engine_start(monkeypatch):
    bridge, runs = _partial_bridge(monkeypatch, slow=0.3)

    started = time.monotonic()
    await bridge._defer_airplay2_startup()
    elapsed = time.monotonic() - started

    # The engine start must not wait for shairport.
    assert elapsed < 0.1
    # Observers see the runtime as starting right away, not as missing.
    assert bridge._airplay2_runtime["ap2"]["status"] == "starting"

    await asyncio.sleep(0.4)
    assert runs == ["start"]


@pytest.mark.asyncio
async def test_deferred_airplay2_start_cancels_the_previous_attempt(monkeypatch):
    bridge, runs = _partial_bridge(monkeypatch, slow=0.5)

    await bridge._defer_airplay2_startup()
    first = bridge._airplay2_startup_task
    # Give the first attempt a tick so it is genuinely in flight.
    await asyncio.sleep(0.05)
    assert runs == ["start"]

    await bridge._defer_airplay2_startup()

    assert first.cancelled() or first.done()
    assert bridge._airplay2_startup_task is not first
    await bridge._airplay2_startup_task
    assert runs == ["start", "start"]


def _status_bridge(monkeypatch, receiver_states, airplay2_states):
    from types import SimpleNamespace

    bridge = object.__new__(AudioBridge)
    bridge._airplay2_runtime = {
        f"ap2-{index}": {"status": state} for index, state in enumerate(airplay2_states)
    }
    bridge._local_provider = SimpleNamespace(
        receivers={
            f"r{index}": SimpleNamespace(status=state)
            for index, state in enumerate(receiver_states)
        }
    )
    bridge._status = "running"
    bridge._running = True
    monkeypatch.setattr(bridge_module.settings, "airplay_engine", "local")
    monkeypatch.setattr(bridge_module.settings, "airplay2_enabled", True)
    monkeypatch.setattr(bridge_module.settings, "dlna_enabled", False)
    return bridge


def test_airplay2_port_conflict_degrades_instead_of_failing_the_app(monkeypatch):
    """An occupied AirPlay 2 port must cost AirPlay 2 only.

    The engine reports "degraded" (readiness stays 200) as long as another
    protocol still works; only an all-receiver failure is "error".
    """
    degraded = _status_bridge(monkeypatch, ["running"] * 4, ["error"])
    assert degraded._derive_status() == "degraded"

    all_failed = _status_bridge(monkeypatch, ["error"] * 4, ["error"])
    assert all_failed._derive_status() == "error"
