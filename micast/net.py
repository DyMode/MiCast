"""One place for outbound HTTP: bounded name resolution and bounded sessions.

Field data (0.3.3, real device): the NAS's DNS went slow, and MiCast turned a
slow resolver into a total loss of its account. aiohttp resolves names in the
*default* thread pool and shields each lookup, so a request that has already
timed out still holds its thread until the system resolver gives up. Every cloud
call that failed started another lookup — per-speaker watchdogs, the device list,
format probes, credential checks — and once that pool was full, every later
lookup, including the one behind the user's QR login, queued behind lookups that
could no longer finish. Nothing recovered on its own, because new work kept
arriving.

Three properties remove the failure mode at its root:

* lookups run in a pool of this module's own, so a resolver in trouble can never
  starve the threads the rest of the process uses for anything else;
* the pool never queues — when every slot is busy, a caller is told "no answer
  right now" immediately instead of adding one more doomed lookup to the pile;
* a short TTL cache means a working answer is not asked for twice, which is what
  keeps the steady state at zero lookups.

Every session MiCast creates should come from `new_session()`, so no call site
has to remember a timeout or a resolver.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor

import aiohttp

logger = logging.getLogger(__name__)

# Resolution slots. Small on purpose: the point is to bound, not to parallelise.
# Two are enough for the cloud (its hosts come from the cache after the first
# success) with room for one unrelated lookup.
RESOLVER_WORKERS = 2
# How long a caller waits before being told to try again later.
RESOLVER_WAIT_SECONDS = 4.0
# A working answer is reused this long; a failing name is remembered for much
# less, so recovery is quick but a storm is still absorbed.
RESOLVER_TTL_SECONDS = 60.0
RESOLVER_FAILURE_TTL_SECONDS = 5.0

# Request bounds, in one place so a call site cannot forget one.
HTTP_CONNECT_TIMEOUT_SECONDS = 8.0
HTTP_TOTAL_TIMEOUT_SECONDS = 20.0
HTTP_POOL_LIMIT = 8


class ResolverBusy(OSError):
    """Every resolution slot is taken — fail now rather than queue.

    An OSError subclass so aiohttp reports it like any other resolution
    failure ("cannot connect to host"), which callers already handle.
    """


class BoundedResolver(aiohttp.abc.AbstractResolver):
    """`getaddrinfo` with a hard bound, a dedicated pool and a TTL cache."""

    def __init__(
        self,
        workers: int = RESOLVER_WORKERS,
        wait_seconds: float = RESOLVER_WAIT_SECONDS,
        ttl_seconds: float = RESOLVER_TTL_SECONDS,
        failure_ttl_seconds: float = RESOLVER_FAILURE_TTL_SECONDS,
    ) -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="micast-dns")
        self._workers = workers
        self._wait_seconds = wait_seconds
        self._ttl = ttl_seconds
        self._failure_ttl = failure_ttl_seconds
        self._cache: dict[tuple[str, int, int], tuple[float, list]] = {}
        self._failures: dict[tuple[str, int, int], float] = {}
        self._in_flight = 0

    # aiohttp calls this; `port`/`family` keep separate answers apart.
    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[aiohttp.abc.ResolveResult]:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return await self._resolve_name(host, port, family)
        # A literal address needs no lookup and must never be blocked by one.
        return [
            aiohttp.abc.ResolveResult(
                hostname=host,
                host=host,
                port=port,
                family=socket.AF_INET if ":" not in host else socket.AF_INET6,
                proto=socket.IPPROTO_TCP,
                flags=0,
            )
        ]

    async def _resolve_name(self, host: str, port: int, family: int) -> list:
        key = (host, port, family)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and cached[0] > now:
            return list(cached[1])
        failed_at = self._failures.get(key)
        if failed_at is not None and now - failed_at < self._failure_ttl:
            raise ResolverBusy(f"DNS 刚刚失败过，稍后再试：{host}")
        if self._in_flight >= self._workers:
            # The whole point: no queueing behind lookups that cannot finish.
            raise ResolverBusy(f"DNS 解析繁忙，稍后再试：{host}")

        loop = asyncio.get_running_loop()
        self._in_flight += 1
        future = loop.run_in_executor(self._pool, self._lookup, host, port, family)
        # Released when the *thread* finishes, not when this caller gives up:
        # a lookup we stopped waiting for still occupies its slot.
        future.add_done_callback(lambda _: self._release())
        try:
            results = await asyncio.wait_for(asyncio.shield(future), self._wait_seconds)
        except (TimeoutError, asyncio.CancelledError):
            self._failures[key] = now
            raise ResolverBusy(f"DNS 查询超时：{host}") from None
        except OSError:
            self._failures[key] = now
            raise
        self._failures.pop(key, None)
        self._cache[key] = (now + self._ttl, list(results))
        return results

    def _release(self) -> None:
        self._in_flight = max(0, self._in_flight - 1)

    @staticmethod
    def _lookup(host: str, port: int, family: int) -> list:
        infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
        return [
            aiohttp.abc.ResolveResult(
                hostname=host,
                host=info[4][0],
                port=port,
                family=info[0],
                proto=info[2],
                flags=0,
            )
            for info in infos
        ]

    def invalidate(self, host: str | None = None) -> None:
        """Forget cached answers (all of them, or one host's)."""
        if host is None:
            self._cache.clear()
            self._failures.clear()
            return
        for key in [key for key in self._cache if key[0] == host]:
            self._cache.pop(key, None)
        for key in [key for key in self._failures if key[0] == host]:
            self._failures.pop(key, None)

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def close(self) -> None:
        """Shut the pool down (the resolver lives as long as the process)."""
        self._pool.shutdown(wait=False)


_resolver: BoundedResolver | None = None


def resolver() -> BoundedResolver:
    """The process-wide resolver (its pool and cache are the point)."""
    global _resolver
    if _resolver is None:
        _resolver = BoundedResolver()
    return _resolver


def new_session(**kwargs) -> aiohttp.ClientSession:
    """An aiohttp session with bounded lookups and bounded requests."""
    kwargs.setdefault(
        "timeout",
        aiohttp.ClientTimeout(
            total=HTTP_TOTAL_TIMEOUT_SECONDS, connect=HTTP_CONNECT_TIMEOUT_SECONDS
        ),
    )
    if "connector" not in kwargs:
        kwargs["connector"] = aiohttp.TCPConnector(
            resolver=resolver(), limit=HTTP_POOL_LIMIT
        )
    return aiohttp.ClientSession(**kwargs)
