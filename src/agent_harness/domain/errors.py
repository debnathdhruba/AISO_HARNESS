"""Safe machine-readable failure contracts; raw adapter exceptions stay internal."""

from enum import StrEnum


class ErrorCode(StrEnum):
    VALIDATION = "validation"
    AUTHORIZATION = "authorization"
    APPROVAL_REQUIRED = "approval_required"
    QUEUE_FULL = "queue_full"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCELLATION = "cancellation"
    TRANSIENT_PROVIDER = "transient_provider"
    PERMANENT_PROVIDER = "permanent_provider"
    TOOL_EXECUTION = "tool_execution"
    STORAGE = "storage"
    CONCURRENCY_CONFLICT = "concurrency_conflict"
    UNCERTAIN_SIDE_EFFECT = "uncertain_side_effect"


class HarnessError(Exception):
    """Message must be authored by the harness, never copied from a provider."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        diagnostic_reference: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.diagnostic_reference = diagnostic_reference


def require(condition: bool, message: str) -> None:
    if not condition:
        raise HarnessError(ErrorCode.VALIDATION, message)
