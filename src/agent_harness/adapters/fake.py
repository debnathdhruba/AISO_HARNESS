"""Deterministic offline provider implementing the same model port as Ollama."""

from collections import deque

from agent_harness.domain.errors import ErrorCode, HarnessError
from agent_harness.domain.model import (
    Capabilities,
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
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
        if any(message.role == Role.ASSISTANT for message in request.messages):
            return ModelResponse(
                Message(Role.ASSISTANT, "Offline demonstration completed."),
                FinishReason.STOP,
                Usage(24, 4),
            )
        return ModelResponse(
            Message(Role.ASSISTANT, "Offline demonstration completed."),
            FinishReason.STOP,
            Usage(16, 8),
        )


def offline_model() -> OfflineModel:
    return OfflineModel()
