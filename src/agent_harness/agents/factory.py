"""Local, versioned agent definitions and scoped instances."""

import json
from dataclasses import dataclass

from agent_harness.domain.entities import Task
from agent_harness.domain.errors import ErrorCode, HarnessError, require
from agent_harness.domain.model import RunOptions


@dataclass(frozen=True)
class AgentDefinition:
    definition_id: str
    version: str
    capabilities: tuple[str, ...]
    instructions: str
    may_delegate: bool = False
    max_children: int = 0
    max_active_children: int = 1
    max_depth: int = 0

    def __post_init__(self) -> None:
        require(
            bool(self.definition_id.strip() and self.version.strip()), "Agent identity required"
        )
        require(bool(self.instructions.strip()), "Agent instructions are required")
        require(len(set(self.capabilities)) == len(self.capabilities), "Duplicate capability")
        require(
            self.max_children >= 0
            and self.max_active_children >= 1
            and self.max_depth >= 0
            and (self.may_delegate or self.max_children == 0),
            "Invalid agent delegation limits",
        )


@dataclass(frozen=True)
class AgentInstance:
    instance_id: str
    user_id: str
    task_id: str
    definition: AgentDefinition
    depth: int
    capabilities: tuple[str, ...]
    max_steps: int


class AgentRegistry:
    def __init__(self, definitions: tuple[AgentDefinition, ...]) -> None:
        keys = {(item.definition_id, item.version) for item in definitions}
        require(len(keys) == len(definitions), "Duplicate agent definition version")
        self._definitions = {(item.definition_id, item.version): item for item in definitions}

    def get(self, definition_id: str, version: str) -> AgentDefinition:
        definition = self._definitions.get((definition_id, version))
        if definition is None:
            raise HarnessError(
                ErrorCode.AUTHORIZATION, "Agent definition/version is not registered"
            )
        return definition


class AgentFactory:
    def __init__(self, registry: AgentRegistry) -> None:
        self.registry = registry

    def create(self, task: Task, options: RunOptions) -> AgentInstance:
        definition = self.registry.get(task.agent_definition_id, task.agent_definition_version)
        capabilities = task.capabilities or (
            options.tool_allowlist if task.agent_definition_id == "single-agent" else ()
        )
        require(
            set(capabilities) <= set(definition.capabilities),
            "Task capabilities exceed the registered agent definition",
        )
        handoff = json.loads(task.handoff.text)
        requested_steps = handoff.get("max_steps", options.max_steps)
        require(
            isinstance(requested_steps, int) and not isinstance(requested_steps, bool),
            "Agent step limit must be an integer",
        )
        return AgentInstance(
            task.agent_instance_id or task.task_id,
            task.user_id,
            task.task_id,
            definition,
            task.depth,
            tuple(capabilities),
            min(options.max_steps, requested_steps),
        )


def local_agent_registry() -> AgentRegistry:
    file_tools = ("list_files", "read_file", "read_file_range", "search_files")
    worker_capabilities = (
        "calculator",
        *file_tools,
        "write_file",
        "edit_file",
        "memory_save",
    )
    return AgentRegistry(
        (
            AgentDefinition(
                "single-agent",
                "1",
                worker_capabilities,
                "Complete the assigned task using only authorized local capabilities.",
            ),
            AgentDefinition(
                "coordinator",
                "1",
                (*worker_capabilities, "delegate_tasks"),
                "Coordinate the task. For long, context-heavy, or multi-part work, split it into "
                "small, independently verifiable tasks and delegate them to task-focused worker "
                "agents. Delegate independent tasks together so they can run in parallel; add "
                "dependencies only when one task truly needs another's result. Keep simple tasks "
                "local. Recombine worker results and verify the final answer.",
                may_delegate=True,
                max_children=8,
                max_active_children=4,
                max_depth=2,
            ),
            AgentDefinition(
                "worker",
                "1",
                worker_capabilities,
                "Complete the explicit bounded subtask using only the supplied handoff and "
                "authorized capabilities. Return concise findings with relevant file paths "
                "when available. The kernel may subdivide this task if its context overflows.",
                may_delegate=True,
                max_children=8,
                max_active_children=4,
                max_depth=2,
            ),
            AgentDefinition(
                "researcher",
                "1",
                ("calculator", *file_tools, "write_file", "edit_file", "memory_save"),
                "Research using the supplied task handoff and authorized local filesystem tools. "
                "Return concise findings and cite relevant file paths when available.",
            ),
            AgentDefinition(
                "writer",
                "1",
                ("calculator", *file_tools, "write_file", "edit_file", "memory_save"),
                "Produce the requested output from the explicit task handoff. Do not invent "
                "unprovided research.",
            ),
        )
    )
