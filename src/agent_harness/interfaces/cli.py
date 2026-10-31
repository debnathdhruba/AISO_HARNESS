"""Trusted-local CLI. No command accepts model text as authority or configuration."""

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from agent_harness.bootstrap import application, runner
from agent_harness.config import Settings, load_settings
from agent_harness.domain.entities import Event, JsonObject, Run
from agent_harness.domain.errors import HarnessError
from agent_harness.domain.memory import MemoryRecord, MemorySharing
from agent_harness.domain.model import RunOptions
from agent_harness.domain.states import ControlIntent, RunStatus
from agent_harness.models.codec import encode_step
from agent_harness.ports.model import TokenCallback
from agent_harness.tools.executor import decide_approval

app = typer.Typer(no_args_is_help=True, help="Standalone agent harness: offline or local Ollama.")
memory_app = typer.Typer(no_args_is_help=True, help="Manage user-scoped agent memory.")
app.add_typer(memory_app, name="memory")


def summary(run: Run) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "user_id": run.user_id,
        "status": run.status.value,
        "version": run.version,
        "deadline": run.deadline.isoformat() if run.deadline else None,
        "models": json.loads(run.config.model.text),
        "result": json.loads(run.result.text) if run.result else None,
        "error": json.loads(run.error.text) if run.error else None,
    }


def memory_summary(record: MemoryRecord) -> dict[str, object]:
    return {
        "record_id": record.record_id,
        "user_id": record.user_id,
        "owner_agent_definition_id": record.owner_agent_definition_id,
        "sharing": record.sharing.value,
        "content": record.content,
        "tags": list(record.tags),
        "source_run_id": record.source_run_id,
        "source_task_id": record.source_task_id,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def parse_tags(value: str) -> tuple[str, ...]:
    return tuple(tag.strip() for tag in value.split(",") if tag.strip())


async def execute(
    settings: Settings,
    user: str,
    task: str | None,
    run_id: str | None,
    on_token: TokenCallback | None = None,
) -> Run:
    async with application(settings, own_scheduler=True) as services:
        if run_id is None:
            assert task is not None
            run = await services.runs.submit(user, task, settings.run_options())
        else:
            run = await services.runs.inspect(user, run_id)
            if run.status == RunStatus.PAUSED:
                run = await services.runs.resume(user, run_id)
        typer.echo(f"run_id={run.run_id}", err=True)
        if run.status in {
            RunStatus.COMPLETED,
            RunStatus.PARTIAL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.PAUSED,
        }:
            return run
        async with runner(services, settings, RunOptions.restore(run.config.model)) as agent:
            return await agent.execute(user, run.run_id, on_token=on_token)


@app.command("run")
def run_command(
    task: Annotated[str, typer.Option(help="Task input")],
    user: Annotated[str, typer.Option(help="Trusted-local principal")],
    provider: Annotated[str | None, typer.Option(help="fake (offline) or ollama")] = None,
    database_name: Annotated[
        str | None, typer.Option(help="MongoDB database name (default: test)")
    ] = None,
    config: Annotated[Path | None, typer.Option(help="TOML settings path")] = None,
    max_steps: Annotated[int | None, typer.Option()] = None,
    timeout: Annotated[int | None, typer.Option(help="Total run deadline in seconds")] = None,
    require_write_approval: Annotated[bool | None, typer.Option()] = None,
    workspace: Annotated[str | None, typer.Option()] = None,
    mode: Annotated[
        str | None, typer.Option(help="auto, chat, tools, two_stage, or router_worker")
    ] = None,
) -> None:
    settings = load_settings(
        config,
        provider=provider,
        database_name=database_name,
        max_steps=max_steps,
        timeout_seconds=timeout,
        require_write_approval=require_write_approval,
        workspace=workspace,
        routing_mode=mode,
    )
    streamed = False

    async def stream_token(token: str) -> None:
        nonlocal streamed
        if not streamed:
            typer.echo("Agent> ", nl=False, err=True)
            streamed = True
        typer.echo(token, nl=False, err=True)

    result = asyncio.run(
        execute(settings, user, task, None, stream_token if settings.provider == "ollama" else None)
    )
    if streamed:
        typer.echo(err=True)
    typer.echo(json.dumps(summary(result), indent=2))
    if result.status != RunStatus.COMPLETED:
        raise typer.Exit(1)


async def chat(settings: Settings, user: str) -> None:
    async with application(settings, own_scheduler=True) as services:
        typer.echo("Agent ready. Enter a task; type 'exit' or press Ctrl+C to stop.")
        options = settings.run_options()
        async with runner(services, settings, options) as agent:
            while True:
                try:
                    task = input("\nYou> ").strip()
                except EOFError:
                    break

                if task.lower() in {"exit", "quit"}:
                    break
                if not task:
                    continue

                try:
                    run = await services.runs.submit(user, task, options)
                    typer.echo(f"run_id={run.run_id}", err=True)
                    streamed = False

                    async def stream_token(token: str) -> None:
                        nonlocal streamed
                        if not streamed:
                            typer.echo("Agent> ", nl=False)
                            streamed = True
                        typer.echo(token, nl=False)

                    result = await agent.execute(
                        user,
                        run.run_id,
                        on_token=(stream_token if settings.provider == "ollama" else None),
                    )
                    if streamed:
                        typer.echo()
                        if result.status != RunStatus.COMPLETED:
                            typer.echo(f"Run status: {result.status.value}", err=True)
                        if result.error is not None:
                            typer.echo(
                                f"Task failed: {json.loads(result.error.text)['message']}",
                                err=True,
                            )
                    else:
                        typer.echo(
                            "Agent> "
                            + json.dumps(
                                {
                                    "status": result.status.value,
                                    "result": (
                                        json.loads(result.result.text) if result.result else None
                                    ),
                                    "error": (
                                        json.loads(result.error.text) if result.error else None
                                    ),
                                },
                                indent=2,
                            )
                        )
                except HarnessError as error:
                    typer.echo(f"Task failed: {error.message}", err=True)

        typer.echo("Agent stopped.")


@app.command("chat")
def chat_command(
    user: Annotated[str, typer.Option(help="Trusted-local principal")] = "local-user",
    config: Annotated[Path, typer.Option(help="TOML settings path")] = Path("examples/ollama.toml"),
) -> None:
    """Run tasks in an interactive loop until exit, quit, or Ctrl+C."""
    try:
        asyncio.run(chat(load_settings(config), user))
    except KeyboardInterrupt:
        typer.echo("\nAgent stopped.")


@app.command("continue")
def continue_command(
    run_id: str,
    user: Annotated[str, typer.Option()],
    database_name: Annotated[str | None, typer.Option(help="MongoDB database name")] = None,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Resume a safely recovered run using its original deadline and models."""
    result = asyncio.run(
        execute(load_settings(config, database_name=database_name), user, None, run_id)
    )
    typer.echo(json.dumps(summary(result), indent=2))
    if result.status != RunStatus.COMPLETED:
        raise typer.Exit(1)


@app.command("pause")
def pause_command(
    run_id: str,
    user: Annotated[str, typer.Option()],
    database_name: Annotated[str | None, typer.Option(help="MongoDB database name")] = None,
) -> None:
    """Persist a pause request; it takes effect at the next safe boundary."""

    async def pause() -> Run:
        async with application(load_settings(database_name=database_name)) as services:
            return await services.runs.request_control(user, run_id, ControlIntent.PAUSE)

    typer.echo(json.dumps(summary(asyncio.run(pause())), indent=2))


@app.command("cancel")
def cancel_command(
    run_id: str,
    user: Annotated[str, typer.Option()],
    database_name: Annotated[str | None, typer.Option(help="MongoDB database name")] = None,
) -> None:
    """Persist cancellation; recovery completes it if the worker has stopped."""

    async def cancel() -> Run:
        async with application(load_settings(database_name=database_name)) as services:
            return await services.runs.request_control(user, run_id, ControlIntent.CANCEL)

    typer.echo(json.dumps(summary(asyncio.run(cancel())), indent=2))


@app.command("resume")
def resume_command(
    run_id: str,
    user: Annotated[str, typer.Option()],
    database_name: Annotated[str | None, typer.Option(help="MongoDB database name")] = None,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Resume a paused run after startup recovery and reconciliation."""
    result = asyncio.run(
        execute(load_settings(config, database_name=database_name), user, None, run_id)
    )
    typer.echo(json.dumps(summary(result), indent=2))
    if result.status not in {RunStatus.COMPLETED, RunStatus.PARTIAL}:
        raise typer.Exit(1)


@app.command("recover")
def recover_command(
    run_id: str,
    user: Annotated[str, typer.Option()],
    database_name: Annotated[str | None, typer.Option(help="MongoDB database name")] = None,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Run startup recovery and show decisions without resuming the selected run."""

    async def recover() -> dict[str, object]:
        settings = load_settings(config, database_name=database_name)
        async with application(settings, own_scheduler=True) as services:
            await services.runs.inspect(user, run_id)
        return await inspect_run(settings, user, run_id, history=False)

    typer.echo(json.dumps(asyncio.run(recover()), indent=2))


@app.command("reconcile")
def reconcile_command(
    invocation_id: str,
    user: Annotated[str, typer.Option()],
    outcome: Annotated[str, typer.Option(help="Observed side-effect outcome: succeeded or failed")],
    database_name: Annotated[str | None, typer.Option(help="MongoDB database name")] = None,
    result_json: Annotated[
        str | None, typer.Option(help="Observed JSON object result when outcome=succeeded")
    ] = None,
    error_message: Annotated[
        str | None, typer.Option(help="Observed failure description when outcome=failed")
    ] = None,
) -> None:
    """Resolve an unknown write only after independently checking its real outcome."""
    if outcome not in {"succeeded", "failed"}:
        raise typer.BadParameter("Outcome must be succeeded or failed")
    result = JsonObject(result_json) if result_json is not None else None
    error = (
        JsonObject(json.dumps({"code": "tool_execution", "message": error_message}))
        if error_message is not None
        else None
    )

    async def reconcile() -> None:
        async with application(load_settings(database_name=database_name)) as services:
            async with services.store.transaction(user) as tx:
                updated = await tx.reconcile_invocation(
                    invocation_id,
                    succeeded=outcome == "succeeded",
                    result=result,
                    error=error,
                )
        typer.echo(
            json.dumps(
                {
                    "invocation_id": updated.invocation_id,
                    "status": updated.status.value,
                },
                indent=2,
            )
        )

    asyncio.run(reconcile())


async def inspect_run(
    settings: Settings, user: str, run_id: str, history: bool
) -> dict[str, object]:
    async with application(settings) as services:
        run = await services.runs.inspect(user, run_id)
        result = summary(run)
        async with services.store.transaction(user) as tx:
            steps = await tx.steps(run_id)
            invocations = await tx.invocations(run_id)
            artifacts = await tx.artifacts(run_id)
            tasks = await tx.tasks(run_id)
            root_task_id = str(json.loads(run.config.policy.text)["root_task_id"])
            budget = await tx.budget_snapshot(root_task_id)
        result["budget"] = {
            "limits": budget.limits.document(),
            "used": budget.used.document(),
            "reserved": budget.reserved.document(),
            "unknown_usage_count": budget.unknown_usage_count,
        }
        result["invocations"] = [
            {
                "invocation_id": i.invocation_id,
                "tool": i.tool_name,
                "version": i.tool_version,
                "step_id": i.step_id,
                "call_id": i.call_id,
                "argument_hash": i.argument_hash,
                "arguments": json.loads(i.arguments.text),
                "status": i.status.value,
                "result": json.loads(i.result.text) if i.result else None,
                "error": json.loads(i.error.text) if i.error else None,
                "approval_expires_at": i.approval_expires_at.isoformat()
                if i.approval_expires_at
                else None,
            }
            for i in invocations
        ]
        result["artifacts"] = [
            {
                "artifact_id": a.artifact_id,
                "name": a.name,
                "version": a.version,
                "status": a.status,
                "checksum": a.checksum,
            }
            for a in artifacts
        ]
        result["tasks"] = [
            {
                "task_id": task.task_id,
                "parent_task_id": task.parent_task_id,
                "dependencies": task.dependencies,
                "agent_instance_id": task.agent_instance_id,
                "agent_definition": f"{task.agent_definition_id}@{task.agent_definition_version}",
                "depth": task.depth,
                "status": task.status.value,
                "required": task.required,
                "capabilities": task.capabilities,
                "result": json.loads(task.result.text) if task.result else None,
                "error": json.loads(task.error.text) if task.error else None,
                "handoff": json.loads(task.handoff.text) if history else None,
            }
            for task in tasks
        ]
        result["steps"] = [
            json.loads(encode_step(step))["data"]
            if history
            else {
                "ordinal": step.ordinal,
                "model": step.request.model,
                "stage": step.stage.value,
                "status": step.status.value,
                "usage": {
                    "input_tokens": step.response.usage.input_tokens,
                    "output_tokens": step.response.usage.output_tokens,
                }
                if step.response
                else None,
            }
            for step in steps
        ]
        return result


@app.command("inspect")
def inspect_command(
    run_id: str,
    user: Annotated[str, typer.Option()],
    database_name: Annotated[str | None, typer.Option(help="MongoDB database name")] = None,
    config: Annotated[Path | None, typer.Option()] = None,
    history: Annotated[
        bool, typer.Option(help="Include private persisted prompts and responses")
    ] = False,
) -> None:
    value = asyncio.run(
        inspect_run(load_settings(config, database_name=database_name), user, run_id, history)
    )
    typer.echo(json.dumps(value, indent=2))


@app.command("trace")
def trace_command(
    run_id: str,
    user: Annotated[str, typer.Option(help="Trusted-local principal")],
    database_name: Annotated[str | None, typer.Option(help="MongoDB database name")] = None,
    config: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Show the persisted model, tool and lifecycle trace for one run."""

    async def read() -> dict[str, object]:
        settings = load_settings(config, database_name=database_name)
        value = await inspect_run(settings, user, run_id, history=True)
        async with application(settings) as services:
            async with services.store.transaction(user) as tx:
                events: list[Event] = []
                after = 0
                while True:
                    batch = await tx.events(run_id, after=after, limit=1000)
                    events.extend(batch)
                    if len(batch) < 1000:
                        break
                    after = batch[-1].sequence
        value["events"] = [
            {
                "sequence": event.sequence,
                "type": event.event_type,
                "task_id": event.task_id,
                "timestamp": event.timestamp.isoformat(),
                "payload": json.loads(event.payload.text),
            }
            for event in events
        ]
        return value

    typer.echo(json.dumps(asyncio.run(read()), indent=2))


@app.command("events")
def events_command(
    run_id: str,
    user: Annotated[str, typer.Option()],
    database_name: Annotated[str | None, typer.Option(help="MongoDB database name")] = None,
    after: Annotated[int, typer.Option()] = 0,
) -> None:
    async def read() -> list[dict[str, object]]:
        async with application(load_settings(database_name=database_name)) as services:
            await services.runs.inspect(user, run_id)
            async with services.store.transaction(user) as tx:
                events = await tx.events(run_id, after=after)
            return [
                {
                    "sequence": e.sequence,
                    "type": e.event_type,
                    "payload": json.loads(e.payload.text),
                }
                for e in events
            ]

    typer.echo(json.dumps(asyncio.run(read()), indent=2))


def approval_command(
    invocation_id: str, user: str, digest: str, database_name: str | None, approve: bool
) -> None:
    async def decide() -> None:
        async with application(load_settings(database_name=database_name)) as services:
            await decide_approval(services.store, user, invocation_id, digest, approve)

    asyncio.run(decide())
    typer.echo("Approval recorded. Use continue RUN_ID to advance the run.")


@app.command("approve")
def approve_command(
    invocation_id: str,
    user: Annotated[str, typer.Option()],
    argument_hash: Annotated[str, typer.Option()],
    database_name: Annotated[str | None, typer.Option(help="MongoDB database name")] = None,
) -> None:
    """Approve the exact invocation and argument hash shown by inspect."""
    approval_command(invocation_id, user, argument_hash, database_name, True)


@app.command("deny")
def deny_command(
    invocation_id: str,
    user: Annotated[str, typer.Option()],
    argument_hash: Annotated[str, typer.Option()],
    database_name: Annotated[str | None, typer.Option(help="MongoDB database name")] = None,
) -> None:
    """Deny an invocation; continuation feeds the denial back to the agent."""
    approval_command(invocation_id, user, argument_hash, database_name, False)


@memory_app.command("save")
def memory_save_command(
    content: Annotated[str, typer.Option(help="Fact explicitly selected for memory")],
    user: Annotated[str, typer.Option(help="Trusted-local principal")],
    agent: Annotated[str, typer.Option(help="Owning agent definition, e.g. coordinator")],
    sharing: Annotated[
        MemorySharing,
        typer.Option(help="private for this agent; shared for this user's agents"),
    ] = MemorySharing.PRIVATE,
    tags: Annotated[str, typer.Option(help="Comma-separated retrieval tags")] = "",
    database_name: Annotated[str | None, typer.Option(help="MongoDB database name")] = None,
) -> None:
    async def save() -> MemoryRecord:
        async with application(load_settings(database_name=database_name)) as services:
            return await services.memory.create(
                user,
                agent,
                content,
                tags=parse_tags(tags),
                sharing=sharing,
            )

    typer.echo(json.dumps(memory_summary(asyncio.run(save())), indent=2))


@memory_app.command("get")
def memory_get_command(
    record_id: str,
    user: Annotated[str, typer.Option(help="Trusted-local principal")],
    agent: Annotated[str, typer.Option(help="Agent definition requesting access")],
    database_name: Annotated[str | None, typer.Option(help="MongoDB database name")] = None,
) -> None:
    async def read() -> MemoryRecord:
        async with application(load_settings(database_name=database_name)) as services:
            return await services.memory.get(user, agent, record_id)

    typer.echo(json.dumps(memory_summary(asyncio.run(read())), indent=2))


@memory_app.command("search")
def memory_search_command(
    query: Annotated[str, typer.Option(help="Lexical query text")],
    user: Annotated[str, typer.Option(help="Trusted-local principal")],
    agent: Annotated[str, typer.Option(help="Agent definition requesting access")],
    limit: Annotated[int, typer.Option(min=1, max=20)] = 5,
    database_name: Annotated[str | None, typer.Option(help="MongoDB database name")] = None,
) -> None:
    async def search() -> tuple[MemoryRecord, ...]:
        async with application(load_settings(database_name=database_name)) as services:
            return await services.memory.retrieve(user, agent, query, limit=limit)

    typer.echo(json.dumps([memory_summary(record) for record in asyncio.run(search())], indent=2))


@memory_app.command("update")
def memory_update_command(
    record_id: str,
    content: Annotated[str, typer.Option(help="Replacement memory content")],
    user: Annotated[str, typer.Option(help="Trusted-local principal")],
    agent: Annotated[str, typer.Option(help="Owning agent definition")],
    sharing: Annotated[MemorySharing, typer.Option()] = MemorySharing.PRIVATE,
    tags: Annotated[str, typer.Option(help="Replacement comma-separated tags")] = "",
    database_name: Annotated[str | None, typer.Option(help="MongoDB database name")] = None,
) -> None:
    async def update() -> MemoryRecord:
        async with application(load_settings(database_name=database_name)) as services:
            return await services.memory.update(
                user,
                agent,
                record_id,
                content=content,
                sharing=sharing,
                tags=parse_tags(tags),
            )

    typer.echo(json.dumps(memory_summary(asyncio.run(update())), indent=2))


@memory_app.command("delete")
def memory_delete_command(
    record_id: str,
    user: Annotated[str, typer.Option(help="Trusted-local principal")],
    agent: Annotated[str, typer.Option(help="Owning agent definition")],
    database_name: Annotated[str | None, typer.Option(help="MongoDB database name")] = None,
) -> None:
    async def delete() -> None:
        async with application(load_settings(database_name=database_name)) as services:
            await services.memory.delete(user, agent, record_id)

    asyncio.run(delete())
    typer.echo(json.dumps({"record_id": record_id, "deleted": True}))


def main() -> None:
    try:
        app()
    except HarnessError as error:
        typer.echo(
            json.dumps(
                {
                    "error": {
                        "code": error.code.value,
                        "message": error.message,
                        "retryable": error.retryable,
                    }
                }
            ),
            err=True,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
