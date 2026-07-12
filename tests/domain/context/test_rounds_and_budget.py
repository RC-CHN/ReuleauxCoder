import json

from reuleauxcoder.domain.context.budget import ContextBudget
from reuleauxcoder.domain.context.checkpoint import CompactionCheckpoint
from reuleauxcoder.domain.context.manager import ContextManager
from reuleauxcoder.domain.context.rounds import group_api_rounds, recent_round_start
from reuleauxcoder.domain.context.provider import ProviderCompactionResult


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

    local = manager.get_context_tokens(messages)
    manager.observe_usage(
        actual_prompt_tokens=1_300,
        cached_input_tokens=900,
        local_request_estimate=local,
        local_history_estimate=local,
        request_boundary="turn:0",
        model_profile="test",
    )

    assert manager.maybe_compress(messages) is True
    assert manager.history_version == 1
    assert manager.checkpoints[-1].source_history_version == 0
    assert manager.checkpoints[-1].replacement_history
    assert manager.checkpoints[-1].cache_epoch == 1


def test_provider_compaction_uses_provider_neutral_boundary() -> None:
    class Adapter:
        def compact_tool_results(self, messages, *, keep_recent_rounds):
            assert keep_recent_rounds == 2
            return ProviderCompactionResult(
                messages=[{"role": "user", "content": "provider compacted"}]
            )

    manager = ContextManager(
        max_tokens=2_000,
        reserved_output_tokens=0,
        safety_margin_tokens=0,
        provider_compactor=Adapter(),
    )
    messages = [{"role": "user", "content": "alpha beta gamma " * 1_000}]
    assert manager.maybe_compress(messages) is True
    assert messages[0]["content"] == "provider compacted"
    assert manager.checkpoints[-1].strategy == ("provider_tool_cache_compaction",)


def test_phase_boundary_commits_semantic_checkpoint_below_token_wall() -> None:
    manager = ContextManager(
        max_tokens=100_000,
        reserved_output_tokens=0,
        safety_margin_tokens=0,
        summarize_keep_recent_turns=2,
    )
    messages = [
        {"role": "assistant", "content": f"completed phase detail {index}"}
        for index in range(8)
    ]

    manager.mark_phase_boundary()

    assert manager.maybe_compress(messages) is True
    assert manager.checkpoints[-1].strategy == ("phase_checkpoint",)
    summary = json.loads(messages[0]["content"].split("\n", 1)[1])
    assert summary["scope"]["checkpoint_kind"] == "phase_checkpoint"
    assert summary["scope"]["recent_rounds_preserved"] == 2


def test_resume_restores_snip_starvation_state_from_checkpoint_epochs() -> None:
    history = [{"role": "assistant", "content": f"round {index}"} for index in range(3)]

    def checkpoint(strategy):
        return CompactionCheckpoint.create(
            trigger="quality_wall",
            strategy=[strategy],
            source_history_version=0,
            replacement_history=history,
            tokens_before=100,
            tokens_after=50,
            preserved_rounds=2,
        )

    manager = ContextManager()
    manager.restore_checkpoints(
        [
            checkpoint("partial_prefix"),
            checkpoint("snip_tool_outputs"),
            checkpoint("provider_tool_cache_compaction"),
        ]
    )

    assert manager._snip_epochs_since_summary == 2
    assert manager._rounds_at_last_semantic_checkpoint == 3
