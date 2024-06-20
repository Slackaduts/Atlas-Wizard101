import asyncio

from asyncio import Task
from typing import Coroutine, Optional


class TaskWatcher:
    def __init__(self):
        self._tasks: list[Task] = []
        self._running = False
        self._selftask: Optional[Task] = None

    def register(self, task: Task):
        self._tasks.append(task)

    def unregister(self, task: Task):
        if not task.done():
            task.cancel()
        self._tasks.remove(task)

    def new_task(self, coro: Coroutine) -> Task:
        task = asyncio.create_task(coro)
        self.register(task)
        return task

    def _tick(self):
        for task in self._tasks:
            if not task.done():
                continue
            exc = task.exception()
            if isinstance(exc, Exception):
                raise exc

    def start_loop(self) -> Task:
        async def __loop(self):
            while True:
                self._tick()
                await asyncio.sleep(0.01)
        if self._running:
            assert self._selftask is not None
            return self._selftask
        self._selftask = asyncio.create_task(__loop(self))
        return self._selftask
