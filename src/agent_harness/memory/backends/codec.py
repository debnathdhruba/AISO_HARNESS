"""Versioned snapshots are JSON; never unpickle persisted data."""

import json
from dataclasses import asdict
from datetime import datetime

from agent_harness.domain.entities import JsonObject, Run, RunConfig, Task
from agent_harness.domain.errors import require
from agent_harness.domain.states import ControlIntent, RunStatus, TaskStatus


def encode(entity: Run | Task) -> str:
    data = asdict(entity)
    for key in ("created_at", "updated_at", "deadline"):
        value = data[key]
        data[key] = value.isoformat() if value is not None else None
    return json.dumps({"schema_version": 1, "data": data}, allow_nan=False, sort_keys=True)


def decode_run(text: str) -> Run:
    snapshot = json.loads(text)
    require(snapshot["schema_version"] == 1, "Unsupported run snapshot version")
    data = snapshot["data"]
    config = data.pop("config")
    for key in ("model", "policy", "budgets", "memory"):
        config[key] = JsonObject(**config[key])
    config.pop("workspace", None)
    data["config"] = RunConfig(**config)
    data["status"] = RunStatus(data["status"])
    data["intent"] = ControlIntent(data["intent"])
    for key in ("created_at", "updated_at", "deadline"):
        data[key] = datetime.fromisoformat(data[key]) if data[key] is not None else None
    for key in ("result", "error"):
        data[key] = JsonObject(**data[key]) if data[key] is not None else None
    return Run(**data)


def decode_task(text: str) -> Task:
    snapshot = json.loads(text)
    require(snapshot["schema_version"] == 1, "Unsupported task snapshot version")
    data = snapshot["data"]
    data["status"] = TaskStatus(data["status"])
    for key in ("created_at", "updated_at", "deadline"):
        data[key] = datetime.fromisoformat(data[key]) if data[key] is not None else None
    data["acceptance_criteria"] = JsonObject(**data["acceptance_criteria"])
    data["result"] = JsonObject(**data["result"]) if data["result"] is not None else None
    data["dependencies"] = tuple(data["dependencies"])
    data.setdefault("agent_definition_id", "single-agent")
    data.setdefault("agent_definition_version", "1")
    data.setdefault("depth", 0)
    data.setdefault("required", True)
    data["capabilities"] = tuple(data.get("capabilities", ()))
    data["handoff"] = JsonObject(**data.get("handoff", {"text": "{}"}))
    data.setdefault("idempotency_key", None)
    data["error"] = JsonObject(**data["error"]) if data.get("error") is not None else None
    return Task(**data)
