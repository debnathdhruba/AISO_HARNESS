"""JSON schema v1 for accepted decisions and exact ordered requests."""

import json
from dataclasses import asdict

from agent_harness.domain.entities import JsonObject
from agent_harness.domain.errors import ErrorCode, HarnessError, require
from agent_harness.domain.model import (
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    Stage,
    Step,
    StepStatus,
    ToolCall,
    Usage,
)


def encode_step(step: Step) -> str:
    return json.dumps({"schema_version": 1, "data": asdict(step)}, allow_nan=False)


def decode_message(data: dict[str, object]) -> Message:
    # json.loads is used only on locally schema-versioned state, never pickled objects.
    raw = json.loads(json.dumps(data))
    return Message(
        Role(raw["role"]),
        raw["content"],
        tuple(
            ToolCall(c["call_id"], c["name"], JsonObject(**c["arguments"]))
            for c in raw["tool_calls"]
        ),
        raw["tool_call_id"],
    )


def decode_step(text: str) -> Step:
    try:
        snapshot = json.loads(text)
        require(snapshot["schema_version"] == 1, "Unsupported step schema version")
        data = snapshot["data"]
        request = data["request"]
        data["request"] = ModelRequest(
            request["request_id"],
            request["model"],
            tuple(decode_message(m) for m in request["messages"]),
            request["max_output_tokens"],
        )
        response = data["response"]
        if response is not None:
            data["response"] = ModelResponse(
                decode_message(response["message"]),
                FinishReason(response["finish_reason"]),
                Usage(**response["usage"]),
            )
        if data["error"] is not None:
            data["error"] = JsonObject(**data["error"])
        data["stage"] = Stage(data["stage"])
        data["status"] = StepStatus(data["status"])
        return Step(**data)
    except (ValueError, TypeError, KeyError):
        raise HarnessError(ErrorCode.STORAGE, "Invalid persisted step") from None
