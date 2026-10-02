"""One generation-aware arbiter for destructive audio recovery actions."""

import asyncio


class RecoveryCoordinator:
    def __init__(self, sessions):
        self.sessions = sessions
        self._pending = {}
        self._running = {}
        self._tokens = {}
        sessions.on_state.append(self._session_changed)

    def _session_changed(self, session):
        owner = session.token.owner
        token = self._tokens.get(owner)
        task = self._pending.get(owner)
        if token is None or task is None or task is asyncio.current_task():
            return
        if session.token != token or not self.sessions.valid(token):
            task.cancel()

    def busy(self, owner):
        return owner in self._running

    async def run(self, owner, action, work):
        if self._session_changed not in self.sessions.on_state:
            self.sessions.on_state.append(self._session_changed)
        session = self.sessions.current(owner)
        token = session.token if session else None
        if token is not None and not self.sessions.valid(token):
            return None
        current = self._running.get(owner)
        if current is asyncio.current_task():
            return await work()
        existing = self._pending.get(owner)
        if existing is not None:
            # Coalesce competing symptoms into the action already being verified.
            return None

        async def execute():
            if token is not None and not self.sessions.valid(token):
                return None
            self._running[owner] = asyncio.current_task()
            try:
                return await work()
            finally:
                self._running.pop(owner, None)

        task = asyncio.create_task(execute(), name=f"recover:{owner}:{action}")
        self._pending[owner] = task
        self._tokens[owner] = token

        def completed(done):
            if self._pending.get(owner) is done:
                self._pending.pop(owner, None)
                self._tokens.pop(owner, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(completed)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._pending.get(owner) is task:
                self._pending.pop(owner, None)

    async def close(self):
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()
        self._tokens.clear()
        if self._session_changed in self.sessions.on_state:
            self.sessions.on_state.remove(self._session_changed)
