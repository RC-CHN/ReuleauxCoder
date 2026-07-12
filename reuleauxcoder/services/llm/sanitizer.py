"""Message sanitizer — repair/trim malformed tool-call history before sending to the LLM."""

from __future__ import annotations

from reuleauxcoder.domain.llm.models import EMPTY_ASSISTANT_CONTENT_PLACEHOLDER
from reuleauxcoder.domain.llm.tool_history import reconcile_tool_call_adjacency

DEFAULT_REASONING_REPLAY_PLACEHOLDER = "[PLACE_HOLDER]"


def _normalize_reasoning_replay_mode(reasoning_replay_mode: str | None) -> str:
    mode = (reasoning_replay_mode or "none").strip().lower()
    if mode not in {"none", "tool_calls"}:
        return "none"
    return mode


def _sanitize_messages_for_llm_core(
    messages: list[dict],
    *,
    preserve_reasoning_content: bool = True,
    backfill_reasoning_content_for_tool_calls: bool = False,
    require_reasoning_content_for_tool_calls: bool = False,
    replay_reasoning_for_non_tool_assistant: bool = False,
    reasoning_replay_placeholder: str = DEFAULT_REASONING_REPLAY_PLACEHOLDER,
    thinking_enabled: bool = False,
) -> list[dict]:
    """Repair/trim malformed tool-call history before sending to the LLM."""
    sanitized: list[dict] = []
    effective_backfill = preserve_reasoning_content and (
        backfill_reasoning_content_for_tool_calls
        or require_reasoning_content_for_tool_calls
    )
    user_turn_had_tool_calls = False

    for msg_index, msg in enumerate(messages):
        item = dict(msg)
        role = item.get("role")

        if role == "user":
            user_turn_had_tool_calls = False
            sanitized.append(item)
            continue

        if role != "assistant":
            sanitized.append(item)
            continue

        if not preserve_reasoning_content and not thinking_enabled:
            item.pop("reasoning_content", None)

        raw_tool_calls = item.get("tool_calls") or []
        if not raw_tool_calls:
            if (
                effective_backfill
                and user_turn_had_tool_calls
                and "reasoning_content" not in item
            ):
                item["reasoning_content"] = reasoning_replay_placeholder
            elif not thinking_enabled and not (
                replay_reasoning_for_non_tool_assistant or user_turn_had_tool_calls
            ):
                item.pop("reasoning_content", None)
            content = item.get("content")
            if content is None or not str(content).strip():
                item["content"] = EMPTY_ASSISTANT_CONTENT_PLACEHOLDER
            sanitized.append(item)
            continue

        user_turn_had_tool_calls = True
        repaired_tool_calls: list[dict] = []
        for tc_index, tc in enumerate(raw_tool_calls):
            tc_item = dict(tc)
            tc_id = (tc_item.get("id") or "").strip()
            if not tc_id:
                tc_id = f"recovered_tool_call_{msg_index}_{tc_index}"
                tc_item["id"] = tc_id

            fn = dict(tc_item.get("function") or {})
            tc_item["function"] = fn
            repaired_tool_calls.append(tc_item)

        item["tool_calls"] = repaired_tool_calls
        if effective_backfill and "reasoning_content" not in item:
            item["reasoning_content"] = reasoning_replay_placeholder
        sanitized.append(item)

    final_messages, _ = reconcile_tool_call_adjacency(sanitized)
    return final_messages


def _sanitize_messages_for_reasoning_replay_none(
    messages: list[dict],
    *,
    preserve_reasoning_content: bool = True,
    backfill_reasoning_content_for_tool_calls: bool = False,
    reasoning_replay_placeholder: str = DEFAULT_REASONING_REPLAY_PLACEHOLDER,
    thinking_enabled: bool = False,
) -> list[dict]:
    return _sanitize_messages_for_llm_core(
        messages,
        preserve_reasoning_content=preserve_reasoning_content,
        backfill_reasoning_content_for_tool_calls=backfill_reasoning_content_for_tool_calls,
        require_reasoning_content_for_tool_calls=False,
        replay_reasoning_for_non_tool_assistant=False,
        reasoning_replay_placeholder=reasoning_replay_placeholder,
        thinking_enabled=thinking_enabled,
    )


def _sanitize_messages_for_reasoning_replay_tool_calls(
    messages: list[dict],
    *,
    preserve_reasoning_content: bool = True,
    backfill_reasoning_content_for_tool_calls: bool = False,
    reasoning_replay_placeholder: str = DEFAULT_REASONING_REPLAY_PLACEHOLDER,
    thinking_enabled: bool = False,
) -> list[dict]:
    return _sanitize_messages_for_llm_core(
        messages,
        preserve_reasoning_content=preserve_reasoning_content,
        backfill_reasoning_content_for_tool_calls=backfill_reasoning_content_for_tool_calls,
        require_reasoning_content_for_tool_calls=preserve_reasoning_content,
        replay_reasoning_for_non_tool_assistant=False,
        reasoning_replay_placeholder=reasoning_replay_placeholder,
        thinking_enabled=thinking_enabled,
    )


def sanitize_messages_for_llm(
    messages: list[dict],
    *,
    preserve_reasoning_content: bool = True,
    backfill_reasoning_content_for_tool_calls: bool = False,
    require_reasoning_content_for_tool_calls: bool = False,
    replay_reasoning_for_non_tool_assistant: bool = False,
    reasoning_replay_mode: str | None = None,
    reasoning_replay_placeholder: str = DEFAULT_REASONING_REPLAY_PLACEHOLDER,
    thinking_enabled: bool = False,
) -> list[dict]:
    """Public entry point: repair/trim messages, dispatching by reasoning_replay_mode."""
    if reasoning_replay_mode is not None:
        mode = _normalize_reasoning_replay_mode(reasoning_replay_mode)
        if mode == "tool_calls":
            return _sanitize_messages_for_reasoning_replay_tool_calls(
                messages,
                preserve_reasoning_content=preserve_reasoning_content,
                backfill_reasoning_content_for_tool_calls=backfill_reasoning_content_for_tool_calls,
                reasoning_replay_placeholder=reasoning_replay_placeholder,
                thinking_enabled=thinking_enabled,
            )
        return _sanitize_messages_for_reasoning_replay_none(
            messages,
            preserve_reasoning_content=preserve_reasoning_content,
            backfill_reasoning_content_for_tool_calls=backfill_reasoning_content_for_tool_calls,
            reasoning_replay_placeholder=reasoning_replay_placeholder,
            thinking_enabled=thinking_enabled,
        )

    return _sanitize_messages_for_llm_core(
        messages,
        preserve_reasoning_content=preserve_reasoning_content,
        backfill_reasoning_content_for_tool_calls=backfill_reasoning_content_for_tool_calls,
        require_reasoning_content_for_tool_calls=require_reasoning_content_for_tool_calls,
        replay_reasoning_for_non_tool_assistant=replay_reasoning_for_non_tool_assistant,
        reasoning_replay_placeholder=reasoning_replay_placeholder,
        thinking_enabled=thinking_enabled,
    )
