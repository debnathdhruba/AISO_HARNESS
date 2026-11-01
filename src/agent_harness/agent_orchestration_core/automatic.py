"""Deterministic admission and fallback decomposition for oversized requests."""

from dataclasses import dataclass

from agent_harness.agent_orchestration_core.paging import ContextPager, PageFrame
from agent_harness.domain.errors import require
from agent_harness.domain.kernel import ExecutionPlan, PlanTask
from agent_harness.domain.model import RunOptions
from agent_harness.logger import audit


@dataclass(frozen=True)
class AutomaticPlan:
    plan: ExecutionPlan
    pages: tuple[PageFrame, ...]
    objective: str


def should_spawn(task_input: str, options: RunOptions) -> bool:
    """Conservative automatic admission; small tasks retain the single-agent fast path."""
    selected = options.auto_kernel_enabled and len(task_input.encode("utf-8")) >= min(
        options.auto_kernel_trigger_bytes, options.max_input_bytes
    )
    audit(
        "kernel.admission.checked",
        input_bytes=len(task_input.encode("utf-8")),
        threshold=options.auto_kernel_trigger_bytes,
        admitted=selected,
    )
    return selected


def decompose(
    task_input: str,
    options: RunOptions,
    *,
    objective: str | None = None,
    force: bool = False,
) -> AutomaticPlan:
    """Create independent page workers when no user-supplied plan is necessary."""
    require(force or should_spawn(task_input, options), "Automatic admission is not required")
    pages = ContextPager(max(256, options.max_input_bytes // 3), overlap_bytes=128).page(task_input)
    require(
        1 <= len(pages) <= 8,
        "Automatic agent_orchestration_core plan exceeds worker limit",
    )
    tasks = tuple(
        PlanTask(
            alias=f"page_{index}",
            objective=(
                "Analyze the assigned context page for this objective: "
                f"{(objective or task_input[:512]).strip()}\n"
                "Return concise relevant findings and preserve important facts, constraints, "
                "paths, values, and evidence for the final synthesizer."
            ),
            input_pages=(page.page_id,),
        )
        for index, page in enumerate(pages, start=1)
    )
    objective_excerpt = (
        (objective or task_input).encode("utf-8")[:4096].decode("utf-8", errors="ignore")
    )
    result = AutomaticPlan(ExecutionPlan("automatic", tasks), pages, objective_excerpt)
    audit("kernel.plan.created", plan_id="automatic", task_count=len(tasks), page_count=len(pages))
    return result
