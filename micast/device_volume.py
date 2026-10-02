"""One hardware-volume adapter for Xiaomi, AirPlay and DLNA targets."""

import asyncio

from micast.config import settings


class DeviceVolume:
    def __init__(self, manager, bridge):
        self.manager = manager
        self.bridge = bridge
        self._lock_owner = manager if manager is not None else bridge
        if not hasattr(self._lock_owner, "_volume_locks"):
            self._lock_owner._volume_locks = {}

    def _lock_for(self, did):
        return self._lock_owner._volume_locks.setdefault(did, asyncio.Lock())

    def _adapter(self, did):
        for prefix, name in (("airplay:", "_airplay_targets"), ("dlna:", "_dlna_targets")):
            if did.startswith(prefix):
                adapter = getattr(self.bridge, name, None)
                if adapter is None:
                    raise ValueError("网络音箱服务不可用")
                return adapter, did[len(prefix) :]
        return self.manager, did

    async def get_volume(self, did, refresh=False):
        adapter, target = self._adapter(did)
        return await adapter.get_volume(target, refresh=refresh)

    async def set_volume(self, did, value):
        async with self._lock_for(did):
            return await self._set_volume(did, value)

    async def _set_volume(self, did, value):
        adapter, target = self._adapter(did)
        if adapter is self.manager:
            return await adapter.set_volume(target, value)
        return await adapter.set_device_volume(target, value)

    async def set_mute(self, did: str, muted: bool) -> int | None:
        async with self._lock_for(did):
            return await self._set_mute(did, muted)

    async def _set_mute(self, did: str, muted: bool) -> int | None:
        """Physical mute: remember the current level, drive it to 0, restore on
        unmute. External AirPlay targets have no readback, so the chain falls
        back to the last-seen cache and then default_volume."""
        if muted:
            if self.manager.is_muted(did):
                return None
            pre = await self.get_volume(did, refresh=True)
            if pre is None:
                pre = await self.get_volume(did, refresh=False)
            if pre is None:
                pre = settings.default_volume
            await self._set_volume(did, 0)
            self.manager._pre_mute_volumes[did] = pre
            self.manager._muted.add(did)
            return pre
        pre = self.manager._pre_mute_volumes.get(did)
        if pre is None:
            return None
        await self._set_volume(did, pre)
        self.manager._pre_mute_volumes.pop(did, None)
        self.manager._muted.discard(did)
        return pre

    def is_muted(self, did: str) -> bool:
        return self.manager.is_muted(did)
