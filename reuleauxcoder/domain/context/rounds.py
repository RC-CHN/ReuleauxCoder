"""Protocol-safe conversation round helpers."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.domain.llm.tool_history import reconcile_tool_call_adjacency


@dataclass(frozen=True, slots=True)
class ConversationRound:
    """One model API response and the inputs/results surrounding it."""

    messages: tuple[dict, ...]


def group_api_rounds(messages: list[dict]) -> list[ConversationRound]:
    """Split history without separating an assistant tool call from its outputs."""

    groups: list[list[dict]] = []
    current: list[dict] = []
    current_has_assistant = False
    for message in messages:
        if message.get("role") == "assistant" and current and current_has_assistant:
            groups.append(current)
            current = []
            current_has_assistant = False
        current.append(message)
        if message.get("role") == "assistant":
            current_has_assistant = True
    if current:
        groups.append(current)
    return [ConversationRound(tuple(group)) for group in groups]


def recent_round_start(messages: list[dict], keep: int) -> int:
    """Return a message index that retains the newest ``keep`` complete rounds."""

    if keep <= 0:
        return len(messages)
    rounds = group_api_rounds(messages)
    if len(rounds) <= keep:
        return 0
    return sum(len(group.messages) for group in rounds[:-keep])


def normalize_history(messages: list[dict], *, reason: str) -> list[dict]:
    """Return API-valid history with every tool call paired to one output."""

    repaired, _ = reconcile_tool_call_adjacency(
        messages,
        missing_content=lambda _call_id, tool_name: (
            f"Tool '{tool_name}' did not return output before {reason}."
        ),
    )
    return repaired
