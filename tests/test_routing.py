import pytest
from conftest import FixedClock, SequenceIds

from agent_harness.adapters.fake import ScriptedModel
from agent_harness.agents.runner import AgentRunner
from agent_harness.application.routing import select_mode
from agent_harness.application.runs import RunService
from agent_harness.domain.entities import JsonObject
from agent_harness.domain.model import FinishReason, Message, ModelResponse, Role, RunOptions
from agent_harness.domain.states import RunStatus
from agent_harness.models.gateway import ModelGateway
from agent_harness.persistence.sqlite import SQLiteStore


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
        ("artifact demo", "tools"),
    ],
)
def test_auto_routing(task: str, expected: str) -> None:
    assert select_mode(task, RunOptions()) == expected


def test_override_and_legacy_snapshots() -> None:
    assert select_mode("Calculate 2+2", RunOptions(routing_mode="chat")) == "chat"
    assert select_mode("Hello", RunOptions(routing_mode="tools")) == "tools"
    assert select_mode("Hello", RunOptions(required_tools=("calculator",))) == "tools"
    assert RunOptions.restore(JsonObject()).routing_mode == "two_stage"
    assert RunOptions().workspace == "."
    assert RunOptions().artifact_root == ".local/artifacts"
    legacy = RunOptions.restore(JsonObject('{"workspace":"legacy-artifact-root"}'))
    assert legacy.artifact_root == "legacy-artifact-root"


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
