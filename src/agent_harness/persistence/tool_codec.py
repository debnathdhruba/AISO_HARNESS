import json
from dataclasses import asdict
from datetime import datetime

from agent_harness.domain.entities import JsonObject
from agent_harness.domain.states import InvocationStatus
from agent_harness.domain.tooling import ArtifactRecord, Effect, Invocation


def encode_invocation(value: Invocation) -> str:
    data = asdict(value)
    data["approval_expires_at"] = (
        value.approval_expires_at.isoformat() if value.approval_expires_at else None
    )
    return json.dumps(data)


def decode_invocation(text: str) -> Invocation:
    data = json.loads(text)
    for key in ("arguments", "result", "error"):
        data[key] = JsonObject(**data[key]) if data[key] is not None else None
    data["status"] = InvocationStatus(data["status"])
    data["effect"] = Effect(data["effect"])
    if data["approval_expires_at"] is not None:
        data["approval_expires_at"] = datetime.fromisoformat(data["approval_expires_at"])
    return Invocation(**data)


def encode_artifact(value: ArtifactRecord) -> str:
    return json.dumps(asdict(value))


def decode_artifact(text: str) -> ArtifactRecord:
    return ArtifactRecord(**json.loads(text))
