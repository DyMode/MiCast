import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from micast import installation
from micast.config import Settings, settings
from micast.dlna_listener import DlnaHttpListener
from micast.docker_install import plan_host_ports
from micast.local_airplay import LocalAirPlayProvider
from micast.ports import probe_udp_group, reserve_tcp


def test_reserved_tcp_is_owned_until_closed():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        preferred = probe.getsockname()[1]
    with reserve_tcp(preferred, strict=True) as lease:
        assert lease.port == preferred
        with pytest.raises(RuntimeError):
            reserve_tcp(preferred, strict=True)
    with reserve_tcp(preferred, strict=True):
        pass


def test_automatic_allocation_skips_busy_port():
    with socket.socket() as occupied:
        occupied.bind(("0.0.0.0", 0))
        occupied.listen()
        preferred = occupied.getsockname()[1]
        with reserve_tcp(preferred) as lease:
            assert preferred < lease.port <= min(65535, preferred + 32)


def test_partial_udp_group_is_released_when_exhausted():
    with socket.socket(type=socket.SOCK_DGRAM) as occupied:
        occupied.bind(("0.0.0.0", 0))
        base = occupied.getsockname()[1] - 1
        with pytest.raises(RuntimeError):
            probe_udp_group(base, 3)
        with socket.socket(type=socket.SOCK_DGRAM) as released:
            released.bind(("0.0.0.0", base))


def mocked_preflight(monkeypatch, blocked):
    leased = []

    def reserve(port, *args, **kwargs):
        if port in blocked:
            raise RuntimeError("端口已被占用")
        lease = SimpleNamespace(port=port, close=lambda: leased.append(port))
        return lease

    def udp(port, **kwargs):
        if port in blocked:
            raise OSError("无法绑定")

    monkeypatch.setattr(installation, "reserve_tcp", reserve)
    monkeypatch.setattr(installation, "probe_udp", udp)
    monkeypatch.setattr(installation, "probe_udp_group", lambda *args: 42600)
    return leased


@pytest.mark.parametrize(
    "blocked,expected",
    [
        ({319, 1900}, {"airplay"}),
        ({5353}, {"dlna"}),
        ({1900}, {"airplay", "airplay2"}),
        ({5353, 1900}, set()),
    ],
)
def test_install_gate_accepts_any_available_protocol(monkeypatch, blocked, expected):
    closed = mocked_preflight(monkeypatch, blocked)
    report = installation.inspect_installation(unix_socket=True, airplay2=True)
    assert {key for key, value in report["features"].items() if value["available"]} == expected
    assert report["installable"] == bool(expected)
    assert closed


def test_core_failure_blocks_even_when_protocols_ready(monkeypatch):
    mocked_preflight(monkeypatch, {42400})
    report = installation.inspect_installation()
    assert not report["installable"]
    assert not report["core_available"]


def test_native_airplay2_preflight_uses_fixed_port_even_with_old_custom_value(monkeypatch):
    closed = mocked_preflight(monkeypatch, {7000})
    report = installation.inspect_installation(
        unix_socket=True, airplay2=True, preferred={"airplay2_port": 42700},
        strict=["airplay2_port"],
    )
    assert report["installable"]
    assert report["features"]["airplay2"]["status"] == "blocked"
    assert report["features"]["airplay"]["available"]
    assert report["features"]["dlna"]["available"]
    assert 42700 not in closed
    mocked_preflight(monkeypatch, set())
    restored = installation.inspect_installation(unix_socket=True, airplay2=True)
    assert restored["features"]["airplay2"]["available"]
    assert restored["selected_ports"]["airplay2_port"] == 7000


def test_fnos_upgrade_migrates_old_port_preserving_other_configuration(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("MICAST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MICAST_DEPLOYMENT", "fnos")
    monkeypatch.setenv("MICAST_AIRPLAY2_PORT", "42702")
    original = {"airplay2_port": 42700, "strict_ports": ["airplay2_port", "stream_port"],
                "stream_port": 42408, "airplay2_enabled": False, "app": {"name": "Home"}}
    path = tmp_path / "micast.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    config = Settings(_env_file=None)
    config.load_from_file()
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert config.airplay2_port == migrated["airplay2_port"] == 7000
    assert migrated["strict_ports"] == ["stream_port"]
    assert migrated["stream_port"] == 42408
    assert migrated["airplay2_enabled"] is False
    assert migrated["app"]["name"] == "Home"


def test_user_disabled_protocols_remain_disabled(monkeypatch):
    mocked_preflight(monkeypatch, set())
    report = installation.inspect_installation(enabled={"airplay": False, "dlna": True})
    assert report["features"]["airplay"]["status"] == "disabled"
    assert "rtsp" not in report["checks"]
    assert report["installable"]


def test_manual_port_policy_survives_save_and_auto_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("MICAST_DATA_DIR", str(tmp_path))
    config = Settings(_env_file=None)
    config.set_ports({"stream_port": 42400})
    assert config.port_is_strict("stream_port")
    restored = Settings(_env_file=None)
    restored.load_from_file()
    assert restored.port_is_strict("stream_port")
    restored.set_ports({"stream_port": None})
    assert not restored.port_is_strict("stream_port")
    with pytest.raises(ValueError):
        restored.set_ports({"airplay_udp_base": 65535})


def test_docker_host_preflight_holds_both_mappings():
    ports = plan_host_ports({"MICAST_PORT": "auto", "MICAST_STREAM_PORT": "auto"})
    assert ports["MICAST_PORT"] != ports["MICAST_STREAM_PORT"]
    with socket.socket() as occupied:
        occupied.bind(("0.0.0.0", 0))
        occupied.listen()
        with pytest.raises(RuntimeError):
            plan_host_ports({"MICAST_PORT": str(occupied.getsockname()[1])})


@pytest.mark.asyncio
async def test_failed_mdns_does_not_raise_and_can_be_retried():
    failing = True
    closed = []

    class Server:
        async def start(self):
            pass

        async def stop(self):
            closed.append(True)

    def zeroconf():
        if failing:
            raise OSError("5353 unavailable")
        return SimpleNamespace(close=lambda: None)

    provider = LocalAirPlayProvider(lambda *_: Server(), zeroconf)
    await provider.start([("a", "A")], "localhost", None, None)
    assert provider.receivers["a"].status == "error"
    failing = False
    await provider.start([("a", "A")], "localhost", None, None)
    assert provider.receivers["a"].status == "running"
    await provider.stop()
    assert closed == [True]


@pytest.mark.asyncio
async def test_optional_dlna_listener_conflict_and_recovery(monkeypatch):
    dlna = SimpleNamespace(http_available=True, http_detail="")
    listener = DlnaHttpListener(dlna)
    with socket.socket() as occupied:
        occupied.bind(("0.0.0.0", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        monkeypatch.setattr(settings, "port", port)
        monkeypatch.setattr(settings, "_preferred_port", None)
        monkeypatch.setattr(settings, "strict_ports", ["port"])
        await listener.start()
        assert not dlna.http_available
        assert listener.task is None
    try:
        await listener.start()
        assert dlna.http_available
        assert listener.server.started
        with socket.socket() as client:
            client.settimeout(1)
            assert client.connect_ex(("127.0.0.1", port)) == 0
    finally:
        await listener.stop()


def test_retry_api_does_not_enable_disabled_feature(tmp_path, monkeypatch):
    from micast.routes import config as routes

    monkeypatch.setenv("MICAST_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "airplay_enabled", False)
    monkeypatch.setattr(routes, "router", APIRouter(prefix="/api/config"))
    bridge = SimpleNamespace(reconcile_classic_feature=AsyncMock())
    app = FastAPI()
    app.include_router(routes.install(bridge))
    with TestClient(app) as client:
        response = client.post("/api/config/protocols/retry", json={"protocol": "airplay"})
    assert response.status_code == 409
    assert not settings.airplay_enabled
    bridge.reconcile_classic_feature.assert_not_awaited()


def test_retry_api_only_calls_requested_protocol(monkeypatch):
    from micast.routes import config as routes

    monkeypatch.setattr(settings, "airplay_enabled", True)
    monkeypatch.setattr(routes, "router", APIRouter(prefix="/api/config"))
    bridge = SimpleNamespace(reconcile_classic_feature=AsyncMock(), reconcile_airplay2=AsyncMock())
    dlna = SimpleNamespace(status="running", detail="正常", reconcile=AsyncMock())
    app = FastAPI()
    app.include_router(routes.install(bridge, dlna))
    with TestClient(app) as client:
        response = client.post("/api/config/protocols/retry", json={"protocol": "airplay"})
    assert response.status_code == 200
    bridge.reconcile_classic_feature.assert_awaited_once()
    bridge.reconcile_airplay2.assert_not_awaited()
    dlna.reconcile.assert_not_awaited()


def test_airplay2_port_api_rejects_edit_and_retry_leaves_other_protocols_running(monkeypatch):
    from micast.routes import config as routes
    monkeypatch.setenv("MICAST_DEPLOYMENT", "fnos")
    monkeypatch.setattr(settings, "airplay2_enabled", True)
    monkeypatch.setattr(routes, "router", APIRouter(prefix="/api/config"))
    bridge = SimpleNamespace(reconcile_classic_feature=AsyncMock(), reconcile_airplay2=AsyncMock())
    dlna = SimpleNamespace(status="running", detail="正常", reconcile=AsyncMock())
    app = FastAPI()
    app.include_router(routes.install(bridge, dlna))
    with TestClient(app) as client:
        for value in (None, 7000, 42700):
            response = client.post("/api/config/ports", json={"id": "airplay2_port", "port": value})
            assert response.status_code == 409 and "7000" in response.json()["detail"]
        response = client.post("/api/config/protocols/retry", json={"protocol": "airplay2"})
        assert response.status_code == 200
    bridge.reconcile_airplay2.assert_awaited_once()
    bridge.reconcile_classic_feature.assert_not_awaited()
    dlna.reconcile.assert_not_awaited()


def test_bridged_controller_reports_unavailable_ingress(monkeypatch):
    from micast.routes.config import _protocol_status

    monkeypatch.setenv("MICAST_DEPLOYMENT", "single")
    states = _protocol_status(None, None)
    assert states["airplay"]["status"] == "unsupported"
    assert states["dlna"]["status"] == "unsupported"


def test_docker_classic_rejects_mdns_only_when_rtsp_unavailable(monkeypatch):
    from micast import docker_install

    def udp(port, **kwargs):
        if port == 1900:
            raise OSError("SSDP busy")

    def tcp(port, **kwargs):
        if port == 42500:
            raise RuntimeError("RTSP exhausted")
        return SimpleNamespace(port=port, close=lambda: None)

    monkeypatch.setattr(docker_install, "probe_udp", udp)
    monkeypatch.setattr(docker_install, "reserve_tcp", tcp)
    with pytest.raises(RuntimeError, match="没有可用投送方式"):
        plan_host_ports({}, host_network=True)


@pytest.mark.asyncio
async def test_real_stream_server_uses_allocated_port_and_releases_it(monkeypatch):
    import httpx

    from micast.stream_server import StreamServer

    monkeypatch.setattr(settings, "strict_ports", [])
    monkeypatch.setattr("micast.config._ENV_PINNED", frozenset())
    with socket.socket() as occupied:
        occupied.bind(("0.0.0.0", 0))
        occupied.listen()
        preferred = occupied.getsockname()[1]
        monkeypatch.setattr(settings, "stream_port", preferred)
        monkeypatch.setattr(settings, "_preferred_stream_port", None)
        server = StreamServer()
        try:
            await server.start()
            actual = settings.stream_port
            assert actual != preferred
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.get(f"http://127.0.0.1:{actual}/diagnostic/builtin.wav")
            assert response.status_code == 200
            assert response.content.startswith(b"RIFF")
        finally:
            await server.stop()
    with reserve_tcp(actual, strict=True):
        pass


def test_external_airplay2_does_not_require_controller_ptp_ports(monkeypatch):
    mocked_preflight(monkeypatch, {319, 320, 9000, 5353, 1900})
    report = installation.inspect_installation(
        airplay2=True,
        classic_available=False,
        external_airplay2=lambda: None,
    )
    assert report["installable"]
    assert report["features"]["airplay2"]["available"]
    assert report["features"]["airplay"]["status"] == "unsupported"
    assert "ptp_event" not in report["checks"]


def test_preflight_keeps_dotenv_defaults_automatic(monkeypatch):
    import sys

    observed = {}
    monkeypatch.setattr(sys, "argv", ["preflight", "--automatic-env-ports", "port"])
    monkeypatch.setenv("MICAST_PORT", "42300")
    monkeypatch.setattr(
        installation,
        "inspect_installation",
        lambda **kwargs: observed.update(kwargs) or {"installable": True},
    )
    assert installation.main() == 0
    assert observed["preferred"]["port"] == 42300
    assert "port" not in observed["strict"]


@pytest.mark.asyncio
async def test_udp_pool_failure_does_not_advertise_unusable_classic_input(monkeypatch):
    from micast import ports

    provider = LocalAirPlayProvider(zeroconf_factory=lambda: SimpleNamespace(close=lambda: None))
    starts = []

    class Server:
        async def start(self):
            starts.append(True)

        async def stop(self):
            pass

    monkeypatch.setattr(provider, "_create_server", lambda *_: Server())
    monkeypatch.setattr(
        ports,
        "probe_udp_group",
        lambda *_: (_ for _ in ()).throw(RuntimeError("UDP pool exhausted")),
    )
    await provider.start([("a", "A")], "localhost", None, None)
    assert provider.receivers["a"].status == "error"
    assert not starts
    monkeypatch.setattr(ports, "probe_udp_group", lambda *_: 42600)
    await provider.start([("a", "A")], "localhost", None, None)
    assert provider.receivers["a"].status == "running"
    await provider.stop()


@pytest.mark.asyncio
async def test_mdns_discovery_can_run_without_classic_receivers():
    created = []

    def factory():
        created.append(True)
        return SimpleNamespace(close=lambda: None)

    provider = LocalAirPlayProvider(zeroconf_factory=factory)
    await provider.start([], "localhost", None, None)
    first = provider.ensure_zeroconf()
    assert provider.ensure_zeroconf() is first
    assert not provider.receivers
    assert created == [True]
    await provider.stop()
