"""Actual-first rewrite planner tests."""

from reuleauxcoder.domain.context.manager import ContextManager


def _messages_with_old_tool_output() -> list[dict]:
    return [
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "tool_calls": [{"id": "old", "function": {"name": "shell"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "old",
            "content": "\n".join(f"line {i}: " + "x" * 120 for i in range(80)),
        },
        {
            "role": "assistant",
            "tool_calls": [{"id": "new", "function": {"name": "read_file"}}],
        },
        {"role": "tool", "tool_call_id": "new", "content": "recent"},
    ]


def _observe(manager: ContextManager, messages: list[dict], actual: int, cached=0):
    local = manager.get_context_tokens(messages)
    return manager.observe_usage(
        actual_prompt_tokens=actual,
        cached_input_tokens=cached,
        local_request_estimate=local,
        local_history_estimate=local,
        request_boundary="turn:0",
        model_profile="test-model",
    )


def test_planning_zone_does_not_rewrite_committed_history() -> None:
    manager = ContextManager(
        max_tokens=10_000, reserved_output_tokens=0, safety_margin_tokens=0
    )
    messages = _messages_with_old_tool_output()
    _observe(manager, messages, actual=5_500, cached=4_000)
    original = [dict(message) for message in messages]

    assert manager.maybe_compress(messages) is False
    assert messages == original
    assert manager._last_rewrite_plan is not None
    assert manager.cache_epoch == 0


def test_quality_wall_commits_one_batched_checkpoint() -> None:
    manager = ContextManager(
        max_tokens=10_000,
        reserved_output_tokens=0,
        safety_margin_tokens=0,
        snip_keep_recent_tools=1,
    )
    messages = _messages_with_old_tool_output()
    _observe(manager, messages, actual=6_200, cached=4_500)

    assert manager.maybe_compress(messages) is True
    assert manager.cache_epoch == 1
    checkpoint = manager.checkpoints[-1]
    assert checkpoint.trigger == "quality_wall"
    assert checkpoint.cached_input_tokens == 4_500
    assert checkpoint.reclaimed_tokens is not None
    assert checkpoint.reclaimed_tokens > 0
    assert "snip_tool_outputs" in checkpoint.strategy


def test_actual_usage_plus_calibrated_growth_drives_prediction() -> None:
    manager = ContextManager(
        max_tokens=10_000, reserved_output_tokens=0, safety_margin_tokens=0
    )
    messages = [{"role": "user", "content": "x" * 300}]
    local = manager.get_context_tokens(messages)
    _observe(manager, messages, actual=1_000)
    assert manager.predict_request_tokens(messages) == 1_000

    messages.append({"role": "assistant", "content": "y" * 600})
    assert manager.predict_request_tokens(messages) > 1_000
    assert manager.latest_usage is not None
    assert manager.latest_usage.local_history_estimate == local


def test_reconfigure_invalidates_pending_candidate_but_keeps_observation_audit() -> (
    None
):
    manager = ContextManager(max_tokens=10_000)
    messages = [{"role": "user", "content": "hello"}]
    _observe(manager, messages, actual=100)
    manager._last_rewrite_plan = {"candidate": True}
    old_limit = manager.request_input_limit

    manager.reconfigure(20_000)

    assert manager._last_rewrite_plan is None
    assert manager.latest_usage is not None
    assert manager.request_input_limit > old_limit
