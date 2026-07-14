"""Build bounded context views for delegated agents."""

from __future__ import annotations

import json

from reuleauxcoder.domain.context.rounds import recent_round_start
from reuleauxcoder.domain.llm.context_messages import is_synthetic_context_message


VALID_CONTEXT_MODES = frozenset({"minimal", "recent", "full"})


def project_parent_context(
    parent_agent, mode: str = "recent", recent_rounds: int = 4
) -> str:
    """Render a provider-neutral, bounded parent history projection."""

    if mode not in VALID_CONTEXT_MODES:
        raise ValueError(f"Unknown sub-agent context mode: {mode}")
    messages = list(getattr(parent_agent, "messages", []))
    if mode == "minimal":
        messages = [
            item
            for item in messages
            if item.get("role") == "user"
            and not is_synthetic_context_message(item)
        ][-2:]
    elif mode == "recent":
        messages = messages[recent_round_start(messages, recent_rounds) :]

    rendered: list[str] = []
    for message in messages:
        role = message.get("role", "unknown")
        content = str(message.get("content") or "")
        if len(content) > 1_200:
            content = content[:600] + "\n…\n" + content[-600:]
        tool_calls = message.get("tool_calls") or []
        tool_names = [
            str((call.get("function") or {}).get("name") or "tool")
            for call in tool_calls
            if isinstance(call, dict)
        ]
        rendered.append(
            json.dumps(
                {"role": role, "content": content, "tool_calls": tool_names},
                ensure_ascii=False,
            )
        )
    return "\n".join(rendered)
