"""Keep mandatory context intact; optional history is selected as complete groups."""

from agent_harness.domain.errors import ErrorCode, HarnessError, require
from agent_harness.domain.model import Message, Role


def size(messages: tuple[Message, ...]) -> int:
    return sum(
        len(m.content.encode("utf-8"))
        + 32
        + sum(
            len(c.arguments.text.encode("utf-8")) + len(c.name.encode("utf-8")) + 64
            for c in m.tool_calls
        )
        for m in messages
    )


def build_context(
    instructions: str, task_input: str, history: tuple[Message, ...], max_input_bytes: int
) -> tuple[Message, ...]:
    mandatory = (Message(Role.SYSTEM, instructions), Message(Role.USER, task_input))
    if size(mandatory) > max_input_bytes:
        raise HarnessError(ErrorCode.BUDGET_EXCEEDED, "Task exceeds the configured context budget")
    groups: list[tuple[Message, ...]] = []
    index = 0
    while index < len(history):
        message = history[index]
        require(message.role != Role.TOOL, "Orphan tool result in history")
        group = [message]
        index += 1
        if message.tool_calls:
            pending = {call.call_id for call in message.tool_calls}
            while pending and index < len(history):
                result = history[index]
                require(
                    result.role == Role.TOOL and result.tool_call_id in pending,
                    "Tool call/result grouping is invalid",
                )
                assert result.tool_call_id is not None
                pending.remove(result.tool_call_id)
                group.append(result)
                index += 1
            require(not pending, "Incomplete tool call/result group")
        groups.append(tuple(group))
    selected: tuple[Message, ...] = ()
    for complete_group in reversed(groups):
        if size(mandatory + complete_group + selected) > max_input_bytes:
            break
        selected = complete_group + selected
    return mandatory + selected
