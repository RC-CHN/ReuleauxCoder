from types import SimpleNamespace

from reuleauxcoder.app.commands.models import CommandEffect
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.capabilities import UICapability, UIProfile
from reuleauxcoder.domain.context.summary import CompactionError
from reuleauxcoder.extensions.command.builtin.compact import (
    CompactContextCommand, _handle_compact, register_actions,
)


def context():
    calls = []
    agent = SimpleNamespace(messages=[], llm=None)
    agent.context = SimpleNamespace(
        predict_request_tokens=lambda messages: 1200,
        checkpoints=[SimpleNamespace(tokens_before=1200, tokens_after=400)],
    )
    def compress(strategy, llm):
        calls.append(strategy)
        return True
    agent.force_compress_context = compress
    return SimpleNamespace(agent=agent, effect=CommandEffect()), calls


def test_compact_runs_one_unified_operation():
    ctx, calls = context()
    _handle_compact(CompactContextCommand(), ctx)
    assert calls == ["summarize"]
    assert "1,200 → 400" in ctx.effect.notifications[-1].message
    assert not ctx.effect.views


def test_summary_failure_is_reported_without_claiming_success():
    ctx, _ = context()
    def fail(*args):
        raise CompactionError("Summary was truncated")
    ctx.agent.force_compress_context = fail
    _handle_compact(CompactContextCommand(), ctx)
    assert "truncated" in ctx.effect.notifications[-1].message


def test_catalog_offers_single_compact_command():
    registry = ActionRegistry()
    register_actions(registry)
    profile = UIProfile("cli", "CLI", frozenset({UICapability.TEXT_INPUT}))
    parsed = registry.parse("/compact", ui_profile=profile)
    assert parsed.command == CompactContextCommand()
    assert not parsed.action.preview
