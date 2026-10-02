"""Application-owned background work with one shutdown and exception boundary."""

import asyncio
import logging

logger = logging.getLogger(__name__)


class RuntimeTasks:
    def __init__(self):
        self.tasks = set()
        self.closed = False

    def start(self, coro, *, name):
        if self.closed:
            coro.close()
            raise RuntimeError("Runtime tasks already stopped")
        task = asyncio.create_task(coro, name=name)
        self.tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error(
                "Background task %s failed",
                task.get_name(),
                exc_info=(type(error), error, error.__traceback__),
            )

    async def close(self):
        self.closed = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
