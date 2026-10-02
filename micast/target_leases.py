"""Generation-scoped target ownership shared by all output protocols."""

import asyncio
from dataclasses import dataclass


@dataclass
class TargetLease:
    target: str
    token: object
    release: object


class TargetLeases:
    def __init__(self, sessions):
        self.sessions = sessions
        self._current = {}
        self._locks = {}

    def lock(self, target):
        return self._locks.setdefault(target, asyncio.Lock())

    def current(self, target):
        return self._current.get(target)

    def owns(self, target, token):
        lease = self.current(target)
        return lease is not None and lease.token == token

    async def acquire(self, target, token, release, *, steal=True, start=None):
        async with self.lock(target):
            if not self.sessions.valid(token):
                return False
            previous = self.current(target)
            if previous and previous.token != token:
                if not steal:
                    return False
                await previous.release()
                self.sessions.target_taken_over(previous.token, target)
            if not self.sessions.valid(token):
                return False
            self._current[target] = TargetLease(target, token, release)
            self.sessions.register(
                token, target, lambda: self.release(target, token), kind="speaker"
            )
            if start is not None:
                try:
                    await start()
                except BaseException:
                    await release()
                    self.forget(target, token)
                    self.sessions.forget(token, target)
                    raise
                if not self.sessions.valid(token):
                    await release()
                    self.forget(target, token)
                    return False
            return True

    async def execute(self, target, token, action):
        """Serialize commands with takeover; cleanup may act on quiet sessions."""
        async with self.lock(target):
            if not self.owns(target, token):
                return False
            await action()
            return True

    def record(self, target, token, release):
        """Record an already serialized hardware handover (Xiaomi adapter)."""
        previous = self.current(target)
        if previous and previous.token != token:
            self.sessions.target_taken_over(previous.token, target)
        self._current[target] = TargetLease(target, token, release)

    async def release(self, target, token):
        async with self.lock(target):
            lease = self.current(target)
            if lease is None or lease.token != token:
                return
            await lease.release()
            if self.current(target) is lease:
                self._current.pop(target, None)

    def forget(self, target, token):
        if self.owns(target, token):
            self._current.pop(target, None)

    def snapshot(self):
        return [
            {"target": key, "owner": item.token.owner, "generation": item.token.generation}
            for key, item in sorted(self._current.items())
        ]
