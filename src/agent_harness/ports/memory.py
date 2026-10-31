"""Persistence boundary for scoped memory records."""

from typing import Protocol

from agent_harness.domain.memory import MemoryRecord


class MemoryRepository(Protocol):
    async def save_memory(self, record: MemoryRecord) -> None: ...

    async def get_memory(
        self, record_id: str, user_id: str, agent_definition_id: str
    ) -> MemoryRecord | None: ...

    async def update_memory(self, record: MemoryRecord, expected_updated_at: str) -> None: ...

    async def delete_memory(
        self, record_id: str, user_id: str, agent_definition_id: str, deleted_at: str
    ) -> None: ...

    async def memory_candidates(
        self, user_id: str, agent_definition_id: str, limit: int
    ) -> tuple[MemoryRecord, ...]: ...
