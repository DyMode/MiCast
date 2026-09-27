import asyncio
import struct

import pytest

from micast.raop.transport import JITTER_BUFFER_PACKETS, RaopSession


class FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, packet, address):
        self.sent.append((packet, address))


def test_gap_requests_missing_packets_once():
    session = RaopSession(lambda _data: None)
    session.expected = 10
    session.client_host = "192.168.0.20"
    session.client_control_port = 6001
    session.control_transport = FakeTransport()
    session.pending[10] = b"first"
    session.push(12, b"third")
    session.push(12, b"third")

    assert len(session.control_transport.sent) == 1
    packet, address = session.control_transport.sent[0]
    assert address == ("192.168.0.20", 6001)
    assert struct.unpack(">BBHHH", packet)[3:] == (11, 1)


def test_late_retransmissions_are_discarded_without_false_wrap_drop():
    session = RaopSession(lambda _data: None)
    session.expected = 100
    decoded = []
    session._decode = decoded.append

    # Packets 99 and 98 are behind the play head. Repeating them must neither
    # fill the jitter buffer nor look like 65,535 missing forward packets.
    for _ in range(20):
        session.push(99, b"late")
        session.push(98, b"older")

    assert session.pending == {}
    assert session.expected == 100
    assert session.dropped_packets == 0
    assert decoded == []


def test_sequence_wrap_decodes_normally():
    session = RaopSession(lambda _data: None)
    session.expected = 0xFFFF
    decoded = []
    session._decode = decoded.append

    session.push(0, b"zero")
    session.push(0xFFFF, b"last")

    assert decoded == [b"last", b"zero"]
    assert session.expected == 1
    assert session.dropped_packets == 0


def test_real_gap_skip_drains_buffer_immediately():
    session = RaopSession(lambda _data: None)
    session.expected = 10
    decoded = []
    session._decode = decoded.append

    # The jitter buffer is JITTER_BUFFER_PACKETS deep; the play head only skips
    # the gap once the buffer overflows past that.
    last = 11 + JITTER_BUFFER_PACKETS
    for sequence in range(11, last + 1):
        session.push(sequence, bytes([sequence]))

    assert session.dropped_packets == 1
    assert session.expected == last + 1
    assert session.pending == {}
    assert decoded == [bytes([sequence]) for sequence in range(11, last + 1)]


@pytest.mark.asyncio
async def test_timing_probe_is_sent_to_sender_port():
    session = RaopSession(lambda _data: None)
    session.client_host = "192.168.0.20"
    session.client_timing_port = 6002
    session.timing_transport = FakeTransport()

    task = asyncio.create_task(session._timing_loop())
    await asyncio.sleep(0.01)
    task.cancel()
    await task

    packet, address = session.timing_transport.sent[0]
    assert packet[:2] == b"\x80\xd2"
    assert len(packet) == 32
    assert address == ("192.168.0.20", 6002)


def _gap_session(monkeypatch):
    """A session stuck on a 3-packet gap, with a controllable clock."""
    import micast.raop.transport as transport_module

    clock = {"now": 1000.0}
    monkeypatch.setattr(transport_module.time, "monotonic", lambda: clock["now"])

    session = RaopSession(lambda _data: None)
    session.expected = 10
    session.client_host = "192.168.0.20"
    session.client_control_port = 6001
    session.control_transport = FakeTransport()
    session.pending[10] = b"first"
    session.push(13, b"fourth")  # 11 and 12 missing
    return session, clock


def test_an_open_gap_is_asked_again_until_the_sender_answers(monkeypatch):
    """One NACK is not enough on a lossy link: its reply can be lost too.

    Field data (0.3.3): 283 packets skipped over ~100s with only ONE resend
    request — every skip is an audible beat.
    """
    session, clock = _gap_session(monkeypatch)
    sent = session.control_transport.sent

    assert len(sent) == 1
    # Still inside the retry window: another incoming packet must not spam.
    session.push(14, b"fifth")
    assert len(sent) == 1

    clock["now"] += 0.1
    session.push(15, b"sixth")
    assert len(sent) == 2
    # The request always describes the gap that is open *now* (11..12).
    assert struct.unpack(">BBHHH", sent[1][0])[3:] == (11, 2)

    # The answer arrives: the gap closes and the bookkeeping is forgotten.
    session.push(11, b"second")
    session.push(12, b"third")
    assert session.requested == {}
    assert session.dropped_packets == 0


def test_retries_stop_at_the_attempt_cap(monkeypatch):
    """Bounded: a sender that never answers must not be asked forever."""
    from micast.raop.transport import RESEND_MAX_ATTEMPTS

    session, clock = _gap_session(monkeypatch)

    for _ in range(RESEND_MAX_ATTEMPTS + 3):
        clock["now"] += 0.1
        session.push(13, b"fourth")

    assert session.resend_requests == RESEND_MAX_ATTEMPTS
