"""RTP/UDP push side of a RAOP client (audio out, timing replies, resend log)."""

import asyncio
import logging
import os
import struct
import time

from micast.raop.crypto import aes_cbc
from micast.raop.transport import build_timing_reply

logger = logging.getLogger(__name__)

NTP_EPOCH_OFFSET = 2208988800


def ntp_rtp_now(clock: int = 44100) -> int:
    """Current wall time as an RTP timestamp (matches the receiver's epoch math)."""
    return int((time.time() + NTP_EPOCH_OFFSET) * clock) & 0xFFFFFFFF


class _TimingResponder(asyncio.DatagramProtocol):
    """External receivers send 0x52 timing requests; without replies many
    drift or mute. Answering costs nothing."""

    def __init__(self, owner: "RaopSendTransport"):
        self.owner = owner

    def datagram_received(self, packet: bytes, addr) -> None:
        reply = build_timing_reply(packet)
        if reply is None:
            return
        self.owner.timing_replies += 1
        self.owner.timing_transport.sendto(reply, addr)


class _ControlLogger(asyncio.DatagramProtocol):
    """Receivers ask for retransmits here (0x55). v1 keeps no ring buffer, so
    requests are counted and logged; receivers conceal the loss."""

    def __init__(self, owner: "RaopSendTransport"):
        self.owner = owner

    def datagram_received(self, packet: bytes, addr) -> None:
        if len(packet) >= 8 and packet[1] & 0x7F == 0x55:
            first, count = struct.unpack_from(">HH", packet, 4)
            self.owner.resend_requests += 1
            if self.owner.resend_requests <= 3:
                logger.info(
                    "RAOP receiver asked to resend %s packets from %s (unsupported)",
                    count,
                    first,
                )


class RaopSendTransport:
    """Owns the three UDP sockets of an outbound RAOP session."""

    def __init__(self, host: str, aes_key: bytes, aes_iv: bytes):
        self.host = host
        self.aes_key = aes_key
        self.aes_iv = aes_iv
        self.audio_port = 0
        self.control_port = 0
        self.timing_port = 0
        # Remote (receiver-side) ports, filled from the SETUP response.
        self.remote_audio_port = 0
        self.remote_control_port = 0
        self.remote_timing_port = 0
        self.seq = 0
        self.rtptime = 0
        self.ssrc = int.from_bytes(os.urandom(4), "big")
        self.timing_replies = 0
        self.resend_requests = 0
        self._audio_transport = None
        self.control_transport = None
        self.timing_transport = None

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        self._audio_transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("0.0.0.0", 0)
        )
        self.control_transport, _ = await loop.create_datagram_endpoint(
            lambda: _ControlLogger(self), local_addr=("0.0.0.0", 0)
        )
        self.timing_transport, _ = await loop.create_datagram_endpoint(
            lambda: _TimingResponder(self), local_addr=("0.0.0.0", 0)
        )
        self.audio_port = self._audio_transport.get_extra_info("sockname")[1]
        self.control_port = self.control_transport.get_extra_info("sockname")[1]
        self.timing_port = self.timing_transport.get_extra_info("sockname")[1]

    def send_packet(self, alac_payload: bytes, frames: int) -> None:
        if not self._audio_transport or not self.remote_audio_port:
            return
        header = struct.pack(
            ">BBHII", 0x80, 0x60, self.seq & 0xFFFF, self.rtptime & 0xFFFFFFFF, self.ssrc
        )
        # Per-packet CBC reset with the session IV; the short tail passes
        # through unencrypted (mirrors the receiver-side decrypt path).
        count = len(alac_payload) // 16 * 16
        payload = (
            aes_cbc(alac_payload[:count], self.aes_key, self.aes_iv, encrypt=True)
            + alac_payload[count:]
        )
        self._audio_transport.sendto(header + payload, (self.host, self.remote_audio_port))
        self.seq = (self.seq + 1) & 0xFFFF
        self.rtptime = (self.rtptime + frames) & 0xFFFFFFFF

    def close(self) -> None:
        for transport in (self._audio_transport, self.control_transport, self.timing_transport):
            if transport:
                transport.close()
        self._audio_transport = self.control_transport = self.timing_transport = None
