import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import pytest

from micast.config import settings
from micast.dlna_client import DlnaDevice, DlnaTargetManager
from micast.playback_sessions import PlaybackSessions
from micast.runtime_snapshot import RuntimeSnapshot


async def test_slow_hardware_snapshot_keeps_its_starting_sequence():
    from micast.routes.playback import build_playback_state

    sessions = PlaybackSessions(lambda: 120)
    sessions.begin("r", "airplay")
    runtime = RuntimeSnapshot()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def volume(*args, **kwargs):
        entered.set()
        await finish.wait()
        return 20

    manager = SimpleNamespace(
        bridge=SimpleNamespace(sessions=sessions, runtime_snapshot=runtime),
        get_control_targets=lambda: [{"deviceID": "s"}], is_playing=lambda _: True,
        is_paused=lambda _: False, is_muted=lambda _: False, get_alias=lambda _: "s",
        get_volume=volume, stream_url_of=lambda _: None,
    )
    task = asyncio.create_task(build_playback_state(manager))
    await asyncio.wait_for(entered.wait(), 1)
    newer = runtime.project(sessions)
    finish.set()
    result = await asyncio.wait_for(task, 1)
    assert result["runtime"]["sequence"] < newer["sequence"]
    await sessions.close_all()


async def test_mixed_targets_publish_actual_transport_and_readback():
    sessions = PlaybackSessions(lambda: 120)
    lease = sessions.begin("r", "dlna")
    for target in ("speaker:s", "airplay:a", "dlna-target:d"):
        await sessions.targets.acquire(target, lease.token, AsyncMock())
    snapshot = RuntimeSnapshot().project(sessions)
    assert "transport" not in snapshot["sessions"][0]["capabilities"]
    targets = {item["output"]: item["capabilities"] for item in snapshot["targets"]}
    assert targets["airplay"]["transport"] == "rtp"
    assert not targets["airplay"]["volume_readback"]
    assert targets["xiaomi"]["transport"] == targets["dlna"]["transport"] == "http"
    assert all(item["seek"] for item in targets.values())
    await sessions.close_all()


async def test_dlna_volume_capability_follows_rendering_service():
    from micast.audio_bridge import AudioBridge

    sessions = PlaybackSessions(lambda: 120)
    lease = sessions.begin("r", "airplay2")
    await sessions.targets.acquire("dlna-target:d", lease.token, AsyncMock())
    device = SimpleNamespace(rendering_url="")
    bridge = object.__new__(AudioBridge)
    bridge._dlna_discovery = SimpleNamespace(resolve=lambda _: device)
    runtime = RuntimeSnapshot()
    runtime.target_capabilities = bridge._target_capabilities
    first = runtime.project(sessions)
    assert not first["targets"][0]["capabilities"]["volume_control"]
    assert not first["targets"][0]["capabilities"]["volume_readback"]
    device.rendering_url = "http://d/rendering"
    second = runtime.project(sessions)
    assert second["revision"] > first["revision"]
    assert second["targets"][0]["capabilities"]["volume_control"]
    await sessions.close_all()


async def test_dlna_didl_uses_registered_format_during_configuration_transition(monkeypatch):
    monkeypatch.setattr(settings, "audio", settings.audio.model_copy(update={"format": "mp3"}))
    sessions = PlaybackSessions(lambda: 120)
    sessions.begin("r", "dlna")
    adapter = dlna_adapter(sessions)
    observed = []
    adapter.stream_content_type = lambda sid: observed.append(sid) or "audio/flac"
    try:
        await adapter.play_targets("r", ["d"], "http://m/stream/r")
        assert observed == ["r"]
        assert "audio/flac" in adapter._soap.await_args_list[0].args[2]["CurrentURIMetaData"]
    finally:
        await sessions.close_all()
        await adapter.close()


@pytest.mark.parametrize("target", ["speaker:s", "airplay:s", "dlna-target:s"])
async def test_recovery_cannot_steal_after_waiting_for_target_lock(target):
    sessions = PlaybackSessions(lambda: 120)
    old = sessions.begin("old", "airplay")
    new = sessions.begin("new", "dlna")
    started = AsyncMock()
    async with sessions.targets.lock(target):
        pending = asyncio.create_task(
            sessions.targets.acquire(target, old.token, AsyncMock(), steal=False, start=started)
        )
        await asyncio.sleep(0)
        sessions.targets.record(target, new.token, AsyncMock())
    assert not await pending
    started.assert_not_awaited()
    assert sessions.targets.owns(target, new.token)
    await sessions.close_all()


def dlna_adapter(sessions):
    device = DlnaDevice(id="d", name="d", location="http://d/desc", control_url="http://d/av")
    adapter = DlnaTargetManager(SimpleNamespace(resolve=lambda _: device), sessions)
    adapter._soap = AsyncMock()
    return adapter


@pytest.mark.parametrize("protocol", ["airplay", "airplay2", "dlna"])
async def test_dlna_command_sequence_cannot_cross_a_takeover(protocol):
    sessions = PlaybackSessions(lambda: 120)
    old = sessions.begin("old", protocol)
    new = sessions.begin("new", "dlna")
    adapter = dlna_adapter(sessions)
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def soap(device, action, arguments):
        calls.append((action, arguments.get("CurrentURI")))
        if action == "SetAVTransportURI" and "old" in arguments["CurrentURI"]:
            entered.set()
            await finish.wait()

    adapter._soap = soap
    try:
        first = asyncio.create_task(adapter.play_targets("old", ["d"], "http://m/old"))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(adapter.play_targets("new", ["d"], "http://m/new"))
        await asyncio.sleep(0)
        assert len(calls) == 1
        finish.set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        assert [action for action, _ in calls] == [
            "SetAVTransportURI", "Play", "Stop", "SetAVTransportURI", "Play"
        ]
        assert sessions.targets.owns("dlna-target:d", new.token)
        await adapter.stop_targets("old")
        assert calls[-1][0] == "Play"
        await sessions.close(old.token)
        assert calls[-1][0] == "Play"
    finally:
        await sessions.close_all()
        await adapter.close()


@pytest.mark.parametrize("format,transcode,mime", [
    ("mp3", True, "audio/mpeg"), ("flac", True, "audio/flac"),
    ("wav", True, "audio/wav"), ("mp3", False, "audio/wav"),
])
async def test_dlna_didl_matches_actual_stream_format(monkeypatch, format, transcode, mime):
    monkeypatch.setattr(settings, "audio", settings.audio.model_copy(
        update={"format": format, "auto_transcode": transcode}
    ))
    sessions = PlaybackSessions(lambda: 120)
    sessions.begin("r", "airplay2")
    adapter = dlna_adapter(sessions)
    try:
        await adapter.play_targets("r", ["d"], "http://m/stream")
        arguments = adapter._soap.await_args_list[0].args[2]
        import html

        root = ET.fromstring(html.unescape(arguments["CurrentURIMetaData"]))
        resource = next(el for el in root.iter() if el.tag.split("}")[-1] == "res")
        assert resource.attrib["protocolInfo"] == f"http-get:*:{mime}:*"
    finally:
        await sessions.close_all()
        await adapter.close()


async def test_pause_during_slow_dlna_start_never_issues_play():
    sessions = PlaybackSessions(lambda: 120)
    lease = sessions.begin("r", "dlna")
    adapter = dlna_adapter(sessions)
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def soap(device, action, arguments):
        calls.append(action)
        if action == "SetAVTransportURI":
            entered.set()
            await finish.wait()

    adapter._soap = soap
    try:
        task = asyncio.create_task(adapter.play_targets("r", ["d"], "http://m/source"))
        await asyncio.wait_for(entered.wait(), 1)
        sessions.pause(lease.token)
        finish.set()
        await asyncio.wait_for(task, 1)
        assert calls == ["SetAVTransportURI", "Stop"]
        assert sessions.targets.current("dlna-target:d") is None
    finally:
        await sessions.close_all()
        await adapter.close()


@pytest.mark.parametrize("protocol", ["airplay", "airplay2", "dlna"])
@pytest.mark.parametrize("output", ["xiaomi", "airplay", "dlna"])
async def test_real_output_adapters_share_takeover_pause_and_resume_contract(monkeypatch, protocol, output):
    from support.playback import device_manager

    from micast.airplay_targets import AirPlayTargetManager

    sessions = PlaybackSessions(lambda: 120)
    former = sessions.begin("former", protocol)
    newer = sessions.begin("newer", "dlna" if protocol != "dlna" else "airplay2")
    readers = {}
    adapter = None
    if output == "xiaomi":
        manager, api = device_manager(sessions)
        key = "speaker:a"

        async def start(owner, steal=True):
            return await manager.play_stream("a", f"http://m/{owner}", owner=owner, force=True, steal=steal)

        async def stop(owner):
            await manager.stop("a", owner=owner)

    elif output == "dlna":
        adapter = dlna_adapter(sessions)
        key = "dlna-target:d"

        async def start(owner, steal=True):
            await adapter.play_targets(owner, ["d"], f"http://m/{owner}", steal=steal)

        async def stop(owner):
            await adapter.stop_targets(owner)

    else:
        device = SimpleNamespace(host="127.0.0.1", port=7000, name="a", needs_password=False)
        adapter = AirPlayTargetManager(SimpleNamespace(resolve=lambda _: device), sessions)
        sender = SimpleNamespace(connect=AsyncMock(), teardown=AsyncMock(), close=AsyncMock(),
                                 flush=AsyncMock(), set_volume=AsyncMock())
        monkeypatch.setattr("micast.airplay_targets.RaopSender", lambda *a: sender)
        key = "airplay:a"

        async def start(owner, steal=True):
            reader = readers.setdefault(owner, asyncio.StreamReader())
            await adapter.start_targets(owner, ["a"], reader, steal=steal)
            await asyncio.sleep(0)

        async def stop(owner):
            await adapter.stop_targets(owner)

    try:
        await start("former")
        assert sessions.targets.owns(key, former.token)
        await start("newer")
        assert sessions.targets.owns(key, newer.token)
        await sessions.close(former.token)
        assert sessions.targets.owns(key, newer.token)
        recovered = sessions.begin("former", protocol, "recovery")
        await start("former", steal=False)
        assert sessions.targets.owns(key, newer.token)
        sessions.register(newer.token, "adapter", lambda: stop("newer"))
        sessions.pause(newer.token)
        await sessions.tick()
        assert not sessions.valid(newer.token)
        sessions.begin("newer", newer.protocol)
        await start("newer", steal=False)
        assert sessions.targets.owns(key, newer.token)
        assert sessions.valid(recovered.token)
    finally:
        await sessions.close_all()
        if output == "airplay":
            await adapter.stop_all()
        elif output == "dlna":
            await adapter.close()


async def test_long_media_tail_uses_eof_time_instead_of_old_start_time():
    clock = [0]
    sessions = PlaybackSessions(lambda: 60, clock=lambda: clock[0])
    lease = sessions.begin("dlna:r", "dlna")
    released = AsyncMock()
    sessions.register(lease.token, "media", released, kind="media")
    clock[0] = 240
    sessions.quiet(lease.token, "media_finished", grace=5)
    await sessions.tick()
    released.assert_not_awaited()
    assert lease.reason == "media_finished"
    clock[0] += 5
    await sessions.tick()
    released.assert_awaited_once()
    assert sessions.current("dlna:r") is None
