"""Bounded executor for trusted synchronous tools that cannot be cancelled."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Callable, TypeVar

from agent_harness.domain.errors import ErrorCode, require
from agent_harness.scheduling.resources import ResourceAdmissionError

T = TypeVar("T")
logger = logging.getLogger(__name__)


class BlockingToolAdapter:
    def __init__(self, workers: int = 2, queue_capacity: int = 8) -> None:
        require(workers >= 1 and queue_capacity >= 0, "Invalid blocking tool capacity")
        self._capacity = workers + queue_capacity
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="agent-harness-tool"
        )
        self._lock = asyncio.Lock()
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False

    async def run(self, operation: Callable[..., T], *args: object) -> T:
        async with self._lock:
            if self._closed:
                raise ResourceAdmissionError(
                    ErrorCode.CANCELLATION, "Blocking tool adapter is shut down"
                )
            if self._inflight >= self._capacity:
                raise ResourceAdmissionError(
                    ErrorCode.QUEUE_FULL, "Blocking tool queue is full"
                ) from None
            self._inflight += 1
            self._idle.clear()
        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(self._executor, partial(operation, *args))
        except BaseException:
            self._finished()
            raise

        abandoned = False

        def finished(done: asyncio.Future[T]) -> None:
            self._finished()
            if abandoned and not done.cancelled():
                error = done.exception()
                if error is not None:
                    logger.error(
                        "A blocking tool failed after its caller stopped waiting (%s)",
                        type(error).__name__,
                    )

        future.add_done_callback(finished)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            abandoned = True
            raise

    async def shutdown(self, *, wait: bool = True) -> None:
        async with self._lock:
            self._closed = True
        if wait:
            await self._idle.wait()
            self._executor.shutdown(wait=True, cancel_futures=False)
        else:
            self._executor.shutdown(wait=False, cancel_futures=False)

    def _finished(self) -> None:
        self._inflight -= 1
        if self._inflight == 0:
            self._idle.set()
