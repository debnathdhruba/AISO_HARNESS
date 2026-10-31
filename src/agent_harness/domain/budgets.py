"""Typed budget limits and reservation/reconciliation values."""

from dataclasses import dataclass

from agent_harness.domain.errors import require


@dataclass(frozen=True)
class BudgetLimits:
    steps: int
    tokens: int
    tool_calls: int

    def __post_init__(self) -> None:
        require(
            self.steps > 0 and self.tokens > 0 and self.tool_calls > 0,
            "Budget limits must be positive",
        )

    def document(self) -> dict[str, int]:
        return {"steps": self.steps, "tokens": self.tokens, "tool_calls": self.tool_calls}


@dataclass(frozen=True)
class BudgetUsage:
    """A reservation uses integers; reconciliation may mark any dimension unknown."""

    steps: int | None = 0
    tokens: int | None = 0
    tool_calls: int | None = 0

    def __post_init__(self) -> None:
        require(
            all(value is None or value >= 0 for value in self.document().values()),
            "Budget usage cannot be negative",
        )

    def document(self) -> dict[str, int | None]:
        return {"steps": self.steps, "tokens": self.tokens, "tool_calls": self.tool_calls}

    def reservation(self) -> dict[str, int]:
        steps, tokens, tool_calls = self.steps, self.tokens, self.tool_calls
        require(
            steps is not None and tokens is not None and tool_calls is not None,
            "Reservations must be bounded for every budget dimension",
        )
        assert steps is not None and tokens is not None and tool_calls is not None
        return {"steps": steps, "tokens": tokens, "tool_calls": tool_calls}


@dataclass(frozen=True)
class BudgetSnapshot:
    limits: BudgetLimits
    used: BudgetUsage
    reserved: BudgetUsage
    unknown_usage_count: int = 0

    def __post_init__(self) -> None:
        require(self.unknown_usage_count >= 0, "Unknown usage count cannot be negative")
