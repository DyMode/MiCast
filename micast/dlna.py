"""UPnP renderer with one bounded local download and generation-owned output.

Every speaker receives a local URL. Raw MP3 and processed audio share the same
cache; network failures are reported without switching to external URLs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import socket
import uuid
from copy import copy
from dataclasses import dataclass
from email.utils import formatdate
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from micast.config import ReceiverConfig, settings
from micast.dlna_delivery import DeliverySuperseded, DlnaDelivery
from micast.dlna_events import DlnaEvents
from micast.media_cache import MediaCache
from micast.playback_sessions import PlaybackSessions, SessionState
from micast.xiaomi.device_manager import DeviceManager

logger = logging.getLogger(__name__)

SSDP_ADDRESS = "239.255.255.250"
SSDP_PORT = 1900
SERVER_HEADER = "MiCast/0.1 UPnP/1.0 DLNA/1.5"
MEDIA_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
AV_TRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
RENDERING_CONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"
CONNECTION_MANAGER = "urn:schemas-upnp-org:service:ConnectionManager:1"
DLNA_SINK_PROTOCOLS = ",".join(
    f"http-get:*:{mime}:*"
    for mime in ("audio/mpeg", "audio/mp4", "audio/aac", "audio/flac", "audio/wav")
)


@dataclass
class DlnaTransportState:
    display_audio_id: str = ""
    display_match_key: tuple | None = None
    display_cover_url: str = ""
    uri: str = ""
    metadata: str = ""
    next_uri: str = ""
    next_metadata: str = ""
    state: str = "STOPPED"
    volume: int = 50
    muted: bool = False
    volume_mode: str = ""
    session_id: str = ""
    muted_volumes: dict[str, int] | None = None
    volume_received: bool = False
    finished: bool = False
    error: str = ""
    source_path: str = ""


class _SsdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, service: DlnaService):
        self.service = service
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        text = data.decode("utf-8", errors="ignore")
        if not text.startswith("M-SEARCH") or "ssdp:discover" not in text.lower():
            return
        headers = _headers(text)
        search_target = headers.get("st", "ssdp:all")
        # Response construction is synchronous and non-blocking; creating one
        # task per multicast discovery packet allows a LAN burst to create an
        # unbounded task backlog.
        self.service.respond(addr, search_target)


class DlnaService:
    """Advertise and control one virtual DMR for every active playback target."""

    def __init__(
        self, device_manager: DeviceManager, sessions: PlaybackSessions | None = None, media=None
    ):
        self.device_manager = device_manager
        self.sessions = sessions or PlaybackSessions(lambda: settings.stale_session_timeout)
        self.sessions.on_state.append(self._session_changed)
        self.media = media
        self.cache = MediaCache()
        self._play_tasks: dict[str, asyncio.Task] = {}
        # Wired to the speaker-facing server in main.py.
        self.stream_server = None
        self.delivery = DlnaDelivery(self)
        self._positions = {}
        self.states: dict[str, DlnaTransportState] = {}
        self.events = DlnaEvents(self.event_snapshot)
        self._next_tasks: dict[str, asyncio.Task] = {}
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _SsdpProtocol | None = None
        self._announce_task: asyncio.Task | None = None
        self._advertised: dict[str, ReceiverConfig] = {}
        self._boot_id = 1
        self._location_host = ""
        self.status = "stopped"
        self.detail = "DLNA 已关闭"
        self.http_available = True
        self.http_detail = ""

    def active_receivers(self) -> list[ReceiverConfig]:
        return (
            [
                item
                for item in settings.active_receivers()
                if item.target_type != "dlna" and item.dlna_enabled is not False
            ]
            if settings.dlna_enabled
            else []
        )

    def receiver(self, receiver_id: str) -> ReceiverConfig | None:
        if self.status in {"error", "unsupported"} or not self.http_available:
            return None
        return next((item for item in self.active_receivers() if item.id == receiver_id), None)

    async def start(self) -> None:
        from micast.deployment import classic_ingress_available

        if not classic_ingress_available():
            self.status = "unsupported"
            self.detail = "当前控制器使用隔离网络，不支持局域网 DLNA 接收；可改用 host 网络部署"
            return
        if not settings.dlna_enabled:
            self.status = "stopped"
            self.detail = "DLNA 已关闭"
            return
        if not self.http_available:
            self.status = "error"
            self.detail = f"DLNA 控制端口不可用: {self.http_detail}"
            return
        if self._transport:
            await self.reconcile()
            return
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", SSDP_PORT))
            interface_ip = _multicast_interface_ip()
            membership = socket.inet_aton(SSDP_ADDRESS) + socket.inet_aton(interface_ip)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            if interface_ip != "0.0.0.0":
                sock.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_MULTICAST_IF,
                    socket.inet_aton(interface_ip),
                )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.setblocking(False)
            loop = asyncio.get_running_loop()
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: _SsdpProtocol(self), sock=sock
            )
            self._transport = transport
            self._protocol = protocol
            self.status = "running"
            self._advertised = {item.id: item for item in self.active_receivers()}
            self.detail = f"DLNA · {len(self.active_receivers())} 个播放入口"
            await self.announce("ssdp:alive")
            self._announce_task = asyncio.create_task(self._announce_loop())
            logger.info("DLNA discovery listening on UDP %s", SSDP_PORT)
        except Exception as exc:
            if sock is not None and self._transport is None:
                sock.close()
            self.status = "error"
            self.detail = f"SSDP 启动失败: {exc}"
            logger.exception("Unable to start DLNA discovery")

    async def stop(self) -> None:
        for receiver_id in self.states:
            self.delivery.cancel(receiver_id)
        pending = list({
            task for task in [*self._play_tasks.values(), *self.delivery._tasks.values()]
            if task is not asyncio.current_task() and not task.done()
        })
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._play_tasks.clear()
        await self.events.close()
        tasks = list(self._next_tasks.values())
        self._next_tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for receiver_id in list(self.states):
            lease = self.sessions.current(self._owner(receiver_id))
            if lease:
                await self.sessions.close(lease.token, "dlna_disabled")
        await self.cache.close()
        if self._transport:
            await self.announce("ssdp:byebye", self._advertised.values())
        if self._announce_task:
            self._announce_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._announce_task
        self._announce_task = None
        if self._transport:
            self._transport.close()
        self._transport = None
        self._protocol = None
        self._advertised.clear()
        self.status = "stopped"
        self.detail = "DLNA 已关闭"

    async def reconcile(self) -> None:
        if not settings.dlna_enabled:
            await self.stop()
            return
        active = {item.id: item for item in self.active_receivers()}
        removed = [item for key, item in self._advertised.items() if key not in active]
        if removed:
            await self.announce("ssdp:byebye", removed)
        valid = set(active)
        for receiver_id in set(self.states) - valid:
            await self.cancel_next(receiver_id)
            lease = self.sessions.current(self._owner(receiver_id))
            if lease:
                await self.sessions.close(lease.token, "receiver_removed")
            await self.cache.release(receiver_id)
            self.delivery.reset(receiver_id)
        self.states = {key: value for key, value in self.states.items() if key in valid}
        if not self._transport:
            await self.start()
            return
        self._advertised = active
        self.detail = f"DLNA · {len(valid)} 个播放入口"
        await self.announce("ssdp:alive")

    async def _announce_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            await self.announce("ssdp:alive")

    def uuid_for(self, receiver_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"micast:dlna:{receiver_id}"))

    def location_for(self, receiver_id: str) -> str:
        return (
            f"http://{settings.effective_stream_host}:{settings.port}"
            f"/dlna/{receiver_id}/description.xml"
        )

    def search_targets(self, receiver: ReceiverConfig) -> list[tuple[str, str]]:
        device_uuid = f"uuid:{self.uuid_for(receiver.id)}"
        return [
            ("upnp:rootdevice", f"{device_uuid}::upnp:rootdevice"),
            (device_uuid, device_uuid),
            (MEDIA_RENDERER, f"{device_uuid}::{MEDIA_RENDERER}"),
            (AV_TRANSPORT, f"{device_uuid}::{AV_TRANSPORT}"),
            (RENDERING_CONTROL, f"{device_uuid}::{RENDERING_CONTROL}"),
            (CONNECTION_MANAGER, f"{device_uuid}::{CONNECTION_MANAGER}"),
        ]

    def respond(self, addr, requested: str) -> None:
        if not self._transport:
            return
        self._refresh_boot_id()
        for receiver in self.active_receivers():
            for target, usn in self.search_targets(receiver):
                if requested not in ("ssdp:all", target):
                    continue
                packet = "\r\n".join(
                    [
                        "HTTP/1.1 200 OK",
                        "CACHE-CONTROL: max-age=120",
                        f"DATE: {formatdate(usegmt=True)}",
                        "EXT:",
                        f"LOCATION: {self.location_for(receiver.id)}",
                        f"SERVER: {SERVER_HEADER}",
                        f"ST: {target}",
                        f"USN: {usn}",
                        f"BOOTID.UPNP.ORG: {self._boot_id}",
                        "CONFIGID.UPNP.ORG: 1",
                        "",
                        "",
                    ]
                ).encode()
                self._transport.sendto(packet, addr)

    async def announce(self, subtype: str, receivers=None) -> None:
        if not self._transport:
            return
        self._refresh_boot_id()
        destination = (SSDP_ADDRESS, SSDP_PORT)
        for receiver in receivers if receivers is not None else self.active_receivers():
            for target, usn in self.search_targets(receiver):
                lines = [
                    "NOTIFY * HTTP/1.1",
                    f"HOST: {SSDP_ADDRESS}:{SSDP_PORT}",
                    f"NT: {target}",
                    f"NTS: {subtype}",
                    f"USN: {usn}",
                ]
                if subtype == "ssdp:alive":
                    lines.extend(
                        [
                            "CACHE-CONTROL: max-age=120",
                            f"LOCATION: {self.location_for(receiver.id)}",
                            f"SERVER: {SERVER_HEADER}",
                            f"BOOTID.UPNP.ORG: {self._boot_id}",
                            "CONFIGID.UPNP.ORG: 1",
                        ]
                    )
                packet = ("\r\n".join(lines) + "\r\n\r\n").encode()
                self._transport.sendto(packet, destination)

    def _refresh_boot_id(self) -> None:
        host = settings.effective_stream_host
        if self._location_host and host != self._location_host:
            self._boot_id += 1
            logger.info("DLNA publish address changed: %s -> %s", self._location_host, host)
        self._location_host = host

    def state_for(self, receiver_id: str) -> DlnaTransportState:
        return self.states.setdefault(receiver_id, DlnaTransportState())

    def event_snapshot(self, receiver_id, service):
        if self.receiver(receiver_id) is None:
            return None
        state = self.state_for(receiver_id)
        if service == "AVTransport":
            return {
                "TransportState": state.state,
                "TransportStatus": "ERROR_OCCURRED" if state.error else "OK",
                "TransportPlaySpeed": "1",
                "AVTransportURI": state.uri,
                "AVTransportURIMetaData": state.metadata,
                "CurrentTrackURI": state.uri,
                "CurrentTrackMetaData": state.metadata,
                "CurrentTrackDuration": self.duration_time(receiver_id),
                "CurrentMediaDuration": self.duration_time(receiver_id),
                "NextAVTransportURI": state.next_uri,
                "NextAVTransportURIMetaData": state.next_metadata,
            }
        if service == "RenderingControl":
            return {"Volume": state.volume, "Mute": int(state.muted)}
        return {
            "SourceProtocolInfo": "",
            "SinkProtocolInfo": DLNA_SINK_PROTOCOLS,
            "CurrentConnectionIDs": "0",
        }

    def _owner(self, receiver_id: str) -> str:
        """DLNA ingress namespaces speaker ownership so it can coexist with an
        AirPlay session targeting the same receiver_id."""
        return f"dlna:{receiver_id}"

    async def set_uri(self, receiver_id: str, uri: str, metadata: str = "") -> None:
        # An empty CurrentURI must fault (701) before touching state: some
        # controllers probe with it first, and wiping the transport state here
        # leaves the control point polling a renderer that has nothing to play.
        if not uri or not uri.strip():
            raise ValueError("媒体地址不能为空")
        self._validate_uri(uri)
        await self.cancel_next(receiver_id)
        lease = self.sessions.current(self._owner(receiver_id))
        if lease:
            await self.sessions.close(lease.token, "media_replaced")
        state = self.state_for(receiver_id)
        state.uri = uri
        state.metadata = metadata
        state.display_audio_id = ""
        state.display_cover_url = ""
        state.display_match_key = None
        state.next_uri = state.next_metadata = ""
        state.finished = False
        state.error = ""
        state.state = "STOPPED"
        state.volume_mode = settings.sender_volume_mode
        state.session_id = uuid.uuid4().hex
        state.source_path = ""
        self.delivery.reset(receiver_id)
        self._positions[receiver_id] = 0
        if self.media is not None:
            self.media.set_position(receiver_id, 0)
        await self.cache.prepare(receiver_id, uri, state.session_id)
        logger.info("DLNA %s media received; background download scheduled", receiver_id)

    async def set_next_uri(self, receiver_id: str, uri: str, metadata: str = "") -> None:
        self._validate_uri(uri)
        state = self.state_for(receiver_id)
        state.next_uri = uri
        state.next_metadata = metadata
        logger.info("DLNA %s queued next media URI: %s", receiver_id, uri)

    @staticmethod
    def _validate_uri(uri):
        if uri and (urlsplit(uri).scheme not in ("http", "https") or not urlsplit(uri).hostname):
            raise ValueError("媒体地址必须是 HTTP 或 HTTPS")

    async def match_track_display(self, receiver_id, state):
        """Attach a library song before either raw or DSP playback is issued."""
        if not settings.touchscreen_lyrics:
            state.display_audio_id = ""
            state.display_cover_url = ""
            return
        values, _ = self._media_metadata(state)
        title = values.get("title") or ""
        artist = values.get("artist") or values.get("creator") or ""
        # Reuse sender-title normalization (e.g. "song - artist") while
        # keeping the controller's original metadata for display.
        from micast.raop.dmap import track_meta

        body = b""
        for tag, value in (("minm", title), ("asar", artist)):
            encoded = value.encode("utf-8")
            body += tag.encode() + len(encoded).to_bytes(4, "big") + encoded
        normalized = track_meta(body)
        title = normalized.get("derived") or normalized.get("title") or title
        artist = normalized.get("artist") or artist
        key = (state.uri, title, artist)
        if state.display_match_key == key:
            return
        state.display_audio_id = ""
        state.display_cover_url = ""
        if not title:
            return
        identity = state.session_id
        try:
            hit = await asyncio.wait_for(
                self.device_manager.search_track(title, artist, fuzzy_fallback=False), timeout=5
            )
        except Exception as exc:
            logger.warning("DLNA track display lookup failed for %s: %s", receiver_id, exc)
            return
        if state.session_id != identity:
            return
        state.display_match_key = key
        state.display_audio_id = str((hit or {}).get("audio_id") or "")
        state.display_cover_url = str((hit or {}).get("cover_url") or "")
        logger.info(
            "DLNA track display for %s: %s",
            receiver_id, "matched" if state.display_audio_id else "no library match",
        )

    def media_volume(self, receiver_id: str, session_id: str) -> int:
        state = self.states.get(receiver_id)
        if not state or state.session_id != session_id or state.muted or state.state != "PLAYING":
            return 0
        return state.volume if state.volume_mode == "independent" else 100

    def request_play(self, receiver_id: str, *, advance_next: bool = False) -> None:
        """Accept SOAP Play promptly; preparation reports TRANSITIONING."""
        state = self.state_for(receiver_id)
        if self.receiver(receiver_id) is None or not state.uri:
            raise ValueError("No media URI or playback target")
        pending = self._play_tasks.get(receiver_id)
        if pending and not pending.done():
            return
        active = self.delivery._tasks.get(receiver_id)
        if active is not None and not active.done():
            return
        if state.state == "PLAYING" and not advance_next:
            return
        resume = state.state == "PAUSED_PLAYBACK"
        identity = state.session_id
        state.state = "TRANSITIONING"
        state.error = ""

        async def start():
            try:
                await self.play(receiver_id, advance_next=advance_next, resume=resume)
            except (asyncio.CancelledError, DeliverySuperseded):
                pass
            except Exception:
                if state.session_id == identity:
                    state.state = "STOPPED"
                    state.error = "播放启动失败"
                    await self.cache.release(receiver_id)
                logger.exception("DLNA %s background playback failed", receiver_id)
            finally:
                if self._play_tasks.get(receiver_id) is asyncio.current_task():
                    self._play_tasks.pop(receiver_id, None)

        self._play_tasks[receiver_id] = asyncio.create_task(
            start(), name=f"dlna-play:{receiver_id}"
        )

    async def play(self, receiver_id: str, *, advance_next: bool = False, resume=None) -> None:
        receiver = self.receiver(receiver_id)
        state = self.state_for(receiver_id)
        if receiver is None or not state.uri:
            raise ValueError("No media URI or playback target")
        previous = copy(state)
        resuming = state.state == "PAUSED_PLAYBACK" if resume is None else resume
        # Explicit compatibility opt-in for controllers that use Play as Next.
        # Ordinary repeated Play remains idempotent and preserves the queue.
        if advance_next and state.state != "PAUSED_PLAYBACK" and state.next_uri:
            await self.next_track(receiver_id)
            return
        elif state.state == "PLAYING":
            return
        if state.finished:
            self._positions[receiver_id] = 0
        state.finished = False
        state.error = ""
        targets = settings.receiver_targets(receiver_id)
        state.volume_mode = state.volume_mode or settings.sender_volume_mode
        requested_id = state.session_id
        state.state = "TRANSITIONING"
        try:
            await self.delivery.play(
                receiver_id, state, resume=resuming
            )
        except (asyncio.CancelledError, DeliverySuperseded):
            raise
        except BaseException:
            if state.session_id != requested_id:
                raise
            current = self.sessions.current(self._owner(receiver_id))
            if (
                current is not None
                and current.identity == previous.session_id
                and current.state == SessionState.ACTIVE
            ):
                state.__dict__.update(previous.__dict__)
            elif state.state != "PAUSED_PLAYBACK":
                state.state = "STOPPED"
                state.error = "播放启动失败"
            raise
        state.state = "PLAYING"
        state.error = ""
        local = self.delivery.modes.get(receiver_id) == "local"
        if local and self.media is not None:
            self.media.set_volume(receiver_id, state)
        if settings.default_volume_enabled and state.volume_mode == "independent":
            for did in targets:
                if self.device_manager.owner_of(did) == self._owner(receiver_id):
                    await self.device_manager.set_volume(did, settings.default_volume)
        if local:
            if state.volume_mode == "linked" and (state.volume_received or state.muted):
                for did in targets:
                    if self.device_manager.owner_of(did) == self._owner(receiver_id):
                        await self.device_manager.set_volume(
                            did, 0 if state.muted else state.volume
                        )
                if state.volume_received:
                    await self._network_source_volume(receiver_id, state.volume)
        elif state.volume_received or state.muted:
            if state.muted:
                await self._set_speaker_mute(receiver_id, True)
            else:
                for did in self._owned_volume_targets(receiver_id):
                    # Direct mode folds controller volume into cloud commands.
                    await self.device_manager.set_volume(did, state.volume)

    def _register_speakers(self, receiver_id, lease, targets, results):
        for did, result in zip(targets, results, strict=True):
            if result is True and f"speaker:{did}" not in lease.resources:
                self.sessions.register(
                    lease.token,
                    f"speaker:{did}",
                    lambda did=did: self.device_manager.stop_playback(
                        did, owner=self._owner(receiver_id)
                    ),
                    kind="speaker",
                )

    async def seek(self, receiver_id: str, target_seconds: float) -> None:
        """DLNA Seek (REL_TIME), decoded from the already downloaded file."""
        state = self.state_for(receiver_id)
        if not state.uri:
            raise ValueError("No media URI for seek")
        self.delivery.cancel(receiver_id)
        await self.delivery.seek(receiver_id, state, max(0, target_seconds))

    async def pause(self, receiver_id: str) -> None:
        self.delivery.cancel(receiver_id)
        await self._cancel_play(receiver_id)
        if self.delivery.modes.get(receiver_id) == "local" and self.media is not None:
            await self.media.cancel_pending(receiver_id)
        self._positions[receiver_id] = self.delivery.position(receiver_id)
        await asyncio.gather(
            *(
                self.device_manager.stop(did, owner=self._owner(receiver_id))
                for did in settings.receiver_targets(receiver_id)
            )
        )
        self.state_for(receiver_id).state = "PAUSED_PLAYBACK"
        lease = self.sessions.current(self._owner(receiver_id))
        if lease:
            self.sessions.pause(lease.token)
            await self.sessions.tick()
        entry = self.cache.owners.get(receiver_id)
        if entry and not entry.ready.is_set():
            await self.cache.release(receiver_id)

    async def stop_playback(self, receiver_id: str) -> None:
        await self.cancel_next(receiver_id)
        lease = self.sessions.current(self._owner(receiver_id))
        if lease:
            await self.sessions.close(lease.token)
        self.state_for(receiver_id).state = "STOPPED"
        self._positions[receiver_id] = 0
        if self.media is not None:
            self.media.set_position(receiver_id, 0)
        await self.cache.release(receiver_id)

    async def next_track(self, receiver_id: str) -> None:
        state = self.state_for(receiver_id)
        if not state.next_uri:
            raise ValueError("No queued media URI")
        self.delivery.cancel(receiver_id)
        # Next is distinct from resume, including when the current item is paused.
        previous = copy(state)
        previous_position = self._positions.get(receiver_id, 0)
        media_position = self.media.position(receiver_id) if self.media is not None else 0
        state.uri, state.metadata = state.next_uri, state.next_metadata
        state.next_uri = state.next_metadata = ""
        state.session_id = uuid.uuid4().hex
        state.state = "STOPPED"
        self._positions[receiver_id] = 0
        if self.media is not None:
            self.media.set_position(receiver_id, 0)
        try:
            await self.play(receiver_id)
        except BaseException:
            current = self.sessions.current(self._owner(receiver_id))
            if current is not None and current.identity == previous.session_id:
                state.__dict__.update(previous.__dict__)
                self._positions[receiver_id] = previous_position
                if self.media is not None:
                    self.media.set_position(receiver_id, media_position)
            raise

    async def cancel_next(self, receiver_id: str) -> None:
        self.delivery.cancel(receiver_id)
        await self._cancel_play(receiver_id)
        if self.media is not None:
            cancel = getattr(self.media, "cancel_pending", None)
            if cancel is not None:
                await cancel(receiver_id)
        self.state_for(receiver_id).finished = False
        task = self._next_tasks.pop(receiver_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _cancel_play(self, receiver_id):
        task = self._play_tasks.get(receiver_id)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _session_changed(self, lease) -> None:
        if lease.protocol != "dlna":
            return
        receiver_id = lease.token.owner.removeprefix("dlna:")
        state = self.states.get(receiver_id)
        if state and state.session_id == lease.identity and lease.state == SessionState.CLOSED:
            state.finished = lease.reason == "media_finished"
            if state.finished and state.next_uri and receiver_id not in self._next_tasks:

                async def advance():
                    try:
                        current = self.sessions.current(lease.token.owner)
                        if (
                            self.states.get(receiver_id) is state
                            and state.session_id == lease.identity
                            and state.finished
                            and state.next_uri
                            and (current is None or current.token == lease.token)
                        ):
                            await self.next_track(receiver_id)
                    except DeliverySuperseded:
                        pass
                    except Exception:
                        logger.exception("DLNA next track failed for %s", receiver_id)
                    finally:
                        self._next_tasks.pop(receiver_id, None)

                self._next_tasks[receiver_id] = asyncio.create_task(advance())
        if (
            state
            and state.session_id == lease.identity
            and lease.state
            in (
                SessionState.CLOSING,
                SessionState.CLOSED,
            )
        ):
            state.state = "STOPPED"
        elif state and state.session_id == lease.identity and lease.state == SessionState.ACTIVE:
            if state.state != "TRANSITIONING":
                state.state = "PLAYING"
        elif state and state.session_id == lease.identity and lease.state == SessionState.PAUSED:
            state.state = "PAUSED_PLAYBACK"
        elif (
            state
            and state.session_id == lease.identity
            and lease.state == SessionState.QUIET
            and lease.reason.startswith("media_")
        ):
            state.state = "STOPPED"
            state.finished = lease.reason == "media_finished"
            state.error = "媒体播放失败" if lease.reason == "media_error" else ""

    def now_playing(self) -> dict:
        """Only current sessions contribute controller-supplied DIDL metadata."""
        result = {}
        for receiver_id, state in self.states.items():
            lease = self.sessions.current(self._owner(receiver_id))
            if (
                lease is None
                or lease.identity != state.session_id
                or lease.state not in (SessionState.ACTIVE, SessionState.PAUSED, SessionState.QUIET)
            ):
                continue
            values, duration = self._media_metadata(state)
            artwork = self.cover_artwork(receiver_id)
            result[self._owner(receiver_id)] = {
                "title": values.get("title"),
                "artist": values.get("artist") or values.get("creator"),
                "album": values.get("album"),
                "duration": duration,
                "audio_id": state.display_audio_id or None,
                "lyric_lines": None,
                # The cover contract is a MiCast-served endpoint URL (same
                # shape as the AirPlay path): controller-hosted art URLs
                # (WMP MDEServer and friends) refuse plain browser requests,
                # so the cover endpoint proxies them with its own headers.
                "cover": (
                    {
                        "url": f"api/playback/cover/{self._owner(receiver_id)}",
                        "rev": state.session_id,
                    }
                    if artwork
                    else None
                ),
            }
        return result

    def cover_artwork(self, receiver_id: str) -> str:
        """Session artwork URL from the controller's DIDL metadata, or "".

        Only absolute http(s) URLs qualify; the fetch itself happens in the
        cover endpoint, on behalf of a browser the origin would otherwise
        refuse.
        """
        state = self.states.get(receiver_id)
        if state is None:
            return ""
        values, _ = self._media_metadata(state)
        artwork = values.get("albumArtURI") or state.display_cover_url or ""
        try:
            parts = urlsplit(artwork)
            valid = parts.scheme in ("http", "https") and bool(parts.hostname)
        except ValueError:
            valid = False
        return artwork if valid else ""

    @staticmethod
    def _media_metadata(state):
        """One parser for current media metadata, SOAP queries and eventing."""
        values, resources = {}, []
        try:
            root = ET.fromstring(state.metadata) if state.metadata else None
        except ET.ParseError:
            return values, None
        for element in root.iter() if root is not None else ():
            key = element.tag.split("}")[-1]
            if key in ("title", "artist", "creator", "album", "albumArtURI"):
                values[key] = element.text or None
            elif key == "res":
                resources.append(element)
        matching = [item for item in resources if (item.text or "").strip() == state.uri]
        for item in matching or resources:
            match = re.fullmatch(
                r"([0-9]{1,9}):([0-5][0-9]):([0-5][0-9])(?:\.[0-9]+)?",
                (item.get("duration") or "").strip(),
            )
            if match:
                hours, minutes, seconds = map(int, match.groups())
                return values, hours * 3600 + minutes * 60 + seconds
        return values, None

    def duration_time(self, receiver_id):
        _, seconds = self._media_metadata(self.state_for(receiver_id))
        # Preserve the existing unknown-duration wire representation.
        if seconds is None:
            return "00:00:00"
        return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"

    def media_token(self, receiver_id: str, identity: str):
        lease = self.sessions.current(self._owner(receiver_id))
        if lease and lease.identity == identity and self.sessions.valid(lease.token):
            return lease.token
        return None

    async def set_volume(self, receiver_id: str, volume: int) -> None:
        state = self.state_for(receiver_id)
        state.volume_mode = state.volume_mode or settings.sender_volume_mode
        state.volume_received = True
        if state.volume_mode == "independent" or state.muted or state.state == "STOPPED":
            state.volume = volume
            if self.media is not None:
                self.media.set_volume(receiver_id, state)
            if state.muted_volumes is not None:
                state.muted_volumes = dict.fromkeys(state.muted_volumes, volume)
            if (
                self.delivery.modes.get(receiver_id) == "proxy"
                and state.state == "PLAYING"
                and not state.muted
            ):
                # Compatibility for an already active raw session after a
                # volume preference changes. New independent sessions use DSP.
                await asyncio.gather(
                    *(
                        self.device_manager.set_volume(did, volume)
                        for did in self._owned_volume_targets(receiver_id)
                    )
                )
            return
        await asyncio.gather(
            *(
                self.device_manager.set_volume(did, volume)
                for did in self._owned_volume_targets(receiver_id)
            )
        )
        self.state_for(receiver_id).volume = volume
        if self.media is not None:
            self.media.set_volume(receiver_id, state)
            await self._network_source_volume(receiver_id, volume)

    async def _network_source_volume(self, receiver_id, volume):
        if self.media is None:
            return
        for name in ("_airplay_targets", "_dlna_targets"):
            adapter = getattr(self.media.bridge, name, None)
            if adapter is not None:
                await adapter.set_volume(self._owner(receiver_id), volume)

    def _owned_volume_targets(self, receiver_id: str) -> list[str]:
        return self.device_manager.owned_targets(receiver_id, self._owner(receiver_id))

    async def get_volume(self, receiver_id: str) -> int:
        state = self.state_for(receiver_id)
        if (state.volume_mode or settings.sender_volume_mode) == "linked" and not state.muted:
            values = await asyncio.gather(
                *(
                    self.device_manager.get_volume(did, refresh=True)
                    for did in self._owned_volume_targets(receiver_id)
                )
            )
            known = [value for value in values if value is not None]
            if known:
                state.volume = round(sum(known) / len(known))
        return state.volume

    async def set_mute(self, receiver_id: str, muted: bool) -> None:
        state = self.state_for(receiver_id)
        if state.muted == muted:
            return
        if (
            (state.volume_mode or settings.sender_volume_mode) == "linked"
            or self.delivery.modes.get(receiver_id) == "proxy"
        ):
            await self._set_speaker_mute(receiver_id, muted)
        state.muted = muted
        if self.media is not None:
            self.media.set_volume(receiver_id, state)

    async def _set_speaker_mute(self, receiver_id: str, muted: bool) -> None:
        state = self.state_for(receiver_id)
        targets = self._owned_volume_targets(receiver_id)
        if muted:
            previous = dict(state.muted_volumes or {})
            for did in targets:
                if did in previous:
                    continue
                value = (
                    state.volume
                    if self.delivery.modes.get(receiver_id) == "proxy"
                    and state.volume_mode == "independent"
                    and state.volume_received
                    else await self.device_manager.get_volume(did, refresh=True)
                )
                if value is None:
                    raise ValueError("无法读取音量，不能安全恢复静音")
                previous[did] = value
            state.muted_volumes = previous
            for did in targets:
                await self.device_manager.set_volume(did, 0)
        else:
            for did, value in (state.muted_volumes or {}).items():
                if did in targets:
                    await self.device_manager.set_volume(did, value)
            state.muted_volumes = None


def _headers(message: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in message.splitlines()[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            result[key.strip().lower()] = value.strip()
    return result


def _multicast_interface_ip() -> str:
    """IP of the interface the kernel routes LAN multicast through."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((SSDP_ADDRESS, SSDP_PORT))
            return probe.getsockname()[0]
    except OSError:
        return "0.0.0.0"


def xml_value(body: bytes, name: str, default: str = "") -> str:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return default
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == name:
            return element.text or default
    return default
