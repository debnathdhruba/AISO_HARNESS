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
from agent_harness.logger import audit
from agent_harness.models.context import size, tool_schema_size
from agent_harness.protocols.model import TokenCallback


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


class ChatChunkPayload(BaseModel):
    model_config = ConfigDict(strict=True)
    model: str
    message: MessagePayload
    done: bool
    done_reason: str | None = None
    prompt_eval_count: int | None = Field(default=None, ge=0)
    eval_count: int | None = Field(default=None, ge=0)


ROUTER_OUTPUT_SCHEMA: dict[str, object] = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "const": "answer"},
                "answer": {"type": "string", "minLength": 1, "maxLength": 8192},
            },
            "required": ["action", "answer"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "const": "tool"},
                "tool": {"type": "string", "minLength": 1},
                "context": {"type": "string", "minLength": 1, "maxLength": 8192},
            },
            "required": ["action", "tool", "context"],
            "additionalProperties": False,
        },
    ]
}


class OllamaClient:
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434",
        max_connections: int = 2,
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
        require(max_connections >= 1, "Ollama connection limit must be positive")
        self._url = base_url.rstrip("/")
        self._max_connections = max_connections
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
            limits=httpx.Limits(
                max_connections=self._max_connections,
                max_keepalive_connections=self._max_connections,
            ),
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
                if response.status_code != 200:
                    detail = (await response.aread()).decode("utf-8", errors="replace")[:300]
                    self._raise_context_overflow(detail)
                    audit(
                        "ollama.request_rejected",
                        status_code=response.status_code,
                        detail=detail,
                    )
                    self._validate_http_status(response.status_code)
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
                self._capabilities[model] = Capabilities(
                    tool_calls="tools" in names,
                    streaming=True,
                )
            except (ValueError, TypeError, KeyError):
                raise HarnessError(
                    ErrorCode.VALIDATION, "Malformed Ollama model metadata"
                ) from None

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if request.model not in self._capabilities:
            await self.discover((request.model,))
        payload = self._chat_payload(request, stream=False)
        raw = await self._post("/api/chat", payload)
        try:
            response = ChatPayload.model_validate_json(raw)
            require(
                response.model == request.model and response.done,
                "Ollama returned an incomplete or mismatched response",
            )
            require(response.message.role == "assistant", "Unexpected Ollama message role")
            return self._response(
                request,
                response.message,
                response.done_reason,
                response.prompt_eval_count,
                response.eval_count,
            )
        except (ValidationError, ValueError, TypeError):
            self._raise_context_overflow(raw.decode("utf-8", errors="replace")[:1000])
            raise HarnessError(ErrorCode.VALIDATION, "Malformed Ollama chat response") from None

    async def generate_stream(
        self, request: ModelRequest, on_token: TokenCallback
    ) -> ModelResponse:
        require(self._http is not None, "Ollama adapter is not started")
        assert self._http is not None
        if request.model not in self._capabilities:
            await self.discover((request.model,))
        payload = self._chat_payload(request, stream=True)
        content: list[str] = []
        message = MessagePayload(role="assistant")
        done_reason: str | None = None
        prompt_eval_count: int | None = None
        eval_count: int | None = None
        finished = False
        total_bytes = 0
        try:
            async with self._http.stream("POST", "/api/chat", json=payload) as response:
                if response.status_code != 200:
                    detail = (await response.aread()).decode("utf-8", errors="replace")[:300]
                    self._raise_context_overflow(detail)
                    audit(
                        "ollama.request_rejected",
                        status_code=response.status_code,
                        detail=detail,
                    )
                    self._validate_http_status(response.status_code)
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    total_bytes += len(line.encode("utf-8"))
                    if total_bytes > 1_048_576:
                        raise HarnessError(
                            ErrorCode.VALIDATION, "Ollama response exceeds size limit"
                        )
                    try:
                        chunk = ChatChunkPayload.model_validate_json(line)
                    except (ValidationError, ValueError, TypeError):
                        self._raise_context_overflow(line[:1000])
                        raise HarnessError(
                            ErrorCode.VALIDATION, "Malformed Ollama streaming response"
                        ) from None
                    require(chunk.model == request.model, "Ollama returned a mismatched model")
                    require(
                        chunk.message.role == "assistant",
                        "Unexpected Ollama message role",
                    )
                    message = chunk.message
                    if message.content:
                        content.append(message.content)
                        await on_token(message.content)
                    if chunk.done_reason is not None:
                        done_reason = chunk.done_reason
                    if chunk.prompt_eval_count is not None:
                        prompt_eval_count = chunk.prompt_eval_count
                    if chunk.eval_count is not None:
                        eval_count = chunk.eval_count
                    finished = finished or chunk.done
            require(finished, "Ollama returned an incomplete streaming response")
            message = message.model_copy(update={"content": "".join(content)})
            return self._response(
                request,
                message,
                done_reason,
                prompt_eval_count,
                eval_count,
            )
        except httpx.TimeoutException:
            raise HarnessError(ErrorCode.DEADLINE_EXCEEDED, "Ollama request timed out") from None
        except httpx.RequestError:
            raise HarnessError(
                ErrorCode.TRANSIENT_PROVIDER, "Cannot reach local Ollama", retryable=True
            ) from None

    def _chat_payload(self, request: ModelRequest, *, stream: bool) -> dict[str, object]:
        if request.model not in self._capabilities:
            raise HarnessError(
                ErrorCode.VALIDATION, "Ollama model capabilities were not discovered"
            )
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
        return {
            **(
                {"format": json.loads(request.json_schema.text)}
                if request.json_schema is not None
                else {"format": ROUTER_OUTPUT_SCHEMA}
                if request.json_mode
                else {}
            ),
            "model": request.model,
            "messages": messages,
            **extra,
            "stream": stream,
            "options": {
                "num_predict": request.max_output_tokens,
                "num_ctx": max(
                    2048,
                    size(request.messages)
                    + tool_schema_size(request.tools)
                    + (
                        request.max_output_tokens
                        if request.max_output_tokens > 0
                        else max(size(request.messages), 8192)
                    )
                    + 512,
                ),
            },
        }

    @staticmethod
    def _raise_context_overflow(detail: str) -> None:
        normalized = detail.casefold()
        markers = (
            "context length",
            "context window",
            "maximum context",
            "context limit",
            "too many tokens",
            "prompt is too long",
            "input is too long",
            "exceeds the context",
            "input length exceeds",
            "number of tokens exceeds",
            "context size exceeded",
        )
        if any(marker in normalized for marker in markers):
            raise HarnessError(
                ErrorCode.BUDGET_EXCEEDED,
                "Ollama rejected the prompt because it exceeds the model context window",
            )

    @staticmethod
    def _validate_http_status(status_code: int) -> None:
        if status_code >= 500 or status_code == 429:
            raise HarnessError(
                ErrorCode.TRANSIENT_PROVIDER,
                f"Local Ollama is temporarily unavailable (HTTP {status_code})",
                retryable=True,
            )
        if status_code != 200:
            raise HarnessError(
                ErrorCode.PERMANENT_PROVIDER,
                "Ollama rejected the request "
                f"(HTTP {status_code}); see ollama.request_rejected log",
            )

    @staticmethod
    def _response(
        request: ModelRequest,
        message: MessagePayload,
        done_reason: str | None,
        prompt_eval_count: int | None,
        eval_count: int | None,
    ) -> ModelResponse:
        calls = tuple(
            ToolCall(
                call.id or str(uuid5(UUID(request.request_id), str(index))),
                call.function.name,
                JsonObject(json.dumps(call.function.arguments)),
            )
            for index, call in enumerate(message.tool_calls)
        )
        reason = (
            FinishReason.TOOL_CALLS
            if calls
            else FinishReason(done_reason)
            if done_reason in {"stop", "length"}
            else FinishReason.UNSUPPORTED
        )
        return ModelResponse(
            Message(Role.ASSISTANT, message.content, calls),
            reason,
            Usage(prompt_eval_count, eval_count),
        )
