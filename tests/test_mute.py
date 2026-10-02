"""Physical-mute tests: state storage, DeviceVolume round-trip, playback state."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from micast.config import settings
from micast.device_volume import DeviceVolume
from micast.routes import playback
from micast.routes.playback import build_playback_state
from micast.xiaomi.device_manager import DeviceManager


class FakeManager:
    """Models the DeviceManager surface DeviceVolume.set_mute touches."""

    def __init__(self, volumes: dict[str, int] | None = None):
        self.volumes: dict[str, int] = dict(volumes or {})
        self._muted: set[str] = set()
        self._pre_mute_volumes: dict[str, int] = {}

    def is_muted(self, did: str) -> bool:
        return did in self._muted

    def muted_devices(self) -> list[str]:
        return sorted(self._muted)

    async def get_volume(self, did: str, refresh: bool = False) -> int | None:
        return self.volumes.get(did)

    async def set_volume(self, did: str, value: int) -> int:
        self.volumes[did] = value
        return value

    def get_control_targets(self) -> list[dict]:
        return [{"deviceID": did} for did in self.volumes]


async def test_device_volume_mute_round_trip():
    manager = FakeManager({"a": 40})
    dv = DeviceVolume(manager, None)
    assert await dv.set_mute("a", True) == 40
    assert manager.volumes["a"] == 0
    assert dv.is_muted("a")
    assert await dv.set_mute("a", False) == 40
    assert manager.volumes["a"] == 40
    assert not dv.is_muted("a")


async def test_device_volume_mute_is_idempotent():
    manager = FakeManager({"a": 40})
    dv = DeviceVolume(manager, None)
    await dv.set_mute("a", True)
    assert await dv.set_mute("a", True) is None
    assert manager.volumes["a"] == 0


async def test_failed_mute_and_unmute_remain_retryable():
    manager = FakeManager({"a": 40})
    adapter = DeviceVolume(manager, None)
    original = manager.set_volume

    async def fail(*args):
        raise OSError("offline")

    manager.set_volume = fail
    with pytest.raises(OSError):
        await adapter.set_mute("a", True)
    assert not adapter.is_muted("a")
    manager.set_volume = original
    await adapter.set_mute("a", True)
    manager.set_volume = fail
    with pytest.raises(OSError):
        await adapter.set_mute("a", False)
    assert adapter.is_muted("a")
    assert manager._pre_mute_volumes["a"] == 40
    manager.set_volume = original
    await adapter.set_mute("a", False)
    assert manager.volumes["a"] == 40


async def test_device_volume_mute_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(settings, "default_volume", 25)
    manager = FakeManager({})
    dv = DeviceVolume(manager, None)
    assert await dv.set_mute("a", True) == 25
    assert manager.volumes["a"] == 0
    assert await dv.set_mute("a", False) == 25
    assert manager.volumes["a"] == 25


def test_device_manager_muted_state():
    dm = DeviceManager(auth=None)
    dm._muted.add("a")
    assert dm.is_muted("a")
    assert not dm.is_muted("b")
    assert dm.muted_devices() == ["a"]


async def test_build_playback_state_reports_muted():
    class FakeDM:
        def get_control_targets(self):
            return [{"deviceID": "a"}, {"deviceID": "b"}]

        def is_playing(self, did):
            return did == "a"

        def is_paused(self, did):
            return False

        def is_muted(self, did):
            return did == "b"

        async def get_volume(self, did, refresh=False):
            return 40 if did == "a" else 0

        def get_alias(self, did):
            return did

    state = await build_playback_state(FakeDM())
    devices = {d["did"]: d for d in state["devices"]}
    assert devices["a"]["muted"] is False
    assert devices["b"]["muted"] is True
    assert state["muted"] is False  # only one of two is muted


def test_mute_endpoint_round_trip(monkeypatch):
    async def run():
        manager = FakeManager({"a": 40, "b": 60})
        old_routes = list(playback.router.routes)
        try:
            playback.install(None, manager)
            endpoint = [
                r.endpoint for r in playback.router.routes if r.path == "/api/playback/mute"
            ][-1]
            result = await endpoint({"muted": True, "device_ids": ["a", "b"]})
            assert result["ok"]
            assert manager.volumes == {"a": 0, "b": 0}
            result = await endpoint({"muted": False, "device_ids": ["a", "b"]})
            assert manager.volumes == {"a": 40, "b": 60}
        finally:
            playback.router.routes[:] = old_routes

    asyncio.run(run())


def test_mute_endpoint_rejects_non_bool():
    async def run():
        manager = SimpleNamespace()
        old_routes = list(playback.router.routes)
        try:
            playback.install(None, manager)
            endpoint = [
                r.endpoint for r in playback.router.routes if r.path == "/api/playback/mute"
            ][-1]
            with pytest.raises(HTTPException):
                await endpoint({"muted": "yes"})
        finally:
            playback.router.routes[:] = old_routes

    asyncio.run(run())
