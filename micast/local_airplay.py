"""Local classic AirPlay provider backed by MiCast's native RAOP receiver."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class LocalReceiver:
    id: str
    name: str
    status: str = "starting"
    stream_url: str = ""
    detail: str = ""
    server: Any = None


class LocalAirPlayProvider:
    def __init__(self, server_factory=None, zeroconf_factory=None):
        self.receivers: dict[str, LocalReceiver] = {}
        self._server_factory = server_factory
        self._zeroconf_factory = zeroconf_factory
        self._zeroconf = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.sessions = None

    @property
    def zeroconf(self):
        """The shared Zeroconf instance (None until the provider is started).

        Also used by AirPlay discovery to browse the LAN — one Zeroconf per
        process keeps mDNS traffic and sockets sane."""
        return self._zeroconf

    def ensure_zeroconf(self):
        """Discovery can use mDNS even when classic input is disabled."""
        if self._zeroconf is None:
            self._zeroconf = self._create_zeroconf()
        return self._zeroconf

    @property
    def own_macs(self) -> set[str]:
        """Lowercase MAC hex of every running MiCast receiver (for discovery
        self-exclusion). Deterministic per receiver name (uuid5), so it is
        stable across restarts."""
        return {item.server.mac.hex().lower() for item in self.receivers.values() if item.server}

    async def start(
        self,
        desired: list[tuple[str, str]],
        hostname: str,
        on_start: Callable[[str], Awaitable[None]] | None,
        on_stop: Callable[[str], Awaitable[None]] | None,
        on_volume: Callable[[str, int], Awaitable[None]] | None = None,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        wanted = dict(desired)
        for receiver_id, item in list(self.receivers.items()):
            if receiver_id in wanted and wanted[receiver_id] == item.name:
                continue
            if item.server:
                await item.server.stop()
            del self.receivers[receiver_id]

        for receiver_id, name in desired:
            if receiver_id in self.receivers and self.receivers[receiver_id].status == "running":
                continue
            item = LocalReceiver(id=receiver_id, name=name)
            self.receivers[receiver_id] = item
            server = None
            try:
                self.ensure_zeroconf()
                if self._server_factory is None:
                    from micast.ports import probe_udp_group
                    from micast.raop.server import udp_pool

                    base, top = udp_pool()
                    probe_udp_group(base, top - base + 1)
                server = self._create_server(hostname, name)
                server.playback_sessions = self.sessions
                server.receiver_id = receiver_id
                server.on_play_start = self._start_handler(receiver_id, on_start, server)
                server.on_play_stop = self._stop_handler(receiver_id, on_stop, server)
                server.on_volume = self._volume_handler(receiver_id, on_volume)
                await server.start()
                item.server = server
                item.status = "running"
                item.detail = "经典 AirPlay · 可连接"
            except Exception as exc:
                if server is not None:
                    try:
                        await server.stop()
                    except Exception:
                        logger.exception("Unable to clean up failed receiver %s", name)
                item.status = "error"
                item.detail = str(exc)
                logger.exception("Local AirPlay receiver %s failed", name)

    async def stop(self) -> None:
        for item in list(self.receivers.values()):
            if item.server:
                try:
                    await item.server.stop()
                except Exception:
                    logger.exception("Unable to stop local receiver %s", item.name)
        self.receivers.clear()
        if self._zeroconf:
            self._zeroconf.close()
            self._zeroconf = None

    async def disconnect(self, receiver_id: str | None = None) -> int:
        """Disconnect senders without removing the advertised receivers."""
        items = (
            [self.receivers[receiver_id]]
            if receiver_id in self.receivers
            else ([] if receiver_id is not None else list(self.receivers.values()))
        )
        counts = await asyncio.gather(
            *(item.server.disconnect_clients() for item in items if item.server),
            return_exceptions=True,
        )
        return sum(value for value in counts if isinstance(value, int))

    def _create_server(self, hostname: str, name: str):
        if self._server_factory:
            return self._server_factory(hostname, name, self._zeroconf)
        from micast.raop.server import RaopServer

        return RaopServer(hostname, name, self._zeroconf)

    def _create_zeroconf(self):
        if self._zeroconf_factory:
            return self._zeroconf_factory()
        from zeroconf import IPVersion, Zeroconf

        return Zeroconf(ip_version=IPVersion.All)

    def _schedule_event(self, receiver_id, callback, server, token, *args):
        async def deliver():
            item = self.receivers.get(receiver_id)
            if not item or item.server is not server:
                return
            if token is not None and self.sessions is not None:
                current = self.sessions.current(receiver_id)
                if (
                    current is None
                    or current.token != token
                    or current.state.value
                    in (
                        "closing",
                        "closed",
                    )
                ):
                    return
            await callback(receiver_id, *args)

        if callback and self._loop:
            asyncio.run_coroutine_threadsafe(deliver(), self._loop)

    def _start_handler(self, receiver_id, callback, server):
        def handle(resume: bool = False, token=None) -> None:
            self._schedule_event(receiver_id, callback, server, token, resume)

        return handle

    def _stop_handler(self, receiver_id, callback, server):
        def handle(token=None) -> None:
            self._schedule_event(receiver_id, callback, server, token)

        return handle

    def _volume_handler(self, receiver_id, callback):
        def handle(percent: int) -> None:
            if callback and self._loop:
                asyncio.run_coroutine_threadsafe(callback(receiver_id, percent), self._loop)

        return handle
