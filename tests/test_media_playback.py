import asyncio
import io
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import av
import numpy as np
import pytest

from micast.config import EqPoint, ReceiverConfig, SpeakerConfig, SpeakerGroupConfig, settings
from micast.dlna import DlnaTransportState
from micast.media_playback import MediaPlayback
from micast.media_source import MediaPCMSource
from micast.playback_sessions import PlaybackSessions
from micast.stream_server import StreamServer


def setup_media(monkeypatch, tmp_path, seconds=0.4):
    path = tmp_path / "source.wav"
    samples = int(48000 * seconds)
    left = (np.sin(np.arange(samples) * 2 * np.pi * 440 / 48000) * 9000).astype("<i2")
    right = (np.sin(np.arange(samples) * 2 * np.pi * 880 / 48000) * 9000).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(np.column_stack((left, right)).tobytes())
    monkeypatch.setattr(
        settings,
        "receivers",
        [ReceiverConfig(id="r", name="r", target_type="group", target_id="g")],
    )
    monkeypatch.setattr(
        settings,
        "groups",
        [
            SpeakerGroupConfig(
                id="g", name="g", speaker_ids=["s"], mode="stereo", channels={"s": "left"}
            )
        ],
    )
    monkeypatch.setattr(
        settings,
        "speakers",
        [SpeakerConfig(did="s", eq_enabled=True, eq_points=[EqPoint(freq=440, gain_db=-6)])],
    )
    monkeypatch.setattr(settings, "sync_groups_enabled", True)
    monkeypatch.setattr(
        settings,
        "audio",
        settings.audio.model_copy(
            update={"format": "wav", "sample_rate": 44100, "auto_transcode": True}
        ),
    )
    monkeypatch.setattr(
        "micast.media_playback.MediaPCMSource",
        lambda url, seek, deferred: MediaPCMSource(str(path), seek, deferred),
    )
    sessions = PlaybackSessions(lambda: 120)
    server = StreamServer()
    monkeypatch.setattr(server, "client_count", lambda sid: 1)
    bridge = SimpleNamespace(
        sessions=sessions,
        stream_server=server,
        _pipelines={},
        _tees={},
        _target_taps={},
        _airplay_targets=None,
        _start_entry_targets=AsyncMock(),
    )
    bridge.entry_stream_ids = lambda owner: [
        sid for sid in server.stream_ids() if sid == owner or sid.startswith(owner + "-")
    ]
    bridge.pipeline_for_stream = lambda sid: bridge._pipelines.get(sid)
    manager = SimpleNamespace(play_stream=AsyncMock(return_value=True))
    media = MediaPlayback(bridge, manager)
    state = DlnaTransportState(
        uri="http://media.invalid/source.wav",
        session_id="first",
        volume_mode="independent",
        volume=100,
    )
    return media, bridge, manager, state


async def wait_until(check):
    async def wait():
        while not check():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), 5)


@pytest.mark.parametrize("paused", [False, True])
async def test_dlna_nonplaying_seek_validates_and_keeps_transport_state(monkeypatch, tmp_path, paused):
    from micast.dlna import DlnaService

    media, bridge, manager, state = setup_media(monkeypatch, tmp_path, seconds=3)
    monkeypatch.setattr(settings, "dlna_enabled", True)
    monkeypatch.setattr(settings, "default_volume_enabled", False)
    manager.stop = AsyncMock()
    service = DlnaService(manager, bridge.sessions, media)
    service.states["r"] = state
    if paused:
        await media.play("r", state)
        await service.pause("r")
    expected = state.state
    before = manager.play_stream.await_count
    await service.seek("r", 1)
    assert state.state == expected
    assert manager.play_stream.await_count == before
    assert media.position("r") == 1
    assert not media.sources and not bridge._pipelines
    await service.play("r")
    assert state.state == "PLAYING"
    assert media.sources["dlna:r"].seek_seconds == 1
    await service.stop()


async def test_dlna_paused_seek_failure_preserves_saved_position(monkeypatch, tmp_path):
    media, bridge, manager, state = setup_media(monkeypatch, tmp_path)
    media.set_position("r", 0.2)
    state.state = "PAUSED_PLAYBACK"

    class RejectedSource:
        async def start(self):
            raise ValueError("bad seek")

        async def stop(self):
            pass

    monkeypatch.setattr("micast.media_playback.MediaPCMSource", lambda *args, **kwargs: RejectedSource())
    with pytest.raises(ValueError, match="bad seek"):
        await media.seek_position("r", state, 10)
    assert media.position("r") == 0.2 and state.state == "PAUSED_PLAYBACK"
    manager.play_stream.assert_not_awaited()
    assert not media._preparing


async def test_cancelled_paused_seek_cannot_commit_a_late_preparation(monkeypatch, tmp_path):
    media, bridge, manager, state = setup_media(monkeypatch, tmp_path)
    media.set_position("r", 0.2)
    state.state = "PAUSED_PLAYBACK"
    entered, ready = asyncio.Event(), asyncio.Event()

    class PendingSource:
        async def start(self):
            entered.set()
            await ready.wait()

        async def stop(self):
            pass

    monkeypatch.setattr("micast.media_playback.MediaPCMSource", lambda *args, **kwargs: PendingSource())
    pending = asyncio.create_task(media.seek_position("r", state, 10))
    await entered.wait()
    await media.cancel_pending("r")
    ready.set()
    with pytest.raises(ValueError, match="定位请求已被替换"):
        await pending
    assert media.position("r") == 0.2 and not media._preparing
    manager.play_stream.assert_not_awaited()


async def test_dlna_duration_survives_real_media_pause_and_seek(monkeypatch, tmp_path):
    from micast.dlna import DlnaService
    from micast.routes.dlna import _dispatch

    media, bridge, manager, state = setup_media(monkeypatch, tmp_path, seconds=3)
    monkeypatch.setattr(settings, "dlna_enabled", True)
    monkeypatch.setattr(settings, "default_volume_enabled", False)
    manager.stop = AsyncMock()
    service = DlnaService(manager, bridge.sessions, media)
    service.states["r"] = state
    state.metadata = f'<item><res duration="0:00:03">{state.uri}</res></item>'
    await media.play("r", state)
    await service.pause("r")
    await service.seek("r", 1)
    position = await _dispatch(service, "r", "AVTransport", "GetPositionInfo", b"")
    assert position["TrackDuration"] == "00:00:03"
    assert position["RelTime"] == "00:00:01" and state.state == "PAUSED_PLAYBACK"
    assert service.now_playing()["dlna:r"]["duration"] == 3
    await service.play("r")
    info = await _dispatch(service, "r", "AVTransport", "GetMediaInfo", b"")
    assert info["MediaDuration"] == "00:00:03"
    await service.stop()


async def test_media_really_decodes_through_shared_eq_and_channel_pipeline(monkeypatch, tmp_path):
    media, bridge, manager, state = setup_media(monkeypatch, tmp_path)
    output = []
    original = bridge.stream_server.broadcast

    async def capture(sid, chunk):
        if chunk:
            output.append(chunk)
        await original(sid, chunk)

    monkeypatch.setattr(bridge.stream_server, "broadcast", capture)
    try:
        await media.play("r", state)
        pipeline = next(iter(bridge._pipelines.values()))
        assert pipeline._eq_curve == [(440.0, -6.0)]
        assert pipeline._channel == "left"
        await wait_until(lambda: pipeline.status == "idle")
        await asyncio.gather(*pipeline._tasks)
        assert output
        with av.open(io.BytesIO(b"".join(output))) as container:
            decoded = np.concatenate(
                [frame.to_ndarray() for frame in container.decode(audio=0)], axis=1
            )
        stereo = decoded.reshape(-1, 2)
        assert len(stereo) == 17640
        assert np.max(np.abs(stereo[:, 0].astype(int) - stereo[:, 1].astype(int))) <= 1
        assert max(abs(stereo[:, 0])) < 9000
        url = manager.play_stream.call_args.args[1]
        assert "/stream/dlna:r-L-q1/for/dlna:r/s" in url
        assert state.uri not in url
        assert bridge.sessions.current("dlna:r").reason == "media_finished"
    finally:
        await bridge.sessions.close_all()
    assert not media.sources and not bridge._pipelines and not bridge.stream_server.stream_ids()


async def test_media_pause_releases_decoder_and_resume_seeks_saved_position(monkeypatch, tmp_path):
    media, bridge, manager, state = setup_media(monkeypatch, tmp_path, seconds=3)
    try:
        await media.play("r", state)
        await wait_until(lambda: media.position("r") > 0.2)
        await media.pause("r")
        position = media.position("r")
        assert position > 0
        assert not media.sources and not bridge._pipelines
        assert bridge.sessions.current("dlna:r").state.value == "paused"
        state.session_id = "second"
        await media.play("r", state, position)
        assert media.sources["dlna:r"].seek_seconds == position
        assert bridge.sessions.current("dlna:r").identity == "second"
        assert manager.play_stream.await_count == 2
    finally:
        await bridge.sessions.close_all()


async def test_rejected_media_output_leaves_no_worker_or_stream(monkeypatch, tmp_path):
    media, bridge, manager, state = setup_media(monkeypatch, tmp_path)
    manager.play_stream.return_value = False
    with pytest.raises(ValueError, match="No speaker"):
        await media.play("r", state)
    assert not media.sources and not bridge._pipelines
    assert not bridge.sessions.snapshot() and not bridge.stream_server.stream_ids()


async def test_failed_media_preflight_preserves_current_playback(monkeypatch, tmp_path):
    media, bridge, manager, state = setup_media(monkeypatch, tmp_path, seconds=3)
    await media.play("r", state)
    original = media.sources["dlna:r"]
    lease = bridge.sessions.current("dlna:r")
    monkeypatch.setattr("micast.media_playback.MediaPCMSource", lambda *a, **k:
                        SimpleNamespace(start=AsyncMock(side_effect=ValueError("invalid media"))))
    try:
        with pytest.raises(ValueError, match="invalid media"):
            await media.play("r", state, 1, resume=True)
        assert media.sources["dlna:r"] is original
        assert bridge.sessions.valid(lease.token)
        assert bridge._pipelines and manager.play_stream.await_count == 1
    finally:
        await bridge.sessions.close_all()


async def test_stop_during_first_media_preflight_cannot_start_a_late_session(monkeypatch, tmp_path):
    media, bridge, manager, state = setup_media(monkeypatch, tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()

    async def prepare():
        started.set()
        await finish.wait()
        return asyncio.StreamReader()

    source = SimpleNamespace(start=prepare, stop=AsyncMock())
    monkeypatch.setattr("micast.media_playback.MediaPCMSource", lambda *a, **k: source)
    task = asyncio.create_task(media.play("r", state))
    await asyncio.wait_for(started.wait(), 1)
    await media.cancel_pending("r")
    finish.set()
    with pytest.raises(ValueError, match="播放会话"):
        await asyncio.wait_for(task, 1)
    manager.play_stream.assert_not_awaited()
    assert not bridge.sessions.snapshot() and not media.sources and not media._preparing


async def test_media_preserves_intro_until_output_connects(monkeypatch, tmp_path):
    media, bridge, manager, state = setup_media(monkeypatch, tmp_path)
    connected = False
    monkeypatch.setattr(bridge.stream_server, "client_count", lambda sid: int(connected))
    task = asyncio.create_task(media.play("r", state))
    try:
        await wait_until(lambda: manager.play_stream.await_count > 0)
        assert media.sources["dlna:r"].position == 0
        assert not media.sources["dlna:r"]._active.is_set()
        connected = True
        await task
        assert media.sources["dlna:r"]._active.is_set()
    finally:
        await bridge.sessions.close_all()


async def test_command_acceptance_without_output_is_a_start_failure(monkeypatch, tmp_path):
    media, bridge, manager, state = setup_media(monkeypatch, tmp_path)
    monkeypatch.setattr(bridge.stream_server, "client_count", lambda sid: 0)
    monkeypatch.setattr("micast.media_playback.OUTPUT_CONNECT_TIMEOUT", 0.01)
    with pytest.raises(ValueError, match="未建立输出连接"):
        await media.play("r", state)
    assert manager.play_stream.await_count == 1
    assert not media.sources and not bridge._pipelines and not bridge.sessions.snapshot()
