"""Port auto-resolution: busy preferred ports slide to a free one."""

import socket
from types import SimpleNamespace

import pytest

from micast.config import port_in_use, resolve_port
from micast.routes.config import _dlna_recast_required


@pytest.fixture
def taken_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    s.listen(1)
    yield s.getsockname()[1]
    s.close()


def test_free_port_used_as_is():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert resolve_port(free, "MICAST_TEST_PORT") == free


def test_busy_port_slides_to_next_free(taken_port, monkeypatch):
    monkeypatch.delenv("MICAST_TEST_PORT", raising=False)
    resolved = resolve_port(taken_port, "MICAST_TEST_PORT")
    assert resolved != taken_port
    assert not port_in_use(resolved)


def test_pinned_port_fails_loudly(taken_port, monkeypatch):
    # Only real env vars (present before .env loading) count as pins.
    monkeypatch.setattr("micast.config._ENV_PINNED", frozenset({"MICAST_TEST_PORT"}))
    with pytest.raises(RuntimeError, match="已被占用"):
        resolve_port(taken_port, "MICAST_TEST_PORT")


def test_port_in_use_detection(taken_port):
    assert port_in_use(taken_port)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert not port_in_use(free)


def test_dlna_recast_guidance_follows_latched_media_uri():
    assert not _dlna_recast_required(None)
    assert not _dlna_recast_required(SimpleNamespace(states={}))
    assert not _dlna_recast_required(SimpleNamespace(states={"speaker": SimpleNamespace(uri="")}))
    # STOPPED media may be resumed without another SetAVTransportURI, so it
    # still needs an explicit re-cast to pick up the new volume mode.
    assert _dlna_recast_required(
        SimpleNamespace(states={"speaker": SimpleNamespace(uri="https://example/media.mp3")})
    )


def test_raop_configure_ports_moves_scan_base(monkeypatch):
    from micast.raop import server as raop_server

    monkeypatch.setattr(raop_server, "_reserved_udp_bases", set())
    raop_server.configure_ports(5100, 6300)
    try:
        assert raop_server.rtsp_base() == 5100
        assert raop_server.udp_pool() == (6300, 6300 + 195)
        base = raop_server._reserve_udp_base()
        assert 6300 <= base <= 6495
    finally:
        raop_server.configure_ports(None, None)
    assert raop_server.rtsp_base() == 42500
    assert raop_server.udp_pool() == (42600, 42795)


def test_set_ports_validates_and_persists(tmp_path, monkeypatch):
    from micast.config import Settings

    monkeypatch.setenv("MICAST_DATA_DIR", str(tmp_path))
    s = Settings()
    s.set_ports({"airplay_rtsp_port": 5100, "stream_port": 18080})
    assert s.airplay_rtsp_port == 5100
    assert s.preferred_port("stream_port") == 18080

    import json

    saved = json.loads((tmp_path / "micast.json").read_text(encoding="utf-8"))
    assert saved["airplay_rtsp_port"] == 5100
    assert saved["stream_port"] == 18080

    s.set_ports({"airplay_rtsp_port": None})
    assert s.airplay_rtsp_port is None
    with pytest.raises(ValueError):
        s.set_ports({"stream_port": 80})
    with pytest.raises(ValueError):
        s.set_ports({"nonsense": 42500})


def test_settings_save_is_atomic_and_leaves_no_temporary_file(tmp_path, monkeypatch):
    from micast.config import Settings

    monkeypatch.setenv("MICAST_DATA_DIR", str(tmp_path))
    s = Settings()
    s.update_app_name("客厅")

    assert (tmp_path / "micast.json").read_text(encoding="utf-8").startswith("{")
    assert not list(tmp_path.glob(".micast.json.*.tmp"))


def test_settings_snapshot_restores_fields_and_private_state(tmp_path, monkeypatch):
    from micast.config import Settings

    monkeypatch.setenv("MICAST_DATA_DIR", str(tmp_path))
    s = Settings()
    snapshot = s.snapshot()
    s.update_app_name("临时名称")
    changed_revision = s.config_revision

    s.restore(snapshot)

    assert s.app.name != "临时名称"
    assert s.config_revision < changed_revision + 1


def test_resolved_port_not_persisted_as_preferred(tmp_path, monkeypatch):
    """A one-off slide (42300 busy) must not become the persisted preference."""
    from micast.config import Settings

    monkeypatch.setenv("MICAST_DATA_DIR", str(tmp_path))
    s = Settings(port=42300, stream_port=42400, _env_file=None)
    s.apply_resolved_port("port", 42307)
    assert s.port == 42307
    assert s.preferred_port("port") == 42300
    s.save_to_file()

    import json

    saved = json.loads((tmp_path / "micast.json").read_text(encoding="utf-8"))
    assert saved["port"] == 42300


def test_resolve_port_stops_at_maximum(monkeypatch):
    checked = []
    monkeypatch.setattr("micast.config._ENV_PINNED", frozenset())
    monkeypatch.setattr("micast.config.port_in_use", lambda port: checked.append(port) or True)
    with pytest.raises(RuntimeError, match="65534-65535"):
        resolve_port(65534, "MICAST_TEST_PORT")
    assert checked == [65534, 65535]


@pytest.mark.asyncio
async def test_rtsp_skips_real_busy_listener(monkeypatch, taken_port):
    from micast.raop import server as raop_server

    monkeypatch.setattr(raop_server, "_rtsp_base", taken_port)
    monkeypatch.setattr(raop_server, "_reserved_rtsp_ports", set())
    server, actual = await raop_server._start_rtsp_server(lambda reader, writer: writer.close())
    try:
        assert taken_port < actual <= min(65535, taken_port + 31)
    finally:
        server.close()
        await server.wait_closed()


def test_ports_report_pruned_by_deployment(monkeypatch):
    from micast.config import settings
    from micast.routes.config import _ports_report

    monkeypatch.setattr(settings, "airplay2_enabled", False)
    monkeypatch.delenv("MICAST_UNIX_SOCKET", raising=False)

    monkeypatch.setenv("MICAST_DEPLOYMENT", "windows")
    ids = {entry["id"] for entry in _ports_report(None, None)}
    assert "airplay2_port" not in ids
    mdns = next(entry for entry in _ports_report(None, None) if entry["id"] == "mdns")
    assert mdns["status"] == "off" and mdns["actual"] is None
    assert {"port", "stream_port", "airplay_rtsp_port", "airplay_udp_base", "mdns", "ssdp"} <= ids
    web = next(e for e in _ports_report(None, None) if e["id"] == "port")
    assert web["editable"]

    monkeypatch.setenv("MICAST_DEPLOYMENT", "fnos")
    monkeypatch.setenv("MICAST_UNIX_SOCKET", "/run/micast/app.sock")
    entries = _ports_report(None, None)
    ids = {entry["id"] for entry in entries}
    assert {"airplay2_port", "nqptp"} <= ids
    web = next(e for e in entries if e["id"] == "port")
    assert web["status"] == "hosted" and not web["editable"]
    ap2 = next(e for e in entries if e["id"] == "airplay2_port")
    assert not ap2["editable"]
    assert ap2["mode"] == "fixed" and ap2["preferred"] == 7000
    failed_bridge = SimpleNamespace(_airplay2_runtime={"airplay2": {
        "status": "error", "detail": "固定 TCP 7000 被占用，释放后重新启动",
    }})
    monkeypatch.setattr(settings, "airplay2_enabled", True)
    failed = next(item for item in _ports_report(failed_bridge, None) if item["id"] == "airplay2_port")
    assert failed["status"] == "error" and "7000" in failed["detail"]
