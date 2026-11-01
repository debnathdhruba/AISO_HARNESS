"""Deterministic completion checks; model prose is only a candidate result."""

import json
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from agent_harness.domain.entities import JsonObject, Task


@dataclass(frozen=True)
class CompletionDecision:
    passed: bool
    verified: bool
    reasons: tuple[str, ...]

    def payload(self) -> JsonObject:
        return JsonObject(
            json.dumps(
                {
                    "passed": self.passed,
                    "verified": self.verified,
                    "reasons": self.reasons,
                }
            )
        )


def validate_completion(
    task: Task,
    result: JsonObject,
    *,
    unresolved_tools: bool,
    pending_required_children: bool,
    cancelled: bool,
    expired: bool,
    default_schema: JsonObject,
) -> CompletionDecision:
    criteria: dict[str, Any] = json.loads(task.acceptance_criteria.text)
    candidate: dict[str, Any] = json.loads(result.text)
    reasons: list[str] = []
    if unresolved_tools:
        reasons.append("required tool actions are unresolved")
    if pending_required_children:
        reasons.append("required child tasks are incomplete")
    if cancelled:
        reasons.append("run cancellation is requested")
    if expired:
        reasons.append("run deadline has expired")

    answer = candidate.get("answer")
    if isinstance(answer, str):
        lowered = answer.casefold()
        required_phrases = criteria.get("required_phrases", ())
        if not isinstance(required_phrases, list | tuple):
            reasons.append("required_phrases must be a list of strings")
            required_phrases = ()
        for phrase in required_phrases:
            if not isinstance(phrase, str) or phrase.casefold() not in lowered:
                reasons.append(f"required phrase is missing: {phrase}")

    schema = criteria.get("result_schema") or json.loads(default_schema.text)
    if schema:
        if not isinstance(schema, dict):
            reasons.append("result_schema must be a JSON Schema object")
        else:
            try:
                Draft202012Validator.check_schema(schema)
                validator = Draft202012Validator(schema)
                errors = sorted(
                    validator.iter_errors(candidate),
                    key=lambda error: list(error.path),
                )
                reasons.extend(
                    "result does not match schema at "
                    + ("/" + "/".join(str(part) for part in error.path) if error.path else "/")
                    for error in errors[:5]
                )
            except SchemaError:
                reasons.append("configured result schema is invalid")

    passed = not reasons
    deterministic = bool(criteria.get("required_phrases") or schema)
    return CompletionDecision(passed, passed and deterministic, tuple(reasons))
