"""MiCast native classic AirPlay RTSP receiver."""

import asyncio
import contextlib
import logging
import socket
import time
import uuid

from zeroconf import ServiceInfo

from micast.config import settings
from micast.raop.crypto import apple_response, decode_b64, decrypt_session_key
from micast.raop.identify import identify
from micast.raop.protocol import RtspRequest, parse_request, response
from micast.raop.transport import RTP_IDLE_TIMEOUT_SECONDS, RaopSession

logger = logging.getLogger(__name__)

# TCP close acknowledgement is best-effort. Some mobile clients disappear
# without completing it; playback teardown must still release speaker state.
RAOP_CLOSE_TIMEOUT_SECONDS = 2.0

RAOP_HANDSHAKE_TIMEOUT_SECONDS = 30.0
RAOP_WATCH_INTERVAL_SECONDS = 3.0

# Sender artwork beyond this is discarded (a 20 MP JPEG is ~8 MB and no
# player UI needs more than a thumbnail's worth of pixels).
_MAX_ARTWORK_BYTES = 10 * 1024 * 1024

_reserved_rtsp_ports: set[int] = set()
_reserved_udp_bases: set[int] = set()


class RaopServer:
    def __init__(self, hostname: str, name: str, zeroconf):
        self.hostname, self.name, self.zeroconf = hostname, name, zeroconf
        self.mac = _mac_for(name)
        self.on_play_start = None
        self.on_play_stop = None
        self._server = None
        self._service = None
        self._reader = asyncio.StreamReader()
        self.port = 0
        self.sessions = 0
        self.total_sessions = 0
        self.decode_errors = 0
        self.dropped_packets = 0
        self.resend_requests = 0
        self.timing_requests = 0
        self.timing_responses = 0
        self._udp_base = 0
        self._sessions_by_writer: dict[asyncio.StreamWriter, RaopSession] = {}
        self.on_volume = None  # Callable[[int], None], volume as 0-100 percent
        # Latest DAAP track metadata (title/artist/album/derived) plus an
        # arrival-event log (seq, meta) — NOT deduplicated, so "back to the
        # previous song" is observable. Consumed by the lyrics/cover matcher.
        self.daap_meta: dict[str, str] = {}
        self.daap_events: list[tuple[int, dict[str, str]]] = []
        self._daap_seq = 0
        # Sender-pushed cover art (iTunes-family senders POST it as a raw
        # image body on SET_PARAMETER). Latest only; the receiver item that
        # owns this server dies with the session, which scopes the cache.
        # ``artwork_rev`` bumps monotonically per receipt so consumers can
        # key caches without hashing bytes.
        self.artwork_bytes: bytes = b""
        self.artwork_content_type: str = ""
        self.artwork_rev: int = 0
        # Rolling lyric window, maintained incrementally as metadata events
        # arrive (dedup consecutive, keep the latest 8). now_playing reads
        # this instead of re-walking the event log on every status tick.
        self.lyric_lines: list[str] = []
        self.playback_sessions = None
        self.receiver_id = name

    @property
    def pcm_reader(self) -> asyncio.StreamReader:
        return self._reader

    @property
    def active_transport_errors(self) -> dict[str, int]:
        """Live counters only; completed sessions must not look like current faults."""
        sessions = list(self._sessions_by_writer.values())
        return {
            "decode_errors": sum(item.decode_errors for item in sessions),
            "dropped_packets": sum(item.dropped_packets for item in sessions),
            "resend_requests": sum(item.resend_requests for item in sessions),
        }

    @property
    def active_timing(self) -> dict[str, int]:
        """Live timing exchange counters.

        The server-level totals are folded in only when a session closes, so
        during playback they read as 0 and look like a broken clock sync —
        diagnostics must read the live sessions, same as transport errors.
        """
        sessions = list(self._sessions_by_writer.values())
        return {
            "timing_requests": sum(item.timing_requests for item in sessions),
            "timing_responses": sum(item.timing_responses for item in sessions),
        }

    @property
    def active_input_buffer_ms(self) -> int:
        """Approximate audio waiting for RTP reordering in active sessions."""
        # Classic AirPlay commonly carries 352 samples per ALAC packet at
        # 44.1 kHz, i.e. about 8 ms of audio per pending packet.
        pending_packets = sum(len(item.pending) for item in self._sessions_by_writer.values())
        return round(pending_packets * 352 / 44100 * 1000)

    @property
    def active_clients(self) -> list[dict[str, str]]:
        """Sender identity for the topology view (best-effort)."""
        return [
            {"host": item.client_host, "name": item.client_name}
            for item in self._sessions_by_writer.values()
        ]

    @property
    def recording_sessions(self) -> int:
        """Sessions that reached RECORD and can legitimately produce PCM."""
        return sum(
            1
            for item in self._sessions_by_writer.values()
            if item.recording and not item.idle_notified
        )

    async def start(self) -> None:
        self._server, self.port = await _start_rtsp_server(self._client)
        self._service = ServiceInfo(
            "_raop._tcp.local.",
            f"{self.mac.hex().upper()}@{self.name}._raop._tcp.local.",
            addresses=[socket.inet_aton(self.hostname)],
            port=self.port,
            properties={
                "txtvers": "1",
                "ch": "2",
                "cn": "0,1",
                "da": "true",
                "et": "0,1",
                "am": "MiCast1,1",
                "md": "0,1,2",
                "pw": "false",
                "sr": "44100",
                "ss": "16",
                "sv": "false",
                "sf": "0x4",
                "tp": "UDP",
                "vn": "3",
                "vs": "105.1",
            },
            server=f"micast-{self.mac.hex()}.local.",
        )
        await asyncio.to_thread(self.zeroconf.register_service, self._service)
        logger.info("Native RAOP receiver %s listening on %s", self.name, self.port)

    async def stop(self) -> None:
        if self._service:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.zeroconf.unregister_service, self._service)
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        await self.disconnect_clients()
        _reserved_rtsp_ports.discard(self.port)
        if not self._reader.at_eof():
            self._reader.feed_eof()

    def _close_session(self, session) -> None:
        """Close a session's transports and hand its UDP port base back."""
        session.close()
        if session.udp_base:
            _reserved_udp_bases.discard(session.udp_base)
            session.udp_base = 0

    async def disconnect_clients(self) -> int:
        """Close every sender session while keeping the receiver available."""
        writers = list(self._sessions_by_writer)
        for writer in writers:
            session = self._sessions_by_writer.get(writer)
            if session:
                # The explicit caller performs the target-speaker cleanup. Avoid
                # scheduling a second delayed pause from the socket finalizer.
                session.stop_notified = True
                self._close_session(session)
            writer.close()
        if writers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(writer.wait_closed() for writer in writers),
                        return_exceptions=True,
                    ),
                    timeout=RAOP_CLOSE_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warning(
                    "Timed out waiting for %d AirPlay client connection(s) to close; "
                    "continuing playback cleanup",
                    len(writers),
                )
        return len(writers)

    def _feed_pcm(self, data: bytes) -> None:
        if not self._reader.at_eof():
            self._reader.feed_data(data)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        session = RaopSession(self._feed_pcm)
        buffer = b""
        self.sessions += 1
        self.total_sessions += 1
        self._sessions_by_writer[writer] = session
        peer = writer.get_extra_info("peername")
        session.on_idle = lambda: self._session_idle(session, peer)
        session.on_resume = lambda: self._session_resumed(session, peer)
        if peer:
            # Best-effort sender identity: mDNS reverse lookup fills in
            # client_name ("Dys-iPhone") shortly after connect; a User-Agent
            # header (desktop iTunes) still wins if it arrives.
            asyncio.get_running_loop().create_task(self._identify_client(session, peer[0]))
        logger.info("AirPlay client connected to %s from %s", self.name, peer)
        watchdog = (
            asyncio.create_task(self._watch_session(session, writer, peer))
            if self.playback_sessions is None
            else None
        )
        try:
            while not reader.at_eof():
                try:
                    read = reader.read(65536)
                    chunk = (
                        await read
                        if session.recording
                        else await asyncio.wait_for(read, RAOP_HANDSHAKE_TIMEOUT_SECONDS)
                    )
                except TimeoutError:
                    logger.warning(
                        "AirPlay %s handshake stalled before RECORD; closing %s",
                        self.name,
                        peer,
                    )
                    break
                if not chunk:
                    break
                buffer += chunk
                while True:
                    request, remaining = parse_request(buffer)
                    if request is None:
                        break
                    buffer = remaining
                    # Capture the sender identity once, from any request that
                    # carries it (desktop iTunes sends User-Agent; iOS does not).
                    user_agent = request.headers.get("user-agent", "")
                    if user_agent and not session.client_name:
                        session.client_name = user_agent
                    if request.method in ("OPTIONS", "SET_PARAMETER", "GET_PARAMETER"):
                        logger.debug("AirPlay %s: %s from %s", self.name, request.method, peer)
                    else:
                        logger.info("AirPlay %s: %s from %s", self.name, request.method, peer)
                    status, headers = await self._dispatch(request, session, writer)
                    if request.method in ("OPTIONS", "SET_PARAMETER", "GET_PARAMETER"):
                        logger.debug(
                            "AirPlay %s: %s response %s", self.name, request.method, status
                        )
                    else:
                        logger.info(
                            "AirPlay %s: %s response %s%s",
                            self.name,
                            request.method,
                            status,
                            f" ({headers.get('Transport')})" if request.method == "SETUP" else "",
                        )
                    writer.write(response(request.cseq, status, headers))
                    await writer.drain()
                    if request.method == "TEARDOWN":
                        return
        except Exception:
            logger.exception("RAOP session failed for %s", self.name)
        finally:
            if watchdog is not None:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
            self.decode_errors += session.decode_errors
            self.dropped_packets += session.dropped_packets
            self.resend_requests += session.resend_requests
            self.timing_requests += session.timing_requests
            self.timing_responses += session.timing_responses
            self._close_session(session)
            self._sessions_by_writer.pop(writer, None)
            self.sessions -= 1
            # Connection dropped without TEARDOWN (app killed, network lost):
            # treat it as a stop unless another session is still streaming.
            if (
                session.recording
                and not session.stop_notified
                and self.on_play_stop
                and not self._has_active_recorder(except_session=session)
            ):
                logger.info("AirPlay %s: %s disconnected without TEARDOWN", self.name, peer)
                session.stop_notified = True
                if self.playback_sessions and getattr(session, "playback_token", None):
                    self.playback_sessions.end(session.playback_token, "disconnected")
                self.on_play_stop(
                    **({"token": session.playback_token} if self.playback_sessions else {})
                )
            logger.info("AirPlay client disconnected from %s: %s", self.name, peer)
            logger.info(
                "AirPlay %s timing: sent=%s received=%s",
                self.name,
                session.timing_requests,
                session.timing_responses,
            )
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), RAOP_CLOSE_TIMEOUT_SECONDS)

    async def _watch_session(self, session: RaopSession, writer, peer) -> None:
        """Check RTP independently of timing replies and close abandoned control sockets."""
        while True:
            await asyncio.sleep(RAOP_WATCH_INTERVAL_SECONDS)
            session._check_idle()
            timeout = float(settings.stale_session_timeout)
            if (
                self.playback_sessions is None
                and session.recording
                and session.last_rtp_at
                and timeout > 0
                and time.monotonic() - session.last_rtp_at >= max(timeout, RTP_IDLE_TIMEOUT_SECONDS)
            ):
                logger.info("AirPlay %s: idle sender expired; closing %s", self.name, peer)
                self._close_session(session)
                writer.close()
                return

    async def _dispatch(self, request: RtspRequest, session: RaopSession, writer):
        headers = {"Audio-Jack-Status": "connected; type=analog"}
        challenge = request.headers.get("apple-challenge")
        if challenge:
            local_ip = writer.get_extra_info("sockname")[0]
            headers["Apple-Response"] = apple_response(challenge, local_ip, self.mac)
        if request.method == "OPTIONS":
            headers["Public"] = (
                "ANNOUNCE, SETUP, RECORD, PAUSE, FLUSH, TEARDOWN, OPTIONS, "
                "GET_PARAMETER, SET_PARAMETER"
            )
        elif request.method == "ANNOUNCE":
            values = _sdp_values(request.body)
            try:
                session.configure(
                    decrypt_session_key(values["rsaaeskey"]),
                    decode_b64(values["aesiv"]),
                    [int(item) for item in values.get("fmtp", "").split()],
                )
            except (KeyError, ValueError):
                return 400, headers
        elif request.method == "SETUP":
            transport = request.headers.get("transport", "")
            client_control_port = _transport_port(transport, "control_port")
            client_timing_port = _transport_port(transport, "timing_port")
            client_host = writer.get_extra_info("peername")[0]
            logger.info("AirPlay %s requested transport: %s", self.name, transport)
            # Retrying SETUP on the same TCP connection must release the
            # previous UDP sockets and reservation before opening replacements.
            self._close_session(session)
            # A new sender takes over: close the previous client first.
            self._takeover_stale_sessions(writer)
            # Fresh UDP ports per session: rebinding the just-closed sender's
            # ports can fail (Windows keeps them briefly), killing the new
            # session too — which is exactly the "both phones fail" case.
            # Allocation is round-robin so a freed base is only reused after a
            # full cycle, and a busy base is skipped rather than fatal.
            base = 0
            for _ in range(6):
                base = _reserve_udp_base()
                try:
                    await session.open(
                        client_host,
                        client_control_port,
                        client_timing_port,
                        (base, base + 1, base + 2),
                    )
                    break
                except OSError:
                    logger.info(
                        "AirPlay %s: UDP base %s still busy, trying next",
                        self.name,
                        base,
                    )
                    _reserved_udp_bases.discard(base)
                    base = 0
            if not base:
                raise RuntimeError("没有可绑定的 AirPlay UDP 端口")
            session.udp_base = base
            audio, control, timing = session.ports
            headers["Transport"] = (
                "RTP/AVP/UDP;unicast;mode=record;"
                f"server_port={audio};control_port={control};timing_port={timing}"
            )
            headers["Session"] = "1"
        elif request.method == "RECORD":
            headers["Audio-Latency"] = "11025"
            session.recording = True
            session.last_rtp_at = time.monotonic()
            if self.playback_sessions:
                lease = self.playback_sessions.begin(self.receiver_id, "airplay", writer)
                session.playback_token = lease.token
                session.idle_managed = True
                lease.activity = lambda: session.last_rtp_at

                def source_became_quiet():
                    session.idle_notified = True
                    self._session_idle(session, writer.get_extra_info("peername"))

                lease.on_quiet = source_became_quiet

                def release_transport():
                    session.stop_notified = True
                    self._close_session(session)
                    writer.close()

                self.playback_sessions.register(
                    lease.token, "sender", release_transport, kind="transport"
                )
            if self.on_play_start:
                self.on_play_start(
                    resume=False,
                    **({"token": session.playback_token} if self.playback_sessions else {}),
                )
        elif request.method == "FLUSH":
            session.flush()
        elif request.method == "SET_PARAMETER":
            percent = _volume_percent(request.body)
            if percent is not None and self.on_volume:
                logger.info("AirPlay %s: phone volume -> %s%%", self.name, percent)
                self.on_volume(percent)
            if "x-dmap-tagged" in request.headers.get("content-type", ""):
                self._note_dmap_metadata(request.body)
            elif request.body and request.headers.get("content-type", "").startswith("image/"):
                self._note_artwork(request.body, request.headers.get("content-type", ""))
        elif (
            request.method == "TEARDOWN"
            and session.recording
            and not session.stop_notified
            and not self._has_active_recorder(except_session=session)
            and self.on_play_stop
        ):
            session.stop_notified = True
            if self.playback_sessions and getattr(session, "playback_token", None):
                self.playback_sessions.end(session.playback_token, "teardown")
            self.on_play_stop(
                **({"token": session.playback_token} if self.playback_sessions else {})
            )
        return 200, headers

    def _note_dmap_metadata(self, body: bytes) -> None:
        """Record track metadata the phone pushes via SET_PARAMETER (dmap).

        Senders like NetEase deliver it seconds AFTER playback starts, and
        scrolling-lyrics senders push a line every few seconds — so every
        arrival is logged as an event, not just the latest."""
        from micast.raop.dmap import track_meta

        try:
            meta = track_meta(body)
        except Exception:
            logger.debug("AirPlay %s: unparsable dmap body", self.name)
            return
        if not meta.get("title"):
            return
        if not self.daap_meta:
            logger.info(
                "AirPlay %s: track metadata: %s - %s (%s)",
                self.name,
                meta["title"],
                meta.get("artist", ""),
                meta.get("album", ""),
            )
        # Scrolling senders can put the title in derived and lyrics in minm.
        # Only a change of the canonical song clears its accumulated lines.
        def song_key(value):
            return (value.get("derived") or value.get("title", ""), value.get("album", ""))

        if self.daap_meta and song_key(self.daap_meta) != song_key(meta):
            self.lyric_lines.clear()
        self.daap_meta = meta
        self._daap_seq += 1
        self.daap_events.append((self._daap_seq, meta))
        if len(self.daap_events) > 128:
            del self.daap_events[:64]
        # Maintain the rolling lyric window in place — the per-tick
        # now_playing read stays O(1) no matter how long the event log grows.
        line = meta.get("lyric_line")
        if line and (not self.lyric_lines or self.lyric_lines[-1] != line):
            self.lyric_lines.append(line)
            if len(self.lyric_lines) > 8:
                del self.lyric_lines[: len(self.lyric_lines) - 8]

    def _note_artwork(self, body: bytes, content_type: str) -> None:
        """Cache the sender-pushed cover; absurd payloads are dropped outright."""
        if len(body) > _MAX_ARTWORK_BYTES:
            logger.info("AirPlay %s: artwork too large (%d bytes), ignored", self.name, len(body))
            return
        if body != self.artwork_bytes:
            logger.info(
                "AirPlay %s: artwork received (%s, %d bytes)",
                self.name,
                content_type,
                len(body),
            )
        self.artwork_bytes = body
        self.artwork_content_type = content_type
        self.artwork_rev += 1

    async def _identify_client(self, session: RaopSession, host: str) -> None:
        name = await identify(host)
        if name and not session.client_name:
            session.client_name = name

    def _has_active_recorder(self, except_session: RaopSession | None = None) -> bool:
        return any(
            item is not except_session and item.recording and not item.idle_notified
            for item in self._sessions_by_writer.values()
        )

    def _session_idle(self, session: RaopSession, peer) -> None:
        if session.stop_notified or self._has_active_recorder(except_session=session):
            return
        session.stop_notified = True
        logger.info("AirPlay %s: no audio from %s, treating sender as gone", self.name, peer)
        if self.on_play_stop:
            self.on_play_stop(
                **({"token": session.playback_token} if self.playback_sessions else {})
            )

    def _session_resumed(self, session: RaopSession, peer) -> None:
        session.stop_notified = False
        logger.info("AirPlay %s: audio resumed from %s", self.name, peer)
        if self.on_play_start:
            # A resume blip must not steal speakers another receiver now owns.
            self.on_play_start(
                resume=True, **({"token": session.playback_token} if self.playback_sessions else {})
            )

    def _takeover_stale_sessions(self, current: asyncio.StreamWriter) -> None:
        """Close other connections that already hold streaming resources.

        Classic AirPlay receivers serve one sender; when a second phone connects,
        the newest SETUP wins and the previous client is dropped.
        """
        for other, other_session in list(self._sessions_by_writer.items()):
            if other is current or other_session.ports == (0, 0, 0):
                continue
            logger.info(
                "AirPlay %s: new sender takes over, closing previous client %s",
                self.name,
                other.get_extra_info("peername"),
            )
            other_session.stop_notified = True
            self._close_session(other_session)
            other.close()


def _sdp_values(body: bytes) -> dict[str, str]:
    result = {}
    for line in body.decode(errors="replace").splitlines():
        if line.startswith("a=") and ":" in line:
            key, value = line[2:].split(":", 1)
            result[key] = value.strip()
    return result


def _volume_percent(body: bytes) -> int | None:
    """Map an AirPlay `volume` parameter (dB, -30..0, -144 = mute) to 0-100."""
    text = body.decode(errors="replace")
    for line in text.splitlines():
        if not line.startswith("volume:"):
            continue
        try:
            db = float(line.split(":", 1)[1].strip())
        except ValueError:
            return None
        if db <= -30:
            return 0
        if db >= 0:
            return 100
        return round((db + 30) / 30 * 100)
    return None


def _mac_for(name: str) -> bytes:
    value = bytearray(uuid.uuid5(uuid.NAMESPACE_DNS, f"micast:{name}").bytes[:6])
    value[0] = (value[0] | 2) & 0xFE
    return bytes(value)


def _transport_port(value: str, key: str) -> int:
    for part in value.split(";"):
        if part.strip().startswith(f"{key}="):
            with contextlib.suppress(ValueError):
                return int(part.split("=", 1)[1])
    return 0


async def _start_rtsp_server(handler):
    # This setting is a receiver pool start, not a single-instance listener.
    last = min(65535, _rtsp_base + 31)
    for port in range(_rtsp_base, last + 1):
        if port in _reserved_rtsp_ports:
            continue
        try:
            server = await asyncio.start_server(handler, "0.0.0.0", port)
        except OSError:
            continue
        _reserved_rtsp_ports.add(port)
        return server, port
    raise RuntimeError(f"没有可用的 AirPlay RTSP 端口（{_rtsp_base}-{last}）")


# Preferred scan starts; configure() overrides them from Settings at startup.
_rtsp_base = 42500
_udp_pool_base = 42600
_UDP_POOL_WIDTH = 196  # bases step by 3, so the top base is base + 195


def configure_ports(rtsp_port: int | None, udp_base: int | None) -> None:
    """Point the RTSP/UDP port scans at user-preferred starting ports.

    None restores the built-in defaults (42500 / 42600).
    """
    global _rtsp_base, _udp_pool_base, _next_udp_base
    _rtsp_base = rtsp_port or 42500
    _udp_pool_base = udp_base or 42600
    _next_udp_base = _udp_pool_base


def rtsp_base() -> int:
    return _rtsp_base


def udp_pool() -> tuple[int, int]:
    """(base, top) of the UDP port pool, for diagnostics."""
    return _udp_pool_base, min(65535, _udp_pool_base + _UDP_POOL_WIDTH - 1)


_next_udp_base = 42600


def _reserve_udp_base() -> int:
    """Round-robin over the port pool: a base freed by a just-closed sender is
    only picked again after a full cycle, giving Windows time to actually
    release the sockets."""
    global _next_udp_base
    top = min(65535, _udp_pool_base + _UDP_POOL_WIDTH - 1)
    for _ in range((top - _udp_pool_base + 1) // 3):
        base = _next_udp_base
        _next_udp_base += 3
        if _next_udp_base > top - 2:
            _next_udp_base = _udp_pool_base
        if base not in _reserved_udp_bases:
            _reserved_udp_bases.add(base)
            return base
    raise RuntimeError(f"没有可用的 AirPlay UDP 端口（{_udp_pool_base}-{top}）")
