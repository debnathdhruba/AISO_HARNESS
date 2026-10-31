from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from agent_harness.domain.model import Capabilities, ModelRequest, ModelResponse

TokenCallback = Callable[[str], Awaitable[None]]


class ModelClient(Protocol):
    def capabilities(self, model: str) -> Capabilities: ...
    async def generate(self, request: ModelRequest) -> ModelResponse: ...


@runtime_checkable
class StreamingModelClient(Protocol):
    async def generate_stream(
        self, request: ModelRequest, on_token: TokenCallback
    ) -> ModelResponse: ...
