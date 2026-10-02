import sys
from types import SimpleNamespace

import pytest

from micast import __main__ as entrypoint


def test_unix_socket_management_does_not_depend_on_dlna_tcp(monkeypatch, tmp_path):
    path = tmp_path / "micast.sock"
    fake_app = object()
    calls = []
    monkeypatch.setenv("MICAST_UNIX_SOCKET", str(path))
    monkeypatch.setattr(
        entrypoint,
        "reserve_tcp",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("TCP must be optional")),
    )
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    monkeypatch.setitem(sys.modules, "micast.main", SimpleNamespace(app=fake_app))
    entrypoint.main()
    assert calls == [(fake_app, {"uds": str(path), "log_level": "info"})]


def test_tcp_launcher_hands_reserved_socket_to_server(monkeypatch):
    calls = []
    fake_app = object()
    closed = []
    lease = SimpleNamespace(port=3456, socket=object(), close=lambda: closed.append(True))
    monkeypatch.delenv("MICAST_UNIX_SOCKET", raising=False)
    monkeypatch.setattr(entrypoint.settings, "port", 3000)
    monkeypatch.setattr(entrypoint.settings, "_preferred_port", None)
    monkeypatch.setattr(entrypoint, "reserve_tcp", lambda *_a, **_k: lease)
    monkeypatch.setitem(sys.modules, "micast.main", SimpleNamespace(app=fake_app))
    # PyInstaller windowed executables do not provide stdout/stderr.
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    class FakeServer:
        def __init__(self, config):
            self.config = config

        def run(self, sockets=None):
            calls.append((self.config, sockets))

    monkeypatch.setattr(entrypoint.uvicorn, "Server", FakeServer)
    entrypoint.main()
    assert calls[0][0].app is fake_app
    assert calls[0][0].port == 3456
    assert calls[0][1] == [lease.socket]
    assert closed == [True]


def test_installed_source_restart_does_not_gate_disabled_protocols(monkeypatch, tmp_path):
    monkeypatch.setenv("MICAST_DATA_DIR", str(tmp_path))
    (tmp_path / "micast.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["micast", "--preflight-if-new"])
    with pytest.raises(SystemExit) as result:
        entrypoint.main()
    assert result.value.code == 0
