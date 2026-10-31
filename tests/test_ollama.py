import json
from uuid import UUID

import httpx
import pytest

from agent_harness.adapters.ollama import OllamaClient
from agent_harness.domain.entities import JsonObject
from agent_harness.domain.errors import ErrorCode, HarnessError
from agent_harness.domain.model import FinishReason, Message, ModelRequest, Role, ToolCall
from agent_harness.domain.tooling import ToolSpec


def request() -> ModelRequest:
    return ModelRequest(
        str(UUID(int=999)), "lfm2.5-thinking:1.2b", (Message(Role.USER, "Hello"),), 128
    )


@pytest.mark.asyncio
async def test_normalized_response_and_multiple_tool_calls() -> None:
    async def handle(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if req.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "tools", "thinking"]})
        assert body["stream"] is False
        assert "tools" not in body
        return httpx.Response(
            200,
            json={
                "model": request().model,
                "done": True,
                "done_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "Action",
                    "thinking": "private trace",
                    "tool_calls": [
                        {"function": {"name": "calc", "arguments": {"x": 1}}},
                        {"function": {"name": "calc", "arguments": {"x": 2}}},
                    ],
                },
                "prompt_eval_count": 10,
                "eval_count": 5,
            },
        )

    async with OllamaClient(transport=httpx.MockTransport(handle)) as client:
        await client.discover((request().model,))
        assert client.capabilities(request().model).tool_calls
        result = await client.generate(request())
        assert result.finish_reason == FinishReason.TOOL_CALLS
        assert len(result.message.tool_calls) == 2
        assert result.message.tool_calls[0].call_id != result.message.tool_calls[1].call_id
        assert result == await client.generate(request())  # Stable synthesized call IDs.
        assert result.usage.input_tokens == 10
        assert "private trace" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [
        (401, ErrorCode.PERMANENT_PROVIDER),
        (404, ErrorCode.PERMANENT_PROVIDER),
        (503, ErrorCode.TRANSIENT_PROVIDER),
    ],
)
async def test_http_errors_are_safe(status: int, code: ErrorCode) -> None:
    async with OllamaClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, text="secret-provider-body"))
    ) as client:
        with pytest.raises(HarnessError) as failure:
            await client.generate(request())
        assert failure.value.code == code
        assert "secret" not in str(failure.value)


@pytest.mark.asyncio
async def test_malformed_json_and_missing_done_flag() -> None:
    for payload in (b"{", b'{"message":{"role":"assistant","content":"x"}}'):
        async with OllamaClient(
            transport=httpx.MockTransport(lambda _, data=payload: httpx.Response(200, content=data))
        ) as client:
            with pytest.raises(HarnessError) as failure:
                await client.generate(request())
            assert failure.value.code == ErrorCode.VALIDATION


@pytest.mark.asyncio
async def test_timeout_mapping() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("sensitive details", request=req)

    async with OllamaClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(HarnessError) as failure:
            await client.generate(request())
        assert failure.value.code == ErrorCode.DEADLINE_EXCEEDED


def test_reject_remote_endpoint() -> None:
    with pytest.raises(HarnessError):
        OllamaClient(base_url="https://example.com")


@pytest.mark.asyncio
async def test_native_tool_schema_and_result_identity_transport() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "tools"]})
        data = json.loads(req.content)
        assert data["tools"][0]["function"]["name"] == "calc"
        results = [m for m in data["messages"] if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in results] == ["a", "b"]
        assert [m["tool_name"] for m in results] == ["calc", "calc"]
        return httpx.Response(
            200,
            json={
                "model": request().model,
                "done": True,
                "done_reason": "stop",
                "message": {"role": "assistant", "content": "Done"},
            },
        )

    calls = (ToolCall("a", "calc", JsonObject()), ToolCall("b", "calc", JsonObject()))
    messages = (
        Message(Role.USER, "Task"),
        Message(Role.ASSISTANT, "", calls),
        Message(Role.TOOL, "1", tool_call_id="a"),
        Message(Role.TOOL, "2", tool_call_id="b"),
    )
    tool = ToolSpec("calc", "1", "Calculate", JsonObject(), JsonObject())
    async with OllamaClient(transport=httpx.MockTransport(handle)) as client:
        result = await client.generate(
            ModelRequest(request().request_id, request().model, messages, 128, (tool,))
        )
    assert result.message.content == "Done"
