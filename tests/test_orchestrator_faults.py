"""PlaybackOrchestrator fault regressions (fnOS 0.3.0 field reports).

- Duplicate session starts (shairport-sync play-begins bounces around track
  gaps and underruns) must not re-issue cloud plays for a session that never
  ended; a real stop/start cycle must.
- The stream-pull verifier must not stop a speaker whose ownership or stream
  URL moved on since the probe began (AirPlay 1/2 switch churn).
"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import micast.playback_orchestrator as orchestrator_module
from micast.playback_orchestrator import PlaybackOrchestrator


class _FakeSettings:
    audio = SimpleNamespace(auto_transcode=True, format="mp3")
    effective_stream_host = "h"
    stream_port = 8080
    touchscreen_lyrics = False
    default_volume_enabled = False

    def receiver_targets(self, receiver_id):
        return ["d1"] if receiver_id == "r1" else []

    def stream_suffix(self, receiver_id, did):
        return ""


def _make_orchestrator(monkeypatch, active: set[str]):
    monkeypatch.setattr(orchestrator_module, "settings", _FakeSettings())
    bridge = SimpleNamespace(
        is_session_active=lambda rid: rid in active,
        _volume_modes={},
        _sender_volumes={},
        lyrics_matched={},
        local_server=lambda rid: None,
    )
    state = {"owner": None, "url": None}
    device_manager = SimpleNamespace(
        play_stream=AsyncMock(return_value=True),
        stop_playback=AsyncMock(),
        stop=AsyncMock(),
        note_codec_capability=Mock(),
        note_play_error=Mock(),
        clear_play_error=Mock(),
        owner_of=lambda did: state["owner"],
        stream_url_of=lambda did: state["url"],
        playing_ids=lambda: [],
        owned_targets=lambda rid, owner: [],
        _state=state,
    )
    tasks: set[asyncio.Task] = set()

    def start_background(coro, name):
        task = asyncio.create_task(coro, name=name)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    orch = PlaybackOrchestrator(bridge, device_manager, start_background)
    return orch, device_manager, tasks


@pytest.mark.asyncio
async def test_duplicate_session_start_does_not_replay(monkeypatch):
    active: set[str] = set()
    orch, device_manager, tasks = _make_orchestrator(monkeypatch, active)

    active.add("r1")
    await orch.on_session_start("r1")
    await orch.on_session_start("r1")  # duplicate play-begins, session never ended
    await orch.on_session_start("r1")

    assert device_manager.play_stream.await_count == 1

    await orch.on_session_stop("r1")
    active.discard("r1")
    active.add("r1")
    await orch.on_session_start("r1")  # genuine stop -> start must replay

    assert device_manager.play_stream.await_count == 2
    for task in list(tasks):
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _run_verify_via_play(monkeypatch, orch, tasks, owner, current_url):
    """Play one target, then drive the background verify task to completion."""
    device_manager = orch.device_manager
    state = device_manager._state

    async def fake_play(did, url, owner=None, force=False, audio_id=None):
        state["url"] = url  # play_stream records the URL as the current one
        return True

    device_manager.play_stream = AsyncMock(side_effect=fake_play)
    state["owner"] = owner
    monkeypatch.setattr(device_manager, "stream_active", lambda did: False, raising=False)
    monkeypatch.setattr(orchestrator_module.asyncio, "sleep", AsyncMock())

    await orch.play_receiver("r1", "http://h:8080/stream/r1")
    if current_url is not None:
        # A later play superseded the session-start URL before the probe ended.
        state["url"] = current_url
    pending = [task for task in tasks if task.get_name().startswith("verify-codec")]
    assert len(pending) == 1
    await pending[0]


@pytest.mark.asyncio
async def test_verify_does_not_stop_after_ownership_move(monkeypatch):
    active: set[str] = {"r1"}
    orch, device_manager, tasks = _make_orchestrator(monkeypatch, active)

    await _run_verify_via_play(
        monkeypatch, orch, tasks, owner="classic", current_url=None
    )

    device_manager.stop_playback.assert_not_awaited()
    device_manager.note_codec_capability.assert_not_called()
    for call in device_manager.note_codec_capability.call_args_list:
        assert call.args[2] is not False


@pytest.mark.asyncio
async def test_verify_does_not_stop_superseded_url(monkeypatch):
    active: set[str] = {"r1"}
    orch, device_manager, tasks = _make_orchestrator(monkeypatch, active)
    new_url = "http://h:8080/stream/r1/for/r1/d1?s=2"

    await _run_verify_via_play(monkeypatch, orch, tasks, owner="r1", current_url=new_url)

    device_manager.stop_playback.assert_not_awaited()
    for call in device_manager.note_codec_capability.call_args_list:
        assert call.args[2] is not False


@pytest.mark.asyncio
async def test_verify_still_stops_genuinely_dead_pull(monkeypatch):
    active: set[str] = {"r1"}
    orch, device_manager, tasks = _make_orchestrator(monkeypatch, active)

    await _run_verify_via_play(monkeypatch, orch, tasks, owner="r1", current_url=None)

    device_manager.stop_playback.assert_awaited_once()
    assert device_manager.stop_playback.await_args.kwargs.get("keep_error") is True
    capability_calls = [
        call for call in device_manager.note_codec_capability.call_args_list
        if call.args[2] is False
    ]
    assert len(capability_calls) == 1


@pytest.mark.asyncio
async def test_format_change_relearns_the_capability(monkeypatch):
    """Switching to a format that works must be RECORDED as supported.

    Field report (0.4.2): the first play recorded MP3, then the user switched
    to FLAC, it played fine — and the capability table still showed only MP3.
    Success was only ever learned at session start, and a format change
    restarts the encoders underneath a live session: no session start fires.
    """
    import micast.playback_orchestrator as orchestrator_module
    from micast.playback_orchestrator import PlaybackOrchestrator

    class _Settings:
        audio = SimpleNamespace(auto_transcode=True, format="flac", sample_rate=48000)
        effective_stream_host = "192.168.0.12"
        stream_port = 8080

        def receiver_targets(self, receiver_id):
            return ["did"]

        def stream_url_for(self, receiver_id, did=None):
            return f"http://{self.effective_stream_host}:{self.stream_port}/stream/{receiver_id}"

    monkeypatch.setattr(orchestrator_module, "settings", _Settings())
    recorded: list[tuple] = []

    class _DM:
        def owner_of(self, did):
            return "r1"

        def playing_ids(self):
            return ["did"]

        def stream_url_of(self, did):
            return "http://host:8080/stream/r1/for/r1/did"

        async def play_stream(self, did, url, **kwargs):
            return True

        def note_codec_capability(self, did, fmt, supported, reason):
            recorded.append((did, fmt, supported, reason))

    class _Server:
        @staticmethod
        def sink_bytes(receiver_id, sink):
            return 40_000

        @staticmethod
        def sink_last_byte_at(receiver_id, sink):
            return time.monotonic()

    bridge = SimpleNamespace(stream_server=_Server(), is_session_active=lambda rid: True)
    orch = PlaybackOrchestrator(bridge, _DM(), lambda coro, name: asyncio.ensure_future(coro))
    tasks: list = []
    monkeypatch.setattr(
        orch, "_start_background", lambda coro, name: tasks.append(asyncio.ensure_future(coro))
    )

    await orch.on_audio_restarted()
    for task in tasks:
        await asyncio.wait_for(task, timeout=15)

    assert ("did", "flac", True, "stream_pull_confirmed") in recorded


@pytest.mark.asyncio
async def test_a_speaker_that_fetches_but_never_takes_bytes_is_not_capable(monkeypatch):
    """The probe must not confuse "opened the URL" with "played it".

    Field report (0.4.2): 厨房小爱 was recorded as PCM-capable while it played
    nothing — the old criterion was a request count.
    """
    import micast.playback_orchestrator as orchestrator_module
    from micast.playback_orchestrator import PlaybackOrchestrator

    class _Settings:
        audio = SimpleNamespace(auto_transcode=False, format="flac", sample_rate=48000)
        effective_stream_host = "192.168.0.12"
        stream_port = 8080

        def receiver_targets(self, receiver_id):
            return ["did"]

        def stream_url_for(self, receiver_id, did=None):
            return f"http://{self.effective_stream_host}:{self.stream_port}/stream/{receiver_id}"

    monkeypatch.setattr(orchestrator_module, "settings", _Settings())
    monkeypatch.setattr(orchestrator_module.asyncio, "sleep", AsyncMock())
    recorded: list[tuple] = []

    class _DM:
        def owner_of(self, did):
            return "r1"

        def stream_url_of(self, did):
            return "http://host:8080/stream/r1/for/r1/did"

        async def stop_playback(self, did, owner=None, keep_error=False):
            return None

        def note_codec_capability(self, did, fmt, supported, reason):
            recorded.append((did, fmt, supported, reason))

        def note_play_error(self, *args, **kwargs):
            return None

    class _Server:
        """The speaker fetched 2 kB and stopped: not a capable pull."""

        @staticmethod
        def sink_bytes(receiver_id, sink):
            return 2_000

        @staticmethod
        def sink_last_byte_at(receiver_id, sink):
            return time.monotonic() - 30

    bridge = SimpleNamespace(stream_server=_Server(), is_session_active=lambda rid: True)
    orch = PlaybackOrchestrator(bridge, _DM(), lambda coro, name: asyncio.ensure_future(coro))

    await orch.verify_receiver_streams("r1", ["did"])

    # Untranscoded means the app is serving raw PCM.
    assert ("did", "pcm", False, "no_stream_pull") in recorded
    assert not [entry for entry in recorded if entry[2] is True]
