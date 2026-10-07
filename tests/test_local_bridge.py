"""Local bridge persistence, protocol isolation and local SOAP integration."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from micast.audio_bridge import AudioBridge
from micast.config import AirPlay2InstanceConfig, Settings, settings
from micast.dlna import DlnaService
from micast.dlna_client import DlnaDevice, DlnaDiscovery, DlnaTargetManager
from micast.playback_sessions import PlaybackSessions
from micast.routes import dlna_devices, receivers
from micast.stream_plan import compute_plan
from micast.stream_server import StreamServer


@pytest.fixture
def local_config(monkeypatch):
    monkeypatch.setattr(Settings, "save_to_file", lambda self: None)
    monkeypatch.setattr(settings, "receivers", [])
    monkeypatch.setattr(settings, "groups", [])
    monkeypatch.setattr(settings, "speakers", [])
    monkeypatch.setattr(settings, "airplay2_instances", [])
    monkeypatch.setattr(settings, "network_discovery_enabled", True)
    monkeypatch.setattr(settings, "default_volume_enabled", False)
    discovery = DlnaDiscovery()
    device = DlnaDevice(
        "uuid:local:device",
        "本地音箱",
        control_url="http://speaker/control",
        model="Test",
        last_seen=time.monotonic(),
    )
    discovery._devices[device.id] = device
    return discovery, device


def test_direct_target_survives_roundtrip_and_account_switch(monkeypatch):
    monkeypatch.setattr(Settings, "save_to_file", lambda self: None)
    cfg = Settings()
    cfg.receivers = []
    cfg.provider_account_id = "old"
    entry = cfg.add_receiver("本地", "dlna", "uuid:local", target_name="本地音箱")
    cfg.airplay2_instances = [
        AirPlay2InstanceConfig(id="ap2", name="本地 2", target_type="dlna", target_id="uuid:local")
    ]
    restored = Settings.model_validate(cfg.model_dump())
    assert restored.receiver_targets(entry.id) == []
    assert restored.receiver_dlna_targets(entry.id) == ["uuid:local"]
    assert restored.receiver_dlna_targets("ap2") == ["uuid:local"]
    assert restored.receiver_stream_variants(entry.id)
    assert restored.bind_provider_account("new")
    assert restored.receivers[0].target_name == "本地音箱"
    assert restored.airplay2_instances[0].target_id == "uuid:local"


def test_bridge_does_not_advertise_virtual_dlna(local_config, monkeypatch):
    monkeypatch.setattr(settings, "dlna_enabled", True)
    entry = settings.add_receiver("本地", "dlna", "uuid:local")
    legacy = settings.add_receiver("米家", "selected")
    assert entry.dlna_enabled is False
    assert [item.id for item in DlnaService(None).active_receivers()] == [legacy.id]
    plan = compute_plan(settings)
    assert plan["entries"][entry.id]["external_dlna"] == ["uuid:local"]


@pytest.mark.asyncio
async def test_routes_create_rename_retarget_and_keep_offline(local_config):
    discovery, device = local_config
    bridge = SimpleNamespace(
        dlna_discovery=discovery, diagnostics={}, apply_config_change=AsyncMock(), status={}
    )
    app = FastAPI()
    app.include_router(receivers.install(bridge))
    app.include_router(dlna_devices.install(bridge))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/api/receivers/definitions",
            json={"name": "书房", "target_type": "dlna", "target_id": device.id},
        )
        assert result.status_code == 200
        entry = result.json()
        assert entry["target_id"] == "uuid:local:device"
        assert entry["target_name"] == "本地音箱"
        assert entry["dlna_enabled"] is False
        renamed = await client.patch(
            f"/api/receivers/definitions/{entry['id']}", json={"name": "书房音乐"}
        )
        assert renamed.status_code == 200
        conflict = await client.post(
            "/api/receivers/definitions",
            json={"name": "重复", "target_type": "dlna", "target_id": device.id},
        )
        assert conflict.status_code == 409
        discovery._devices.clear()
        offline = (await client.get("/api/dlna-devices")).json()[0]
        assert offline["name"] == "本地音箱"
        assert offline["online"] is False
        assert offline["attached_receiver"] == entry["id"]
        assert settings.receivers[0].name == "书房音乐"


@pytest.mark.asyncio
async def test_direct_bridge_uses_local_soap_and_scoped_stop(local_config):
    discovery, device = local_config
    entry = settings.add_receiver("本地", "dlna", device.id)
    sessions = PlaybackSessions(lambda: 300)
    manager = DlnaTargetManager(discovery, sessions)
    commands = []

    def respond(request):
        commands.append((request.headers["soapaction"], request.content.decode()))
        return httpx.Response(
            200,
            text='<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body/></s:Envelope>',
        )

    await manager._client.aclose()
    manager._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    bridge = object.__new__(AudioBridge)
    bridge.sessions = sessions
    bridge._target_taps = {}
    bridge._airplay_targets = None
    bridge._dlna_targets = manager
    bridge._volume_modes = {}
    first = sessions.begin(entry.id, "airplay")
    await bridge._start_entry_targets(entry.id, resume=False)
    assert [action.split("#")[1].rstrip('"') for action, _ in commands] == [
        "SetAVTransportURI",
        "Play",
    ]
    assert "uuid%3Alocal%3Adevice" in commands[0][1]
    second = sessions.begin("another", "airplay")
    await manager.play_targets("another", [device.id], "http://host/stream/another")
    before = len(commands)
    await sessions.close(first.token)
    assert len(commands) == before  # old cleanup must not stop the new owner
    await sessions.close(second.token)
    assert "#Stop" in commands[-1][0]
    await manager.close()


@pytest.mark.asyncio
async def test_volume_falls_back_to_pcm_without_rendering_control(local_config):
    discovery, device = local_config
    entry = settings.add_receiver("本地", "dlna", device.id)
    pipeline = SimpleNamespace(
        set_input_volume=lambda value: values.append(value), set_loudness_level=lambda value: None
    )
    values = []
    bridge = object.__new__(AudioBridge)
    bridge._sender_volumes = {}
    bridge._volume_modes = {entry.id: "linked"}
    bridge._pipelines = {entry.id: pipeline}
    bridge._airplay2_pipelines = {}
    bridge._airplay_targets = None
    bridge._dlna_discovery = discovery
    bridge._dlna_targets = SimpleNamespace(set_volume=AsyncMock())
    bridge.on_receiver_volume = None
    await bridge._local_volume(entry.id, 25)
    assert values == [25]
    bridge._dlna_targets.set_volume.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_refuses_busy_target_without_stopping_it(local_config):
    discovery, device = local_config
    sessions = PlaybackSessions(lambda: 300)
    lease = sessions.begin("music", "airplay")
    stop = AsyncMock()
    await sessions.targets.acquire(f"dlna-target:{device.id}", lease.token, stop)
    manager = SimpleNamespace(_soap=AsyncMock())
    bridge = SimpleNamespace(
        dlna_discovery=discovery,
        dlna_target_manager=manager,
        sessions=sessions,
        _stream_server=StreamServer(),
    )
    app = FastAPI()
    app.include_router(dlna_devices.install(bridge))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.post(f"/api/dlna-devices/{device.id}/test", json={"mode": "sample"})
    assert result.status_code == 409
    stop.assert_not_awaited()
    manager._soap.assert_not_awaited()
    assert bridge._stream_server._calibration_sessions == {}


@pytest.mark.asyncio
async def test_http_200_soap_fault_is_not_play_success(local_config):
    discovery, device = local_config
    manager = DlnaTargetManager(discovery)
    await manager._client.aclose()
    manager._client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text='<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault/></s:Body></s:Envelope>',
            )
        )
    )
    with pytest.raises(RuntimeError, match="SOAP Fault"):
        await manager._soap(device, "Play", {"InstanceID": "0", "Speed": "1"})
    await manager.close()


@pytest.mark.asyncio
async def test_continuous_probe_serves_audio_and_always_stops(local_config):
    import asyncio
    from urllib.parse import urlsplit
    from xml.sax.saxutils import unescape

    discovery, device = local_config
    sessions = PlaybackSessions(lambda: 300)
    server = StreamServer()
    commands = []
    pulls = []
    url = ""

    async def soap(target, action, arguments):
        nonlocal url
        commands.append(action)
        if action == "SetAVTransportURI":
            url = unescape(arguments["CurrentURI"])
        if action == "Play":

            async def pull():
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(server._app), base_url="http://stream"
                ) as client:
                    return await client.get(urlsplit(url).path)

            pulls.append(asyncio.create_task(pull()))

    bridge = SimpleNamespace(
        dlna_discovery=discovery,
        dlna_target_manager=SimpleNamespace(_soap=soap),
        sessions=sessions,
        _stream_server=server,
    )
    app = FastAPI()
    app.include_router(dlna_devices.install(bridge))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        result = await client.post(f"/api/dlna-devices/{device.id}/test", json={"mode": "stream"})
    assert result.status_code == 200
    assert result.json()["status"] == "pulled"
    responses = await asyncio.gather(*pulls)
    assert len(responses[0].content) > 44100 * 4 * 5
    assert commands == ["SetAVTransportURI", "Play", "Stop"]
    assert server._calibration_sessions == {}
    assert sessions.targets.current(f"dlna-target:{device.id}") is None
