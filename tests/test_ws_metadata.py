import json
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

from micast.routes import ws


@pytest.mark.asyncio
async def test_metadata_push_is_fast_without_accelerating_topology_or_cloud(monkeypatch):
    clock = [0]
    counts = {"topology": 0, "playback": 0}
    class Bridge:
        @property
        def _now_playing(self):
            return {"r1": {"lyric_lines": ["第一行" if clock[0] == 0 else "第二行"]}}
        @property
        def status(self):
            return {"status": "running", "now_playing": self._now_playing}
    class Socket:
        cookies = {}
        def __init__(self): self.messages = []
        async def accept(self): pass
        async def close(self): pass
        async def send_text(self, value): self.messages.append(json.loads(value))
        async def send_json(self, value): self.messages.append(value)
    async def sleep(seconds):
        assert seconds == .5
        clock[0] += 1
        if clock[0] == 13: raise WebSocketDisconnect()
    def topology(*args):
        counts["topology"] += 1
        return {}
    async def playback(*args):
        counts["playback"] += 1
        return {"playing": True}
    monkeypatch.setattr(ws.asyncio, "sleep", sleep)
    monkeypatch.setattr(ws, "build_topology", topology)
    monkeypatch.setattr(ws, "build_playback_state", playback)
    socket = Socket()
    router = ws.install(Bridge(), SimpleNamespace())
    await router.routes[-1].endpoint(socket)
    statuses = [m for m in socket.messages if m["type"] == "status"]
    assert [m["data"]["now_playing"]["r1"]["lyric_lines"] for m in statuses] == [["第一行"], ["第二行"]]
    assert counts == {"topology": 4, "playback": 2}
