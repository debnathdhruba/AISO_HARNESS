"""Strict parsing and authorization of one-shot decomposition plans."""

import json
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from agent_harness.agents.factory import AgentRegistry
from agent_harness.domain.entities import JsonObject
from agent_harness.domain.errors import ErrorCode, HarnessError, require
from agent_harness.domain.kernel import ExecutionPlan, PlanTask

PLAN_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["plan_id", "tasks"],
    "properties": {
        "plan_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "version": {"type": "integer", "const": 1},
        "tasks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["alias", "objective"],
                "properties": {
                    "alias": {"type": "string", "minLength": 1, "maxLength": 64},
                    "objective": {"type": "string", "minLength": 1, "maxLength": 8000},
                    "agent_definition_id": {"type": "string", "minLength": 1},
                    "agent_version": {"type": "string", "minLength": 1},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "input_pages": {"type": "array", "items": {"type": "string"}},
                    "capabilities": {"type": "array", "items": {"type": "string"}},
                    "acceptance_criteria": {"type": "object"},
                    "result_schema": {"type": "object"},
                    "max_steps": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
        },
    },
}


@dataclass(frozen=True)
class PlanCompiler:
    """Compiles an untrusted JSON proposal to a capability-safe execution plan."""

    registry: AgentRegistry
    parent_capabilities: tuple[str, ...]
    known_pages: tuple[str, ...] = ()

    def compile(self, proposal: JsonObject) -> ExecutionPlan:
        try:
            raw = json.loads(proposal.text)
            Draft202012Validator(PLAN_SCHEMA).validate(raw)
        except (ValidationError, ValueError, TypeError) as error:
            raise HarnessError(
                ErrorCode.VALIDATION, "Invalid agent_orchestration_core plan"
            ) from error
        assert isinstance(raw, dict)
        tasks = tuple(self._task(item) for item in raw["tasks"])
        plan = ExecutionPlan(str(raw["plan_id"]), tasks, int(raw.get("version", 1)))
        for task in plan.tasks:
            require(
                set(task.input_pages) <= set(self.known_pages),
                "Plan references an unavailable input page",
            )
        return plan

    def _task(self, raw: Any) -> PlanTask:
        assert isinstance(raw, dict)
        definition_id = str(raw.get("agent_definition_id", "worker"))
        version = str(raw.get("agent_version", "1"))
        definition = self.registry.get(definition_id, version)
        capabilities = tuple(str(item) for item in raw.get("capabilities", ()))
        permitted = set(definition.capabilities) & set(self.parent_capabilities)
        require(set(capabilities) <= permitted, "Plan task requests an unauthorized capability")
        result_schema = raw.get("result_schema", {})
        try:
            Draft202012Validator.check_schema(result_schema)
        except SchemaError as error:
            raise HarnessError(
                ErrorCode.VALIDATION, "Plan task result schema is invalid"
            ) from error
        return PlanTask(
            alias=str(raw["alias"]),
            objective=str(raw["objective"]),
            agent_definition_id=definition_id,
            agent_version=version,
            depends_on=tuple(str(item) for item in raw.get("depends_on", ())),
            input_pages=tuple(str(item) for item in raw.get("input_pages", ())),
            capabilities=capabilities,
            acceptance_criteria=JsonObject(json.dumps(raw.get("acceptance_criteria", {}))),
            result_schema=JsonObject(json.dumps(result_schema)),
            max_steps=raw.get("max_steps"),
        )
