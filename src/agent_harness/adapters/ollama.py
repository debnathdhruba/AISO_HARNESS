"""Local-only Ollama HTTP adapter. No downloads, SDK globals or cloud fallbacks."""

import json
from uuid import UUID, uuid5

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_harness.domain.entities import JsonObject
from agent_harness.domain.errors import ErrorCode, HarnessError, require
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
from agent_harness.models.context import size, tool_schema_size


class FunctionPayload(BaseModel):
    model_config = ConfigDict(strict=True)
    name: str = Field(min_length=1)
    arguments: dict[str, object]


class CallPayload(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str | None = None
    function: FunctionPayload


class MessagePayload(BaseModel):
    model_config = ConfigDict(strict=True)
    role: str
    content: str = ""
    tool_calls: list[CallPayload] = Field(default_factory=list)
    # Ollama's private thinking field is deliberately not retained or forwarded.


class ChatPayload(BaseModel):
    model_config = ConfigDict(strict=True)
    model: str
    message: MessagePayload
    done: bool
    done_reason: str
    prompt_eval_count: int | None = Field(default=None, ge=0)
    eval_count: int | None = Field(default=None, ge=0)


class OllamaClient:
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        url = httpx.URL(base_url)
        require(
            url.scheme == "http"
            and url.host in {"127.0.0.1", "localhost", "::1"}
            and not url.username
            and not url.password
            and not url.query
            and url.path in {"", "/"},
            "Ollama endpoint must be local HTTP",
        )
        self._url = base_url.rstrip("/")
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._capabilities: dict[str, Capabilities] = {}

    async def __aenter__(self) -> "OllamaClient":
        require(self._http is None, "Ollama adapter already started")
        self._http = httpx.AsyncClient(
            base_url=self._url,
            transport=self._transport,
            trust_env=False,
            follow_redirects=False,
            timeout=180,
            limits=httpx.Limits(max_connections=1),
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def capabilities(self, model: str) -> Capabilities:
        return self._capabilities.get(model, Capabilities())

    async def _post(self, path: str, payload: dict[str, object]) -> bytes:
        require(self._http is not None, "Ollama adapter is not started")
        assert self._http is not None
        try:
            async with self._http.stream("POST", path, json=payload) as response:
                if response.status_code >= 500 or response.status_code == 429:
                    raise HarnessError(
                        ErrorCode.TRANSIENT_PROVIDER,
                        "Local Ollama is temporarily unavailable",
                        retryable=True,
                    )
                if response.status_code != 200:
                    raise HarnessError(
                        ErrorCode.PERMANENT_PROVIDER,
                        "Ollama rejected the request; check installed model names",
                    )
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 1_048_576:
                        raise HarnessError(
                            ErrorCode.VALIDATION, "Ollama response exceeds size limit"
                        )
                return bytes(data)
        except httpx.TimeoutException:
            raise HarnessError(ErrorCode.DEADLINE_EXCEEDED, "Ollama request timed out") from None
        except httpx.RequestError:
            raise HarnessError(
                ErrorCode.TRANSIENT_PROVIDER, "Cannot reach local Ollama", retryable=True
            ) from None

    async def discover(self, models: tuple[str, ...]) -> None:
        for model in dict.fromkeys(models):
            try:
                data = json.loads(await self._post("/api/show", {"model": model}))
                names = data["capabilities"]
                require(
                    isinstance(names, list) and "completion" in names,
                    "Ollama model lacks completion capability",
                )
                self._capabilities[model] = Capabilities(tool_calls="tools" in names)
            except (ValueError, TypeError, KeyError):
                raise HarnessError(
                    ErrorCode.VALIDATION, "Malformed Ollama model metadata"
                ) from None

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if request.model not in self._capabilities:
            await self.discover((request.model,))
        if request.tools:
            require(
                self.capabilities(request.model).tool_calls, "Selected model does not support tools"
            )
        call_names = {c.call_id: c.name for m in request.messages for c in m.tool_calls}
        messages: list[dict[str, object]] = []
        for message in request.messages:
            native: dict[str, object] = {"role": message.role.value, "content": message.content}
            if message.tool_calls:
                native["tool_calls"] = [
                    {
                        "id": c.call_id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.loads(c.arguments.text)},
                    }
                    for c in message.tool_calls
                ]
            if message.role == Role.TOOL:
                require(message.tool_call_id in call_names, "Tool result has no matching request")
                assert message.tool_call_id is not None
                native["tool_name"] = call_names[message.tool_call_id]
                native["tool_call_id"] = message.tool_call_id
            messages.append(native)
        extra: dict[str, object] = {}
        if request.tools:
            extra["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": json.loads(spec.input_schema.text),
                    },
                }
                for spec in request.tools
            ]
        raw = await self._post(
            "/api/chat",
            {
                **({"format": "json"} if request.json_mode else {}),
                "model": request.model,
                "messages": messages,
                **extra,
                "stream": False,
                "options": {
                    "num_predict": request.max_output_tokens,
                    "num_ctx": max(
                        2048,
                        size(request.messages)
                        + tool_schema_size(request.tools)
                        + request.max_output_tokens
                        + 512,
                    ),
                },
                "keep_alive": "5m",
            },
        )
        try:
            payload = ChatPayload.model_validate_json(raw)
            require(
                payload.model == request.model and payload.done,
                "Ollama returned an incomplete or mismatched response",
            )
            require(payload.message.role == "assistant", "Unexpected Ollama message role")
            calls = tuple(
                ToolCall(
                    c.id or str(uuid5(UUID(request.request_id), str(index))),
                    c.function.name,
                    JsonObject(json.dumps(c.function.arguments)),
                )
                for index, c in enumerate(payload.message.tool_calls)
            )
            reason = (
                FinishReason.TOOL_CALLS
                if calls
                else FinishReason(payload.done_reason)
                if payload.done_reason in {"stop", "length"}
                else FinishReason.UNSUPPORTED
            )
            return ModelResponse(
                Message(Role.ASSISTANT, payload.message.content, calls),
                reason,
                Usage(payload.prompt_eval_count, payload.eval_count),
            )
        except (ValidationError, ValueError, TypeError):
            raise HarnessError(ErrorCode.VALIDATION, "Malformed Ollama chat response") from None
