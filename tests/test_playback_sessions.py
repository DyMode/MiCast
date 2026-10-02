"""Lifecycle contracts shared by every ingress; time advances without sleeps."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from support.playback import device_manager, registry

from micast.audio_bridge import AudioBridge
from micast.audio_encoder import raw_pcm_format
from micast.config import ReceiverConfig, settings
from micast.dlna import DlnaService
from micast.playback_sessions import SessionState
from micast.routes.playback import _verify_callback_epoch
from micast.stream_server import StreamServer, _serve_seekable_media


@pytest.mark.parametrize("protocol", ["airplay", "airplay2"])
async def test_real_source_idle_cleans_outputs_then_transport_without_any_http_client(protocol):
    sessions, clock = registry()
    lease = sessions.begin("r1", protocol)
    lease.activity = lambda: 1000.0
    output, transport = Mock(), Mock()
    sessions.register(lease.token, "output", output)
    sessions.register(lease.token, "sender", transport, "transport")
    clock[0] += 15
    await sessions.tick()
    assert lease.state == SessionState.QUIET
    assert not sessions.valid(lease.token)
    output.assert_not_called()
    clock[0] += 3
    await sessions.tick()
    output.assert_called_once()
    transport.assert_not_called()
    clock[0] = 1060
    await sessions.tick()
    transport.assert_called_once()
    assert sessions.snapshot() == []


async def test_duplicate_start_callbacks_do_not_refresh_real_audio_deadline():
    sessions, clock = registry()
    lease = sessions.begin("r1", "airplay2")
    lease.activity = lambda: 1000.0
    for second in (5, 10, 15):
        clock[0] = 1000 + second
        sessions.begin("r1", "airplay2")
    await sessions.tick()
    assert lease.state == SessionState.QUIET


async def test_any_real_branch_keeps_group_alive_and_output_padding_is_irrelevant(monkeypatch):
    bridge = AudioBridge()
    monkeypatch.setattr(bridge, "entry_stream_ids", lambda rid: ["r1-L", "r1-R"])
    pipelines = {
        "r1-L": SimpleNamespace(source_idle_ms=lambda: 60000),
        "r1-R": SimpleNamespace(source_idle_ms=lambda: 100),
    }
    monkeypatch.setattr(bridge, "pipeline_for_stream", pipelines.get)
    assert bridge.entry_source_idle_ms("r1") == 100
    sessions, clock = registry()
    lease = sessions.begin("r1", "airplay2")
    lease.activity = lambda: clock[0] - bridge.entry_source_idle_ms("r1") / 1000
    clock[0] += 120
    await sessions.tick()
    assert sessions.valid(lease.token)
    pipelines["r1-R"].source_idle_ms = lambda: 60000
    clock[0] += 60
    await sessions.tick()
    assert sessions.current("r1") is None


async def test_disabled_expiry_keeps_only_the_control_transport():
    sessions, clock = registry(0)
    lease = sessions.begin("r1", "airplay")
    lease.activity = lambda: 1000.0
    output, transport = Mock(), Mock()
    sessions.register(lease.token, "stream", output)
    sessions.register(lease.token, "sender", transport, "transport")
    clock[0] += 15
    await sessions.tick()
    clock[0] += 3600
    await sessions.tick()
    output.assert_called_once()
    transport.assert_not_called()


async def test_resume_during_grace_cancels_old_stop():
    sessions, clock = registry()
    lease = sessions.begin("r1", "airplay", "phone")
    release = Mock()
    sessions.register(lease.token, "output", release)
    sessions.quiet(lease.token)
    clock[0] += 2
    resumed = sessions.begin("r1", "airplay", "phone")
    clock[0] += 3
    await sessions.tick()
    assert resumed.token == lease.token
    release.assert_not_called()


async def test_old_generation_stop_cannot_end_replacement():
    sessions, _ = registry()
    old = sessions.begin("r1", "airplay", "phone-a")
    released = Mock()
    sessions.register(old.token, "sender", released, "transport")
    new = sessions.begin("r1", "airplay", "phone-b")
    sessions.end(old.token)
    await sessions.tick()
    assert sessions.valid(new.token)
    released.assert_called_once()


async def test_cleanup_failure_remains_visible_and_retries():
    sessions, clock = registry()
    lease = sessions.begin("r1", "dlna")
    release = Mock(side_effect=[RuntimeError("offline"), None])
    sessions.register(lease.token, "speaker:a", release, "speaker")
    await sessions.close(lease.token)
    assert sessions.snapshot()[0]["cleanup_failures"] == 1
    assert sessions.snapshot()[0]["state"] == "closing"
    clock[0] += 2
    await sessions.tick()
    assert release.call_count == 2
    assert sessions.snapshot() == []


async def test_start_during_cleanup_gets_new_generation():
    sessions, _ = registry()
    old = sessions.begin("r1", "airplay")
    entered, finish = asyncio.Event(), asyncio.Event()

    async def release():
        entered.set()
        await finish.wait()

    sessions.register(old.token, "output", release)
    ending = asyncio.create_task(sessions.close(old.token))
    await entered.wait()
    new = sessions.begin("r1", "airplay")
    assert new.token != old.token
    finish.set()
    await ending
    assert sessions.valid(new.token)


async def test_paused_dlna_stops_media_but_keeps_resume_state_until_expiry():
    sessions, clock = registry()
    lease = sessions.begin("dlna:r1", "dlna", "track")
    media, speaker = Mock(), Mock()
    sessions.register(lease.token, "media:1", media, "media")
    sessions.register(lease.token, "speaker:a", speaker, "speaker")
    sessions.pause(lease.token)
    await sessions.tick()
    media.assert_called_once()
    speaker.assert_not_called()
    clock[0] += 60
    await sessions.tick()
    speaker.assert_called_once()
    assert sessions.snapshot() == []


async def test_stop_is_idempotent_and_shutdown_releases_every_protocol():
    sessions, _ = registry()
    callbacks = []
    for owner, protocol in [("r1", "airplay"), ("ap2", "airplay2"), ("dlna:r1", "dlna")]:
        lease = sessions.begin(owner, protocol)
        release = Mock()
        callbacks.append(release)
        sessions.register(lease.token, "resource", release)
    await sessions.close_all()
    await sessions.close_all()
    assert sessions.snapshot() == []
    for callback in callbacks:
        callback.assert_called_once()


async def test_http_pull_is_a_session_resource_and_retries_after_stop_are_rejected():
    sessions, _ = registry()
    lease = sessions.begin("r1", "airplay")
    server = StreamServer()
    server.sessions = sessions
    server.register_stream("r1", raw_pcm_format(48000))
    request = Request({"type": "http", "method": "GET", "path": "/stream/r1", "query_string": b""})
    response = await server._serve_stream(request, "r1", "r1", "speaker-a")
    assert any(key.startswith("stream:") for key in lease.resources)
    await sessions.close(lease.token)
    assert [chunk async for chunk in response.body_iterator] == []
    assert server.client_count("r1") == 0
    with pytest.raises(HTTPException) as exc:
        await server._serve_stream(request, "r1", "r1", "speaker-a")
    assert exc.value.status_code == 410


def test_replaced_airplay2_process_cannot_deliver_stop_callback():
    bridge = SimpleNamespace(_airplay2_sources={"ap2": SimpleNamespace(epoch="new-process")})
    with pytest.raises(HTTPException) as exc:
        _verify_callback_epoch(bridge, {"device_id": "ap2", "epoch": "old-process"})
    assert exc.value.status_code == 409
    _verify_callback_epoch(bridge, {"device_id": "ap2", "epoch": "new-process"})


def test_callback_retry_or_late_stop_cannot_override_a_newer_event():
    source = SimpleNamespace(epoch="process")
    bridge = SimpleNamespace(_airplay2_sources={"ap2": source})
    assert _verify_callback_epoch(bridge, {"device_id": "ap2", "epoch": "process", "event_seq": 20})
    assert not _verify_callback_epoch(bridge, {"device_id": "ap2", "epoch": "process", "event_seq": 10})
    assert not _verify_callback_epoch(bridge, {"device_id": "ap2", "epoch": "process", "event_seq": 20})


async def test_dlna_replace_pause_stop_and_disable_release_scoped_resources(monkeypatch):
    monkeypatch.setattr(settings, "receivers", [ReceiverConfig(
        id="r1", name="r1", target_type="speaker", target_id="a",
    )])
    monkeypatch.setattr(settings, "dlna_enabled", True)
    manager = SimpleNamespace(play_stream=AsyncMock(return_value=True),
                              stop_playback=AsyncMock(), stop=AsyncMock())
    sessions, _ = registry()
    dlna = DlnaService(manager, sessions)
    await dlna.set_uri("r1", "http://media/song1")
    await dlna.play("r1")
    old = sessions.current("dlna:r1")
    proxy = Mock()
    sessions.register(old.token, "media:old", proxy, "media")
    await dlna.set_uri("r1", "http://media/song2")
    proxy.assert_called_once()
    manager.stop_playback.assert_awaited_with("a", owner="dlna:r1")
    await dlna.play("r1")
    new = sessions.current("dlna:r1")
    assert new.token != old.token
    assert dlna.media_token("r1", old.identity) is None
    await dlna.pause("r1")
    assert new.state == SessionState.PAUSED
    await dlna.play("r1")
    await dlna.stop()
    assert dlna.state_for("r1").state == "STOPPED"
    assert sessions.snapshot() == []


async def test_proxy_is_aborted_by_session_close_even_if_http_client_stays_connected(monkeypatch):
    class Pump:
        def __init__(self, *args):
            self._done = Mock(is_set=lambda: True)
            self.stopped = False

        def start(self):
            return self

        def abort(self):
            self.stopped = True

        async def close(self):
            self.abort()

        async def read(self):
            return b"" if self.stopped else b"audio"

    monkeypatch.setattr("micast.stream_server.MediaProxyPump", Pump)
    sessions, _ = registry()
    lease = sessions.begin("dlna:r1", "dlna")
    response = await _serve_seekable_media("http://media/song", 0, None, sessions, lease.token)
    assert await anext(response.body_iterator) == b"audio"
    await sessions.close(lease.token)
    assert [chunk async for chunk in response.body_iterator] == []


async def test_session_close_cancels_a_request_blocked_sending_audio():
    from micast.stream_server import SessionStreamingResponse

    sessions, _ = registry()
    lease = sessions.begin("r1", "airplay")
    sending, released = asyncio.Event(), asyncio.Event()

    async def chunks():
        try:
            yield b"audio"
        finally:
            released.set()

    async def send(message):
        if message["type"] == "http.response.body":
            sending.set()
            await asyncio.Future()  # half-open client with a blocked socket

    response = SessionStreamingResponse(chunks(), sessions=sessions, session_token=lease.token)
    task = asyncio.create_task(response(
        {"type": "http", "asgi": {"spec_version": "2.4"}}, AsyncMock(), send
    ))
    await sending.wait()
    await sessions.close(lease.token)
    assert task.cancelled()
    assert released.is_set()
    assert sessions.snapshot() == []




async def test_old_same_owner_speaker_cleanup_cannot_stop_new_generation():
    sessions, _ = registry()
    manager, api = device_manager(sessions)
    old = sessions.begin("r1", "airplay", "old-phone")
    await manager.play_stream("a", "http://old", owner="r1")
    new = sessions.begin("r1", "airplay", "new-phone")
    await manager.play_stream("a", "http://new", owner="r1")
    await sessions.tick()
    api.pause.assert_not_awaited()
    assert manager.owner_of("a") == "r1"
    assert manager._speaker_tokens["a"] == new.token
    assert old.token != new.token


async def test_old_dlna_stop_cannot_stop_airplay_on_the_same_speaker():
    sessions, _ = registry()
    manager, api = device_manager(sessions)
    old = sessions.begin("dlna:r1", "dlna", "track")
    await manager.play_stream("a", "http://media", owner="dlna:r1")
    new = sessions.begin("r1", "airplay", "phone")
    await manager.play_stream("a", "http://stream", owner="r1")
    await sessions.close(old.token)
    api.stop.assert_not_awaited()
    assert sessions.valid(new.token)
    assert manager.owner_of("a") == "r1"


async def test_taking_last_speaker_ends_old_phone_but_keeps_other_group_members():
    sessions, _ = registry()
    manager, _ = device_manager(sessions)
    old = sessions.begin("group", "airplay", "phone")
    transport = Mock()
    sessions.register(old.token, "sender", transport, "transport")
    await manager.play_stream("a", "http://group", owner="group")
    await manager.play_stream("b", "http://group", owner="group")
    new = sessions.begin("ap2", "airplay2")
    await manager.play_stream("a", "http://ap2", owner="ap2")
    await sessions.tick()
    assert sessions.valid(old.token)
    transport.assert_not_called()
    await manager.play_stream("b", "http://ap2", owner="ap2")
    await sessions.tick()
    transport.assert_called_once()
    assert sessions.current("group") is None
    assert sessions.valid(new.token)


async def test_slow_cleanup_does_not_block_another_receiver():
    sessions, _ = registry()
    slow = sessions.begin("slow", "airplay")
    fast = sessions.begin("fast", "airplay2")
    blocked, released = asyncio.Event(), asyncio.Event()
    sessions.register(slow.token, "sender", blocked.wait, "transport")
    sessions.register(fast.token, "sender", released.set, "transport")
    ending = asyncio.create_task(sessions.close_all())
    await asyncio.wait_for(released.wait(), 0.2)
    blocked.set()
    await ending
    assert sessions.snapshot() == []


async def test_stop_winning_against_inflight_cloud_play_cannot_resurrect_speaker():
    sessions, _ = registry()
    manager, api = device_manager(sessions)
    lease = sessions.begin("r1", "airplay")
    entered, finish = asyncio.Event(), asyncio.Event()

    async def slow_play(*args, **kwargs):
        entered.set()
        await finish.wait()

    api.play_music_url = slow_play
    play = asyncio.create_task(manager.play_stream("a", "http://stream", owner="r1"))
    await entered.wait()
    await sessions.close(lease.token)
    finish.set()
    assert await play is False
    assert manager.playing_ids() == []
    api.stop.assert_awaited_once()


async def test_naturally_finished_dlna_is_not_replayed_by_watchdog(monkeypatch):
    monkeypatch.setattr("micast.xiaomi.device_manager.STATUS_CHECK_INTERVAL_SECONDS", 0.001)
    sessions, _ = registry()
    manager, api = device_manager(sessions)
    lease = sessions.begin("dlna:r1", "dlna", "track")
    await manager.play_stream("a", "http://media", owner="dlna:r1")
    api.get_status.side_effect = [{"status": 1}, {"status": 0}, {"status": 0}, {"status": 0}]
    manager.stream_active = lambda did: False  # finite media bypasses AirPlay's pull observer
    await asyncio.wait_for(manager._watchdog_loop("a"), 0.5)
    await sessions.tick()
    api.play_music_url.assert_awaited_once()
    assert manager.owner_of("a") is None
    assert manager.stream_url_of("a") is None
    assert sessions.current(lease.token.owner) is None
