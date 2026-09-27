"""Bounded name resolution — the architectural guard for a degraded resolver.

Field data (0.3.3, real device): slow DNS plus unbounded, shielded lookups made
MiCast lose its account completely. Every cloud call that failed left its
`getaddrinfo` holding a default-pool thread, callers kept arriving, and the QR
login the user was waiting for queued behind lookups that could not finish.

These tests pin the three properties that remove that failure mode: lookups are
bounded, a busy resolver refuses *immediately* instead of queueing, and answers
are reused so the steady state asks nothing at all.
"""

import asyncio
import socket
import threading
import time

import pytest

from micast import net


def _result(host: str) -> list:
    return [
        net.aiohttp.abc.ResolveResult(
            hostname=host, host="10.0.0.1", port=80, family=socket.AF_INET, proto=6, flags=0
        )
    ]


@pytest.mark.asyncio
async def test_a_literal_address_never_waits_for_a_lookup():
    """LAN targets are addresses; they must not be held up by DNS at all."""
    resolver = net.BoundedResolver()
    resolver._in_flight = resolver._workers  # every slot taken

    results = await resolver.resolve("192.168.0.12", 8080)

    assert results[0]["host"] == "192.168.0.12" and results[0]["port"] == 8080


@pytest.mark.asyncio
async def test_an_answer_is_reused_instead_of_asked_again(monkeypatch):
    calls = {"n": 0}

    def fake_lookup(host, port, family):
        calls["n"] += 1
        return _result(host)

    monkeypatch.setattr(net.BoundedResolver, "_lookup", staticmethod(fake_lookup))
    resolver = net.BoundedResolver()

    first = await resolver.resolve("api.io.mi.com", 443)
    second = await resolver.resolve("api.io.mi.com", 443)

    assert first == second
    assert calls["n"] == 1  # the cache is what keeps the steady state at zero


@pytest.mark.asyncio
async def test_a_busy_resolver_refuses_instead_of_queueing(monkeypatch):
    """The amplifier: queueing behind lookups that cannot finish."""
    release = threading.Event()

    def hanging_lookup(host, port, family):
        release.wait(2.0)
        return _result(host)

    monkeypatch.setattr(net.BoundedResolver, "_lookup", staticmethod(hanging_lookup))
    resolver = net.BoundedResolver(workers=1, wait_seconds=0.05)

    stuck = asyncio.create_task(resolver.resolve("account.xiaomi.com", 443))
    await asyncio.sleep(0.05)  # let the single slot fill

    started = time.monotonic()
    with pytest.raises(net.ResolverBusy):
        await resolver.resolve("api.io.mi.com", 443)
    # Refused, not queued: it must not sit here for a resolver timeout.
    assert time.monotonic() - started < 0.5

    release.set()
    with pytest.raises(net.ResolverBusy):
        await stuck  # the waiter is told, it does not hang forever


@pytest.mark.asyncio
async def test_a_recent_failure_is_not_retried_on_every_call(monkeypatch):
    calls = {"n": 0}

    def failing_lookup(host, port, family):
        calls["n"] += 1
        raise OSError("no route to host")

    monkeypatch.setattr(net.BoundedResolver, "_lookup", staticmethod(failing_lookup))
    resolver = net.BoundedResolver()

    for _ in range(4):
        with pytest.raises(OSError):
            await resolver.resolve("account.xiaomi.com", 443)

    assert calls["n"] == 1  # negative cache absorbs the storm


@pytest.mark.asyncio
async def test_sessions_are_built_with_bounded_requests_and_that_resolver():
    session = net.new_session()

    try:
        assert session.timeout.total == net.HTTP_TOTAL_TIMEOUT_SECONDS
        assert session.connector._resolver is net.resolver()  # noqa: SLF001
        assert net.resolver() is net.resolver()  # one pool per process
    finally:
        await session.close()
