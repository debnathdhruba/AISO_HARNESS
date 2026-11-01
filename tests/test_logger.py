import importlib
import json
from pathlib import Path

from agent_harness.logger import audit, configure_logging

audit_module = importlib.import_module("agent_harness.logger.audit")


def test_audit_logger_writes_bounded_json_events_without_prompt_content(tmp_path: Path) -> None:
    log_file = tmp_path / "lifecycle.log"
    configure_logging(log_file)
    try:
        audit("test.step", run_id="run-1", input_bytes=123, prompt="must-not-be-logged")
        record = json.loads(log_file.read_text(encoding="utf-8").splitlines()[-1])
        assert record["event"] == "test.step"
        assert record["run_id"] == "run-1"
        assert record["input_bytes"] == 123
        assert "prompt" not in record
    finally:
        logger = audit_module.logging.getLogger(audit_module.LOGGER_NAME)
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)
        audit_module._configured = False
