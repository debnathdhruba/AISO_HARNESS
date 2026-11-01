import json

import pytest
from conftest import FixedClock, SequenceIds

from agent_harness.adapters.fake import offline_model
from agent_harness.agent_orchestration_core.automatic import decompose, should_spawn
from agent_harness.agent_orchestration_core.paging import ContextPager
from agent_harness.agent_orchestration_core.planning import PlanCompiler
from agent_harness.agent_orchestration_core.runtime import KernelPlanRuntime
from agent_harness.agents.factory import AgentFactory, local_agent_registry
from agent_harness.agents.runner import AgentRunner
from agent_harness.agents.scheduler import TaskScheduler
from agent_harness.application.agent_orchestration_core import AgentOrchestrationCoreService
from agent_harness.application.coordinator import TaskCoordinator
from agent_harness.application.runs import RunService
from agent_harness.domain.entities import JsonObject, Task
from agent_harness.domain.errors import HarnessError
from agent_harness.domain.kernel import ExecutionPlan, PlanTask
from agent_harness.domain.model import RunOptions
from agent_harness.domain.states import RunStatus
from agent_harness.memory.backends.sqlite import SQLiteStore
from agent_harness.memory.short_term import KernelMemory
from agent_harness.models.gateway import ModelGateway


def compiler(*, pages: tuple[str, ...] = ("page-1",)) -> PlanCompiler:
    return PlanCompiler(
        local_agent_registry(),
        ("calculator", "read_file", "write_file", "list_files"),
        pages,
    )


def proposal(tasks: list[dict[str, object]]) -> JsonObject:
    return JsonObject(json.dumps({"plan_id": "review", "version": 1, "tasks": tasks}))


def test_compiler_accepts_acyclic_authorized_plan() -> None:
    plan = compiler().compile(
        proposal(
            [
                {
                    "alias": "scan",
                    "objective": "Scan the supplied page.",
                    "input_pages": ["page-1"],
                    "capabilities": ["list_files"],
                },
                {
                    "alias": "sum",
                    "objective": "Summarize the scan.",
                    "depends_on": ["scan"],
                },
            ]
        )
    )

    assert tuple(task.alias for task in plan.ordered()) == ("scan", "sum")
    assert json.loads(plan.document().text)["plan_id"] == "review"


@pytest.mark.parametrize(
    "tasks",
    [
        [
            {"alias": "a", "objective": "A", "depends_on": ["b"]},
            {"alias": "b", "objective": "B", "depends_on": ["a"]},
        ],
        [{"alias": "a", "objective": "A", "input_pages": ["missing"]}],
        [{"alias": "a", "objective": "A", "capabilities": ["write_file"]}],
        [{"alias": "a", "objective": "A", "result_schema": {"type": 3}}],
        [{"alias": "a", "objective": "A", "unexpected": True}],
    ],
)
def test_compiler_rejects_invalid_or_unauthorized_plans(tasks: list[dict[str, object]]) -> None:
    with pytest.raises(HarnessError):
        compiler().compile(proposal(tasks))


def test_pager_respects_utf8_limit_and_retains_overlap() -> None:
    pages = ContextPager(256, 32).page(("first paragraph.\n\n" * 30) + "€" * 100)

    assert len(pages) > 1
    assert all(len(page.text.encode("utf-8")) <= 256 for page in pages)
    assert pages[0].byte_start == 0
    assert all(
        left.byte_end >= right.byte_start for left, right in zip(pages, pages[1:], strict=False)
    )


def test_kernel_specs_preassign_pages_and_capabilities() -> None:
    plan = ExecutionPlan(
        "plan",
        (
            PlanTask(
                "worker",
                "Read the supplied page.",
                input_pages=("page-1",),
                capabilities=("list_files",),
            ),
        ),
    )
    pages = ContextPager(256).page("source material")
    specs = KernelPlanRuntime.specs(plan, pages)

    assert specs[0].capabilities == ("list_files",)
    assert specs[0].enforce_capabilities is True
    handoff = json.loads(specs[0].handoff.text)
    assert handoff["kernel"]["plan_id"] == "plan"
    assert handoff["memory_keys"] == ["page:page-1"]


@pytest.mark.asyncio
async def test_kernel_memory_is_immutable_and_capability_scoped() -> None:
    memory = KernelMemory()
    await memory.put("result:research", JsonObject('{"answer":"42"}'), readers=("writer",))

    assert json.loads((await memory.read("writer", "result:research")).text)["answer"] == "42"
    with pytest.raises(HarnessError):
        await memory.read("unrelated", "result:research")
    with pytest.raises(HarnessError):
        await memory.put("result:research", JsonObject(), readers=("writer",))


def test_kernel_specs_reject_pages_that_exceed_the_task_frame() -> None:
    plan = ExecutionPlan("plan", (PlanTask("worker", "Read page.", input_pages=("page-1",)),))
    pages = ContextPager(256).page("x" * 256)

    with pytest.raises(HarnessError, match="bounded input frame"):
        KernelPlanRuntime.specs(plan, pages, max_input_bytes=256)


def test_automatic_admission_decomposes_oversized_input_into_parallel_workers() -> None:
    options = RunOptions(max_input_bytes=4096, auto_kernel_trigger_bytes=1024)
    source = "paragraph.\n\n" * 700

    assert should_spawn(source, options) is True
    automatic = decompose(source, options)

    assert len(automatic.pages) == len(automatic.plan.tasks)
    assert len(automatic.plan.tasks) > 1
    assert all(not task.depends_on for task in automatic.plan.tasks)
    assert automatic.objective == source[:1024]


def test_automatic_admission_keeps_small_tasks_on_single_agent_path() -> None:
    assert should_spawn("Explain FIFO.", RunOptions(auto_kernel_trigger_bytes=1024)) is False


@pytest.mark.asyncio
async def test_kernel_service_runs_preplanned_workers_then_root_reducer(store: SQLiteStore) -> None:
    clock, ids = FixedClock(), SequenceIds()
    scheduler = TaskScheduler(concurrency=2, queue_capacity=8)
    factory = local_agent_registry()
    coordinator = TaskCoordinator(store, clock, ids, AgentFactory(factory), scheduler)
    service = AgentOrchestrationCoreService(store, coordinator)
    options = RunOptions(routing_mode="chat")
    run = await RunService(store, clock, ids).submit("alice", "Summarize pages", options)
    agent = AgentRunner(store, ModelGateway(offline_model()), clock, ids)
    plan = ExecutionPlan(
        "summary",
        (
            PlanTask("leaf", "Read this page.", input_pages=("page-1",)),
            PlanTask("reduce", "Use the leaf finding.", depends_on=("leaf",)),
        ),
    )
    pages = ContextPager(256).page("A short source document.")
    observed_handoffs: dict[str, dict[str, object]] = {}

    async def execute_child(task: Task) -> Task:
        task_id = task.task_id
        async with store.transaction("alice") as tx:
            current = await tx.get_task(task_id)
        assert current is not None
        observed_handoffs[current.objective] = json.loads(current.handoff.text)
        await agent.execute_task("alice", run.run_id, task_id)
        async with store.transaction("alice") as tx:
            result = await tx.get_task(task_id)
        assert result is not None
        return result

    try:
        result = await service.execute(
            "alice", run.run_id, plan, pages, execute_child, agent.execute_task
        )
    finally:
        await scheduler.shutdown()

    assert result.status == RunStatus.COMPLETED
    dependent = observed_handoffs["Use the leaf finding."]
    assert dependent["dependency_results"][0]["status"] == "succeeded"
