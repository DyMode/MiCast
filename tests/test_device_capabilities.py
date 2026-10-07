import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from micast.audio_bridge import AudioBridge
from micast.config import ReceiverConfig, Settings, settings
from micast.control_routing import select_route
from micast.device_capabilities import VERIFICATION_AGE, CapabilityLedger
from micast.dlna_client import DlnaDevice, DlnaDiscovery, DlnaTargetManager
from micast.playback_sessions import PlaybackSessions
from micast.recovery import RecoveryCoordinator
from micast.routes import device_capabilities


def verified(ledger, key="dlna:d", format="MP3"):
    item = ledger.identify(key, model="OH2", firmware="1")
    proof = ledger.record(key, "dlna", "play_stream", "pulled", format=format)
    ledger.confirm_audio(key, "play_stream", format, proof.pulled_at, item.revision)


def test_persistence_expiry_firmware_and_exact_format(tmp_path):
    now = [1000.0]
    path = tmp_path / "capabilities.json"
    ledger = CapabilityLedger(path, clock=lambda: now[0])
    verified(ledger)
    loaded = CapabilityLedger(path, clock=lambda: now[0])
    assert loaded.confirmed("dlna:d", "play_stream", "MP3")
    assert not loaded.confirmed("dlna:d", "play_stream", "WAV")
    assert not loaded.confirmed("dlna:d", "play_file", "MP3")
    now[0] += VERIFICATION_AGE + 1
    assert not loaded.confirmed("dlna:d", "play_stream", "MP3")
    loaded.identify("dlna:d", firmware="2")
    assert not loaded.devices["dlna:d"].records
    assert loaded.devices["dlna:d"].revision == 1


def test_retarget_discards_association_without_duplicate_entry(monkeypatch):
    monkeypatch.setattr(Settings, "save_to_file", lambda self: None)
    cfg = Settings()
    cfg.airplay2_instances = []
    cfg.receivers = []
    entry = cfg.upsert_airplay2_instance(
        instance_id="ap2",
        name="入口",
        target_type="speaker",
        target_id="old",
        control_policy="local",
        local_target_id="d",
    )
    cfg.upsert_airplay2_instance(
        instance_id=entry.id, name="入口", target_type="speaker", target_id="new"
    )
    assert len(cfg.airplay2_instances) == 1
    assert cfg.airplay2_instances[0].local_target_id is None
    assert cfg.airplay2_instances[0].control_policy == "legacy"
    classic = cfg.add_receiver("经典", "speaker", "old")
    cfg.update_receiver(classic.id, control_policy="local", local_target_id="d")
    updated = cfg.update_receiver(classic.id, target_id="new")
    assert updated.local_target_id is None
    assert updated.control_policy == "legacy"


@pytest.mark.parametrize("confirmed", [False, True])
async def test_recovery_is_bounded_and_stop_prevents_restart(monkeypatch, confirmed):
    from micast import dlna_client

    monkeypatch.setattr(dlna_client, "STREAM_WATCH_INTERVAL", 0.002)
    monkeypatch.setattr(dlna_client, "STREAM_PULL_TIMEOUT", 0.01)
    monkeypatch.setattr(dlna_client, "STREAM_RETRY_BACKOFF", (0.001, 0.001))
    monkeypatch.setattr(settings.audio, "auto_transcode", True)
    monkeypatch.setattr(settings.audio, "format", "mp3")
    discovery = DlnaDiscovery()
    discovery._devices["d"] = DlnaDevice(
        "d", "OH2", model="OH2", firmware="1", control_url="http://d", last_seen=time.monotonic()
    )
    sessions = PlaybackSessions(lambda: 120)
    lease = sessions.begin("r", "airplay")
    ledger = CapabilityLedger()
    if confirmed:
        verified(ledger)
    manager = DlnaTargetManager(
        discovery,
        sessions,
        capabilities=ledger,
        stream_metrics=SimpleNamespace(sink_last_byte_at=lambda *_: 0),
        source_active=lambda _: True,
    )
    manager._soap = AsyncMock()
    try:
        await manager.play_targets("r", ["d"], "http://m/stream/r")
        runtime = manager._targets["r"]["d"]
        await asyncio.wait_for(asyncio.shield(runtime.monitor), 1)
        assert runtime.retries == (2 if confirmed else 0)
        assert runtime.failure_stage == "no_stream_pull"
        assert [call.args[1] for call in manager._soap.await_args_list].count("Play") == (
            3 if confirmed else 1
        )
        await manager.retry_owned("r")
        sessions.quiet(lease.token, grace=0)
        before = manager._soap.await_count
        await asyncio.wait_for(asyncio.shield(runtime.monitor), 1)
        assert manager._soap.await_count == before
    finally:
        await manager.close()


def test_confirmation_rejects_replaced_test_and_firmware():
    now = [1000.0]
    ledger = CapabilityLedger(clock=lambda: now[0])
    verified(ledger)
    proof = ledger.devices["dlna:d"].records["dlna:play_stream:MP3"]
    earlier = proof.pulled_at
    now[0] += 1
    ledger.record("dlna:d", "dlna", "play_stream", "pulled", format="MP3")
    with pytest.raises(ValueError):
        ledger.confirm_audio("dlna:d", "play_stream", "MP3", earlier, 0)
    ledger.identify("dlna:d", firmware="2")
    with pytest.raises(ValueError):
        ledger.confirm_audio("dlna:d", "play_stream", "MP3", now[0], 0)


def test_unsupported_clears_old_audio_proof_and_cannot_be_reconfirmed():
    ledger = CapabilityLedger(clock=lambda: 1000)
    verified(ledger)
    record = ledger.record("dlna:d", "dlna", "play_stream", "unsupported", format="MP3")
    assert record.pulled_at == record.confirmed_at == 0
    assert not ledger.describe("dlna:d")["records"][0]["can_confirm"]
    with pytest.raises(ValueError):
        ledger.confirm_audio("dlna:d", "play_stream", "MP3", 1000, 0)


def test_expired_confirmation_remains_stale_after_a_fresh_pull_and_clock_rollback():
    now = [1000.0]
    ledger = CapabilityLedger(clock=lambda: now[0])
    verified(ledger)
    now[0] += VERIFICATION_AGE + 1
    ledger.record("dlna:d", "dlna", "play_stream", "pulled", format="MP3")
    record = ledger.describe("dlna:d")["records"][0]
    assert record["stale"] and record["can_confirm"]
    assert not ledger.confirmed("dlna:d", "play_stream", "MP3")
    now[0] = 999
    assert not ledger.confirmed("dlna:d", "play_stream", "MP3")
    assert not ledger.describe("dlna:d")["records"][0]["can_confirm"]


def test_long_device_identifiers_do_not_invalidate_proof_on_each_refresh():
    ledger = CapabilityLedger(clock=lambda: 1000)
    model = "a" * 200
    ledger.identify("d", model=model)
    ledger.record("d", "dlna", "stop", "accepted")
    ledger.identify("d", model=model)
    assert ledger.devices["d"].revision == 0
    assert ledger.devices["d"].records


def test_route_requires_explicit_association_online_exact_proof_and_current_firmware():
    ledger = CapabilityLedger()
    verified(ledger)
    device = DlnaDevice(
        "d", "OH2", model="OH2", firmware="1", control_url="http://d", last_seen=time.monotonic()
    )
    discovery = DlnaDiscovery()
    discovery._devices["d"] = device
    entry = ReceiverConfig(
        id="r",
        name="入口",
        target_type="speaker",
        target_id="xiaomi-did",
        control_policy="auto",
        local_target_id="d",
    )
    assert select_route(entry, discovery, ledger, "MP3").channel == "dlna"
    assert select_route(entry, discovery, ledger, "FLAC").channel == "cloud"
    device.firmware = "2"
    assert select_route(entry, discovery, ledger, "MP3").channel == "cloud"
    entry.control_policy = "local"
    device.last_seen = 0
    assert select_route(entry, discovery, ledger, "MP3").channel == "dlna"
    entry.local_target_id = None
    assert select_route(entry, discovery, ledger, "MP3").channel == "blocked"


def test_registered_stream_format_and_route_remain_bound_to_session(monkeypatch):
    entry = ReceiverConfig(
        id="r",
        name="入口",
        target_type="speaker",
        target_id="did",
        control_policy="auto",
        local_target_id="d",
    )
    monkeypatch.setattr(settings, "receivers", [entry])
    monkeypatch.setattr(settings, "airplay2_instances", [])
    monkeypatch.setattr(settings.audio, "auto_transcode", True)
    monkeypatch.setattr(settings.audio, "format", "mp3")
    ledger = CapabilityLedger()
    verified(ledger)
    discovery = DlnaDiscovery()
    discovery._devices["d"] = DlnaDevice(
        "d", "OH2", model="OH2", firmware="1", control_url="http://d", last_seen=time.monotonic()
    )
    bridge = AudioBridge.__new__(AudioBridge)
    bridge.sessions = PlaybackSessions(lambda: 120)
    bridge.capabilities = ledger
    bridge._dlna_discovery = discovery
    bridge._stream_server = SimpleNamespace(stream_content_type=lambda _: "audio/flac")
    bridge.sessions.begin("r", "airplay", "old")
    assert bridge.resolve_control_route("r").channel == "cloud"
    verified(ledger, format="FLAC")
    assert bridge.resolve_control_route("r").channel == "cloud"
    bridge.sessions.begin("r", "airplay", "new")
    assert bridge.resolve_control_route("r").channel == "dlna"


async def test_failed_commands_are_not_unsupported_and_stopped_owner_cannot_retry():
    discovery = DlnaDiscovery()
    discovery._devices["d"] = DlnaDevice(
        "d", "OH2", control_url="http://d", last_seen=time.monotonic()
    )
    sessions = PlaybackSessions(lambda: 120)
    lease = sessions.begin("r", "airplay")
    ledger = CapabilityLedger()
    manager = DlnaTargetManager(discovery, sessions, capabilities=ledger)

    async def soap(device, action, arguments):
        if action != "Stop":
            raise httpx.ReadTimeout("timeout")

    manager._soap = AsyncMock(side_effect=soap)
    try:
        await manager.play_targets("r", ["d"], "http://m/stream/r")
        assert manager.statuses()["r"]["d"]["failure_stage"] == "control_timeout"
        assert not ledger.devices
        sessions.quiet(lease.token, grace=0)
        before = manager._soap.await_count
        with pytest.raises(ValueError):
            await manager.retry_owned("r")
        assert manager._soap.await_count == before
    finally:
        await manager.close()


async def test_capability_reads_never_query_cloud_and_policy_guard(monkeypatch):
    monkeypatch.setattr(Settings, "save_to_file", lambda self: None)
    entry = ReceiverConfig(id="r", name="入口", target_type="speaker", target_id="did")
    monkeypatch.setattr(settings, "receivers", [entry])
    monkeypatch.setattr(settings, "airplay2_instances", [])
    sessions = PlaybackSessions(lambda: 120)
    cloud = AsyncMock()
    bridge = SimpleNamespace(
        capabilities=CapabilityLedger(),
        dlna_discovery=None,
        sessions=sessions,
        diagnostics={},
        apply_config_change=AsyncMock(),
        device_manager=SimpleNamespace(list_devices=cloud),
        resolve_control_route=lambda _: select_route(entry, None, None, "MP3"),
    )
    app = FastAPI()
    app.include_router(device_capabilities.install(bridge))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://m"
    ) as client:
        response = await client.get("/api/capabilities")
        assert response.status_code == 200
        cloud.assert_not_awaited()
        assert response.json()["entries"][0]["route"]["channel"] == "cloud"
        response = await client.patch("/api/capabilities/classic/r", json={"policy": "local"})
        assert response.status_code == 400
        sessions.begin("r", "airplay")
        response = await client.patch("/api/capabilities/classic/r", json={"policy": "auto"})
        assert response.status_code == 409
        assert entry.control_policy == "legacy"


async def test_confirmation_checks_latest_firmware_without_requiring_a_panel_refresh():
    ledger = CapabilityLedger()
    verified(ledger)
    old = ledger.devices["dlna:d"].records["dlna:play_stream:MP3"].pulled_at
    discovery = DlnaDiscovery()
    discovery._devices["d"] = DlnaDevice(
        "d", "OH2", model="OH2", firmware="2", control_url="http://d"
    )
    app = FastAPI()
    app.include_router(
        device_capabilities.install(SimpleNamespace(capabilities=ledger, dlna_discovery=discovery))
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://m"
    ) as client:
        response = await client.post(
            "/api/capabilities/confirm",
            json={
                "device_id": "dlna:d",
                "action": "play_stream",
                "format": "MP3",
                "pulled_at": old,
                "revision": 0,
            },
        )
        assert response.status_code == 409
        assert ledger.devices["dlna:d"].revision == 1


async def test_capabilities_do_not_expose_cached_cloud_devices_from_previous_account(monkeypatch):
    monkeypatch.setattr(settings, "receivers", [])
    monkeypatch.setattr(settings, "airplay2_instances", [])
    ledger = CapabilityLedger()
    ledger.identify("xiaomi:old", name="旧账号设备")
    bridge = SimpleNamespace(capabilities=ledger, dlna_discovery=None, diagnostics={})
    manager = SimpleNamespace(_devices=[{"deviceID": "new", "name": "当前账号设备"}])
    app = FastAPI()
    app.include_router(device_capabilities.install(bridge, manager))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://m"
    ) as client:
        response = await client.get("/api/capabilities")
        assert [item["id"] for item in response.json()["devices"]] == ["xiaomi:new"]


async def test_stopping_during_manual_retry_returns_conflict_instead_of_success():
    sessions = PlaybackSessions(lambda: 120)
    lease = sessions.begin("r", "airplay")
    recovery = RecoveryCoordinator(sessions)
    entered = asyncio.Event()

    async def retry(owner):
        entered.set()
        await asyncio.Event().wait()

    bridge = SimpleNamespace(
        sessions=sessions,
        recovery=recovery,
        dlna_target_manager=SimpleNamespace(retry_owned=retry),
        resolve_control_route=lambda _: SimpleNamespace(channel="dlna"),
    )
    app = FastAPI()
    app.include_router(device_capabilities.install(bridge))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://m"
        ) as client:
            request = asyncio.create_task(client.post("/api/capabilities/retry/r"))
            await asyncio.wait_for(entered.wait(), 1)
            sessions.end(lease.token, "user_stopped", immediate=True)
            response = await asyncio.wait_for(request, 1)
            assert response.status_code == 409
    finally:
        await recovery.close()


def test_v1_wav_stream_proof_is_invalidated_without_losing_mp3(tmp_path):
    import json

    path = tmp_path / "capabilities.json"
    ledger = CapabilityLedger(path, clock=lambda: 1000)
    verified(ledger, format="WAV")
    verified(ledger, format="MP3")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = 1
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = CapabilityLedger(path, clock=lambda: 1000)
    assert not loaded.confirmed("dlna:d", "play_stream", "WAV")
    assert loaded.confirmed("dlna:d", "play_stream", "MP3")
