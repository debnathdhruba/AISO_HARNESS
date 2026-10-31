from typing import Protocol

from agent_harness.domain.model import Capabilities, ModelRequest, ModelResponse


class ModelClient(Protocol):
    def capabilities(self, model: str) -> Capabilities: ...
    async def generate(self, request: ModelRequest) -> ModelResponse: ...
