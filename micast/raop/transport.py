"""RTP/ALAC transport for classic RAOP."""

import asyncio
import logging
import struct
import time
from collections.abc import Callable

import av
from Crypto.Cipher import AES

from micast.audio_metrics import metrics
from micast.raop.crypto import alac_cookie

logger = logging.getLogger(__name__)

# A recording session with no RTP audio for this long is treated as a dead
# sender (phone powered off, left Wi-Fi, killed app) — normal playback sends
# RTP continuously, and graceful pauses re-trigger playback on resume.
RTP_IDLE_TIMEOUT_SECONDS = 15.0

# Jitter-buffer depth in packets. Classic AirPlay carries 352 samples per ALAC
# packet (~8 ms at 44.1 kHz), so 16 packets is only ~128 ms — a Wi-Fi loss
# burst plus one retransmit round trip easily exceeds that, and every overflow
# force-skips the play head (audible as missing beats). ~400 ms gives resends
# time to land while staying far below the speaker-side stream buffer.
JITTER_BUFFER_PACKETS = 48

# Retransmission: ask again while the gap is still open.
#
# Field data (0.3.3): 283 packets skipped over ~100s while only ONE resend
# request went out, and every skip is an audible beat. A single NACK is not
# enough on a lossy Wi-Fi link — the reply to it can be lost too, and iOS keeps
# a short history precisely so that repeated requests can still be answered.
# There is no timer here on purpose: an incoming packet is the natural moment to
# notice the gap is still open, and they arrive every ~8 ms.
RESEND_RETRY_SECONDS = 0.06
RESEND_MAX_ATTEMPTS = 6


def build_timing_reply(packet: bytes) -> bytes | None:
    """Answer a 0x52 timing request with the current NTP timestamp.

    Shared by the receiver-side TimingProtocol and the sender-side transport
    (external AirPlay devices drift or mute without these replies)."""
    if len(packet) < 32 or packet[1] & 0x7F != 0x52:
        return None
    now = time.time() + 2208988800
    stamp = struct.pack(">II", int(now), int((now % 1) * (1 << 32)))
    response = bytearray(32)
    response[:2] = b"\x80\xd3"
    response[8:16] = packet[24:32]
    response[16:24] = stamp
    response[24:32] = stamp
    return bytes(response)


class AudioProtocol(asyncio.DatagramProtocol):
    def __init__(self, session):
        self.session = session

    def datagram_received(self, packet: bytes, _addr) -> None:
        self.session.process_rtp(packet)


class ControlProtocol(asyncio.DatagramProtocol):
    def __init__(self, session):
        self.session = session

    def connection_made(self, transport):
        self.session.control_transport = transport

    def datagram_received(self, packet: bytes, _addr) -> None:
        # Retransmitted audio uses a 4-byte resend header before the original RTP packet.
        if len(packet) > 16 and packet[1] & 0x7F == 0x56:
            self.session.process_rtp(packet[4:])


class TimingProtocol(asyncio.DatagramProtocol):
    def __init__(self, session):
        self.session = session

    def connection_made(self, transport):
        self.transport = transport
        self.session.timing_transport = transport

    def datagram_received(self, packet: bytes, addr) -> None:
        if len(packet) < 32:
            return
        packet_type = packet[1] & 0x7F
        if packet_type == 0x53:
            self.session.timing_responses += 1
            return
        reply = build_timing_reply(packet)
        if reply is not None:
            self.transport.sendto(reply, addr)


class RaopSession:
    def __init__(self, pcm_callback):
        self.pcm_callback = pcm_callback
        self.aes_key = None
        self.aes_iv = None
        self.decoder = None
        self.resampler = av.AudioResampler(format="s16", layout="stereo", rate=44100)
        self.transports = []
        self.ports = (0, 0, 0)
        self.udp_base = 0
        # Best-effort sender identity: RTSP User-Agent (iTunes/desktop) when
        # present, otherwise just the client IP from SETUP.
        self.client_name = ""
        self.pending: dict[int, bytes] = {}
        self.expected: int | None = None
        self.decode_errors = 0
        self.dropped_packets = 0
        self.control_transport = None
        self.client_host = ""
        self.client_control_port = 0
        self.client_timing_port = 0
        self.resend_sequence = 0
        self.resend_requests = 0
        self.timing_transport = None
        self.timing_requests = 0
        self.timing_responses = 0
        self._timing_task = None
        self.requested: dict[int, tuple[int, float]] = {}  # gap start -> (attempts, last sent)
        self._resend_blind_logged = False
        # Dead-sender detection: RECORD marks the session as recording, RTP
        # traffic keeps last_rtp_at fresh, and the timing loop flags idleness.
        self.recording = False
        self.last_rtp_at = 0.0
        self.idle_notified = False
        self.stop_notified = False
        self.on_idle: Callable[[], None] | None = None
        self.on_resume: Callable[[], None] | None = None

    def configure(self, aes_key: bytes, aes_iv: bytes, fmtp: list[int]) -> None:
        self.aes_key, self.aes_iv = aes_key, aes_iv
        self.decoder = av.CodecContext.create("alac", "r")
        self.decoder.extradata = alac_cookie(fmtp)

    async def open(
        self,
        client_host: str = "",
        client_control_port: int = 0,
        client_timing_port: int = 0,
        local_ports: tuple[int, int, int] | None = None,
    ) -> None:
        self.client_host = client_host
        self.client_control_port = client_control_port
        self.client_timing_port = client_timing_port
        loop = asyncio.get_running_loop()
        requested_ports = local_ports or (0, 0, 0)
        self.transports = []
        try:
            audio, _ = await loop.create_datagram_endpoint(
                lambda: AudioProtocol(self), local_addr=("0.0.0.0", requested_ports[0])
            )
            self.transports.append(audio)
            control, _ = await loop.create_datagram_endpoint(
                lambda: ControlProtocol(self), local_addr=("0.0.0.0", requested_ports[1])
            )
            self.transports.append(control)
            timing, _ = await loop.create_datagram_endpoint(
                lambda: TimingProtocol(self), local_addr=("0.0.0.0", requested_ports[2])
            )
            self.transports.append(timing)
        except Exception:
            # A half-bound session must not leak sockets: the caller retries
            # with a different port base when Windows still holds a port.
            for transport in self.transports:
                transport.close()
            self.transports = []
            raise
        self.ports = tuple(item.get_extra_info("sockname")[1] for item in self.transports)
        if self.client_host and self.client_timing_port:
            self._timing_task = asyncio.create_task(self._timing_loop())

    async def _timing_loop(self) -> None:
        try:
            while self.timing_transport:
                now = time.time() + 2208988800
                stamp = struct.pack(">II", int(now), int((now % 1) * (1 << 32)))
                packet = bytearray(32)
                packet[:2] = b"\x80\xd2"
                packet[24:32] = stamp
                self.timing_transport.sendto(packet, (self.client_host, self.client_timing_port))
                self.timing_requests += 1
                self._check_idle()
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            pass

    def _check_idle(self) -> None:
        if getattr(self, "idle_managed", False):
            return  # the shared lifecycle owns this session's deadlines
        if (
            self.recording
            and not self.idle_notified
            and self.last_rtp_at
            and time.monotonic() - self.last_rtp_at > RTP_IDLE_TIMEOUT_SECONDS
        ):
            self.idle_notified = True
            if self.on_idle:
                self.on_idle()

    def process_rtp(self, packet: bytes) -> None:
        if len(packet) < 13 or self.decoder is None:
            return
        self.last_rtp_at = time.monotonic()
        if self.idle_notified:
            self.idle_notified = False
            if self.on_resume:
                self.on_resume()
        sequence = struct.unpack_from(">H", packet, 2)[0]
        payload = packet[12:]
        if self.aes_key and self.aes_iv:
            count = len(payload) // 16 * 16
            payload = (
                AES.new(self.aes_key, AES.MODE_CBC, self.aes_iv).decrypt(payload[:count])
                + payload[count:]
            )
        self.push(sequence, payload)

    def push(self, sequence: int, payload: bytes) -> None:
        if self.expected is None:
            self.expected = sequence

        # RTP sequence numbers are unsigned 16-bit values.  A retransmission
        # can arrive after its packet has already been decoded or skipped.  In
        # modular arithmetic that old packet otherwise looks ~65,535 packets
        # *ahead*, eventually overflowing the jitter buffer and moving
        # ``expected`` backwards.  That false wrap is audible as a brief stall.
        distance = (sequence - self.expected) & 0xFFFF
        if distance >= 0x8000:
            return

        # Duplicate originals/retransmissions do not enlarge the jitter buffer.
        self.pending.setdefault(sequence, payload)
        self._drain_pending()
        if self.pending:
            nearest = min(self.pending, key=lambda value: (value - self.expected) & 0xFFFF)
            missing = (nearest - self.expected) & 0xFFFF
            if 0 < missing <= JITTER_BUFFER_PACKETS:
                if self._can_request_resend():
                    self._request_resend(self.expected, missing)
                elif not self._resend_blind_logged:
                    self._resend_blind_logged = True
                    logger.warning(
                        "RTP gap of %s packet(s) but resend impossible "
                        "(control_transport=%s, client_control_port=%s) — "
                        "every gap will be audible",
                        missing,
                        self.control_transport is not None,
                        self.client_control_port,
                    )
        if len(self.pending) > JITTER_BUFFER_PACKETS:
            next_sequence = min(self.pending, key=lambda value: (value - self.expected) & 0xFFFF)
            missing = (next_sequence - self.expected) & 0xFFFF
            # Only a genuine forward gap may advance the play head.  Values in
            # the backwards half of the sequence space are stale packets.
            if not 0 < missing < 0x8000:
                self.pending.pop(next_sequence, None)
                return
            self.dropped_packets += missing
            metrics.note_link_skip(missing)
            self.expected = next_sequence
            self.requested.clear()
            self._drain_pending()

    def _drain_pending(self) -> None:
        """Decode the contiguous run at the current RTP play head."""
        while self.expected in self.pending:
            sequence = self.expected
            self._decode(self.pending.pop(sequence))
            self.requested.pop(sequence, None)
            self.expected = (sequence + 1) & 0xFFFF

    def _can_request_resend(self) -> bool:
        return bool(self.control_transport and self.client_host and self.client_control_port)

    def _request_resend(self, first: int, count: int) -> None:
        """Ask the sender for a gap, again and again while it stays open.

        On a lossy link the reply to a single NACK is easily lost with the audio
        it was meant to replace, so one request per gap leaves the beat missing.
        Every arriving packet re-enters here (see push), which is what drives the
        retries without a timer.
        """
        if not self._can_request_resend():
            return
        attempts, last_at = self.requested.get(first, (0, 0.0))
        if attempts >= RESEND_MAX_ATTEMPTS:
            return
        now = time.monotonic()
        if last_at and now - last_at < RESEND_RETRY_SECONDS:
            return
        self.requested[first] = (attempts + 1, now)
        self.resend_requests += 1
        metrics.note_link_resend()
        self.resend_sequence = (self.resend_sequence + 1) & 0xFFFF
        packet = struct.pack(">BBHHH", 0x80, 0xD5, self.resend_sequence, first, count)
        self.control_transport.sendto(packet, (self.client_host, self.client_control_port))

    def _decode(self, payload: bytes) -> None:
        try:
            for frame in self.decoder.decode(av.Packet(payload)):
                converted = self.resampler.resample(frame)
                frames = converted if isinstance(converted, list) else [converted]
                for output in frames:
                    if output is None:
                        continue
                    length = output.samples * 2 * 2
                    self.pcm_callback(bytes(output.planes[0])[:length])
        except Exception as exc:
            self.decode_errors += 1
            metrics.note_link_decode_error()
            if self.decode_errors <= 3:
                logger.warning("Unable to decode ALAC packet: %s", exc)

    def flush(self) -> None:
        self.pending.clear()
        self.expected = None
        self.requested.clear()
        if self.decoder:
            self.decoder.flush_buffers()

    def close(self) -> None:
        if self._timing_task:
            self._timing_task.cancel()
            self._timing_task = None
        for transport in self.transports:
            transport.close()
        self.transports.clear()
        self.timing_transport = None
