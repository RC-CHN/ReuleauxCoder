from reuleauxcoder.domain.context.budget import ContextBudget
from reuleauxcoder.domain.context.rounds import group_api_rounds, recent_round_start


def test_round_group_keeps_parallel_tool_outputs_with_call():
    messages = [
        {"role": "user", "content": "inspect"},
        {"role": "assistant", "tool_calls": [
            {"id": "a", "function": {"name": "read_file"}},
            {"id": "b", "function": {"name": "grep"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "A"},
        {"role": "tool", "tool_call_id": "b", "content": "B"},
        {"role": "assistant", "content": "done"},
    ]
    rounds = group_api_rounds(messages)
    assert len(rounds) == 2
    assert [item.get("tool_call_id") for item in rounds[0].messages[2:]] == ["a", "b"]
    assert recent_round_start(messages, 1) == 4


def test_effective_budget_reserves_output_and_fixed_overhead():
    budget = ContextBudget(model_window=100_000, reserved_output=10_000,
                           fixed_prompt_tokens=5_000, tool_schema_tokens=3_000,
                           safety_margin=2_000)
    assert budget.available_input == 80_000
    assert budget.threshold(.7) == 56_000
