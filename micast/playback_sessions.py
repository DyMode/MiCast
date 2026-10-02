"""Protocol-independent playback leases and their owned resources.

Receivers survive sessions. Sessions own transports, output connections and
media workers. Only this registry decides when those resources expire; adapters
report real activity and register idempotent, generation-scoped release actions.
"""

import asyncio
import inspect
import logging
import time
from collections.abc import Callable, Iterator, MutableSet
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger(__name__)
SOURCE_IDLE_SECONDS = 15.0
OUTPUT_GRACE_SECONDS = 3.0
LIFECYCLE_TICK_SECONDS = 2.0
CLEANUP_TIMEOUT_SECONDS = 15.0


class SessionState(StrEnum):
    ACTIVE = "active"
    QUIET = "quiet"
    PAUSED = "paused"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(frozen=True)
class SessionToken:
    owner: str
    generation: int


@dataclass
class Resource:
    release: Callable
    kind: str
    retry_at: float = 0
    failures: int = 0
    requested_at: float | None = None


@dataclass
class PlaybackSession:
    token: SessionToken
    protocol: str
    identity: object
    last_activity: float
    state: SessionState = SessionState.ACTIVE
    reason: str = ""
    quiet_at: float | None = None
    output_due: float | None = None
    transport_due: float | None = None
    activity: Callable[[], float | None] | None = None
    on_quiet: Callable[[], None] | None = None
    resources: dict[str, Resource] = field(default_factory=dict)
    releasing: bool = False


class PlaybackSessions:
    def __init__(self, timeout: Callable[[], float], clock=time.monotonic):
        self._timeout = timeout
        self._clock = clock
        self._generation = 0
        self._current: dict[str, PlaybackSession] = {}
        self._retiring: dict[SessionToken, PlaybackSession] = {}
        self._tick_lock = asyncio.Lock()
        self.on_state: list[Callable[[PlaybackSession], None]] = []
        from micast.target_leases import TargetLeases

        self.targets = TargetLeases(self)

    def current(self, owner: str) -> PlaybackSession | None:
        return self._current.get(owner)

    def valid(self, token: SessionToken) -> bool:
        session = self.current(token.owner)
        return bool(session and session.token == token and session.state == SessionState.ACTIVE)

    def _notify(self, session: PlaybackSession) -> None:
        for callback in self.on_state:
            try:
                callback(session)
            except Exception:
                logger.exception("Playback state observer failed for %s", session.token.owner)

    def begin(self, owner: str, protocol: str, identity=None) -> PlaybackSession:
        previous = self.current(owner)
        if (
            previous
            and not previous.releasing
            and previous.state != SessionState.CLOSING
            and (identity is None or identity == previous.identity)
        ):
            was_active = previous.state == SessionState.ACTIVE
            previous.state = SessionState.ACTIVE
            previous.quiet_at = previous.output_due = previous.transport_due = None
            if not was_active:
                previous.last_activity = self._clock()
            previous.reason = ""
            self._notify(previous)
            return previous
        if previous:
            self.end(previous.token, "replaced", immediate=True)
            self._retiring[previous.token] = previous
        self._generation += 1
        session = PlaybackSession(
            SessionToken(owner, self._generation), protocol, identity, self._clock()
        )
        self._current[owner] = session
        self._notify(session)
        return session

    def register(self, token: SessionToken, key: str, release: Callable, kind="output") -> bool:
        session = self.current(token.owner)
        if session is None or session.token != token or session.state == SessionState.CLOSING:
            return False
        session.resources[key] = Resource(release, kind)
        return True

    def forget(self, token: SessionToken, key: str) -> None:
        session = self.current(token.owner)
        if session is None or session.token != token:
            session = self._retiring.get(token)
        if session and session.token == token:
            session.resources.pop(key, None)

    def release(self, token: SessionToken, key: str) -> None:
        session = self.current(token.owner)
        if session is None or session.token != token:
            session = self._retiring.get(token)
        if session and key in session.resources:
            session.resources[key].requested_at = self._clock()

    def target_taken_over(self, token: SessionToken, key: str) -> None:
        """The last audible target leaving ends its former owner's ingress too."""
        session = self.current(token.owner)
        if session is None or session.token != token:
            return
        self.forget(token, key)
        if not any(
            name.startswith(("speaker:", "airplay:", "dlna-target:"))
            for name in session.resources
        ):
            self.end(token, "targets_taken_over", immediate=True)

    def quiet(self, token: SessionToken, reason="source_idle", grace=OUTPUT_GRACE_SECONDS) -> None:
        session = self.current(token.owner)
        if not session or session.token != token or session.state != SessionState.ACTIVE:
            return
        session.state = SessionState.QUIET
        session.quiet_at = self._clock()
        session.output_due = session.quiet_at + grace
        session.reason = reason
        self._notify(session)
        if session.on_quiet is not None:
            try:
                session.on_quiet()
            except Exception:
                logger.exception("Protocol idle notification failed for %s", token.owner)

    def pause(self, token: SessionToken) -> None:
        session = self.current(token.owner)
        if session and session.token == token:
            session.state = SessionState.PAUSED
            session.quiet_at = self._clock()
            session.output_due = session.quiet_at  # media workers/HTTP stop immediately
            session.reason = "paused"
            self._notify(session)

    def end(self, token: SessionToken, reason="stopped", immediate=False) -> None:
        session = self.current(token.owner)
        if not session or session.token != token:
            return
        if session.state == SessionState.CLOSING:
            if immediate:
                session.output_due = session.transport_due = self._clock()
            return
        now = self._clock()
        session.state = SessionState.CLOSING
        session.quiet_at = now
        session.output_due = now if immediate else now + OUTPUT_GRACE_SECONDS
        session.transport_due = now
        session.reason = reason
        self._notify(session)

    async def close(self, token: SessionToken, reason="stopped") -> None:
        self.end(token, reason, immediate=True)
        await self.tick()

    async def close_all(self, receiver_id: str | None = None, reason="shutdown") -> None:
        for session in list(self._current.values()):
            owner = session.token.owner
            if receiver_id is None or owner in (receiver_id, f"dlna:{receiver_id}"):
                self.end(session.token, reason, immediate=True)
        await self.tick()

    async def tick(self) -> None:
        async with self._tick_lock:
            now = self._clock()
            for session in list(self._current.values()) + list(self._retiring.values()):
                if session.state == SessionState.ACTIVE and session.activity is not None:
                    try:
                        observed = session.activity()
                        if isinstance(observed, (int, float)):
                            session.last_activity = max(session.last_activity, observed)
                    except Exception:
                        logger.exception(
                            "Ingress activity observation failed for %s", session.token
                        )
                    if now - session.last_activity >= SOURCE_IDLE_SECONDS:
                        self.quiet(session.token)
                timeout = float(self._timeout())
                if session.state in (SessionState.QUIET, SessionState.PAUSED) and timeout > 0:
                    baseline = (
                        session.quiet_at
                        if (
                            session.state == SessionState.PAUSED
                            or session.reason.startswith("media_")
                        )
                        else session.last_activity
                    )
                    if baseline is not None and now - baseline >= max(timeout, SOURCE_IDLE_SECONDS):
                        self.end(session.token, "idle_timeout", immediate=True)
            await asyncio.gather(
                *(
                    self._release_resources(session)
                    for session in list(self._current.values()) + list(self._retiring.values())
                )
            )

    async def _release_resources(self, session: PlaybackSession) -> None:
        priority = {"connection": 0, "output": 1, "media": 2, "speaker": 3, "transport": 4}
        resources = sorted(
            session.resources.items(),
            key=lambda item: priority.get(item[1].kind, 1),
        )
        for key, resource in resources:
            due = session.transport_due if resource.kind == "transport" else session.output_due
            if resource.requested_at is not None:
                due = resource.requested_at
            if (
                session.state == SessionState.PAUSED
                and resource.kind == "speaker"
                and resource.requested_at is None
            ):
                continue
            if due is None or self._clock() < due or self._clock() < resource.retry_at:
                continue
            try:
                session.releasing = True
                result = resource.release()
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, CLEANUP_TIMEOUT_SECONDS)
                # A callback can register a replacement under the same key.
                if session.resources.get(key) is resource:
                    session.resources.pop(key)
            except Exception:
                resource.failures += 1
                resource.retry_at = self._clock() + min(60, 2 ** min(resource.failures, 6))
                logger.exception("Session resource release failed: %s/%s", session.token, key)
            finally:
                session.releasing = False
        if (
            session.state == SessionState.QUIET
            and session.reason.startswith("media_")
            and not session.resources
        ):
            session.state = SessionState.CLOSING
        if session.state == SessionState.CLOSING and not session.resources:
            session.state = SessionState.CLOSED
            if self.current(session.token.owner) is session:
                self._current.pop(session.token.owner, None)
            self._retiring.pop(session.token, None)
            self._notify(session)

    def snapshot(self) -> list[dict]:
        return [
            {
                "owner": s.token.owner,
                "generation": s.token.generation,
                "protocol": s.protocol,
                "state": s.state.value,
                "reason": s.reason,
                "resources": list(s.resources),
                "cleanup_failures": sum(r.failures for r in s.resources.values()),
            }
            for s in list(self._current.values()) + list(self._retiring.values())
        ]


class ActiveSessions(MutableSet[str]):
    """Compatibility view for pipeline gates; the registry owns the state."""

    def __init__(self, registry: PlaybackSessions, protocol: Callable[[str], str]):
        self.registry, self.protocol = registry, protocol

    @classmethod
    def _from_iterable(cls, iterable):
        return set(iterable)

    def __contains__(self, owner):
        session = self.registry.current(owner)
        return bool(
            session
            and session.protocol in ("airplay", "airplay2")
            and session.state == SessionState.ACTIVE
        )

    def __iter__(self) -> Iterator[str]:
        return iter([owner for owner in self.registry._current if owner in self])

    def __len__(self):
        return sum(1 for _ in self)

    def add(self, owner):
        self.registry.begin(owner, self.protocol(owner))

    def discard(self, owner):
        session = self.registry.current(owner)
        if session:
            self.registry.quiet(session.token)

    def update(self, owners):
        for owner in owners:
            self.add(owner)
