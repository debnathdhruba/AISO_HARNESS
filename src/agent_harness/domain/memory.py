"""User-scoped durable memory contracts."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from agent_harness.domain.entities import identifier, utc
from agent_harness.domain.errors import require


class MemorySharing(StrEnum):
    PRIVATE = "private"
    SHARED = "shared"


@dataclass(frozen=True)
class MemoryRecord:
    record_id: str
    user_id: str
    owner_agent_definition_id: str
    content: str
    tags: tuple[str, ...]
    sharing: MemorySharing
    created_at: datetime
    updated_at: datetime
    source_run_id: str | None = None
    source_task_id: str | None = None
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        identifier(self.record_id)
        if self.source_run_id is not None:
            identifier(self.source_run_id)
        if self.source_task_id is not None:
            identifier(self.source_task_id)
            require(self.source_run_id is not None, "Memory task provenance needs a run")
        require(
            isinstance(self.user_id, str) and bool(self.user_id.strip()),
            "Memory principal is required",
        )
        require(
            isinstance(self.owner_agent_definition_id, str)
            and bool(self.owner_agent_definition_id.strip()),
            "Memory owner is required",
        )
        require(
            isinstance(self.content, str)
            and bool(self.content.strip())
            and len(self.content.encode("utf-8")) <= 8192,
            "Memory content must be non-empty and at most 8192 bytes",
        )
        require(isinstance(self.sharing, MemorySharing), "Invalid memory sharing policy")
        require(
            isinstance(self.tags, tuple)
            and len(self.tags) <= 20
            and all(
                isinstance(tag, str) and bool(tag.strip()) and len(tag) <= 80 for tag in self.tags
            )
            and len(set(self.tags)) == len(self.tags),
            "Memory tags must be unique non-empty strings of at most 80 characters",
        )
        utc(self.created_at)
        utc(self.updated_at)
        require(self.updated_at >= self.created_at, "Memory update precedes creation")
        if self.deleted_at is not None:
            utc(self.deleted_at)
            require(self.deleted_at >= self.updated_at, "Memory deletion precedes update")
