"""Deterministic offline provider implementing the same model port as Ollama."""

import json
from collections import deque

from agent_harness.domain.entities import JsonObject
from agent_harness.domain.errors import ErrorCode, HarnessError
from agent_harness.domain.model import (
    Capabilities,
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    ToolCall,
    Usage,
)


class ScriptedModel:
    def __init__(self, responses: tuple[ModelResponse | HarnessError, ...]) -> None:
        self._responses = deque(responses)
        self.requests: list[ModelRequest] = []

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(tool_calls=True, cancellation=True)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self._responses:
            raise HarnessError(ErrorCode.PERMANENT_PROVIDER, "Fake response script is exhausted")
        response = self._responses.popleft()
        if isinstance(response, HarnessError):
            raise response
        return response


class OfflineModel:
    """Stateless fixture so replaying a saved draft does not reset a script cursor."""

    def capabilities(self, model: str) -> Capabilities:
        return Capabilities(tool_calls=True, cancellation=True)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if request.tools and "artifact" in request.messages[1].content.lower():
            results = [json.loads(m.content) for m in request.messages if m.role == Role.TOOL]
            calls: tuple[ToolCall, ...]
            if not results:
                calls = (
                    ToolCall("calc-a", "calculator", JsonObject('{"expression":"21*2"}')),
                    ToolCall("calc-b", "calculator", JsonObject('{"expression":"6*7"}')),
                )
            elif len(results) == 2:
                calls = (
                    ToolCall(
                        "write",
                        "artifact_write",
                        JsonObject('{"name":"answer.txt","content":"42"}'),
                    ),
                )
            elif len(results) == 3 and results[-1]["status"] == "succeeded":
                calls = (
                    ToolCall(
                        "read",
                        "artifact_read",
                        JsonObject(
                            json.dumps({"artifact_id": results[-1]["result"]["artifact_id"]})
                        ),
                    ),
                )
            else:
                references = [
                    r["result"]["artifact_id"]
                    for r in results
                    if r.get("result") and "artifact_id" in r["result"]
                ]
                text = (
                    "Artifact demonstration completed: 42. Artifact ID: " + references[-1]
                    if references
                    else "Artifact demonstration could not complete the write."
                )
                return ModelResponse(
                    Message(Role.ASSISTANT, text), FinishReason.STOP, Usage(24, 12)
                )
            return ModelResponse(
                Message(Role.ASSISTANT, "", calls), FinishReason.TOOL_CALLS, Usage(24, 12)
            )
        if any(message.role == Role.ASSISTANT for message in request.messages):
            draft = next(m.content for m in reversed(request.messages) if m.role == Role.ASSISTANT)
            return ModelResponse(
                Message(
                    Role.ASSISTANT,
                    draft
                    if draft.startswith("Artifact demonstration")
                    else "Offline demonstration completed.",
                ),
                FinishReason.STOP,
                Usage(24, 4),
            )
        return ModelResponse(
            Message(Role.ASSISTANT, "Return the deterministic offline demonstration result."),
            FinishReason.STOP,
            Usage(16, 8),
        )


def offline_model() -> OfflineModel:
    return OfflineModel()
