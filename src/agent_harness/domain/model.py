"""Provider-independent messages, model decisions and persisted steps."""

import json
from dataclasses import asdict, dataclass
from enum import StrEnum

from agent_harness.domain.entities import JsonObject, identifier
from agent_harness.domain.errors import require
from agent_harness.domain.tooling import ToolSpec


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class FinishReason(StrEnum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    UNSUPPORTED = "unsupported"


class Stage(StrEnum):
    DECISION = "decision"
    RESPONSE = "response"


class StepStatus(StrEnum):
    REQUESTED = "requested"
    ACCEPTED = "accepted"
    APPLIED = "applied"
    FAILED = "failed"


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: JsonObject

    def __post_init__(self) -> None:
        require(bool(self.call_id.strip() and self.name.strip()), "Tool call ID/name required")


@dataclass(frozen=True)
class Message:
    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        require(isinstance(self.role, Role), "Invalid message role")
        require(not self.tool_calls or self.role == Role.ASSISTANT, "Only assistants request tools")
        require((self.role == Role.TOOL) == bool(self.tool_call_id), "Tool result requires call ID")
        require(
            len({call.call_id for call in self.tool_calls}) == len(self.tool_calls),
            "Duplicate tool call IDs",
        )


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        for count in (self.input_tokens, self.output_tokens):
            require(count is None or count >= 0, "Token usage cannot be negative")


@dataclass(frozen=True)
class Capabilities:
    tool_calls: bool = False
    structured_output: bool = False
    streaming: bool = False
    usage: bool = True
    cancellation: bool = False


@dataclass(frozen=True)
class ModelRequest:
    request_id: str
    model: str
    messages: tuple[Message, ...]
    max_output_tokens: int
    tools: tuple[ToolSpec, ...] = ()

    def __post_init__(self) -> None:
        identifier(self.request_id)
        require(bool(self.model.strip()) and bool(self.messages), "Model and messages required")
        require(1 <= self.max_output_tokens <= 8192, "Invalid output token limit")


@dataclass(frozen=True)
class ModelResponse:
    message: Message
    finish_reason: FinishReason
    usage: Usage = Usage()

    def __post_init__(self) -> None:
        require(self.message.role == Role.ASSISTANT, "Response must be an assistant message")


@dataclass(frozen=True)
class Step:
    step_id: str
    task_id: str
    run_id: str
    user_id: str
    ordinal: int
    stage: Stage
    request: ModelRequest
    status: StepStatus = StepStatus.REQUESTED
    response: ModelResponse | None = None
    error: JsonObject | None = None

    def __post_init__(self) -> None:
        for value in (self.step_id, self.task_id, self.run_id):
            identifier(value)
        require(bool(self.user_id.strip()) and self.ordinal > 0, "Invalid step scope/ordinal")
        require(
            (self.status in {StepStatus.ACCEPTED, StepStatus.APPLIED})
            == (self.response is not None),
            "Step response/status mismatch",
        )


@dataclass(frozen=True)
class RunOptions:
    provider: str = "fake"
    decision_model: str = "lfm2.5-thinking:1.2b"
    response_model: str = "gemma3:1b"
    max_steps: int = 8
    timeout_seconds: int = 180
    max_output_tokens: int = 1024
    max_input_bytes: int = 16384
    tool_allowlist: tuple[str, ...] = (
        "calculator",
        "artifact_write",
        "artifact_read",
        "artifact_list",
        "read_file",
        "write_file",
        "search_files",
        "edit_file",
        "delegate_tasks",
    )
    max_tool_calls: int = 8
    repeated_action_limit: int = 2
    require_write_approval: bool = False
    workspace: str = "."
    artifact_root: str = ".local/artifacts"
    required_tools: tuple[str, ...] = ()
    routing_mode: str = "auto"
    response_max_tokens: int = 512

    def __post_init__(self) -> None:
        require(self.provider in {"fake", "ollama"}, "Unsupported provider")
        require(self.routing_mode in {"auto", "chat", "tools", "two_stage"}, "Invalid routing mode")
        require(1 <= self.response_max_tokens <= 8192, "Invalid response token limit")
        require(
            set(self.required_tools) <= set(self.tool_allowlist), "Required tools must be permitted"
        )
        require(
            bool(self.decision_model.strip() and self.response_model.strip()), "Models required"
        )
        require(1 <= self.max_steps <= 100, "Step limit must be 1..100")
        require(1 <= self.timeout_seconds <= 3600, "Timeout must be 1..3600 seconds")
        require(1 <= self.max_output_tokens <= 8192, "Output limit must be 1..8192 tokens")
        require(256 <= self.max_input_bytes <= 131072, "Input limit must be 256..131072 bytes")
        require(
            1 <= self.max_tool_calls <= 100 and 1 <= self.repeated_action_limit <= 10,
            "Invalid tool call limits",
        )

    def snapshot(self) -> JsonObject:
        return JsonObject(json.dumps(asdict(self)))

    @classmethod
    def restore(cls, snapshot: JsonObject) -> "RunOptions":
        data = json.loads(snapshot.text)
        # Legacy Phase 2 runs had no tool authorization. Do not grant capabilities on resume.
        data["tool_allowlist"] = tuple(data.get("tool_allowlist", ()))
        data["required_tools"] = tuple(data.get("required_tools", ()))
        data.setdefault("artifact_root", data.get("workspace", ".local/artifacts"))
        data.setdefault("routing_mode", "two_stage")
        return cls(**data)
