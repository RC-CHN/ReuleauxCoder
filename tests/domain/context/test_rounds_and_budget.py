from reuleauxcoder.domain.context.budget import ContextBudget
from reuleauxcoder.domain.context.manager import ContextManager
from reuleauxcoder.domain.context.rounds import group_api_rounds, recent_round_start


def test_round_group_keeps_parallel_tool_outputs_with_call() -> None:
    messages = [
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "a", "function": {"name": "read_file"}},
                {"id": "b", "function": {"name": "grep"}},
            ],
        },
        {"role": "tool", "tool_call_id": "a", "content": "A"},
        {"role": "tool", "tool_call_id": "b", "content": "B"},
        {"role": "assistant", "content": "done"},
    ]

    rounds = group_api_rounds(messages)

    assert len(rounds) == 2
    assert [item.get("tool_call_id") for item in rounds[0].messages[2:]] == ["a", "b"]
    assert recent_round_start(messages, 1) == 4


def test_effective_budget_reserves_output_and_fixed_overhead() -> None:
    budget = ContextBudget(
        model_window=100_000,
        reserved_output=10_000,
        fixed_prompt_tokens=5_000,
        tool_schema_tokens=3_000,
        safety_margin=2_000,
    )
    assert budget.available_input == 80_000
    assert budget.threshold(0.7) == 56_000


def test_compression_records_versioned_checkpoint() -> None:
    manager = ContextManager(
        max_tokens=2_000,
        reserved_output_tokens=0,
        safety_margin_tokens=0,
        snip_threshold_chars=20,
        snip_min_lines=2,
    )
    messages = [
        {"role": "user", "content": "run"},
        {"role": "assistant", "tool_calls": [{"id": "x", "function": {"name": "shell"}}]},
        {"role": "tool", "tool_call_id": "x", "content": "\n".join(str(i) * 100 for i in range(20))},
        {"role": "assistant", "tool_calls": [{"id": "y", "function": {"name": "read_file"}}]},
        {"role": "tool", "tool_call_id": "y", "content": "new"},
        {"role": "assistant", "tool_calls": [{"id": "z", "function": {"name": "grep"}}]},
        {"role": "tool", "tool_call_id": "z", "content": "latest"},
    ]

    assert manager.maybe_compress(messages) is True
    assert manager.history_version == 1
    assert manager.checkpoints[-1].source_history_version == 0
    assert manager.checkpoints[-1].replacement_history
