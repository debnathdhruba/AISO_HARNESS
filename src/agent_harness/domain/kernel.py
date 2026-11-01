"""Pure contracts for deterministic agent_orchestration_core plans and result envelopes."""

import json
import re
from dataclasses import dataclass, field

from agent_harness.domain.entities import JsonObject
from agent_harness.domain.errors import require

_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")


@dataclass(frozen=True)
class PlanTask:
    """A proposed unit of work; it has no authority until the kernel validates it."""

    alias: str
    objective: str
    agent_definition_id: str = "worker"
    agent_version: str = "1"
    depends_on: tuple[str, ...] = ()
    input_pages: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    acceptance_criteria: JsonObject = field(default_factory=JsonObject)
    result_schema: JsonObject = field(default_factory=JsonObject)
    max_steps: int | None = None

    def __post_init__(self) -> None:
        require(bool(_ALIAS.fullmatch(self.alias)), "Plan task alias is invalid")
        require(
            bool(self.objective.strip()) and len(self.objective) <= 8_000,
            "Task objective is invalid",
        )
        require(
            bool(self.agent_definition_id.strip() and self.agent_version.strip()),
            "Plan task agent identity is required",
        )
        require(self.alias not in self.depends_on, "Plan task cannot depend on itself")
        require(len(set(self.depends_on)) == len(self.depends_on), "Duplicate plan dependency")
        require(len(set(self.input_pages)) == len(self.input_pages), "Duplicate input page")
        require(len(self.input_pages) <= 16, "Too many input pages")
        require(len(set(self.capabilities)) == len(self.capabilities), "Duplicate capability")
        require(
            self.max_steps is None or 1 <= self.max_steps <= 100,
            "Plan task step limit must be 1..100",
        )


@dataclass(frozen=True)
class ExecutionPlan:
    """A bounded, acyclic and immutable logical task graph."""

    plan_id: str
    tasks: tuple[PlanTask, ...]
    version: int = 1

    def __post_init__(self) -> None:
        require(bool(_ALIAS.fullmatch(self.plan_id)), "Plan identifier is invalid")
        require(1 <= len(self.tasks) <= 8, "Plan must contain 1..8 tasks")
        require(self.version == 1, "Unsupported plan version")
        aliases = {task.alias for task in self.tasks}
        require(len(aliases) == len(self.tasks), "Plan task aliases must be unique")
        require(
            all(dependency in aliases for task in self.tasks for dependency in task.depends_on),
            "Plan dependency does not exist",
        )
        self._assert_acyclic()

    def _assert_acyclic(self) -> None:
        remaining = {task.alias: set(task.depends_on) for task in self.tasks}
        resolved: set[str] = set()
        while remaining:
            ready = {alias for alias, dependencies in remaining.items() if dependencies <= resolved}
            require(bool(ready), "Plan dependencies contain a cycle")
            resolved.update(ready)
            for alias in ready:
                del remaining[alias]

    def ordered(self) -> tuple[PlanTask, ...]:
        """Stable topological order, preserving declared order among ready nodes."""
        pending = list(self.tasks)
        resolved: set[str] = set()
        ordered: list[PlanTask] = []
        while pending:
            ready = next((task for task in pending if set(task.depends_on) <= resolved), None)
            require(ready is not None, "Plan dependencies contain a cycle")
            assert ready is not None
            ordered.append(ready)
            pending.remove(ready)
            resolved.add(ready.alias)
        return tuple(ordered)

    def document(self) -> JsonObject:
        return JsonObject(
            json.dumps(
                {
                    "plan_id": self.plan_id,
                    "version": self.version,
                    "tasks": [
                        {
                            "alias": task.alias,
                            "objective": task.objective,
                            "agent_definition_id": task.agent_definition_id,
                            "agent_version": task.agent_version,
                            "depends_on": list(task.depends_on),
                            "input_pages": list(task.input_pages),
                            "capabilities": list(task.capabilities),
                            "acceptance_criteria": json.loads(task.acceptance_criteria.text),
                            "result_schema": json.loads(task.result_schema.text),
                            "max_steps": task.max_steps,
                        }
                        for task in self.tasks
                    ],
                }
            )
        )


@dataclass(frozen=True)
class ResultEnvelope:
    """Kernel-owned immutable representation of one completed logical task."""

    alias: str
    task_id: str
    attempt: int
    status: str
    schema: str
    output: JsonObject
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require(bool(_ALIAS.fullmatch(self.alias)), "Result alias is invalid")
        require(self.attempt >= 1, "Result attempt must be positive")
        require(
            self.status in {"succeeded", "failed", "cancelled", "blocked"},
            "Invalid result status",
        )
        require(bool(self.schema.strip()), "Result schema is required")
        require(all(bool(warning.strip()) for warning in self.warnings), "Invalid result warning")
