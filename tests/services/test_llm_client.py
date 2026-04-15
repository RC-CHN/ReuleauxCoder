from reuleauxcoder.services.llm.client import _sanitize_messages_for_llm


def test_sanitize_messages_backfills_reasoning_content_for_assistant_tool_calls() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
    ]

    sanitized = _sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        backfill_reasoning_content_for_tool_calls=True,
    )

    assert sanitized[0]["reasoning_content"] == ""


def test_sanitize_messages_does_not_backfill_when_disabled() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
    ]

    sanitized = _sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        backfill_reasoning_content_for_tool_calls=False,
    )

    assert "reasoning_content" not in sanitized[0]


def test_sanitize_messages_strips_reasoning_content_when_preserve_disabled() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "hidden",
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
    ]

    sanitized = _sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=False,
        backfill_reasoning_content_for_tool_calls=True,
    )

    assert "reasoning_content" not in sanitized[0]
