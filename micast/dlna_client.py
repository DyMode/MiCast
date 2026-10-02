"""DLNA renderer discovery and casting (MiCast as a DLNA control point).

Unlike AirPlay targets (we push RTP), a DLNA renderer PULLS: we hand it the
receiver's HTTP stream URL via AVTransport SetAVTransportURI and tell it to
play — the same pattern Xiaomi speakers use, only over local SOAP instead of
the Mi cloud.
"""

import asyncio
import logging
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from xml.sax.saxutils import escape as _xml_escape

import httpx

from micast.airplay_discovery import classify_device
from micast.config import settings

logger = logging.getLogger(__name__)

SSDP_ADDRESS = "239.255.255.250"
SSDP_PORT = 1900
MEDIA_RENDERER_ST = "urn:schemas-upnp-org:device:MediaRenderer:1"
AVTRANSPORT_SERVICE_PREFIX = "urn:schemas-upnp-org:service:AVTransport"
RESCAN_INTERVAL_SECONDS = 60.0
DESCRIPTION_TIMEOUT = 5.0


@dataclass
class DlnaDevice:
    id: str  # UDN, e.g. "uuid:…"
    name: str
    location: str = ""  # description URL
    control_url: str = ""  # absolute AVTransport control URL
    model: str = ""
    kind: str = "speaker"
    last_seen: float = 0.0
    rendering_url: str = ""
    rendering_service: str = ""

    @property
    def online(self) -> bool:
        # Devices are re-probed every RESCAN interval; allow one missed cycle.
        stale_after = RESCAN_INTERVAL_SECONDS * 2.5
        return bool(self.control_url) and (time.monotonic() - self.last_seen) < stale_after


def _search_request() -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDRESS}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {MEDIA_RENDERER_ST}\r\n"
        "\r\n"
    ).encode()


class _SearchProtocol(asyncio.DatagramProtocol):
    def __init__(self, discovery: "DlnaDiscovery"):
        self.discovery = discovery

    def datagram_received(self, data: bytes, addr) -> None:
        headers = {}
        for line in data.decode(errors="replace").split("\r\n")[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        location = headers.get("location")
        if location:
            self.discovery.note_location(location)


def _own_udns() -> set[str]:
    """UDNs of MiCast's own DLNA advertisements (mirrors DlnaService.uuid_for)
    so discovery never offers ourselves as a cast target."""
    from micast.config import settings

    return {
        f"uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'micast:dlna:{receiver.id}')}"
        for receiver in settings.receivers
    }


class DlnaDiscovery:
    """Periodic M-SEARCH for MediaRenderer devices; registry is runtime-only."""

    def __init__(self):
        self._devices: dict[str, DlnaDevice] = {}
        self._transport = None
        self._task: asyncio.Task | None = None
        self._pending_locations: set[str] = set()
        self._fetch_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task:
            return
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _SearchProtocol(self), local_addr=("0.0.0.0", 0)
        )
        self._task = asyncio.create_task(self._scan_loop())
        logger.info("Browsing LAN for DLNA renderers (SSDP M-SEARCH)")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self._transport:
            self._transport.close()
            self._transport = None

    def devices(self) -> list[DlnaDevice]:
        return sorted(self._devices.values(), key=lambda device: device.name.lower())

    def resolve(self, device_id: str) -> DlnaDevice | None:
        device = self._devices.get(device_id)
        return device if device and device.control_url else None

    def note_location(self, location: str) -> None:
        # Ignore our own renderer descriptions before attempting HTTP. This
        # also suppresses stale SSDP replies from receivers removed moments
        # ago, whose description route correctly no longer exists.
        path = urlparse(location).path
        parsed = urlparse(location)
        own_ids = {receiver.id for receiver in settings.receivers}
        own_path = path.startswith("/dlna/") and path.endswith("/description.xml")
        own_address = (
            parsed.hostname == settings.effective_stream_host
            and (parsed.port or 80) == settings.port
        )
        if (own_path and own_address) or any(
            path.endswith(f"/dlna/{receiver_id}/description.xml") for receiver_id in own_ids
        ):
            return
        # Every search response is a fresh liveness observation. The old code
        # ignored known locations without touching last_seen, so every healthy
        # renderer was reported offline after the stale window elapsed.
        known = next(
            (device for device in self._devices.values() if device.location == location),
            None,
        )
        if known is not None:
            known.last_seen = time.monotonic()
            return
        if location not in self._pending_locations:
            self._pending_locations.add(location)

    async def _scan_loop(self) -> None:
        try:
            while True:
                self._transport.sendto(_search_request(), (SSDP_ADDRESS, SSDP_PORT))
                await asyncio.sleep(3)  # let responses arrive
                await self._fetch_pending()
                await asyncio.sleep(RESCAN_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("DLNA discovery loop failed")

    async def _fetch_pending(self) -> None:
        pending, self._pending_locations = self._pending_locations, set()
        for location in pending:
            try:
                await self._fetch_description(location)
            except Exception as exc:
                logger.info("DLNA description fetch failed for %s: %s", location, exc)

    async def _fetch_description(self, location: str) -> None:
        async with self._fetch_lock, httpx.AsyncClient(timeout=DESCRIPTION_TIMEOUT) as client:
            response = await client.get(location)
            response.raise_for_status()
        root = ET.fromstring(response.text)

        def text_of(parent, name) -> str:
            for element in parent.iter():
                if element.tag.split("}")[-1] == name and element.text:
                    return element.text.strip()
            return ""

        device_el = next((el for el in root.iter() if el.tag.split("}")[-1] == "device"), None)
        if device_el is None:
            return
        device_type = text_of(device_el, "deviceType")
        if not device_type.startswith(MEDIA_RENDERER_ST):
            return
        udn = text_of(device_el, "UDN")
        if not udn:
            return
        if udn in _own_udns():
            return  # MiCast's own DLNA advertisement, not an external renderer
        control_url = ""
        rendering_url = ""
        rendering_service = ""
        for service in device_el.iter():
            if service.tag.split("}")[-1] != "service":
                continue
            service_type = text_of(service, "serviceType")
            if service_type.startswith(AVTRANSPORT_SERVICE_PREFIX):
                relative = text_of(service, "controlURL")
                if relative:
                    control_url = urljoin(location, relative)
            elif service_type.startswith("urn:schemas-upnp-org:service:RenderingControl:"):
                relative = text_of(service, "controlURL")
                if relative:
                    rendering_url = urljoin(location, relative)
                    rendering_service = service_type
        name = text_of(device_el, "friendlyName") or udn
        model = text_of(device_el, "modelName")
        device = DlnaDevice(
            id=udn,
            name=name,
            location=location,
            control_url=control_url,
            model=model,
            kind=classify_device(model, name),
            last_seen=time.monotonic(),
            rendering_url=rendering_url,
            rendering_service=rendering_service,
        )
        if self._devices.get(udn) != device:
            self._devices[udn] = device
            logger.info(
                "Discovered DLNA renderer: %s (%s)%s",
                name,
                control_url or "无 AVTransport",
                "" if control_url else " — 不可投放",
            )
        else:
            self._devices[udn].last_seen = time.monotonic()


# ---------------------------------------------------------------------------
# Casting
# ---------------------------------------------------------------------------

_SOAP_ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body>{body}</s:Body></s:Envelope>"
)

_DIDL_METADATA = (
    '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
    '<item id="0" parentID="-1" restricted="1">'
    "<dc:title>{title}</dc:title>"
    "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
    '<res protocolInfo="http-get:*:{mime}:*">{url}</res>'
    "</item></DIDL-Lite>"
)


def _soap_action(service: str, action: str, arguments: dict[str, str]) -> tuple[str, str]:
    inner = "".join(f"<{key}>{value}</{key}>" for key, value in arguments.items())
    body = f'<u:{action} xmlns:u="{service}">{inner}</u:{action}>'
    return f'"{service}#{action}"', _SOAP_ENVELOPE.format(body=body)


class _DlnaRuntime:
    def __init__(self, device_id: str, name: str):
        self.device_id = device_id
        self.name = name
        self.status = "idle"  # idle | playing | error
        self.detail = ""

    def snapshot(self) -> dict:
        return {
            "id": self.device_id,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
        }


class DlnaTargetManager:
    """Starts/stops playback on DLNA renderers attached to group receivers."""

    def __init__(self, discovery: DlnaDiscovery, sessions=None, stream_content_type=None):
        self._discovery = discovery
        self.sessions = sessions
        self.stream_content_type = stream_content_type
        self._targets: dict[str, dict[str, _DlnaRuntime]] = {}  # receiver -> udn -> runtime
        self._last_urls: dict[str, str] = {}
        self._last_channels: dict[str, dict[str, str]] = {}
        self._client = httpx.AsyncClient(timeout=8.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def _volume_action(self, device_id: str, action: str, volume: int | None = None) -> str:
        device = self._discovery.resolve(device_id)
        if not device or not device.rendering_url:
            raise ValueError("此音箱未提供音量控制")
        arguments = {"InstanceID": "0", "Channel": "Master"}
        if volume is not None:
            arguments["DesiredVolume"] = str(volume)
        soap_action, body = _soap_action(device.rendering_service, action, arguments)
        response = await self._client.post(
            device.rendering_url,
            content=body.encode(),
            headers={"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": soap_action},
        )
        response.raise_for_status()
        root = ET.fromstring(response.text)
        if any(el.tag.split("}")[-1] == "Fault" for el in root.iter()):
            raise ValueError("音箱拒绝了音量请求")
        return response.text

    async def get_volume(self, device_id: str, refresh: bool = False) -> int:
        root = ET.fromstring(await self._volume_action(device_id, "GetVolume"))
        value = next(
            (el.text for el in root.iter() if el.tag.split("}")[-1] == "CurrentVolume"), None
        )
        if value is None or not 0 <= int(value) <= 100:
            raise ValueError("音箱返回的音量无效")
        return int(value)

    async def set_device_volume(self, device_id: str, percent: int) -> int:
        await self._volume_action(device_id, "SetVolume", percent)
        return await self.get_volume(device_id, refresh=True)

    async def set_volume(self, receiver_id: str, percent: int) -> None:
        for udn, runtime in self._targets.get(receiver_id, {}).items():
            if runtime.status == "playing":
                try:
                    await self.set_device_volume(udn, percent)
                except Exception as exc:
                    logger.warning("DLNA 音量设置失败 %s: %s", udn, exc)

    def statuses(self) -> dict[str, dict]:
        return {
            receiver_id: {udn: rt.snapshot() for udn, rt in targets.items()}
            for receiver_id, targets in self._targets.items()
            if targets
        }

    def playing_targets(self, receiver_id: str) -> list[dict]:
        return [
            rt.snapshot()
            for rt in self._targets.get(receiver_id, {}).values()
            if rt.status == "playing"
        ]

    async def play_targets(
        self,
        receiver_id: str,
        device_ids: list[str],
        url: str,
        channels: dict[str, str] | None = None,
        *,
        steal: bool = True,
    ) -> None:
        if not device_ids:
            return
        self._last_urls[receiver_id] = url
        self._last_channels[receiver_id] = dict(channels or {})
        targets = self._targets.setdefault(receiver_id, {})
        for udn in list(targets):
            if udn not in device_ids:
                await self._stop_owned(targets[udn])
                targets.pop(udn)
        await asyncio.gather(
            *(
                self._play_one(
                    receiver_id, targets, udn, self._url_for(receiver_id, udn, url), steal
                )
                for udn in device_ids
            )
        )

    def _url_for(self, receiver_id: str, udn: str, base_url: str) -> str:
        """A channel-assigned renderer pulls the stereo group's per-channel
        stream variant instead of the full mix."""
        from micast.config import settings

        side = self._last_channels.get(receiver_id, {}).get(udn)
        suffix = (
            settings.receiver_channel_variant_suffix(receiver_id, side)
            if side in ("left", "right")
            else ""
        )
        if self.sessions is not None:
            suffix = settings.stream_suffix(receiver_id, udn)
        url = base_url + suffix
        if self.sessions is not None:
            from urllib.parse import quote

            url += f"/for/{quote(receiver_id, safe='')}/{quote(udn, safe='')}"
        return url

    async def stop_targets(self, receiver_id: str) -> None:
        self._last_urls.pop(receiver_id, None)
        self._last_channels.pop(receiver_id, None)
        targets = self._targets.get(receiver_id, {})
        results = await asyncio.gather(
            *(self._stop_owned(runtime) for runtime in targets.values()), return_exceptions=True
        )
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise ExceptionGroup("DLNA target cleanup failed", failures)

    async def stop_all(self) -> None:
        for receiver_id in list(self._targets):
            await self.stop_targets(receiver_id)

    async def reconcile(
        self, receiver_id: str, device_ids: list[str], channels: dict[str, str] | None = None
    ) -> None:
        """Membership/channel changed mid-session: replay on the new set if
        this receiver is currently casting, otherwise just prune."""
        url = self._last_urls.get(receiver_id)
        if url:
            await self.play_targets(receiver_id, device_ids, url, channels, steal=False)
            return
        targets = self._targets.get(receiver_id, {})
        for udn in list(targets):
            if udn not in device_ids:
                await self._stop_owned(targets.pop(udn))

    async def _play_one(
        self, receiver_id: str, targets: dict[str, _DlnaRuntime], udn: str, url: str,
        steal: bool = True,
    ) -> None:
        device = self._discovery.resolve(udn)
        # Release callbacks capture one generation's object, never a mutable
        # runtime reused by a replacement session.
        runtime = _DlnaRuntime(udn, udn)
        targets[udn] = runtime
        if device is None or not device.control_url:
            runtime.status = "error"
            runtime.detail = "设备不在线或不支持 AVTransport"
            return
        runtime.name = device.name

        async def start():
            from micast.audio_encoder import _FORMATS, raw_pcm_format
            from micast.config import settings

            mime = (
                _FORMATS[settings.audio.format].content_type
                if settings.audio.auto_transcode else raw_pcm_format().content_type
            )
            if self.stream_content_type is not None:
                from urllib.parse import unquote, urlsplit

                sid = unquote(urlsplit(url).path.partition("/stream/")[2].split("/for/")[0])
                mime = self.stream_content_type(sid) or mime
            safe_url = _xml_escape(url)
            metadata = _DIDL_METADATA.format(title="MiCast", mime=mime, url=safe_url)
            runtime.status = "connecting"
            runtime.command_sent = True
            await self._soap(
                device, "SetAVTransportURI",
                {"InstanceID": "0", "CurrentURI": safe_url,
                 "CurrentURIMetaData": _xml_escape(metadata)},
            )
            if self.sessions is not None and not self.sessions.valid(runtime.token):
                return
            await self._soap(device, "Play", {"InstanceID": "0", "Speed": "1"})
            runtime.status = "playing"
            runtime.detail = ""
            logger.info("DLNA target %s playing %s", device.name, url)

        try:
            if self.sessions is not None:
                lease = self.sessions.current(receiver_id)
                if lease is None or not self.sessions.valid(lease.token):
                    return
                runtime.token = lease.token
                if not await self.sessions.targets.acquire(
                    f"dlna-target:{udn}",
                    lease.token,
                    lambda runtime=runtime: self._stop_one(runtime),
                    steal=steal,
                    start=start,
                ):
                    runtime.status = "idle"
                    return
            else:
                await start()
        except Exception as exc:
            runtime.status = "error"
            runtime.detail = str(exc)
            logger.warning("DLNA target %s failed: %s", device.name, exc)

    async def _stop_owned(self, runtime: _DlnaRuntime) -> None:
        if self.sessions is not None and hasattr(runtime, "token"):
            await self.sessions.targets.execute(
                f"dlna-target:{runtime.device_id}", runtime.token,
                lambda: self._stop_one(runtime),
            )
        else:
            await self._stop_one(runtime)

    async def _stop_one(self, runtime: _DlnaRuntime) -> None:
        if (
            self.sessions is not None
            and hasattr(runtime, "token")
            and not self.sessions.targets.owns(f"dlna-target:{runtime.device_id}", runtime.token)
        ):
            return
        device = self._discovery.resolve(runtime.device_id)
        commanded = runtime.status in ("playing", "connecting") or getattr(
            runtime, "command_sent", False
        )
        if commanded and (not device or not device.control_url):
            runtime.detail = "停止待重试: 设备暂时不可达"
            raise RuntimeError(runtime.detail)
        if device and device.control_url and commanded:
            try:
                await self._soap(device, "Stop", {"InstanceID": "0"})
            except Exception as exc:
                logger.info("DLNA stop on %s failed: %s", runtime.name, exc)
                runtime.detail = f"停止待重试: {exc}"
                raise
        runtime.status = "idle"
        runtime.command_sent = False
        runtime.detail = ""
        if self.sessions is not None and hasattr(runtime, "token"):
            self.sessions.targets.forget(f"dlna-target:{runtime.device_id}", runtime.token)

    async def _soap(self, device: DlnaDevice, action: str, arguments: dict[str, str]) -> None:
        service = f"{AVTRANSPORT_SERVICE_PREFIX}:1"
        soap_action, body = _soap_action(service, action, arguments)
        response = await self._client.post(
            device.control_url,
            content=body.encode(),
            headers={"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": soap_action},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"{action} 被设备拒绝（HTTP {response.status_code}）")
