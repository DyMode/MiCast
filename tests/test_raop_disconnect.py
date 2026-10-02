import asyncio
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from micast.config import settings
from micast.local_airplay import LocalAirPlayProvider, LocalReceiver
from micast.raop.protocol import RtspRequest
from micast.raop.server import RaopServer
from micast.raop.transport import RaopSession


class HangingWriter:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        await asyncio.Future()


@pytest.mark.asyncio
async def test_disconnect_does_not_wait_forever_for_mobile_client(monkeypatch):
    monkeypatch.setattr("micast.raop.server.RAOP_CLOSE_TIMEOUT_SECONDS", 0.01)
    server = object.__new__(RaopServer)
    writer = HangingWriter()
    session = SimpleNamespace(stop_notified=False)
    server._sessions_by_writer = {writer: session}
    closed_sessions = []
    server._close_session = closed_sessions.append

    disconnected = await asyncio.wait_for(server.disconnect_clients(), timeout=0.2)

    assert disconnected == 1
    assert writer.closed is True
    assert session.stop_notified is True
    assert closed_sessions == [session]


@pytest.mark.asyncio
async def test_idle_control_socket_expires_without_timing_port(monkeypatch):
    monkeypatch.setattr("micast.raop.server.RAOP_WATCH_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(settings, "stale_session_timeout", 60)
    receiver = RaopServer("127.0.0.1", "Test", None)
    receiver.on_play_stop = Mock()
    listener = await asyncio.start_server(receiver._client, "127.0.0.1", 0)
    reader, writer = await asyncio.open_connection("127.0.0.1", listener.sockets[0].getsockname()[1])
    try:
        writer.write(b"RECORD * RTSP/1.0\r\nCSeq: 1\r\n\r\n")
        await writer.drain()
        await reader.readuntil(b"\r\n\r\n")
        session = next(iter(receiver._sessions_by_writer.values()))
        session.last_rtp_at = time.monotonic() - 120
        assert session._timing_task is None
        assert await asyncio.wait_for(reader.read(), 0.5) == b""
        assert receiver.sessions == 0
        assert not receiver._sessions_by_writer
        receiver.on_play_stop.assert_called_once()
    finally:
        writer.close()
        await writer.wait_closed()
        listener.close()
        await listener.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout, age", [(0, 120), (60, 1)])
async def test_watchdog_keeps_live_or_indefinitely_paused_sender(monkeypatch, timeout, age):
    monkeypatch.setattr("micast.raop.server.RAOP_WATCH_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(settings, "stale_session_timeout", timeout)
    receiver = RaopServer("127.0.0.1", "Test", None)
    session = RaopSession(lambda data: None)
    session.recording = True
    session.last_rtp_at = time.monotonic() - age
    writer = HangingWriter()
    task = asyncio.create_task(receiver._watch_session(session, writer, None))
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not writer.closed


@pytest.mark.asyncio
async def test_teardown_from_probe_does_not_stop_other_sender():
    receiver = RaopServer("127.0.0.1", "Test", None)
    receiver.on_play_stop = Mock()
    probe = RaopSession(lambda data: None)
    await receiver._dispatch(
        RtspRequest("TEARDOWN", "*", "RTSP/1.0", {"cseq": "1"}, b""), probe, None
    )
    receiver.on_play_stop.assert_not_called()


@pytest.mark.asyncio
async def test_disconnect_unknown_receiver_does_not_disconnect_everyone():
    provider = LocalAirPlayProvider()
    provider.receivers["live"] = LocalReceiver("live", "Live", server=Mock())
    assert await provider.disconnect("removed") == 0
    provider.receivers["live"].server.disconnect_clients.assert_not_called()


@pytest.mark.asyncio
async def test_idle_sender_is_connected_but_not_active_and_resume_restores_activity():
    receiver = RaopServer("127.0.0.1", "Test", None)
    session = RaopSession(lambda data: None)
    session.recording = True
    receiver._sessions_by_writer[HangingWriter()] = session
    assert receiver.recording_sessions == 1
    session.idle_notified = True
    assert receiver.recording_sessions == 0
    assert not receiver._has_active_recorder()
    session.idle_notified = False
    assert receiver.recording_sessions == 1


@pytest.mark.asyncio
async def test_repeated_setup_releases_previous_udp_transports_and_port_base():
    from micast.raop.server import _reserved_udp_bases

    receiver = RaopServer("127.0.0.1", "Test", None)
    session = RaopSession(lambda data: None)
    writer = Mock()
    writer.get_extra_info.return_value = ("127.0.0.1", 1234)
    receiver._sessions_by_writer[writer] = session
    request = RtspRequest("SETUP", "*", "RTSP/1.0", {"transport": "RTP/AVP/UDP"}, b"")
    try:
        await receiver._dispatch(request, session, writer)
        old_transports = list(session.transports)
        old_base = session.udp_base
        await receiver._dispatch(request, session, writer)
        assert all(transport.is_closing() for transport in old_transports)
        assert old_base not in _reserved_udp_bases
        assert len(session.transports) == 3
    finally:
        receiver._close_session(session)
