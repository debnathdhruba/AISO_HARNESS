"""Explicit memory writes and bounded principal/agent-scoped retrieval."""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta
from typing import TypeVar

from agent_harness.domain.errors import ErrorCode, HarnessError, require
from agent_harness.domain.memory import MemoryRecord, MemorySharing
from agent_harness.memory.long_term.retrieval import (
    LexicalMemoryRetriever,
    MemoryRetrievalProvider,
    render_memory_context,
)
from agent_harness.protocols.memory import MemoryRepository
from agent_harness.protocols.state import Clock, IdGenerator
from agent_harness.scheduling import ResourceKind, ResourceScheduler

T = TypeVar("T")


class MemoryService:
    def __init__(
        self,
        repository: MemoryRepository,
        clock: Clock,
        ids: IdGenerator,
        retriever: MemoryRetrievalProvider | None = None,
        scheduler: ResourceScheduler | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self._ids = ids
        self._retriever = retriever or LexicalMemoryRetriever()
        self._scheduler = scheduler

    async def _run(self, user_id: str, operation: Callable[[], Awaitable[T]]) -> T:
        if self._scheduler is None:
            return await operation()
        return await self._scheduler.run(
            ResourceKind.MEMORY,
            operation,
            key=user_id,
        )

    async def create(
        self,
        user_id: str,
        agent_definition_id: str,
        content: str,
        *,
        tags: tuple[str, ...] = (),
        sharing: MemorySharing = MemorySharing.PRIVATE,
        source_run_id: str | None = None,
        source_task_id: str | None = None,
        record_id: str | None = None,
    ) -> MemoryRecord:
        now = self._clock.utc_now()
        record = MemoryRecord(
            record_id or self._ids.new_id(),
            user_id,
            agent_definition_id,
            content,
            tags,
            sharing,
            now,
            now,
            source_run_id,
            source_task_id,
        )
        await self._run(user_id, lambda: self._repository.save_memory(record))
        return record

    async def get(self, user_id: str, agent_definition_id: str, record_id: str) -> MemoryRecord:
        record = await self._run(
            user_id,
            lambda: self._repository.get_memory(record_id, user_id, agent_definition_id),
        )
        if record is None:
            raise HarnessError(ErrorCode.AUTHORIZATION, "Memory record is unavailable")
        return record

    async def update(
        self,
        user_id: str,
        agent_definition_id: str,
        record_id: str,
        *,
        content: str,
        tags: tuple[str, ...],
        sharing: MemorySharing,
    ) -> MemoryRecord:
        current = await self._get_owner_record(user_id, agent_definition_id, record_id)
        updated = replace(
            current,
            content=content,
            tags=tags,
            sharing=sharing,
            updated_at=max(self._clock.utc_now(), current.updated_at + timedelta(microseconds=1)),
        )
        await self._run(
            user_id,
            lambda: self._repository.update_memory(updated, current.updated_at.isoformat()),
        )
        return updated

    async def delete(self, user_id: str, agent_definition_id: str, record_id: str) -> None:
        record = await self._get_owner_record(user_id, agent_definition_id, record_id)
        deleted_at = max(self._clock.utc_now(), record.updated_at + timedelta(microseconds=1))
        await self._run(
            user_id,
            lambda: self._repository.delete_memory(
                record_id, user_id, agent_definition_id, deleted_at.isoformat()
            ),
        )

    async def retrieve(
        self,
        user_id: str,
        agent_definition_id: str,
        query: str,
        *,
        limit: int = 5,
        max_bytes: int = 2048,
    ) -> tuple[MemoryRecord, ...]:
        require(1 <= limit <= 20, "Memory retrieval limit must be 1..20")
        require(1 <= max_bytes <= 16384, "Memory retrieval budget must be 1..16384 bytes")
        require(bool(query.strip()) and len(query) <= 4096, "Invalid memory query")
        candidates = await self._run(
            user_id,
            lambda: self._repository.memory_candidates(user_id, agent_definition_id, 200),
        )
        ranked = self._retriever.rank(query, candidates)
        selected: list[MemoryRecord] = []
        seen: set[str] = set()
        for match in ranked:
            record = match.record
            if record.record_id in seen:
                continue
            cost = len(render_memory_context((*selected, record)).encode("utf-8"))
            if cost > max_bytes:
                continue
            selected.append(record)
            seen.add(record.record_id)
            if len(selected) == limit:
                break
        return tuple(selected)

    async def _get_owner_record(
        self, user_id: str, agent_definition_id: str, record_id: str
    ) -> MemoryRecord:
        record = await self._run(
            user_id,
            lambda: self._repository.get_memory(record_id, user_id, agent_definition_id),
        )
        if record is None or record.owner_agent_definition_id != agent_definition_id:
            raise HarnessError(ErrorCode.AUTHORIZATION, "Memory record is unavailable")
        if record.deleted_at is not None:
            raise HarnessError(ErrorCode.AUTHORIZATION, "Memory record is unavailable")
        return record
