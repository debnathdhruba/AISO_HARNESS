"""Validate and persist intent before effects; resume never blindly replays writes."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta
from uuid import UUID, uuid5

from agent_harness.domain.budgets import BudgetUsage
from agent_harness.domain.entities import JsonObject
from agent_harness.domain.errors import ErrorCode, HarnessError, PauseRequested, require
from agent_harness.domain.model import Message, Role, RunOptions, Step
from agent_harness.domain.states import InvocationStatus
from agent_harness.domain.tooling import Approval, Effect, Invocation, argument_hash
from agent_harness.logger import audit
from agent_harness.protocols.state import Clock, StateStore
from agent_harness.scheduling import (
    BlockingToolAdapter,
    ResourceAdmissionCancelled,
    ResourceAdmissionError,
    ResourceKind,
    ResourceScheduler,
)
from agent_harness.tools.registry import RegisteredTool, ToolRegistry

def error_payload(error: HarnessError) -> JsonObject:
    return JsonObject(json.dumps({"code": error.code.value, "message": error.message}))


class ToolExecutor:
    def __init__(
        self,
        store: StateStore,
        registry: ToolRegistry,
        clock: Clock,
        scheduler: ResourceScheduler | None = None,
        blocking_tools: BlockingToolAdapter | None = None,
        ownership_check: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.store, self.registry, self.clock = store, registry, clock
        self.scheduler = scheduler or ResourceScheduler()
        self.blocking_tools = blocking_tools or BlockingToolAdapter()
        self.ownership_check = ownership_check

    async def _change(
        self,
        invocation: Invocation,
        status: InvocationStatus,
        *,
        result: JsonObject | None = None,
        error: HarnessError | None = None,
    ) -> Invocation:
        updated = replace(
            invocation, status=status, result=result, error=error_payload(error) if error else None
        )
        async with self.store.transaction(invocation.user_id) as tx:
            await tx.save_invocation(updated, invocation.status)
        return updated

    async def _plan(self, step: Step, options: RunOptions) -> tuple[Invocation, ...]:
        assert step.response is not None
        async with self.store.transaction(step.user_id) as tx:
            prior = await tx.invocations(step.run_id)
            existing = tuple(i for i in prior if i.step_id == step.step_id)
            if existing:
                require(
                    len(existing) == len(step.response.message.tool_calls), "Incomplete tool intent"
                )
                return existing
            if len(prior) + len(step.response.message.tool_calls) > options.max_tool_calls:
                raise HarnessError(ErrorCode.BUDGET_EXCEEDED, "Tool call budget exhausted")
            records: list[Invocation] = []
            supplied = {spec.name: spec for spec in step.request.tools}
            for call in step.response.message.tool_calls:
                spec = supplied.get(call.name)
                digest = argument_hash(call.arguments)
                needs_approval = (
                    options.require_write_approval
                    and spec is not None
                    and spec.effect != Effect.READ_ONLY
                )
                invocation = Invocation(
                    str(uuid5(UUID(step.step_id), call.call_id)),
                    step.run_id,
                    step.task_id,
                    step.user_id,
                    step.step_id,
                    call.call_id,
                    call.name,
                    spec.version if spec else "unregistered",
                    call.arguments,
                    digest,
                    spec.effect if spec else Effect.READ_ONLY,
                    approval_expires_at=self.clock.utc_now() + timedelta(minutes=5)
                    if needs_approval
                    else None,
                )
                await tx.save_invocation(invocation, None)
                try:
                    if spec is None or call.name not in options.tool_allowlist:
                        raise HarnessError(ErrorCode.AUTHORIZATION, "Tool is not permitted")
                    registered = self.registry.get(spec.name, spec.version)
                    require(registered.spec == spec, "Registered tool contract changed")
                    self.registry.validate(call.arguments, spec.input_schema)
                    repeated = sum(
                        i.tool_name == call.name and i.argument_hash == digest
                        for i in (*prior, *records)
                    )
                    if repeated >= options.repeated_action_limit:
                        raise HarnessError(
                            ErrorCode.BUDGET_EXCEEDED, "Repeated action limit exceeded"
                        )
                    updated = replace(
                        invocation,
                        status=InvocationStatus.AWAITING_APPROVAL
                        if needs_approval
                        else InvocationStatus.READY,
                    )
                except HarnessError as error:
                    updated = replace(
                        invocation, status=InvocationStatus.FAILED, error=error_payload(error)
                    )
                await tx.save_invocation(updated, InvocationStatus.PLANNED)
                records.append(updated)
                audit(
                    "tool.invocation.planned",
                    run_id=step.run_id,
                    task_id=step.task_id,
                    invocation_id=invocation.invocation_id,
                    tool=invocation.tool_name,
                    status=updated.status.value,
                    approval_required=needs_approval,
                )
        return tuple(records)

    async def process(self, step: Step, options: RunOptions) -> bool:
        """Return false only while explicit human approval is required."""
        invocations = await self._plan(step, options)
        for invocation in invocations:
            if invocation.status in {
                InvocationStatus.SUCCEEDED,
                InvocationStatus.FAILED,
                InvocationStatus.CANCELLED,
            }:
                continue
            if invocation.status == InvocationStatus.EXECUTING:
                error = HarnessError(
                    ErrorCode.UNCERTAIN_SIDE_EFFECT,
                    "Interrupted tool attempt requires reconciliation",
                )
                invocation = await self._change(
                    invocation,
                    InvocationStatus.FAILED
                    if invocation.effect == Effect.READ_ONLY
                    else InvocationStatus.UNKNOWN,
                    error=error,
                )
            if invocation.status == InvocationStatus.UNKNOWN:
                raise HarnessError(
                    ErrorCode.UNCERTAIN_SIDE_EFFECT,
                    "Tool outcome is unknown; automatic replay is forbidden",
                )
            if invocation.status == InvocationStatus.FAILED:
                continue
            if invocation.status == InvocationStatus.AWAITING_APPROVAL:
                if (
                    invocation.approval_expires_at is not None
                    and self.clock.utc_now() >= invocation.approval_expires_at
                ):
                    await self._change(
                        invocation,
                        InvocationStatus.FAILED,
                        error=HarnessError(ErrorCode.AUTHORIZATION, "Tool approval expired"),
                    )
                    continue
                return False
            if invocation.approval_expires_at is not None:
                async with self.store.transaction(step.user_id) as tx:
                    approval = await tx.get_approval(invocation.invocation_id)
                if not (
                    approval
                    and approval.approved
                    and approval.user_id == step.user_id
                    and approval.tool_version == invocation.tool_version
                    and approval.argument_hash == invocation.argument_hash
                    and approval.expires_at == invocation.approval_expires_at
                    and approval.expires_at > self.clock.utc_now()
                ):
                    await self._change(
                        invocation,
                        InvocationStatus.FAILED,
                        error=HarnessError(
                            ErrorCode.AUTHORIZATION, "Approval is missing, changed or expired"
                        ),
                    )
                    continue
            tool = self.registry.get(invocation.tool_name, invocation.tool_version)
            require(
                any(spec == tool.spec for spec in step.request.tools),
                "Tool contract changed after the decision was accepted",
            )
            self.registry.validate(invocation.arguments, tool.spec.input_schema)
            if self.ownership_check is not None:
                await self.ownership_check()
            async with self.store.transaction(step.user_id) as tx:
                run = await tx.get_run(step.run_id)
            assert run is not None and run.deadline is not None
            if run.intent.value == "cancel":
                raise HarnessError(
                    ErrorCode.CANCELLATION, "Run cancellation requested before tool execution"
                )
            if run.intent.value == "pause":
                raise PauseRequested()
            remaining = (run.deadline - self.clock.utc_now()).total_seconds()
            if remaining <= 0:
                raise HarnessError(ErrorCode.DEADLINE_EXCEEDED, "Run deadline exceeded before tool")
            reservation_id = f"tool:{invocation.invocation_id}"
            async with self.store.transaction(step.user_id) as tx:
                await tx.reserve_budget(
                    reservation_id,
                    invocation.task_id,
                    BudgetUsage(steps=0, tokens=0, tool_calls=1),
                )
                await tx.reconcile_budget(
                    reservation_id,
                    BudgetUsage(steps=0, tokens=0, tool_calls=1),
                )
            invocation = await self._change(invocation, InvocationStatus.EXECUTING)
            audit(
                "tool.execution_started",
                run_id=step.run_id,
                task_id=step.task_id,
                invocation_id=invocation.invocation_id,
                tool=invocation.tool_name,
            )
            execution_started = False
            try:
                if self.ownership_check is not None:
                    await self.ownership_check()
                async with self.store.transaction(step.user_id) as tx:
                    current_run = await tx.get_run(step.run_id)
                assert current_run is not None
                if current_run.intent.value == "cancel":
                    raise HarnessError(
                        ErrorCode.CANCELLATION,
                        "Run cancellation requested before tool execution",
                    )
                if current_run.intent.value == "pause":
                    raise PauseRequested()

                async def invoke(
                    tool: RegisteredTool = tool,
                    invocation: Invocation = invocation,
                ) -> JsonObject:
                    nonlocal execution_started
                    execution_started = True
                    if tool.blocking_execute is not None:
                        return await self.blocking_tools.run(tool.blocking_execute, invocation)
                    assert tool.execute is not None
                    return await tool.execute(invocation)

                timeout = min(remaining, tool.spec.timeout_seconds)
                output = await self.scheduler.run(
                    ResourceKind.TOOL, invoke, key=tool.spec.name, timeout=timeout
                )
                if self.ownership_check is not None:
                    await self.ownership_check()
                self.registry.validate(output, tool.spec.output_schema)
                require(
                    len(output.text.encode("utf-8")) <= tool.spec.max_output_bytes,
                    "Tool output exceeds size limit",
                )
            except PauseRequested:
                if invocation.status == InvocationStatus.EXECUTING:
                    await self._change(invocation, InvocationStatus.READY)
                raise
            except ResourceAdmissionError as caught:
                await self._change(
                    invocation,
                    InvocationStatus.CANCELLED
                    if caught.code == ErrorCode.CANCELLATION
                    else InvocationStatus.FAILED,
                    error=caught,
                )
                raise
            except BaseException as caught:
                if not isinstance(caught, (Exception, asyncio.CancelledError)):
                    raise
                error = (
                    caught
                    if isinstance(caught, HarnessError)
                    else HarnessError(
                        ErrorCode.DEADLINE_EXCEEDED
                        if isinstance(caught, TimeoutError)
                        else ErrorCode.CANCELLATION
                        if isinstance(caught, asyncio.CancelledError)
                        else ErrorCode.TOOL_EXECUTION,
                        "Tool did not complete successfully",
                    )
                )
                admission_cancelled = isinstance(caught, ResourceAdmissionCancelled)
                pre_execution_cancel = (
                    isinstance(caught, HarnessError)
                    and caught.code == ErrorCode.CANCELLATION
                    and not execution_started
                )
                uncertain = (
                    invocation.effect != Effect.READ_ONLY
                    and execution_started
                    and not admission_cancelled
                )
                await self._change(
                    invocation,
                    InvocationStatus.UNKNOWN
                    if uncertain
                    else InvocationStatus.CANCELLED
                    if admission_cancelled or pre_execution_cancel
                    else InvocationStatus.FAILED,
                    error=error,
                )
                if isinstance(caught, asyncio.CancelledError):
                    raise
                if uncertain:
                    raise HarnessError(
                        ErrorCode.UNCERTAIN_SIDE_EFFECT,
                        "Write outcome needs reconciliation; it will not be replayed",
                    ) from None
                continue
            await self._change(invocation, InvocationStatus.SUCCEEDED, result=output)
            audit(
                "tool.execution_succeeded",
                run_id=step.run_id,
                task_id=step.task_id,
                invocation_id=invocation.invocation_id,
                tool=invocation.tool_name,
                output_bytes=len(output.text.encode("utf-8")),
            )
        return True

    async def observations(self, step: Step) -> tuple[Message, ...]:
        async with self.store.transaction(step.user_id) as tx:
            records = {
                i.call_id: i for i in await tx.invocations(step.run_id) if i.step_id == step.step_id
            }
        assert step.response is not None
        messages = []
        for call in step.response.message.tool_calls:
            record = records[call.call_id]
            content = JsonObject(
                json.dumps(
                    {
                        "status": record.status,
                        "result": json.loads(record.result.text) if record.result else None,
                        "error": json.loads(record.error.text) if record.error else None,
                    }
                )
            ).text
            messages.append(Message(Role.TOOL, content, tool_call_id=call.call_id))
        return tuple(messages)


async def decide_approval(
    store: StateStore, user_id: str, invocation_id: str, argument_digest: str, approved: bool
) -> None:
    async with store.transaction(user_id) as tx:
        invocation = await tx.get_invocation(invocation_id)
        require(
            invocation is not None and invocation.approval_expires_at is not None,
            "Invocation is not awaiting approval",
        )
        assert invocation is not None and invocation.approval_expires_at is not None
        await tx.save_approval(
            Approval(
                invocation_id,
                user_id,
                invocation.tool_version,
                argument_digest,
                approved,
                invocation.approval_expires_at,
            )
        )
