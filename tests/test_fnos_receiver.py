"""Receiver startup behavior tested with real sockets and child processes."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import micast.fnos_receiver as runtime


@pytest.mark.parametrize("protocol,expected", [("airplay2", 7000), ("auto", 7000), ("classic", 5000)])
def test_packaged_native_port_contract(protocol, expected):
    from micast.ports import native_receiver_port
    assert native_receiver_port(protocol) == expected


def test_native_port_contract_rejects_unknown_protocol():
    from micast.ports import native_receiver_port
    with pytest.raises(ValueError):
        native_receiver_port("other")


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
async def test_delayed_listener_is_not_restarted(host, monkeypatch):
    monkeypatch.setattr(runtime, "owns_listener", lambda *args: True)
    family = socket.AF_INET6 if host == "::1" else socket.AF_INET
    with socket.socket(family) as server:
        try:
            server.bind((host, 0))
        except OSError:
            pytest.skip("IPv6 loopback unavailable")
        alive = SimpleNamespace(poll=lambda: None, pid=1)
        task = asyncio.create_task(asyncio.to_thread(
            runtime.await_listener, alive, alive, server.getsockname()[1], 4
        ))
        await asyncio.sleep(1.3)
        assert not task.done()
        server.listen()
        assert await task == "ready"


def test_clock_failure_is_separate_from_listener_failure():
    assert runtime.await_listener(SimpleNamespace(poll=lambda: None),
                                  SimpleNamespace(poll=lambda: 1), 42700) == "clock_exited"


@pytest.mark.parametrize("listening", [False, True])
def test_fixed_receiver_ignores_legacy_port_and_never_changes_port(tmp_path, monkeypatch, listening):
    # Real subprocess cleanup and TCP readiness; native ownership is tested
    # separately because Windows does not expose Linux procfs.
    for key, value in {
        "MICAST_AIRPLAY2_RUNTIME": str(tmp_path), "MICAST_DATA_DIR": str(tmp_path),
        "PYTHON_BIN": sys.executable, "MICAST_AIRPLAY2_PORT": "42700",
    }.items():
        monkeypatch.setenv(key, value)
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    monkeypatch.setattr(runtime, "AIRPLAY2_RECEIVER_PORT", port)
    monkeypatch.setattr(runtime, "owns_listener", lambda *args: True)
    children, commands = [], []
    original_spawn = subprocess.Popen
    original_wait = runtime.await_listener

    def spawn(command, **kwargs):
        commands.append(command)
        script = "import time;time.sleep(30)"
        if "-p" in command and listening:
            script = ("import socket,time;s=socket.socket();"
                      f"s.bind(('127.0.0.1',{port}));s.listen();time.sleep(1)")
        child = original_spawn([sys.executable, "-c", script], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    monkeypatch.setattr(runtime, "await_listener",
                        lambda r, c, p: original_wait(r, c, p, timeout=1.5))
    with pytest.raises(RuntimeError):
        runtime.supervise()
    report = json.loads((tmp_path / "airplay2-startup.json").read_text(encoding="utf-8"))
    assert len(report["attempts"]) == 1
    assert report["attempts"][0]["state"] == ("ready" if listening else "listener_timeout")
    assert report["preferred"] == port
    assert report["fixed_port"]
    receivers = [command for command in commands if "-p" in command]
    assert len(receivers) == 1 and receivers[0][-1] == str(port)
    assert all(child.poll() is not None for child in children)
    assert not (tmp_path / "airplay2-ready.json").exists()
    assert not (tmp_path / "shairport-sync.pid").exists()


def test_occupied_fixed_port_stops_before_spawning_and_release_allows_retry(tmp_path, monkeypatch):
    for key, value in {"MICAST_AIRPLAY2_RUNTIME": str(tmp_path),
                       "MICAST_DATA_DIR": str(tmp_path), "PYTHON_BIN": sys.executable}.items():
        monkeypatch.setenv(key, value)
    calls = []
    def cannot_spawn(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("reached clock startup")
    monkeypatch.setattr(runtime.subprocess, "Popen", cannot_spawn)
    with socket.socket() as busy:
        busy.bind(("0.0.0.0", 0))
        busy.listen()
        port = busy.getsockname()[1]
        monkeypatch.setattr(runtime, "AIRPLAY2_RECEIVER_PORT", port)
        with pytest.raises(RuntimeError, match=str(port)):
            runtime.supervise()
        assert not calls
    with pytest.raises(RuntimeError, match="reached clock startup"):
        runtime.supervise()
    assert len(calls) == 1


@pytest.mark.parametrize("port", [7000])
def test_generated_config_uses_the_selected_port(port):
    text = runtime.configuration(port, 'MiCast "name"', '/app/callback.py', 'python3', 'airplay2')
    assert f"port = {port};" in text
    assert json.dumps('MiCast "name"') in text



def test_native_startup_report_is_bounded(tmp_path, monkeypatch):
    import micast.diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "settings", SimpleNamespace(config_path=tmp_path / "micast.json"))
    (tmp_path / "shairport-startup.log").write_bytes(b"x" * 65536 + b"end")
    data = diagnostics.receiver_startup_logs()
    assert data["shairport-startup.log"]["truncated"]
    assert len(data["shairport-startup.log"]["text"]) == 32768
    assert data["shairport-startup.log"]["text"].endswith("end")


@pytest.mark.parametrize("family", ["tcp", "tcp6"])
def test_listener_is_matched_to_child_socket_inode(tmp_path, monkeypatch, family):
    pid = 123
    process = tmp_path / str(pid)
    (process / "fd").mkdir(parents=True)
    (process / "net").mkdir()
    (process / "fd" / "4").touch()
    # Linux /proc socket tables have inode in column 9, state 0A is LISTEN.
    (process / "net" / family).write_text(
        "header\n0: 00000000:1B58 00000000:0000 0A 0 0 0 1000 0 999\n"
    )
    monkeypatch.setattr(runtime.os, "readlink", lambda _: "socket:[888]")
    assert not runtime.owns_listener(pid, 7000, tmp_path)
    monkeypatch.setattr(runtime.os, "readlink", lambda _: "socket:[999]")
    assert runtime.owns_listener(pid, 7000, tmp_path)
    assert not runtime.owns_listener(pid, 7001, tmp_path)


def test_foreign_listener_is_never_marked_ready(monkeypatch):
    monkeypatch.setattr(runtime, "owns_listener", lambda *args: False)
    monkeypatch.setattr(runtime, "probe_listener", lambda *args: True)
    alive = SimpleNamespace(poll=lambda: None, pid=123)
    assert runtime.await_listener(alive, alive, 7000, timeout=0.02) == "listener_timeout"


@pytest.mark.skipif(sys.platform != "linux" or not os.environ.get("MICAST_TEST_NATIVE_RUNTIME"),
                    reason="Requires Linux and an explicitly configured native runtime")
def test_real_packaged_receiver_uses_fixed_port_and_owned_listener(tmp_path):
    """Run on fnOS as the app user with its provisioned NQPTP loader caps."""
    env = {**os.environ, "MICAST_AIRPLAY2_RUNTIME": os.environ["MICAST_TEST_NATIVE_RUNTIME"],
           "MICAST_DATA_DIR": str(tmp_path), "MICAST_LOG_DIR": str(tmp_path),
           "PYTHON_BIN": sys.executable, "MICAST_AIRPLAY2_PORT": "42700",
           "MICAST_AIRPLAY_NAME": "MiCast-port-verification"}
    ready = tmp_path / "airplay2-ready.json"
    env["MICAST_AIRPLAY2_READY_FILE"] = str(ready)
    process = subprocess.Popen([sys.executable, "-m", "micast.fnos_receiver"],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 25
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        if process.poll() is not None:
            pytest.fail(process.communicate()[1].decode(errors="replace"))
        assert ready.exists(), "Native receiver never became ready"
        data = json.loads(ready.read_text())
        assert data["port"] == 7000
        assert runtime.owns_listener(data["pid"], 7000)
        report = json.loads((tmp_path / "airplay2-startup.json").read_text())
        assert len(report["attempts"]) == 1 and report["state"] == "ready"
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            runtime.stop_process(process)
