"""Bounded UPnP event subscriptions with per-subscriber ordered delivery."""

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass
from html import escape
from urllib.parse import urlsplit

import httpx

from micast.dlna_callback import pin_callback

logger = logging.getLogger(__name__)
SERVICES = {"AVTransport", "RenderingControl", "ConnectionManager"}


class SubscriptionError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


@dataclass
class Subscription:
    sid: str
    receiver: str
    service: str
    callbacks: tuple[str, ...]
    expires: float
    peer: str | None = None
    targets: tuple[tuple[str, str, str], ...] = ()
    sequence: int = 0
    task: asyncio.Task | None = None


class DlnaEvents:
    def __init__(
        self,
        snapshot,
        *,
        clock=time.monotonic,
        send=None,
        interval=0.25,
        max_subscriptions=128,
        max_per_receiver=32,
        max_per_peer=16,
        allow_loopback=False,
    ):
        self.snapshot = snapshot
        self.clock = clock
        self.send = send or self._send
        self.interval = interval
        self.subscriptions: dict[str, Subscription] = {}
        self.max_subscriptions = max_subscriptions
        self.max_per_receiver = max_per_receiver
        self.max_per_peer = max_per_peer
        self.allow_loopback = allow_loopback
        self._sending = asyncio.Semaphore(8)
        self._validating = asyncio.Semaphore(8)
        self._generation = 0
        self._client = None

    async def subscribe_from_peer(self, receiver, service, headers, peer):
        if not peer:
            raise SubscriptionError(412, "Control point address unavailable")
        if headers.get("sid"):
            return self.subscribe(receiver, service, headers, peer=peer)
        if headers.get("nt", "").lower() != "upnp:event":
            raise SubscriptionError(412, "NT must be upnp:event")
        self._admit(receiver, peer)
        callbacks = self._callbacks(headers)
        generation = self._generation
        try:
            await asyncio.wait_for(self._validating.acquire(), 0.2)
        except TimeoutError as exc:
            raise SubscriptionError(503, "Callback validation busy") from exc
        try:
            targets = tuple(
                [
                    await pin_callback(url, peer, allow_loopback=self.allow_loopback)
                    for url in callbacks
                ]
            )
        except (ValueError, OSError, TimeoutError) as exc:
            raise SubscriptionError(412, "Callback must belong to control point") from exc
        finally:
            self._validating.release()
        if generation != self._generation:
            raise SubscriptionError(503, "Subscription service stopped")
        return self.subscribe(receiver, service, headers, peer=peer, targets=targets)

    @staticmethod
    def _callbacks(headers):
        value = headers.get("callback", "")
        if len(value) > 4096:
            raise SubscriptionError(412, "Callback header too large")
        callbacks = tuple(re.findall(r"<([^<>]+)>", value))
        if not callbacks or len(callbacks) > 4 or re.sub(r"<[^<>]+>", "", value).strip():
            raise SubscriptionError(412, "Invalid callback")
        try:
            for url in callbacks:
                parsed = urlsplit(url)
                if (
                    parsed.scheme not in ("http", "https")
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                    or parsed.fragment
                ):
                    raise ValueError()
                _ = parsed.port
        except ValueError as exc:
            raise SubscriptionError(412, "Invalid callback") from exc
        return callbacks

    def _admit(self, receiver, peer):
        for sid, item in list(self.subscriptions.items()):
            if item.expires <= self.clock():
                self.subscriptions.pop(sid)
                if item.task:
                    item.task.cancel()
        entries = list(self.subscriptions.values())
        if (
            len(entries) >= self.max_subscriptions
            or sum(item.receiver == receiver for item in entries) >= self.max_per_receiver
            or sum(item.peer == peer for item in entries) >= self.max_per_peer
        ):
            raise SubscriptionError(503, "Subscription limit reached")

    def subscribe(self, receiver, service, headers, *, peer=None, targets=()):
        if service not in SERVICES:
            raise SubscriptionError(404, "Unknown DLNA service")
        timeout = headers.get("timeout", "Second-1800")
        if len(timeout) > 32:
            raise SubscriptionError(400, "Invalid timeout")
        if timeout.lower() == "second-infinite":
            seconds = 1800
        elif re.fullmatch(r"Second-[1-9][0-9]*", timeout, re.IGNORECASE):
            seconds = min(int(timeout.split("-")[1]), 1800)
        else:
            raise SubscriptionError(400, "Invalid timeout")
        sid = headers.get("sid")
        if sid:
            if headers.get("callback") or headers.get("nt"):
                raise SubscriptionError(400, "Renewal must contain only SID and timeout")
            subscription = self._find(sid, receiver, service)
            if subscription.peer != peer:
                raise SubscriptionError(412, "Subscription belongs to another control point")
        else:
            if headers.get("nt", "").lower() != "upnp:event":
                raise SubscriptionError(412, "NT must be upnp:event")
            callbacks = self._callbacks(headers)
            self._admit(receiver, peer)
            sid = f"uuid:{uuid.uuid4()}"
            subscription = Subscription(sid, receiver, service, callbacks, 0, peer, targets)
            self.subscriptions[sid] = subscription
        subscription.expires = self.clock() + seconds
        return subscription, seconds

    def _find(self, sid, receiver, service):
        subscription = self.subscriptions.get(sid)
        if (
            subscription is None
            or subscription.receiver != receiver
            or subscription.service != service
            or subscription.expires <= self.clock()
        ):
            raise SubscriptionError(412, "Unknown or expired subscription")
        return subscription

    async def start(self, subscription):
        if subscription.task is None and self.subscriptions.get(subscription.sid) is subscription:
            subscription.task = asyncio.create_task(self._run(subscription))

    async def unsubscribe(self, receiver, service, headers, *, peer=None):
        if headers.get("callback") or headers.get("nt"):
            raise SubscriptionError(400, "Unsubscribe must contain only SID")
        subscription = self._find(headers.get("sid"), receiver, service)
        if subscription.peer != peer:
            raise SubscriptionError(412, "Subscription belongs to another control point")
        self.subscriptions.pop(subscription.sid, None)
        if subscription.task:
            subscription.task.cancel()
            await asyncio.gather(subscription.task, return_exceptions=True)

    async def close(self):
        self._generation += 1
        tasks = [item.task for item in self.subscriptions.values() if item.task]
        self.subscriptions.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _run(self, subscription):
        previous = None
        try:
            while (
                self.subscriptions.get(subscription.sid) is subscription
                and subscription.expires > self.clock()
            ):
                values = self.snapshot(subscription.receiver, subscription.service)
                if values is None:
                    return
                body = event_body(subscription.service, values)
                if body != previous:
                    delivered = False
                    targets = subscription.targets or tuple(
                        (url, "", "") for url in subscription.callbacks
                    )
                    for callback, host, tls_host in targets:
                        try:
                            headers = {
                                "NT": "upnp:event",
                                "NTS": "upnp:propchange",
                                "SID": subscription.sid,
                                "SEQ": str(subscription.sequence),
                                "Content-Type": 'text/xml; charset="utf-8"',
                            }
                            if host:
                                headers["Host"] = host
                            async with self._sending:
                                if self.send == self._send:
                                    await self._send(callback, headers, body.encode(), tls_host)
                                else:
                                    await self.send(callback, headers, body.encode())
                            delivered = True
                            break
                        except Exception:
                            logger.debug("DLNA event delivery failed for %s", subscription.sid)
                    if delivered:
                        previous = body
                        subscription.sequence = subscription.sequence % 4294967295 + 1
                    else:
                        await asyncio.sleep(1)
                await asyncio.sleep(self.interval)
        finally:
            if self.subscriptions.get(subscription.sid) is subscription:
                self.subscriptions.pop(subscription.sid, None)

    async def _send(self, callback, headers, body, tls_host=""):
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=3,
                trust_env=False,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
            )
        extensions = {"sni_hostname": tls_host} if tls_host else {}
        # GENA needs only the status, never an unbounded response body.
        async with self._client.stream(
            "NOTIFY",
            callback,
            headers=headers,
            content=body,
            extensions=extensions,
        ) as response:
            response.raise_for_status()


def event_body(service, values):
    if service == "ConnectionManager":
        properties = "".join(
            f"<e:property><{key}>{escape(str(value))}</{key}></e:property>"
            for key, value in values.items()
        )
    else:
        namespace = "AVT" if service == "AVTransport" else "RCS"
        channel = ' channel="Master"' if service == "RenderingControl" else ""
        fields = "".join(
            f'<{key}{channel} val="{escape(str(value), quote=True)}"/>'
            for key, value in values.items()
        )
        change = (
            f'<Event xmlns="urn:schemas-upnp-org:metadata-1-0/{namespace}/">'
            f'<InstanceID val="0">{fields}</InstanceID></Event>'
        )
        properties = f"<e:property><LastChange>{escape(change)}</LastChange></e:property>"
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">' + properties + "</e:propertyset>"
    )
