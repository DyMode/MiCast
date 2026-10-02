"""StreamServer.start() failure-path hygiene.

A bind failure (or a cancelled startup task) must leave no stale
``_task``/``_server`` behind, must not leak a CancelledError out of start(),
and bind-retries inside _serve must keep start()'s readiness check and
stop()'s shutdown signal pointed at the live Server instance.
"""

import asyncio

import pytest

import micast.stream_server as stream_server_module
from micast.stream_server import StreamServer


class _FailingServer:
    """ uvicorn stand-in: every serve() dies with SystemExit (bind failure)."""

    constructions = 0

    def __init__(self, config):
        type(self).constructions += 1
        self.config = config
        self.started = False
        self.should_exit = False

    async def serve(self, sockets=None):
        raise SystemExit(1)


@pytest.mark.asyncio
async def test_start_failure_cleans_task_and_server_state(monkeypatch):
    server = StreamServer()
    _FailingServer.constructions = 0
    monkeypatch.setattr(stream_server_module, "Server", _FailingServer)
    # The retry loop sleeps 1s between attempts; collapse it while still
    # yielding to the loop (a non-yielding mock starves the serve task).
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(stream_server_module.asyncio, "sleep", fast_sleep)

    with pytest.raises(RuntimeError, match="启动失败|启动超时"):
        await server.start()

    assert server._task is None
    assert server._server is None
    # Binding is reserved before serve; process startup failure must clean up.
    assert _FailingServer.constructions == 1


@pytest.mark.asyncio
async def test_start_with_cancelled_serve_task_reports_cleanly(monkeypatch):
    """stop() racing start() cancels the serve task; start() must translate
    that into its normal RuntimeError instead of leaking CancelledError."""

    class _HangingServer:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.should_exit = False

        async def serve(self, sockets=None):
            await asyncio.Event().wait()

    server = StreamServer()
    monkeypatch.setattr(stream_server_module, "Server", _HangingServer)

    async def cancel_soon():
        while server._task is None:
            await asyncio.sleep(0)
        server._task.cancel()

    canceller = asyncio.create_task(cancel_soon())
    with pytest.raises(RuntimeError, match="音频流服务启动失败"):
        await server.start()
    await canceller

    assert server._task is None
    assert server._server is None
