import ast
import asyncio
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from micast.dlna_callback import pin_callback
from micast.dlna_events import DlnaEvents, SubscriptionError
from micast.receiver_startup import local_receiver_environment


def headers(host="10.0.0.20"):
    return {"nt": "upnp:event", "callback": f"<http://{host}:1234/events>"}


@pytest.mark.parametrize("target", ["127.0.0.1", "169.254.169.254", "10.0.0.21"])
async def test_remote_control_point_cannot_target_internal_or_other_host(target):
    events = DlnaEvents(lambda *_: {})
    with pytest.raises(SubscriptionError) as exc:
        await events.subscribe_from_peer("r", "AVTransport", headers(target), "10.0.0.20")
    assert exc.value.status == 412 and not events.subscriptions


async def test_dns_callback_is_pinned_and_rebinding_cannot_redirect(monkeypatch):
    answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.20", 1234))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: answers)
    sent = []

    async def send(url, request_headers, body):
        sent.append((url, request_headers["Host"]))

    events = DlnaEvents(lambda *_: {"Volume": 20}, send=send)
    subscription, _ = await events.subscribe_from_peer(
        "r",
        "RenderingControl",
        headers("control.local"),
        "10.0.0.20",
    )
    answers[:] = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 1234))]
    try:
        await events.start(subscription)
        await asyncio.sleep(0.02)
        assert sent == [("http://10.0.0.20:1234/events", "control.local:1234")]
        with pytest.raises(ValueError):
            await pin_callback("http://control.local/events", "10.0.0.20")
    finally:
        await events.close()


async def test_subscription_owner_controls_renewal_and_unsubscribe():
    events = DlnaEvents(lambda *_: {})
    subscription, _ = await events.subscribe_from_peer("r", "AVTransport", headers(), "10.0.0.20")
    sid = {"sid": subscription.sid}
    with pytest.raises(SubscriptionError):
        await events.subscribe_from_peer("r", "AVTransport", sid, "10.0.0.21")
    with pytest.raises(SubscriptionError):
        await events.unsubscribe("r", "AVTransport", sid, peer="10.0.0.21")
    renewed, _ = await events.subscribe_from_peer("r", "AVTransport", sid, "10.0.0.20")
    assert renewed is subscription
    await events.unsubscribe("r", "AVTransport", sid, peer="10.0.0.20")


@pytest.mark.parametrize("limit", ["max_subscriptions", "max_per_receiver", "max_per_peer"])
def test_subscription_quotas_reject_without_allocating_resources(limit):
    limits = {"max_subscriptions": 99, "max_per_receiver": 99, "max_per_peer": 99}
    limits[limit] = 2
    events = DlnaEvents(lambda *_: {}, **limits)
    for _ in range(2):
        events.subscribe("r", "AVTransport", headers())
    with pytest.raises(SubscriptionError) as exc:
        events.subscribe("r", "AVTransport", headers())
    assert exc.value.status == 503 and len(events.subscriptions) == 2


@pytest.mark.parametrize("value", ["<http://10.0.0.20/x>" * 5, "x" * 4097])
def test_callback_header_and_count_have_limits(value):
    events = DlnaEvents(lambda *_: {})
    with pytest.raises(SubscriptionError):
        events.subscribe("r", "AVTransport", {"nt": "upnp:event", "callback": value})
    assert not events.subscriptions


async def test_custom_local_command_starts_without_bundled_readiness(tmp_path, monkeypatch):
    from micast.pcm_source import LocalPCMSource

    monkeypatch.delenv("MICAST_AIRPLAY2_RUNTIME", raising=False)
    child = tmp_path / "pcm.py"
    child.write_text(
        "import sys,time\nsys.stdout.buffer.write(b'pcm');sys.stdout.flush()\ntime.sleep(30)\n"
    )
    settings = SimpleNamespace(
        airplay2_port=42700,
        port_is_strict=lambda _: False,
        airplay2_pcm_source=f"local:{sys.executable} {child}",
        config_path=tmp_path / "micast.json",
    )
    source = LocalPCMSource(
        f"{sys.executable} {child}", local_receiver_environment(settings, "a", "single")
    )
    try:
        reader = await asyncio.wait_for(source.start(), 2)
        assert await asyncio.wait_for(reader.readexactly(3), 1) == b"pcm"
        assert source.alive
    finally:
        await source.stop()


async def test_readiness_wait_drains_verbose_stderr_before_marker(tmp_path):
    from micast.pcm_source import LocalPCMSource

    child = tmp_path / "verbose.py"
    marker = tmp_path / "ready.json"
    child.write_text(
        "import sys,time,os,pathlib\n"
        "sys.stderr.buffer.write(b'x'*1000000);sys.stderr.flush()\n"
        "pathlib.Path(os.environ['MICAST_AIRPLAY2_READY_FILE']).write_text('{}')\n"
        "time.sleep(30)\n"
    )
    source = LocalPCMSource(
        f"{sys.executable} {child}", {"MICAST_AIRPLAY2_READY_FILE": str(marker)}
    )
    try:
        await asyncio.wait_for(source.start(), 3)
        assert source.alive and marker.exists()
        assert len(b"".join(source._stderr_tail)) <= 16 * 4096
    finally:
        await source.stop()


async def test_cancelled_local_start_does_not_leave_child_process(tmp_path):
    from micast.pcm_source import LocalPCMSource

    child = tmp_path / "waiting.py"
    child.write_text("import time\ntime.sleep(30)\n")
    source = LocalPCMSource(
        f"{sys.executable} {child}", {"MICAST_AIRPLAY2_READY_FILE": str(tmp_path / "absent.json")}
    )
    pending = asyncio.create_task(source.start())
    try:
        for _ in range(100):
            if source.alive:
                break
            await asyncio.sleep(0.01)
        assert source.alive
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not source.alive and source._stderr_task is None
    finally:
        await source.stop()


async def test_delivery_concurrency_and_shutdown_are_bounded():
    active = peak = 0

    async def send(*_):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.03)
        finally:
            active -= 1

    events = DlnaEvents(lambda *_: {"Volume": 10}, send=send)
    tasks = []
    for _ in range(16):
        subscription, _ = events.subscribe("r", "RenderingControl", headers())
        await events.start(subscription)
        tasks.append(subscription.task)
    await asyncio.sleep(0.01)
    await events.close()
    assert peak == 8 and active == 0
    assert all(task.done() for task in tasks) and not events.subscriptions


@pytest.mark.parametrize("raw", ["auto", "", "42301"])
def test_docker_launch_uses_effective_plan_not_parent_environment(tmp_path, monkeypatch, raw):
    from micast import docker_install

    env_file = tmp_path / ".env"
    env_file.write_text("MICAST_PORT=auto\nMICAST_STREAM_PORT=auto\n", encoding="utf-8")
    monkeypatch.setenv("MICAST_PORT", raw)
    monkeypatch.setenv("MICAST_STREAM_PORT", "auto")
    monkeypatch.setattr(
        sys, "argv", ["audit", "--mode", "single", "--env-file", str(env_file), "--up"]
    )
    monkeypatch.setattr(
        docker_install,
        "plan_host_ports",
        lambda *_args, **_kwargs: {
            "MICAST_PORT": "42301",
            "MICAST_STREAM_PORT": "42401",
        },
    )
    calls = []
    monkeypatch.setattr(
        docker_install.subprocess, "run", lambda *args, **kwargs: calls.append(kwargs)
    )
    docker_install.main()
    assert calls[0]["env"]["MICAST_PORT"] == "42301"
    assert calls[0]["env"]["MICAST_STREAM_PORT"] == "42401"
    assert os.environ["MICAST_PORT"] == raw


def test_custom_local_source_does_not_require_private_readiness_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("MICAST_AIRPLAY2_RUNTIME", str(tmp_path))
    settings = SimpleNamespace(
        airplay2_port=42700,
        port_is_strict=lambda _: False,
        airplay2_pcm_source="local:custom-receiver",
        config_path=tmp_path / "micast.json",
    )
    assert "MICAST_AIRPLAY2_READY_FILE" not in local_receiver_environment(settings, "a", "single")
    settings.airplay2_pcm_source = f"local:{tmp_path / 'run-shairport'}"
    assert local_receiver_environment(settings, "a", "single")["MICAST_AIRPLAY2_READY_FILE"]


async def test_retry_checks_enabled_state_inside_configuration_lock(monkeypatch):
    from micast import config_apply
    from micast.config import settings
    from micast.protocol_runtime import ProtocolRuntime, ProtocolUnavailable

    monkeypatch.setattr(config_apply, "_lock", asyncio.Lock())
    monkeypatch.setenv("MICAST_NETWORK_MODE", "native")
    monkeypatch.setattr(settings, "airplay_enabled", True)
    bridge = SimpleNamespace(reconcile_classic_feature=AsyncMock())
    async with config_apply._lock:
        retry = asyncio.create_task(ProtocolRuntime(bridge).retry("airplay"))
        await asyncio.sleep(0)
        settings.airplay_enabled = False
    with pytest.raises(ProtocolUnavailable):
        await retry
    bridge.reconcile_classic_feature.assert_not_awaited()


async def test_dlna_concurrent_start_stop_owns_one_generation(monkeypatch):
    from micast import dlna_listener

    opened = []

    class Lease:
        socket = object()
        port = 42300
        closed = False

        def close(self):
            self.closed = True

    def reserve(*_args, **_kwargs):
        lease = Lease()
        opened.append(lease)
        return lease

    class Server:
        def __init__(self, _):
            self.started = False
            self.should_exit = False

        async def serve(self, **_kwargs):
            await asyncio.sleep(0.02)
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0.001)

    monkeypatch.setattr(dlna_listener, "reserve_tcp", reserve)
    monkeypatch.setattr(dlna_listener, "Server", Server)
    monkeypatch.setattr(type(dlna_listener.settings), "apply_resolved_port", lambda *_: None)
    service = SimpleNamespace(http_available=False, http_detail="", stop=AsyncMock())
    listener = dlna_listener.DlnaHttpListener(service)
    starting = asyncio.create_task(listener.start())
    await asyncio.sleep(0.001)
    stopping = asyncio.create_task(listener.stop())
    await asyncio.gather(starting, stopping)
    assert opened[0].closed and listener.task is None
    await listener.start()
    assert not opened[1].closed and listener.server.started
    await listener.stop()
    assert all(lease.closed for lease in opened)


def test_atomic_store_failure_preserves_previous_file_and_cleans_temporary(tmp_path, monkeypatch):
    from micast import config_store

    path = tmp_path / "settings.json"
    config_store.write_json(path, {"revision": 1})
    previous = path.read_bytes()

    def fail(*_):
        raise OSError("disk failure")

    monkeypatch.setattr(config_store.os, "replace", fail)
    with pytest.raises(OSError):
        config_store.write_json(path, {"revision": 2})
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]


async def test_runtime_tasks_stop_all_and_reject_late_work():
    from micast.runtime_tasks import RuntimeTasks

    owned = RuntimeTasks()
    task = owned.start(asyncio.sleep(60), name="audit")
    await owned.close()
    assert task.done() and not owned.tasks
    with pytest.raises(RuntimeError):
        owned.start(asyncio.sleep(1), name="late")


async def test_application_startup_failure_closes_every_resource(monkeypatch):
    from micast import main

    stopped = []

    async def close(name):
        stopped.append(name)
        if name == "devices":
            raise RuntimeError("cleanup failure")

    def closer(name):
        return lambda: close(name)

    class Orchestrator:
        def __init__(self, *_args, **_kwargs):
            self.reconcile_group = AsyncMock()

        def attach(self):
            pass

        def schedule_codec_probe(self, **_kwargs):
            pass

        stop_all = staticmethod(closer("orchestrator"))

    async def failed_start():
        raise RuntimeError("startup failure")

    bridge = SimpleNamespace(
        start=failed_start,
        stop=closer("bridge"),
        attach_device_manager=lambda _: None,
        attach_supervisor=lambda _: None,
    )
    auth = SimpleNamespace(
        subscribe_expiry=lambda _: None,
        unsubscribe_expiry=lambda _: None,
        stored_identity=lambda: (False, None),
        connection_state=lambda: {"status": "off"},
        close=closer("auth"),
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            bridge=bridge,
            auth=auth,
            dlna=SimpleNamespace(stop=closer("dlna")),
            device_manager=SimpleNamespace(close=closer("devices")),
            notifier=SimpleNamespace(close=closer("notifier")),
        )
    )
    monkeypatch.setattr(main, "PlaybackOrchestrator", Orchestrator)
    monkeypatch.setattr(main, "AudioSupervisor", lambda *_: SimpleNamespace(tick=AsyncMock()))
    monkeypatch.setattr(main, "run_runtime_monitor", lambda: asyncio.sleep(60))
    monkeypatch.setattr(type(main.settings), "configure_airplay2_deployment", lambda *_: None)
    monkeypatch.setattr(main.raop_server, "configure_ports", lambda *_: None)
    with pytest.raises(RuntimeError, match="cleanup failure"):
        async with main.lifespan(app):
            pytest.fail("failed startup must not yield")
    assert stopped == ["orchestrator", "devices", "dlna", "auth", "notifier", "bridge"]


async def test_closing_events_during_dns_validation_cannot_add_late_subscription(monkeypatch):
    from micast import dlna_events

    entered, finish = asyncio.Event(), asyncio.Event()

    async def pin(*_, **_kwargs):
        entered.set()
        await finish.wait()
        return ("http://10.0.0.20/events", "10.0.0.20", "10.0.0.20")

    monkeypatch.setattr(dlna_events, "pin_callback", pin)
    events = DlnaEvents(lambda *_: {})
    pending = asyncio.create_task(
        events.subscribe_from_peer(
            "r",
            "AVTransport",
            headers(),
            "10.0.0.20",
        )
    )
    await entered.wait()
    await events.close()
    finish.set()
    with pytest.raises(SubscriptionError):
        await pending
    assert not events.subscriptions


async def test_local_proxy_peer_does_not_authorize_loopback_callback():
    events = DlnaEvents(lambda *_: {})
    with pytest.raises(SubscriptionError):
        await events.subscribe_from_peer("r", "AVTransport", headers("127.0.0.1"), "127.0.0.1")
    assert not events.subscriptions


def test_docker_preferences_and_printed_command_preserve_effective_ports(
    tmp_path, monkeypatch, capsys
):
    from micast import docker_install

    path = tmp_path / ".env"
    path.write_text('export MICAST_PORT="auto" # choose\nMICAST_STREAM_PORT=42401 # fixed\n')
    assert docker_install.read_port_preferences(path) == {
        "MICAST_PORT": "auto",
        "MICAST_STREAM_PORT": "42401",
    }
    monkeypatch.setenv("MICAST_PORT", "auto")
    monkeypatch.delenv("MICAST_STREAM_PORT", raising=False)
    monkeypatch.setattr(sys, "argv", ["plan", "--mode", "single", "--env-file", str(path)])
    monkeypatch.setattr(
        docker_install,
        "plan_host_ports",
        lambda *_args, **_kwargs: {
            "MICAST_PORT": "42301",
            "MICAST_STREAM_PORT": "42401",
        },
    )

    def reject(*_args, **_kwargs):
        pytest.fail("planning must not launch Docker")

    monkeypatch.setattr(docker_install.subprocess, "run", reject)
    docker_install.main()
    command = capsys.readouterr().out
    assert "MICAST_PORT" in command and "42301" in command
    assert "MICAST_STREAM_PORT" in command and "42401" in command
    assert "MICAST_PORT=auto" not in command


def test_fnos_preflight_bundle_runs_with_standard_library_only(tmp_path):
    root = Path(__file__).resolve().parents[1]
    for original, target in [
        ("installation.py", "port-preflight.py"),
        ("ports.py", "ports.py"),
        ("config_store.py", "config_store.py"),
    ]:
        shutil.copyfile(root / "micast" / original, tmp_path / target)
    result = subprocess.run(
        [sys.executable, "-S", str(tmp_path / "port-preflight.py"), "--help"],
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert b"--fnos" in result.stdout


def test_protocol_status_does_not_claim_starting_receiver_is_ready(monkeypatch):
    from micast.config import settings
    from micast.protocol_runtime import protocol_status

    monkeypatch.setenv("MICAST_NETWORK_MODE", "native")
    monkeypatch.setenv("MICAST_AIRPLAY2_MODE", "single")
    monkeypatch.setattr(settings, "airplay2_enabled", True)
    bridge = SimpleNamespace(_airplay2_runtime={"a": {"status": "starting"}})
    assert protocol_status(bridge, None)["airplay2"]["status"] == "blocked"


def test_domain_boundaries_do_not_depend_on_http_or_audio_coordinator():
    root = Path(__file__).resolve().parents[1] / "micast"
    for name in (
        "config_models",
        "config_store",
        "receiver_startup",
        "protocol_runtime",
        "dlna_callback",
        "runtime_tasks",
    ):
        tree = ast.parse((root / f"{name}.py").read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        assert not any(
            module.startswith(("fastapi", "micast.routes", "micast.audio_bridge"))
            for module in imports
        ), name


async def test_notify_does_not_buffer_control_point_response_body():
    async def callback(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            await reader.readexactly(5)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000000\r\n\r\n")
            await writer.drain()
            # Return no payload: a buffered request would fail, while GENA
            # can acknowledge the status without downloading a body.
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(callback, "127.0.0.1", 0)
    events = DlnaEvents(lambda *_: {})
    try:
        port = server.sockets[0].getsockname()[1]
        await asyncio.wait_for(events._send(f"http://127.0.0.1:{port}/events", {}, b"event"), 1)
    finally:
        await events.close()
        server.close()
        await server.wait_closed()


async def test_dlna_rejects_oversized_soap_before_dispatch(monkeypatch):
    import httpx
    from fastapi import APIRouter, FastAPI

    from micast.routes import dlna

    monkeypatch.setattr(dlna, "router", APIRouter(prefix="/dlna"))
    monkeypatch.setattr(dlna, "_receiver", lambda *_: object())
    dispatch = AsyncMock()
    monkeypatch.setattr(dlna, "_dispatch", dispatch)
    app = FastAPI()
    app.include_router(dlna.install(SimpleNamespace()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/dlna/r/AVTransport/control", content=b"x" * (1024 * 1024 + 1)
        )
    assert response.status_code == 413
    dispatch.assert_not_awaited()


@pytest.mark.parametrize("contents", [None, b"#!/bin/sh\r\nexit 0\r\n"])
async def test_broken_airplay2_launcher_fails_before_spawn(tmp_path, monkeypatch, contents):
    from unittest.mock import AsyncMock

    from micast.pcm_source import LocalPCMSource
    launcher = tmp_path / "run-shairport"
    if contents is not None:
        launcher.write_bytes(contents)
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    source = LocalPCMSource(str(launcher))
    with pytest.raises(RuntimeError, match="AirPlay 2 启动脚本"):
        await source.start()
    spawn.assert_not_awaited()
