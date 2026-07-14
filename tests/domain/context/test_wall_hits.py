"""Actual-first rewrite planner tests."""

from reuleauxcoder.domain.context.manager import ContextManager
from reuleauxcoder.interfaces.events import UIEventBus


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


def test_below_snip_wall_does_not_rewrite_committed_history() -> None:
    manager = ContextManager(
        max_tokens=10_000, reserved_output_tokens=0, safety_margin_tokens=0
    )
    messages = _messages_with_old_tool_output()
    _observe(manager, messages, actual=5_900, cached=4_000)
    original = [dict(message) for message in messages]

    assert manager.maybe_compress(messages) is False
    assert messages == original
    assert manager.cache_epoch == 0


def test_low_gain_snip_is_deferred_between_sixty_and_seventy_five_percent() -> None:
    manager = ContextManager(
        max_tokens=10_000, reserved_output_tokens=0, safety_margin_tokens=0
    )
    messages = [
        {"role": "user", "content": "keep the full conversation"},
        {"role": "assistant", "content": "No old tool output is available."},
    ]
    _observe(manager, messages, actual=6_500, cached=6_000)
    original = [dict(message) for message in messages]

    assert manager.maybe_compress(messages) is False
    assert messages == original
    assert manager.checkpoints == ()
    assert manager.cache_epoch == 0


def test_profitable_snip_commits_at_sixty_percent() -> None:
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
    assert checkpoint.trigger == "profitable_snip"
    assert checkpoint.cached_input_tokens == 4_500
    assert checkpoint.reclaimed_tokens is not None
    assert checkpoint.reclaimed_tokens >= manager._snip_min_gain
    assert "snip_tool_outputs" in checkpoint.strategy


def test_semantic_wall_summarizes_and_keeps_five_recent_user_turns() -> None:
    ui_bus = UIEventBus()

    class SummaryLLM:
        def chat(self, **_kwargs):
            started = ui_bus.history_snapshot()[-1]
            assert started.data["phase"] == "before"
            assert started.data["trigger"] == "semantic_wall"
            return type("Response", (), {"content": ""})()

    messages = []
    for index in range(9):
        messages.extend(
            [
                {"role": "user", "content": f"request {index} " + "u" * 1_200},
                {
                    "role": "assistant",
                    "content": f"answer {index} " + "a" * 1_200,
                },
            ]
        )
    probe = ContextManager(reserved_output_tokens=0, safety_margin_tokens=0)
    actual = probe.get_context_tokens(messages)
    manager = ContextManager(
        max_tokens=int(actual / 0.76),
        reserved_output_tokens=0,
        safety_margin_tokens=0,
        summarize_keep_recent_turns=5,
        ui_bus=ui_bus,
    )
    _observe(manager, messages, actual=actual, cached=int(actual * 0.9))

    assert manager.maybe_compress(messages, SummaryLLM()) is True

    checkpoint = manager.checkpoints[-1]
    assert checkpoint.trigger == "semantic_wall"
    assert "partial_prefix" in checkpoint.strategy
    assert messages[0]["role"] == "system"
    remaining_users = [
        message["content"] for message in messages if message.get("role") == "user"
    ]
    assert [content.split()[1] for content in remaining_users[-5:]] == [
        str(index) for index in range(4, 9)
    ]
    assert ui_bus.history_snapshot()[-1].data["phase"] == "after"


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


def test_reconfigure_updates_compression_walls_and_keeps_observation_audit() -> None:
    manager = ContextManager(max_tokens=10_000)
    messages = [{"role": "user", "content": "hello"}]
    _observe(manager, messages, actual=100)
    old_limit = manager.request_input_limit
    old_thresholds = manager.rewrite_thresholds

    manager.reconfigure(20_000)

    assert manager.latest_usage is not None
    assert manager.request_input_limit > old_limit
    assert manager.rewrite_thresholds["snip_wall"] > old_thresholds["snip_wall"]
    assert manager.rewrite_thresholds["semantic_wall"] > old_thresholds["semantic_wall"]
