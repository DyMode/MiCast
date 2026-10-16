"""Readiness contract: serving HTTP must not wait for the audio path.

The fnOS app centre treats a slow ``cmd/main start`` as a failed enable and can
kill the process, so ``/health`` has to answer as soon as the HTTP layer is up
while receivers, discovery and pipelines come up in the background. ``/ready``
is the endpoint that reports that second half.
"""

import asyncio
import time

from fastapi.testclient import TestClient

from micast import main as main_module


def test_health_answers_while_the_audio_path_is_still_starting(monkeypatch):
    app = main_module.app
    finished = False

    async def slow_start() -> None:
        nonlocal finished
        # Bridge.start() marks itself "starting" and keeps that status while the
        # receivers come up.
        app.state.bridge._status = "starting"
        await asyncio.sleep(2)
        finished = True

    monkeypatch.setattr(app.state.bridge, "start", slow_start)

    with TestClient(app) as client:
        started = time.monotonic()
        response = client.get("/health")
        elapsed = time.monotonic() - started

        assert response.status_code == 200
        # The audio path sleeps for 2s; answering later than that means the
        # lifespan awaited it again.
        assert elapsed < 1.5
        assert not finished

        # /ready is the endpoint that reports the half still coming up.
        ready = client.get("/app/micast/ready")
        assert ready.status_code == 503
        assert ready.json()["status"] == "not_ready"
        assert ready.json()["audio"] == "starting"
