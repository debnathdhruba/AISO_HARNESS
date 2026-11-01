import pytest
from conftest import FixedClock, SequenceIds

from agent_harness.adapters.fake import ScriptedModel
from agent_harness.agents.runner import AgentRunner
from agent_harness.application.routing import select_mode
from agent_harness.application.runs import RunService
from agent_harness.domain.entities import JsonObject
from agent_harness.domain.model import FinishReason, Message, ModelResponse, Role, RunOptions
from agent_harness.domain.states import RunStatus
from agent_harness.memory.backends.sqlite import SQLiteStore
from agent_harness.models.gateway import ModelGateway


@pytest.mark.parametrize(
    "task,expected",
    [
        ("Explain a queue", "chat"),
        ("Write a poem about rain", "chat"),
        ("Hello", "chat"),
        ("Calculate 6*7", "tools"),
        ("Please save this in answer.txt", "tools"),
        ("Search files for an import in the workspace", "tools"),
        ("Edit file src/example.py", "tools"),
        ("Read implementation.md", "tools"),
    ],
)
def test_auto_routing(task: str, expected: str) -> None:
    assert select_mode(task, RunOptions()) == expected


def test_override_and_legacy_snapshots() -> None:
    assert select_mode("Calculate 2+2", RunOptions(routing_mode="chat")) == "chat"
    assert select_mode("Hello", RunOptions(routing_mode="tools")) == "tools"
    assert select_mode("Hello", RunOptions(required_tools=("calculator",))) == "tools"
    assert RunOptions.restore(JsonObject()).routing_mode == "two_stage"
    legacy = RunOptions.restore(
        JsonObject(
            '{"workspace":"legacy-root","artifact_root":"legacy-root",'
            '"tool_allowlist":["artifact_read","read_file"],'
            '"required_tools":["artifact_read"]}'
        )
    )
    assert legacy.tool_allowlist == ("read_file",)
    assert legacy.required_tools == ()


def test_ollama_auto_keeps_greetings_on_the_fast_chat_path() -> None:
    assert select_mode("hi", RunOptions(provider="ollama")) == "chat"
    assert select_mode("Calculate 6*7", RunOptions(provider="ollama")) == "tools"


@pytest.mark.asyncio
async def test_chat_calls_only_gemma_and_persists_route(store: SQLiteStore) -> None:
    clock, ids = FixedClock(), SequenceIds()
    run = await RunService(store, clock, ids).submit(
        "alice", "Explain a queue", RunOptions(max_steps=1)
    )
    assert RunOptions.restore(run.config.model).routing_mode == "chat"
    model = ScriptedModel(
        (
            ModelResponse(
                Message(Role.ASSISTANT, "A queue stores waiting items."), FinishReason.STOP
            ),
        )
    )
    result = await AgentRunner(store, ModelGateway(model), clock, ids).execute("alice", run.run_id)
    assert result.status == RunStatus.COMPLETED
    assert len(model.requests) == 1
    assert model.requests[0].model == "gemma3:1b"
    assert model.requests[0].tools == ()
    assert model.requests[0].max_output_tokens == 512
