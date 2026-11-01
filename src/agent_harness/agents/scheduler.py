"""Fair in-process admission for ready child tasks."""

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from agent_harness.domain.entities import Task
from agent_harness.domain.errors import ErrorCode, HarnessError, require
from agent_harness.logger import audit


@dataclass
class _QueuedTask:
    task: Task
    execute: Callable[[Task], Awaitable[Task]]
    result: asyncio.Future[Task]
    active_limit: int


class TaskScheduler:
    """Round-robin across runs and FIFO within each run."""

    def __init__(self, concurrency: int = 4, queue_capacity: int = 16) -> None:
        require(concurrency >= 1 and queue_capacity >= 0, "Invalid task scheduler limits")
        self._concurrency = concurrency
        self._queue_capacity = queue_capacity
        self._lock = asyncio.Lock()
        self._queues: dict[str, deque[_QueuedTask]] = {}
        self._rotation: deque[str] = deque()
        self._active = 0
        self._active_by_run: dict[str, int] = {}
        self._workers: dict[asyncio.Future[Task], asyncio.Task[None]] = {}
        self._closed = False

    @property
    def concurrency(self) -> int:
        return self._concurrency

    async def run(
        self,
        tasks: tuple[Task, ...],
        execute: Callable[[Task], Awaitable[Task]],
        *,
        active_limit: int,
    ) -> tuple[Task, ...]:
        require(1 <= active_limit <= self._concurrency, "Invalid active child limit")
        if not tasks:
            return ()
        loop = asyncio.get_running_loop()
        futures = tuple(loop.create_future() for _ in tasks)
        async with self._lock:
            require(not self._closed, "Task scheduler is shut down")
            queued = sum(len(queue) for queue in self._queues.values())
            if queued + len(tasks) > self._queue_capacity:
                raise HarnessError(
                    ErrorCode.QUEUE_FULL, "Task queue capacity is full", retryable=True
                )
            for task, future in zip(tasks, futures, strict=True):
                queue = self._queues.setdefault(task.run_id, deque())
                queue.append(_QueuedTask(task, execute, future, active_limit))
                if task.run_id not in self._rotation:
                    self._rotation.append(task.run_id)
        try:
            await self._admit()
            return tuple(await asyncio.gather(*futures))
        except BaseException:
            await self._cancel(futures)
            raise

    async def _admit(self) -> None:
        async with self._lock:
            while self._active < self._concurrency and self._rotation:
                run_id = self._rotation.popleft()
                queue = self._queues.get(run_id)
                if not queue:
                    self._queues.pop(run_id, None)
                    continue
                item_index = next(
                    (
                        index
                        for index, queued in enumerate(queue)
                        if self._active_by_run.get(run_id, 0) < queued.active_limit
                    ),
                    None,
                )
                if item_index is None:
                    self._rotation.append(run_id)
                    if all(
                        self._active_by_run.get(candidate, 0)
                        >= self._queues[candidate][0].active_limit
                        for candidate in self._rotation
                        if self._queues.get(candidate)
                    ):
                        break
                    continue
                item = queue[item_index]
                del queue[item_index]
                self._active += 1
                self._active_by_run[run_id] = self._active_by_run.get(run_id, 0) + 1
                if queue:
                    self._rotation.append(run_id)
                else:
                    self._queues.pop(run_id, None)
                worker = asyncio.create_task(self._execute(item))
                self._workers[item.result] = worker
                audit(
                    "task.scheduler.dispatched",
                    run_id=item.task.run_id,
                    task_id=item.task.task_id,
                    active=self._active,
                )

    async def _execute(self, item: _QueuedTask) -> None:
        try:
            result = await item.execute(item.task)
        except asyncio.CancelledError:
            if not item.result.done():
                item.result.cancel()
            raise
        except Exception as error:
            if not item.result.done():
                item.result.set_exception(error)
        else:
            if not item.result.done():
                item.result.set_result(result)
        finally:
            audit(
                "task.scheduler.released",
                run_id=item.task.run_id,
                task_id=item.task.task_id,
            )
            async with self._lock:
                self._active -= 1
                active = self._active_by_run[item.task.run_id] - 1
                if active:
                    self._active_by_run[item.task.run_id] = active
                else:
                    del self._active_by_run[item.task.run_id]
                self._workers.pop(item.result, None)
            await self._admit()

    async def _cancel(self, futures: tuple[asyncio.Future[Task], ...]) -> None:
        selected = set(futures)
        async with self._lock:
            for run_id, queue in tuple(self._queues.items()):
                retained = deque(item for item in queue if item.result not in selected)
                for item in queue:
                    if item.result in selected and not item.result.done():
                        item.result.cancel()
                if retained:
                    self._queues[run_id] = retained
                else:
                    self._queues.pop(run_id, None)
                    self._rotation = deque(item for item in self._rotation if item != run_id)
            workers = [
                self._workers[future]
                for future in selected
                if future in self._workers and not self._workers[future].done()
            ]
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    async def shutdown(self) -> None:
        async with self._lock:
            self._closed = True
            queued = [item for queue in self._queues.values() for item in queue]
            self._queues.clear()
            self._rotation.clear()
        for item in queued:
            if not item.result.done():
                item.result.set_exception(
                    HarnessError(ErrorCode.CANCELLATION, "Task scheduler shut down")
                )
