import asyncio
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import httpx
import pytest
from fastapi import APIRouter, FastAPI

from micast.config import settings
from micast.dlna import DlnaService
from micast.dlna_events import DlnaEvents, SubscriptionError
from micast.routes import dlna as routes
from tests.test_dlna import FakeDeviceManager, configure


async def until(condition):
    async def wait():
        while not condition():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), 3)


async def test_actual_notify_initial_play_pause_and_volume_events(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(settings, "sender_volume_mode", "independent")
    received = []

    async def callback(reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode()
            headers = dict(line.split(": ", 1) for line in head.split("\r\n")[1:] if ": " in line)
            body = await reader.readexactly(int(headers["Content-Length"]))
            received.append((head, headers, body))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(callback, "127.0.0.1", 0)
    service = DlnaService(FakeDeviceManager())
    service.device_manager.stop_playback = AsyncMock()
    service.events.interval = 0.01
    # This in-process integration fixture uses an explicitly trusted local
    # callback server; production rejects loopback even behind a local proxy.
    service.events.allow_loopback = True
    monkeypatch.setattr(routes, "router", APIRouter(prefix="/dlna"))
    app = FastAPI()
    app.include_router(routes.install(service))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        url = f"<http://127.0.0.1:{server.sockets[0].getsockname()[1]}/events>"
        response = await client.request("SUBSCRIBE", "/dlna/living/AVTransport/event", headers={"CALLBACK": url, "NT": "upnp:event"})
        assert response.status_code == 200
        sid = response.headers["sid"]
        await until(lambda: len(received) == 1)
        assert received[0][0].startswith("NOTIFY /events HTTP/1.1")
        assert received[0][1]["SEQ"] == "0" and received[0][1]["SID"] == sid
        change = ET.fromstring(received[0][2]).find(".//LastChange").text
        assert 'TransportState val="STOPPED"' in change
        await service.set_uri("living", "http://media.local/song.mp3", '<item><title>A &amp; B</title></item>')
        await service.play("living")
        await until(lambda: any(b'PLAYING' in item[2] for item in received))
        await service.pause("living")
        await until(lambda: any(b'PAUSED_PLAYBACK' in item[2] for item in received))
        renewed = await client.request("SUBSCRIBE", "/dlna/living/AVTransport/event", headers={"SID": sid, "TIMEOUT": "Second-60"})
        assert renewed.status_code == 200 and renewed.headers["sid"] == sid
        assert renewed.headers["timeout"] == "Second-60"
        assert [int(item[1]["SEQ"]) for item in received] == list(range(len(received)))
        response = await client.request("SUBSCRIBE", "/dlna/living/RenderingControl/event", headers={"CALLBACK": url, "NT": "upnp:event"})
        volume_sid = response.headers["sid"]
        await until(lambda: any(item[1]["SID"] == volume_sid for item in received))
        await service.set_volume("living", 63)
        await service.set_mute("living", True)
        await until(lambda: any(b'val=&quot;63&quot;' in item[2] and b'Mute' in item[2] and b'val=&quot;1&quot;' in item[2] for item in received))
        response = await client.request("UNSUBSCRIBE", "/dlna/living/AVTransport/event", headers={"SID": sid})
        assert response.status_code == 200 and sid not in service.events.subscriptions
        count = len([item for item in received if item[1]["SID"] == sid])
        await service.stop_playback("living")
        await asyncio.sleep(0.03)
        assert len([item for item in received if item[1]["SID"] == sid]) == count
    finally:
        await client.aclose()
        await service.stop()
        server.close()
        await server.wait_closed()


async def test_subscriptions_expire_renew_and_stop_without_late_delivery():
    now, sent = [0], []

    async def send(url, headers, body):
        sent.append(headers["SEQ"])

    events = DlnaEvents(lambda *args: {"Volume": 20}, clock=lambda: now[0], send=send, interval=0.005)
    subscription, _ = events.subscribe("r", "RenderingControl", {"nt": "upnp:event", "callback": "<http://controller/events>", "timeout": "Second-1"})
    await events.start(subscription)
    await until(lambda: sent == ["0"])
    now[0] = 0.9
    renewed, _ = events.subscribe("r", "RenderingControl", {"sid": subscription.sid, "timeout": "Second-2"})
    assert renewed is subscription and subscription.expires == 2.9
    now[0] = 3
    await until(lambda: not events.subscriptions)
    with pytest.raises(SubscriptionError) as exc:
        events.subscribe("r", "RenderingControl", {"sid": subscription.sid})
    assert exc.value.status == 412
    await events.close()
    assert subscription.task.done() and sent == ["0"]


async def test_callback_failure_uses_next_callback_and_cancellation_stops_pending_send():
    entered = asyncio.Event()
    attempts = []
    blocked = False

    async def send(url, headers, body):
        attempts.append((url, headers["SEQ"]))
        if url.endswith("bad"):
            raise httpx.ConnectError("offline")
        if blocked:
            entered.set()
            await asyncio.Future()

    state = {"Volume": 20}
    events = DlnaEvents(lambda *args: state, send=send, interval=0.005)
    subscription, _ = events.subscribe("r", "RenderingControl", {"nt": "upnp:event", "callback": "<http://host/bad> <http://host/good>"})
    await events.start(subscription)
    await until(lambda: subscription.sequence == 1)
    assert attempts == [("http://host/bad", "0"), ("http://host/good", "0")]
    blocked = True
    state["Volume"] = 30
    await asyncio.wait_for(entered.wait(), 1)
    await events.unsubscribe("r", "RenderingControl", {"sid": subscription.sid})
    assert subscription.task.done() and not events.subscriptions


@pytest.mark.parametrize("headers,status", [
    ({}, 412), ({"nt": "upnp:event", "callback": "<file:///tmp/a>"}, 412),
    ({"nt": "upnp:event", "callback": "<http://[bad>"}, 412),
    ({"nt": "upnp:event", "callback": "<http://host/x>", "timeout": "Second-0"}, 400),
])
def test_invalid_subscription_headers(headers, status):
    events = DlnaEvents(lambda *args: {})
    with pytest.raises(SubscriptionError) as exc:
        events.subscribe("r", "AVTransport", headers)
    assert exc.value.status == status and not events.subscriptions


async def test_subscription_sid_is_bound_to_receiver_and_service():
    events = DlnaEvents(lambda *args: {})
    subscription, _ = events.subscribe("r", "AVTransport", {"nt": "upnp:event", "callback": "<http://host/x>"})
    for receiver, service in [("other", "AVTransport"), ("r", "RenderingControl")]:
        with pytest.raises(SubscriptionError):
            events.subscribe(receiver, service, {"sid": subscription.sid})
        with pytest.raises(SubscriptionError):
            await events.unsubscribe(receiver, service, {"sid": subscription.sid})
    await events.close()


async def test_failed_initial_delivery_retries_without_losing_subscription_or_sequence():
    attempts = []

    async def send(url, headers, body):
        attempts.append(headers["SEQ"])
        if len(attempts) == 1:
            raise httpx.ConnectError("temporarily offline")

    events = DlnaEvents(lambda *args: {"TransportState": "STOPPED"}, send=send, interval=0.005)
    subscription, _ = events.subscribe("r", "AVTransport", {"nt": "upnp:event", "callback": "<http://host/events>"})
    await events.start(subscription)
    await until(lambda: subscription.sequence == 1)
    assert attempts == ["0", "0"] and subscription.sid in events.subscriptions
    await events.close()
    assert subscription.task.done()


def test_scpd_declares_evented_variables():
    namespace = {"u": "urn:schemas-upnp-org:service-1-0"}
    for service, expected in [("AVTransport", ["LastChange"]), ("RenderingControl", ["LastChange"]), ("ConnectionManager", ["SourceProtocolInfo", "SinkProtocolInfo", "CurrentConnectionIDs"])]:
        root = ET.fromstring(routes._scpd(service))
        names = [node.findtext("u:name", namespaces=namespace) for node in root.findall("u:serviceStateTable/u:stateVariable", namespace) if node.attrib["sendEvents"] == "yes"]
        assert names == expected
