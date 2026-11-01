"""Bounded resource queues with explicit cancellation and shutdown semantics."""

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeVar

from agent_harness.domain.errors import ErrorCode, HarnessError, require

T = TypeVar("T")
logger = logging.getLogger(__name__)


class ResourceAdmissionError(HarnessError):
    """A request failed before its operation started."""


class ResourceAdmissionCancelled(asyncio.CancelledError):
    """A queued request was cancelled before its operation started."""


class ResourceKind(StrEnum):
    MODEL = "model"
    TOOL = "tool"
    MEMORY = "memory"


@dataclass(frozen=True)
class ResourceLimit:
    capacity: int
    queue_capacity: int
    per_key_capacity: int | None = None

    def __post_init__(self) -> None:
        require(self.capacity >= 1, "Resource capacity must be positive")
        require(self.queue_capacity >= 0, "Resource queue capacity cannot be negative")
        require(
            self.per_key_capacity is None or self.per_key_capacity >= 1,
            "Per-key capacity must be positive",
        )


@dataclass(frozen=True)
class ResourceSnapshot:
    resource: ResourceKind
    active: int
    queued: int
    capacity: int
    queue_capacity: int
    closed: bool


@dataclass
class _Waiter:
    key: str | None
    future: asyncio.Future[None]
    owner: asyncio.Task[object] | None
    waiting: bool = True
    admitted: bool = False


@dataclass
class _Lane:
    active: int = 0
    waiting: int = 0
    active_by_key: dict[str, int] = field(default_factory=dict)
    queued: deque[_Waiter] = field(default_factory=deque)


class ResourceScheduler:
    def __init__(
        self,
        limits: Mapping[ResourceKind, ResourceLimit] | None = None,
    ) -> None:
        self._limits = dict(
            limits
            or {
                ResourceKind.MODEL: ResourceLimit(2, 8, 1),
                ResourceKind.TOOL: ResourceLimit(4, 16, 2),
                ResourceKind.MEMORY: ResourceLimit(2, 8, 1),
            }
        )
        require(set(self._limits) == set(ResourceKind), "Every resource needs a queue policy")
        self._lanes = {kind: _Lane() for kind in ResourceKind}
        self._lock = asyncio.Lock()
        self._closed = False
        self._idle = asyncio.Event()
        self._idle.set()
        self._active_owners: dict[asyncio.Task[object], int] = {}
        self._shutdown_owners: set[asyncio.Task[object]] = set()

    async def __aenter__(self) -> "ResourceScheduler":
        require(not self._closed, "Resource scheduler is closed")
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.shutdown(cancel_running=True)

    async def run(
        self,
        resource: ResourceKind,
        operation: Callable[[], Awaitable[T]],
        *,
        key: str | None = None,
        timeout: float | None = None,
    ) -> T:
        admitted = False
        try:
            if timeout is None:
                await self._acquire(resource, key)
                admitted = True
                return await operation()
            async with asyncio.timeout(timeout):
                await self._acquire(resource, key)
                admitted = True
                return await operation()
        except TimeoutError:
            if not admitted:
                raise ResourceAdmissionError(
                    ErrorCode.DEADLINE_EXCEEDED, f"{resource.value} queue deadline exceeded"
                ) from None
            raise
        except asyncio.CancelledError:
            if not admitted:
                raise ResourceAdmissionCancelled() from None
            raise
        finally:
            if admitted:
                await self._release(resource, key)

    def snapshot(self) -> tuple[ResourceSnapshot, ...]:
        return tuple(
            ResourceSnapshot(
                kind,
                lane.active,
                lane.waiting,
                self._limits[kind].capacity,
                self._limits[kind].queue_capacity,
                self._closed,
            )
            for kind, lane in self._lanes.items()
        )

    async def shutdown(self, *, cancel_running: bool = False, wait: bool = True) -> None:
        current = asyncio.current_task()
        async with self._lock:
            draining = self._shutdown_owners | set(self._active_owners)
            if not self._closed:
                self._closed = True
                for lane in self._lanes.values():
                    queued, lane.queued = lane.queued, deque()
                    for waiter in queued:
                        if waiter.owner is not None:
                            draining.add(waiter.owner)
                        if not waiter.future.done():
                            waiter.future.set_exception(
                                ResourceAdmissionError(
                                    ErrorCode.CANCELLATION,
                                    "Resource scheduler shut down before admission",
                                )
                            )
                self._update_idle_locked()
            owners = tuple(owner for owner in self._active_owners if owner is not current)
            draining.discard(current)
            self._shutdown_owners.update(draining)
        if cancel_running:
            for owner in owners:
                owner.cancel()
        if wait:
            await self._idle.wait()
            if draining:
                results = await asyncio.gather(*draining, return_exceptions=True)
                for result in results:
                    if (
                        isinstance(result, ResourceAdmissionError)
                        and result.code == ErrorCode.CANCELLATION
                    ) or isinstance(result, asyncio.CancelledError):
                        continue
                    if isinstance(result, Exception):
                        logger.error(
                            "A resource owner failed during shutdown (%s)",
                            type(result).__name__,
                        )
                async with self._lock:
                    self._shutdown_owners.difference_update(draining)

    async def _acquire(self, resource: ResourceKind, key: str | None) -> None:
        owner = asyncio.current_task()
        require(owner is not None, "Resource requests require an asyncio task")
        assert owner is not None
        loop = asyncio.get_running_loop()
        waiter = _Waiter(key, loop.create_future(), owner)
        async with self._lock:
            if self._closed:
                raise ResourceAdmissionError(
                    ErrorCode.CANCELLATION, "Resource scheduler is shut down"
                )
            lane = self._lanes[resource]
            self._dispatch_locked(resource, lane)
            if not self._can_start(resource, lane, key) and (
                len(lane.queued) >= self._limits[resource].queue_capacity
            ):
                raise ResourceAdmissionError(
                    ErrorCode.QUEUE_FULL, f"{resource.value} resource queue is full"
                )
            lane.queued.append(waiter)
            lane.waiting += 1
            self._idle.clear()
            self._dispatch_locked(resource, lane)
        try:
            await waiter.future
        except BaseException:
            async with self._lock:
                if waiter in lane.queued:
                    lane.queued.remove(waiter)
                    self._dispatch_locked(resource, lane)
                elif waiter.admitted:
                    self._release_locked(resource, lane, key, owner)
                if waiter.waiting:
                    waiter.waiting = False
                    lane.waiting -= 1
                self._update_idle_locked()
            raise

    async def _release(self, resource: ResourceKind, key: str | None) -> None:
        owner = asyncio.current_task()
        assert owner is not None
        async with self._lock:
            lane = self._lanes[resource]
            self._release_locked(resource, lane, key, owner)

    def _release_locked(
        self,
        resource: ResourceKind,
        lane: _Lane,
        key: str | None,
        owner: asyncio.Task[object],
    ) -> None:
        require(lane.active > 0, "Resource permit released more than once")
        lane.active -= 1
        if key is not None:
            active = lane.active_by_key[key] - 1
            if active:
                lane.active_by_key[key] = active
            else:
                del lane.active_by_key[key]
        count = self._active_owners[owner] - 1
        if count:
            self._active_owners[owner] = count
        else:
            del self._active_owners[owner]
        self._dispatch_locked(resource, lane)
        self._update_idle_locked()

    def _can_start(self, resource: ResourceKind, lane: _Lane, key: str | None) -> bool:
        limit = self._limits[resource]
        if lane.active >= limit.capacity:
            return False
        return (
            key is None
            or limit.per_key_capacity is None
            or lane.active_by_key.get(key, 0) < limit.per_key_capacity
        )

    def _dispatch_locked(self, resource: ResourceKind, lane: _Lane) -> None:
        limit = self._limits[resource]
        while lane.active < limit.capacity and lane.queued:
            selected = next(
                (
                    index
                    for index, waiter in enumerate(lane.queued)
                    if waiter.future.cancelled()
                    or waiter.key is None
                    or limit.per_key_capacity is None
                    or lane.active_by_key.get(waiter.key, 0) < limit.per_key_capacity
                ),
                None,
            )
            if selected is None:
                break
            waiter = lane.queued[selected]
            del lane.queued[selected]
            if waiter.future.cancelled():
                continue
            assert waiter.owner is not None
            waiter.waiting = False
            lane.waiting -= 1
            lane.active += 1
            if waiter.key is not None:
                lane.active_by_key[waiter.key] = lane.active_by_key.get(waiter.key, 0) + 1
            self._active_owners[waiter.owner] = self._active_owners.get(waiter.owner, 0) + 1
            waiter.admitted = True
            waiter.future.set_result(None)

    def _update_idle_locked(self) -> None:
        if not any(lane.active or lane.waiting for lane in self._lanes.values()):
            self._idle.set()
        else:
            self._idle.clear()
