"""Durable tool identities, exact schemas, approvals and artifact manifests."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256

from agent_harness.domain.entities import JsonObject, identifier, utc
from agent_harness.domain.errors import require
from agent_harness.domain.states import InvocationStatus


class Effect(StrEnum):
    READ_ONLY = "read_only"
    IDEMPOTENT_WRITE = "idempotent_write"
    NON_IDEMPOTENT_WRITE = "non_idempotent_write"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str
    description: str
    input_schema: JsonObject
    output_schema: JsonObject
    effect: Effect = Effect.READ_ONLY
    timeout_seconds: float = 5
    max_output_bytes: int = 8192
    idempotency: str = "none"
    supports_cancellation: bool = True
    supports_reconciliation: bool = False

    def __post_init__(self) -> None:
        require(bool(self.name.strip() and self.version.strip()), "Tool name/version required")
        require(
            0 < self.timeout_seconds <= 60 and 128 <= self.max_output_bytes <= 65536,
            "Invalid tool execution limits",
        )


def argument_hash(arguments: JsonObject) -> str:
    return sha256(arguments.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Invocation:
    invocation_id: str
    run_id: str
    task_id: str
    user_id: str
    step_id: str
    call_id: str
    tool_name: str
    tool_version: str
    arguments: JsonObject
    argument_hash: str
    effect: Effect
    status: InvocationStatus = InvocationStatus.PLANNED
    result: JsonObject | None = None
    error: JsonObject | None = None
    approval_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        for value in (self.invocation_id, self.run_id, self.task_id, self.step_id):
            identifier(value)
        require(
            self.argument_hash == argument_hash(self.arguments), "Invocation argument hash mismatch"
        )
        if self.approval_expires_at is not None:
            utc(self.approval_expires_at)


@dataclass(frozen=True)
class Approval:
    invocation_id: str
    user_id: str
    tool_version: str
    argument_hash: str
    approved: bool
    expires_at: datetime


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    run_id: str
    task_id: str
    user_id: str
    invocation_id: str
    name: str
    relative_path: str
    version: int
    checksum: str
    size: int
    status: str = "staged"
    media_type: str = "text/plain; charset=utf-8"

    def __post_init__(self) -> None:
        for value in (self.artifact_id, self.run_id, self.task_id, self.invocation_id):
            identifier(value)
        require(
            self.status in {"staged", "committed"} and self.version > 0 and self.size >= 0,
            "Invalid artifact manifest",
        )
