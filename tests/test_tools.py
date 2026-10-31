import asyncio
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from conftest import FixedClock, SequenceIds

from agent_harness.adapters.fake import ScriptedModel, offline_model
from agent_harness.agents.runner import AgentRunner
from agent_harness.application.runs import RunService
from agent_harness.artifacts.service import ArtifactService
from agent_harness.domain.entities import JsonObject, Run, Task
from agent_harness.domain.errors import HarnessError
from agent_harness.domain.model import (
    FinishReason,
    Message,
    ModelResponse,
    Role,
    RunOptions,
    ToolCall,
)
from agent_harness.domain.states import InvocationStatus, RunStatus
from agent_harness.domain.tooling import Effect, Invocation, ToolSpec, argument_hash
from agent_harness.models.gateway import ModelGateway
from agent_harness.persistence.sqlite import SQLiteStore, SQLiteTransaction
from agent_harness.tools.builtins import builtin_registry, calculate
from agent_harness.tools.executor import ToolExecutor, decide_approval
from agent_harness.tools.registry import RegisteredTool, ToolRegistry
from agent_harness.tools.workspace import WorkspaceFileService


def action(*calls: ToolCall) -> ModelResponse:
    return ModelResponse(
        Message(Role.ASSISTANT, "Action prose is not final", calls), FinishReason.STOP
    )


def final(text: str = "Done") -> ModelResponse:
    return ModelResponse(Message(Role.ASSISTANT, text), FinishReason.STOP)


def native_path(path: Path) -> Path:
    return Path("\\\\?\\" + str(path)) if os.name == "nt" else path


@pytest.mark.asyncio
async def test_offline_full_tools_multiple_calls_and_artifact(
    store: SQLiteStore, tmp_path: Path
) -> None:
    clock, ids = FixedClock(), SequenceIds()
    artifacts = ArtifactService(store, tmp_path / "files")
    executor = ToolExecutor(
        store, builtin_registry(artifacts, tmp_path / "files", tmp_path / "files"), clock
    )
    run = await RunService(store, clock, ids).submit(
        "alice",
        "artifact demo",
        RunOptions(
            routing_mode="tools",
        ),
    )
    result = await AgentRunner(store, ModelGateway(offline_model()), clock, ids, executor).execute(
        "alice", run.run_id
    )
    assert result.status == RunStatus.COMPLETED
    async with store.transaction("alice") as tx:
        calls = await tx.invocations(run.run_id)
        assert [i.tool_name for i in calls] == [
            "calculator",
            "calculator",
            "artifact_write",
            "artifact_read",
        ]
        assert all(i.status == InvocationStatus.SUCCEEDED for i in calls)
        assert json.loads(calls[-1].result.text)["content"] == "42"
        records = await tx.artifacts(run.run_id)
        assert len(records) == 1 and records[0].status == "committed"
        steps = await tx.steps(run.run_id)
        results = [m for m in steps[1].request.messages if m.role == Role.TOOL]
        assert [m.tool_call_id for m in results] == ["calc-a", "calc-b"]
    assert native_path(tmp_path / "files" / records[0].relative_path).read_text() == "42"


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_approval_binding_pause_and_resume(
    store: SQLiteStore, tmp_path: Path, approved: bool
) -> None:
    clock, ids = FixedClock(), SequenceIds()
    registry = builtin_registry(
        ArtifactService(store, tmp_path / "files"), tmp_path / "files", tmp_path / "files"
    )
    run = await RunService(store, clock, ids).submit(
        "alice", "artifact demo", RunOptions(routing_mode="tools", require_write_approval=True)
    )
    agent = AgentRunner(
        store, ModelGateway(offline_model()), clock, ids, ToolExecutor(store, registry, clock)
    )
    assert (await agent.execute("alice", run.run_id)).status == RunStatus.WAITING
    async with store.transaction("alice") as tx:
        pending = next(
            i
            for i in await tx.invocations(run.run_id)
            if i.status == InvocationStatus.AWAITING_APPROVAL
        )
        assert await tx.artifacts(run.run_id) == ()
    with pytest.raises(HarnessError):
        await decide_approval(store, "alice", pending.invocation_id, "wrong-hash", True)
    with pytest.raises(HarnessError):
        await decide_approval(store, "bob", pending.invocation_id, pending.argument_hash, True)
    await decide_approval(store, "alice", pending.invocation_id, pending.argument_hash, approved)
    assert (await agent.execute("alice", run.run_id)).status == RunStatus.COMPLETED
    async with store.transaction("alice") as tx:
        calls = await tx.invocations(run.run_id)
        assert len([i for i in calls if i.tool_name == "calculator"]) == 2
        assert bool(await tx.artifacts(run.run_id)) is approved


@pytest.mark.asyncio
async def test_invalid_disallowed_and_repeated_calls_become_observations(
    store: SQLiteStore, tmp_path: Path
) -> None:
    clock, ids = FixedClock(), SequenceIds()
    registry = builtin_registry(ArtifactService(store, tmp_path), tmp_path, tmp_path)
    calls = (
        ToolCall("bad", "calculator", JsonObject('{"wrong":1}')),
        ToolCall("denied", "shell", JsonObject()),
        ToolCall("first", "calculator", JsonObject('{"expression":"1+2"}')),
        ToolCall("repeat", "calculator", JsonObject('{"expression":"1+2"}')),
    )
    model = ScriptedModel((action(*calls), final("draft"), final()))
    run = await RunService(store, clock, ids).submit(
        "alice", "Task", RunOptions(routing_mode="tools", repeated_action_limit=1)
    )
    result = await AgentRunner(
        store, ModelGateway(model), clock, ids, ToolExecutor(store, registry, clock)
    ).execute("alice", run.run_id)
    assert result.status == RunStatus.COMPLETED
    async with store.transaction("alice") as tx:
        invocations = await tx.invocations(run.run_id)
    assert [i.status for i in invocations] == [
        InvocationStatus.FAILED,
        InvocationStatus.FAILED,
        InvocationStatus.SUCCEEDED,
        InvocationStatus.FAILED,
    ]
    assert len([m for m in model.requests[1].messages if m.role == Role.TOOL]) == 4
    assert all(not request.tools for request in model.requests if request.model == "gemma3:1b")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["exception", "oversized", "timeout"])
async def test_tool_errors_are_bounded(store: SQLiteStore, mode: str) -> None:
    async def handler(invocation: Invocation) -> JsonObject:
        if mode == "exception":
            raise ValueError("private diagnostic")
        if mode == "timeout":
            await asyncio.Event().wait()
        return JsonObject(json.dumps({"text": "x" * 1000}))

    spec = ToolSpec(
        "fault",
        "1",
        "test",
        JsonObject('{"type":"object"}'),
        JsonObject('{"type":"object"}'),
        timeout_seconds=0.01,
        max_output_bytes=128,
    )
    registry = ToolRegistry((RegisteredTool(spec, handler),))
    clock, ids = FixedClock(), SequenceIds()
    run = await RunService(store, clock, ids).submit(
        "alice", "Task", RunOptions(routing_mode="tools", tool_allowlist=("fault",))
    )
    model = ScriptedModel((action(ToolCall("fault", "fault", JsonObject())), final(), final()))
    result = await AgentRunner(
        store, ModelGateway(model), clock, ids, ToolExecutor(store, registry, clock)
    ).execute("alice", run.run_id)
    assert result.status == RunStatus.COMPLETED
    async with store.transaction("alice") as tx:
        invocation = (await tx.invocations(run.run_id))[0]
    assert invocation.status == InvocationStatus.FAILED
    assert "private diagnostic" not in invocation.error.text


@pytest.mark.asyncio
async def test_interrupted_write_is_unknown_and_never_replayed(store: SQLiteStore) -> None:
    class PowerLoss(BaseException):
        pass

    executions = 0

    async def handler(invocation: Invocation) -> JsonObject:
        nonlocal executions
        executions += 1
        raise PowerLoss()

    spec = ToolSpec("unsafe", "1", "test", JsonObject(), JsonObject(), Effect.NON_IDEMPOTENT_WRITE)
    clock, ids = FixedClock(), SequenceIds()
    executor = ToolExecutor(store, ToolRegistry((RegisteredTool(spec, handler),)), clock)
    run = await RunService(store, clock, ids).submit(
        "alice", "Task", RunOptions(routing_mode="tools", tool_allowlist=("unsafe",))
    )
    model = ScriptedModel((action(ToolCall("write", "unsafe", JsonObject())),))
    with pytest.raises(PowerLoss):
        await AgentRunner(store, ModelGateway(model), clock, ids, executor).execute(
            "alice", run.run_id
        )
    resumed = AgentRunner(store, ModelGateway(ScriptedModel(())), clock, ids, executor)
    result = await resumed.execute("alice", run.run_id)
    assert result.status == RunStatus.FAILED and executions == 1
    async with store.transaction("alice") as tx:
        assert (await tx.invocations(run.run_id))[0].status == InvocationStatus.UNKNOWN


def invocation_for(run: Run, task: Task, number: int = 9) -> Invocation:
    arguments = JsonObject()
    return Invocation(
        str(UUID(int=number)),
        run.run_id,
        task.task_id,
        run.user_id,
        str(UUID(int=8)),
        "call",
        "artifact_write",
        "1",
        arguments,
        argument_hash(arguments),
        Effect.IDEMPOTENT_WRITE,
    )


def test_builtin_registry_registers_workspace_file_tools(
    store: SQLiteStore, tmp_path: Path
) -> None:
    registry = builtin_registry(
        ArtifactService(store, tmp_path / "artifacts"),
        tmp_path / "workspace",
        tmp_path / "artifacts",
    )
    assert {
        "read_file",
        "write_file",
        "search_files",
        "edit_file",
    } <= {
        spec.name
        for spec in registry.allowed(("read_file", "write_file", "search_files", "edit_file"))
    }
    assert registry.get("write_file").spec.effect == Effect.NON_IDEMPOTENT_WRITE
    assert registry.get("edit_file").spec.effect == Effect.NON_IDEMPOTENT_WRITE


@pytest.mark.asyncio
async def test_artifact_scope_versions_checksums_and_paths(
    store: SQLiteStore, tmp_path: Path, run: Run, task: Task
) -> None:
    async with store.transaction(run.user_id) as tx:
        await tx.create_run(run)
        await tx.create_task(task)
    artifacts = ArtifactService(store, tmp_path / "workspace")
    invocation = invocation_for(run, task)
    for name in ("../escape", "folder/file", "C:\\escape", ".."):
        with pytest.raises(HarnessError):
            await artifacts.write(invocation, name, "x")
    first = json.loads((await artifacts.write(invocation, "a.txt", "first")).text)
    assert json.loads((await artifacts.write(invocation, "a.txt", "first")).text) == first
    second = json.loads(
        (await artifacts.write(invocation_for(run, task, 10), "a.txt", "second")).text
    )
    assert second["version"] == 2
    with pytest.raises(HarnessError):
        await artifacts.read(replace(invocation, user_id="bob"), first["artifact_id"])
    async with store.transaction(run.user_id) as tx:
        record = (await tx.artifacts(run.run_id))[0]
    native_path(tmp_path / "workspace" / record.relative_path).write_text("tampered")
    with pytest.raises(HarnessError):
        await artifacts.read(invocation, first["artifact_id"])


@pytest.mark.asyncio
async def test_workspace_file_tools_read_write_edit_and_search(
    tmp_path: Path, run: Run, task: Task
) -> None:
    root = tmp_path / "workspace"
    files = WorkspaceFileService(root, tmp_path / "artifacts")
    invocation = invocation_for(run, task)
    written = json.loads(
        (await files.write(invocation, "src/example.py", "value = 1\nvalue = 2\n")).text
    )
    assert written == {"path": "src/example.py", "bytes_written": len(b"value = 1\nvalue = 2\n")}
    assert json.loads((await files.read(invocation, "src/example.py")).text)["content"] == (
        "value = 1\nvalue = 2\n"
    )
    with pytest.raises(HarnessError, match="exactly once"):
        await files.edit(invocation, "src/example.py", "value", "result")
    edited = json.loads(
        (await files.edit(invocation, "src/example.py", "value = 1", "result = 1")).text
    )
    assert edited["replacements"] == 1
    results = json.loads((await files.search("result = 1", "*.py")).text)
    assert results["matches"] == [{"path": "src/example.py", "line": 1, "text": "result = 1"}]
    assert results["skipped_non_utf8"] == 0


@pytest.mark.asyncio
async def test_workspace_file_tools_reject_traversal_and_symlink_escape(
    tmp_path: Path, run: Run, task: Task
) -> None:
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("private")
    linked = root / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(linked), str(outside)], capture_output=True
        )
        assert result.returncode == 0, result.stderr

    files = WorkspaceFileService(root, tmp_path / "artifacts")
    invocation = invocation_for(run, task)
    for path in ("../secret.txt", "linked/secret.txt", "C:/secret.txt", "linked\\secret.txt"):
        with pytest.raises(HarnessError):
            await files.read(invocation, path)
        with pytest.raises(HarnessError):
            await files.write(invocation, path, "changed")
    assert (outside / "secret.txt").read_text() == "private"


@pytest.mark.asyncio
async def test_workspace_file_tools_hide_internal_and_artifact_storage(
    tmp_path: Path, run: Run, task: Task
) -> None:
    root = tmp_path / "workspace"
    artifact_root = root / "private_artifacts"
    (root / ".local").mkdir(parents=True)
    artifact_root.mkdir()
    (root / ".local" / "state.sqlite").write_text("runtime-secret")
    (artifact_root / "secret.txt").write_text("artifact-secret")
    files = WorkspaceFileService(root, artifact_root)
    invocation = invocation_for(run, task)

    for path in (".local/state.sqlite", "private_artifacts/secret.txt"):
        with pytest.raises(HarnessError):
            await files.read(invocation, path)
    results = json.loads((await files.search("artifact-secret", "*")).text)
    assert results["matches"] == []


@pytest.mark.asyncio
async def test_workspace_search_reports_file_budget_truncation(
    tmp_path: Path, run: Run, task: Task
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.txt").write_text("needle")
    (root / "b.txt").write_text("needle")
    files = WorkspaceFileService(
        root, tmp_path / "artifacts", max_search_files=1, max_search_bytes=1024
    )
    result = json.loads((await files.search("needle", "*.txt")).text)
    assert result["scanned_files"] == 1
    assert result["matches"] == [{"path": "a.txt", "line": 1, "text": "needle"}]
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_symlink_or_windows_junction_escape(
    store: SQLiteStore, tmp_path: Path, run: Run, task: Task
) -> None:
    target, linked = tmp_path / "outside", tmp_path / "linked"
    target.mkdir()
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(linked), str(target)], capture_output=True
        )
        assert result.returncode == 0, result.stderr
    with pytest.raises(HarnessError, match="symlink or reparse"):
        await ArtifactService(store, linked).write(invocation_for(run, task), "x.txt", "x")
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("expression", ["__import__('os')", "2**1000", "1/0", "True", "9" * 201])
def test_calculator_rejects_unsafe_or_unbounded_expressions(expression: str) -> None:
    with pytest.raises(HarnessError):
        calculate(expression)


@pytest.mark.asyncio
async def test_final_prose_cannot_satisfy_required_tool(store: SQLiteStore, tmp_path: Path) -> None:
    clock, ids = FixedClock(), SequenceIds()
    registry = builtin_registry(ArtifactService(store, tmp_path), tmp_path, tmp_path)
    model = ScriptedModel((final("42"), final("42")))
    run = await RunService(store, clock, ids).submit(
        "alice",
        "Use calculator",
        RunOptions(routing_mode="tools", required_tools=("calculator",), max_steps=2),
    )
    result = await AgentRunner(
        store, ModelGateway(model), clock, ids, ToolExecutor(store, registry, clock)
    ).execute("alice", run.run_id)
    assert result.status == RunStatus.FAILED
    assert all(r.model == "lfm2.5-thinking:1.2b" for r in model.requests)
    assert all([t.name for t in r.tools] == ["calculator"] for r in model.requests)


@pytest.mark.asyncio
async def test_staged_manifest_reconciles_after_rename_gap(
    store: SQLiteStore, tmp_path: Path, run: Run, task: Task, monkeypatch
) -> None:
    async with store.transaction(run.user_id) as tx:
        await tx.create_run(run)
        await tx.create_task(task)
    original = SQLiteTransaction.save_artifact
    failed = False

    async def fail_commit(self, artifact, expected):
        nonlocal failed
        if artifact.status == "committed" and not failed:
            failed = True
            raise RuntimeError("crash between rename and database commit")
        await original(self, artifact, expected)

    monkeypatch.setattr(SQLiteTransaction, "save_artifact", fail_commit)
    artifacts = ArtifactService(store, tmp_path / "gap")
    invocation = invocation_for(run, task)
    with pytest.raises(RuntimeError):
        await artifacts.write(invocation, "result.txt", "42")
    async with store.transaction(run.user_id) as tx:
        manifest = (await tx.artifacts(run.run_id))[0]
    assert manifest.status == "staged"
    assert native_path(tmp_path / "gap" / manifest.relative_path).read_text() == "42"
    await artifacts.write(invocation, "result.txt", "42")
    async with store.transaction(run.user_id) as tx:
        records = await tx.artifacts(run.run_id)
    assert len(records) == 1 and records[0].status == "committed"


@pytest.mark.asyncio
async def test_expired_approval_cannot_execute(tmp_path: Path) -> None:
    from test_phase2 import MovingClock

    clock, ids = MovingClock(), SequenceIds()
    async with SQLiteStore(tmp_path / "expiry.sqlite", clock, ids) as store:
        executor = ToolExecutor(
            store,
            builtin_registry(
                ArtifactService(store, tmp_path / "files"),
                tmp_path / "files",
                tmp_path / "files",
            ),
            clock,
        )
        run = await RunService(store, clock, ids).submit(
            "alice",
            "artifact demo",
            RunOptions(routing_mode="tools", require_write_approval=True, timeout_seconds=600),
        )
        agent = AgentRunner(store, ModelGateway(offline_model()), clock, ids, executor)
        assert (await agent.execute("alice", run.run_id)).status == RunStatus.WAITING
        async with store.transaction("alice") as tx:
            pending = next(
                i
                for i in await tx.invocations(run.run_id)
                if i.status == InvocationStatus.AWAITING_APPROVAL
            )
        clock.elapsed = 301
        with pytest.raises(HarnessError):
            await decide_approval(
                store, "alice", pending.invocation_id, pending.argument_hash, True
            )
        assert (await agent.execute("alice", run.run_id)).status == RunStatus.COMPLETED
        async with store.transaction("alice") as tx:
            assert await tx.artifacts(run.run_id) == ()
