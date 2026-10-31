from agent_harness.domain.errors import ErrorCode, HarnessError
from agent_harness.domain.model import Capabilities, ModelRequest, ModelResponse
from agent_harness.ports.model import ModelClient


class ModelGateway:
    """One configured adapter; no hidden routing, retries or provider fallbacks."""

    def __init__(self, client: ModelClient) -> None:
        self._client = client

    def capabilities(self, model: str) -> Capabilities:
        return self._client.capabilities(model)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        response = await self._client.generate(request)
        if not isinstance(response, ModelResponse):
            raise HarnessError(ErrorCode.VALIDATION, "Malformed model response")
        return response
