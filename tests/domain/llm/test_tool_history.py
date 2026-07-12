from reuleauxcoder.domain.llm.tool_history import reconcile_tool_call_adjacency


def _assistant(*tool_call_ids: str) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": tool_call_id,
                "type": "function",
                "function": {"name": f"tool_{tool_call_id}", "arguments": "{}"},
            }
            for tool_call_id in tool_call_ids
        ],
    }


def test_reconcile_moves_responses_before_intervening_user_messages() -> None:
    messages = [
        _assistant("a", "b"),
        {"role": "tool", "tool_call_id": "a", "content": "A"},
        {"role": "user", "content": "[SESSION_EXIT]"},
        {"role": "tool", "tool_call_id": "b", "content": "B"},
        {"role": "tool", "tool_call_id": "a", "content": "duplicate"},
        {"role": "tool", "tool_call_id": "orphan", "content": "orphan"},
        {"role": "user", "content": "resumed"},
    ]

    repaired, synthesized = reconcile_tool_call_adjacency(messages)

    assert synthesized == 0
    assert repaired == [
        messages[0],
        messages[1],
        messages[3],
        messages[2],
        messages[6],
    ]


def test_reconcile_synthesizes_missing_response_inside_tool_block() -> None:
    messages = [_assistant("a", "b"), {"role": "user", "content": "later"}]

    repaired, synthesized = reconcile_tool_call_adjacency(
        messages,
        missing_content=lambda tool_call_id, tool_name: (
            f"recovered {tool_name} ({tool_call_id})"
        ),
    )

    assert synthesized == 2
    assert [message["role"] for message in repaired] == [
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    assert repaired[1]["content"] == "recovered tool_a (a)"
    assert repaired[2]["content"] == "recovered tool_b (b)"
