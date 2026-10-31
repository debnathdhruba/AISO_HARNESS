from agent_harness.domain.errors import ErrorCode, HarnessError
from agent_harness.domain.model import Capabilities, ModelRequest, ModelResponse
from agent_harness.ports.model import ModelClient, StreamingModelClient, TokenCallback
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
        return self._validate_response(response)

    async def generate_stream(
        self, request: ModelRequest, on_token: TokenCallback
    ) -> ModelResponse:
        async def generate() -> ModelResponse:
            if isinstance(self._client, StreamingModelClient):
                return await self._client.generate_stream(request, on_token)
            response = await self._client.generate(request)
            if response.message.content:
                await on_token(response.message.content)
            return response

        response = await self._scheduler.run(
            ResourceKind.MODEL,
            generate,
            key=self._provider_key,
        )
        return self._validate_response(response)

    @staticmethod
    def _validate_response(response: ModelResponse) -> ModelResponse:
        if not isinstance(response, ModelResponse):
            raise HarnessError(ErrorCode.VALIDATION, "Malformed model response")
        return response
