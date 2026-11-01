from uuid import uuid4

import pytest

from agent_harness.adapters.fake import ScriptedModel
from agent_harness.domain.model import FinishReason, Message, ModelRequest, ModelResponse, Role
from agent_harness.llm.unified import UnifiedLLM
from agent_harness.models.gateway import ModelGateway
from agent_harness.tools.builtins import builtin_registry


def response(text: str) -> ModelResponse:
    return ModelResponse(Message(Role.ASSISTANT, text), FinishReason.STOP)


def request(text: str, *, tools=()) -> ModelRequest:
    return ModelRequest(
        str(uuid4()),
        "gemma3:1b",
        (Message(Role.SYSTEM, "Answer the user."), Message(Role.USER, text)),
        256,
        tuple(tools),
    )


@pytest.mark.asyncio
async def test_ordinary_response_uses_lfm_only() -> None:
    client = ScriptedModel((response("Hello."),))
    result = await ModelGateway(UnifiedLLM(client)).generate(request("Say hello."))

    assert result.message.content == "Hello."
    assert [item.model for item in client.requests] == ["lfm2.5-thinking:1.2b"]


@pytest.mark.asyncio
async def test_content_heavy_response_uses_gemma_directly() -> None:
    client = ScriptedModel((response("A concise summary."),))
    result = await ModelGateway(UnifiedLLM(client)).generate(
        request("Summarize the main findings and open questions.")
    )

    assert result.message.content == "A concise summary."
    assert [item.model for item in client.requests] == ["gemma3:1b"]


@pytest.mark.asyncio
async def test_tool_tasks_use_lfm_without_gemma_for_short_answer() -> None:
    client = ScriptedModel(
        (response('{"type":"answer","answer_context":"The answer is 42."}'),)
    )
    calculator = builtin_registry().get("calculator").spec
    result = await ModelGateway(UnifiedLLM(client)).generate(
        request("Calculate 6 * 7.", tools=(calculator,))
    )

    assert result.message.content == "The answer is 42."
    assert [item.model for item in client.requests] == ["lfm2.5-thinking:1.2b"]


@pytest.mark.asyncio
async def test_tool_gathering_then_content_synthesis_uses_gemma_once() -> None:
    client = ScriptedModel(
        (
            response(
                '{"type":"answer","answer_context":"The documents describe two risks."}'
            ),
            response("The main risks are A and B."),
        )
    )
    read_file = builtin_registry().get("read_file").spec
    result = await ModelGateway(UnifiedLLM(client)).generate(
        request("Summarize the risks in these documents.", tools=(read_file,))
    )

    assert result.message.content == "The main risks are A and B."
    assert [item.model for item in client.requests] == [
        "lfm2.5-thinking:1.2b",
        "gemma3:1b",
    ]
