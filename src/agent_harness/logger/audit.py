"""JSON audit events; prompt contents, secrets, and tool arguments are excluded."""

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LOGGER_NAME = "agent_harness.lifecycle"
_SENSITIVE_FIELDS = frozenset(
    {"prompt", "content", "text", "arguments", "secret", "token", "password", "result"}
)
_configured = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "event": getattr(record, "event_name", record.getMessage()),
        }
        fields = getattr(record, "event_fields", {})
        if isinstance(fields, dict):
            payload.update(fields)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def configure_logging(log_file: str | Path | None = None, level: str | None = None) -> None:
    """Configure one bounded rotating sink; repeated application setup is harmless."""
    global _configured
    if _configured:
        return
    target = Path(log_file or os.environ.get("AGENT_HARNESS_LOG_FILE", ".local/agent_harness.log"))
    target.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(target, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(_JsonFormatter())
    console = logging.StreamHandler()
    console.setFormatter(_JsonFormatter())
    logger = logging.getLogger(LOGGER_NAME)
    selected_level = (level or os.environ.get("AGENT_HARNESS_LOG_LEVEL", "INFO")).upper()
    logger.setLevel(getattr(logging, selected_level, logging.INFO))
    logger.addHandler(handler)
    if os.environ.get("AGENT_HARNESS_LOG_CONSOLE", "true").lower() not in {"0", "false", "off"}:
        logger.addHandler(console)
    logger.propagate = False
    _configured = True


def audit(event: str, **fields: Any) -> None:
    """Write a bounded lifecycle event without accepting arbitrary prompt payloads."""
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        if key.lower() in _SENSITIVE_FIELDS or any(
            marker in key.lower() for marker in ("prompt", "password", "secret", "argument")
        ):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value if not isinstance(value, str) else value[:256]
        elif isinstance(value, (tuple, list)):
            safe[key] = [str(item)[:128] for item in value[:16]]
        else:
            safe[key] = str(value)[:256]
    logging.getLogger(LOGGER_NAME).info(event, extra={"event_name": event, "event_fields": safe})
