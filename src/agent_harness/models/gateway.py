from agent_harness.domain.errors import ErrorCode, HarnessError
from agent_harness.domain.model import Capabilities, ModelRequest, ModelResponse
from agent_harness.ports.model import ModelClient
from agent_harness.scheduling import ResourceKind, ResourceScheduler


class ModelGateway:
    """One configured adapter; no hidden routing, retries or provider fallbacks."""

    def __init__(
        self,
        client: ModelClient,
        scheduler: ResourceScheduler | None = None,
        provider_key: str = "default",
    ) -> None:
        self._client = client
        self._scheduler = scheduler or ResourceScheduler()
        self._provider_key = provider_key

    def capabilities(self, model: str) -> Capabilities:
        return self._client.capabilities(model)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        response = await self._scheduler.run(
            ResourceKind.MODEL,
            lambda: self._client.generate(request),
            key=self._provider_key,
        )
        if not isinstance(response, ModelResponse):
            raise HarnessError(ErrorCode.VALIDATION, "Malformed model response")
        return response
