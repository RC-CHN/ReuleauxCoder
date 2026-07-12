"""Provider-neutral repair of assistant tool-call/message adjacency."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable

MissingToolContent = Callable[[str, str], str]


def reconcile_tool_call_adjacency(
    messages: list[dict],
    *,
    missing_content: MissingToolContent | None = None,
) -> tuple[list[dict], int]:
    """Return history satisfying the assistant/tool response contract.

    Existing responses are moved immediately behind their assistant tool-call
    block and into call order. Missing responses are synthesized, while orphan
    and duplicate tool messages are discarded.
    """
    responses: dict[str, list[dict]] = defaultdict(list)
    for message in messages:
        if message.get("role") != "tool":
            continue
        tool_call_id = str(message.get("tool_call_id") or "").strip()
        if tool_call_id:
            responses[tool_call_id].append(dict(message))

    repaired: list[dict] = []
    synthesized = 0
    for message in messages:
        if message.get("role") == "tool":
            continue
        item = dict(message)
        repaired.append(item)
        if item.get("role") != "assistant":
            continue

        for tool_call in item.get("tool_calls") or ():
            tool_call_id = str(tool_call.get("id") or "").strip()
            if not tool_call_id:
                continue
            function = tool_call.get("function") or {}
            tool_name = str(function.get("name") or "unknown_tool")
            available = responses.get(tool_call_id)
            if available:
                response = available.pop(0)
                content = response.get("content")
                if content is None or not str(content).strip():
                    response["content"] = f"Tool '{tool_name}' output missing."
                repaired.append(response)
                continue

            content = (
                missing_content(tool_call_id, tool_name)
                if missing_content is not None
                else f"Tool '{tool_name}' output missing."
            )
            repaired.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": content,
                }
            )
            synthesized += 1

    return repaired, synthesized
