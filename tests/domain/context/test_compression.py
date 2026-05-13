from reuleauxcoder.domain.context.compression import (
    HardCollapseStrategy,
    SummarizeStrategy,
    ToolOutputSnipStrategy,
)
from reuleauxcoder.domain.context.manager import ContextManager
from reuleauxcoder.interfaces.events import UIEventBus


class DummyResponse:
    def __init__(self, content: str):
        self.content = content


class DummyLLM:
    def __init__(self, content: str):
        self.content = content

    def chat(self, messages):
        return DummyResponse(self.content)


class FailingLLM:
    def chat(self, messages):
        raise RuntimeError("boom")


def test_tool_output_snip_strategy_truncates_long_tool_message() -> None:
    messages = [
        {
            "role": "tool",
            "content": "\n".join([f"line {i}" for i in range(20)]) + ("x" * 1600),
        }
    ]

    changed = ToolOutputSnipStrategy().compress(messages)

    assert changed is True
    assert "snipped" in messages[0]["content"]
    assert "line 0" in messages[0]["content"]


def test_tool_output_snip_strategy_skips_short_or_non_tool_messages() -> None:
    messages = [
        {"role": "assistant", "content": "x" * 2000},
        {"role": "tool", "content": "short"},
    ]

    changed = ToolOutputSnipStrategy().compress(messages)

    assert changed is False
    assert messages[0]["content"] == "x" * 2000
    assert messages[1]["content"] == "short"


def test_summarize_strategy_requires_llm_and_enough_messages() -> None:
    strategy = SummarizeStrategy()
    assert (
        strategy.compress([{"role": "user", "content": "one"}], llm=DummyLLM("summary"))
        is False
    )
    assert (
        strategy.compress([{"role": "user", "content": "one"}] * 20, llm=None) is False
    )


def test_summarize_strategy_replaces_old_messages_with_summary() -> None:
    messages = [{"role": "user", "content": f"msg {i}"} for i in range(12)]

    changed = SummarizeStrategy().compress(messages, llm=DummyLLM("summary text"))

    assert changed is True
    assert messages[0]["content"].startswith("[Context compressed]")
    assert "summary text" in messages[0]["content"]
    assert messages[1]["role"] == "assistant"
    assert len(messages) == 10


def test_summarize_strategy_returns_false_when_llm_errors() -> None:
    messages = [{"role": "user", "content": f"msg {i}"} for i in range(12)]
    original = list(messages)

    changed = SummarizeStrategy().compress(messages, llm=FailingLLM())

    assert changed is False
    assert messages == original


def test_hard_collapse_strategy_keeps_tail_with_reset_markers() -> None:
    messages = [{"role": "user", "content": f"msg {i}"} for i in range(8)]

    changed = HardCollapseStrategy().compress(messages)

    assert changed is True
    assert messages[0]["content"] == "[Hard context reset - older messages dropped]"
    assert messages[1]["content"] == "Context reset. Continuing."
    assert len(messages) == 6


def test_hard_collapse_strategy_skips_when_message_count_small() -> None:
    messages = [{"role": "user", "content": f"msg {i}"} for i in range(4)]
    assert HardCollapseStrategy().compress(messages) is False


# ---------------------------------------------------------------------------
# Integration: token count actually drops after snip
# ---------------------------------------------------------------------------

def _build_realistic_conversation(
    rounds: int = 4,
    large_tool_lines: int = 200,
) -> list[dict]:
    """Build a conversation with alternating user/assistant and big tool outputs."""
    msgs: list[dict] = [
        {"role": "system", "content": "You are a coding assistant."},
        {"role": "user", "content": "Refactor the auth module."},
    ]
    for r in range(rounds):
        msgs.append({
            "role": "assistant",
            "content": f"Working on round {r}...",
            "tool_calls": [{"id": f"tc_{r}", "name": "read_file", "arguments": {}}],
        })
        # Large tool output simulating an override read
        msgs.append({
            "role": "tool",
            "tool_call_id": f"tc_{r}",
            "content": "\n".join(
                f"line {i}: {'x' * 30}" for i in range(large_tool_lines)
            ),
        })
    return msgs


def test_snip_reduces_token_count() -> None:
    """After _snip_tool_outputs runs, get_context_tokens returns fewer tokens."""
    messages = _build_realistic_conversation(rounds=5, large_tool_lines=200)

    mgr = ContextManager(
        max_tokens=1_000_000,
        ui_bus=UIEventBus(),
        snip_keep_recent_tools=2,
    )

    before = mgr.get_context_tokens(messages)
    # Force snip
    changed = mgr._snip_tool_outputs(messages)
    after = mgr.get_context_tokens(messages)

    assert changed is True
    assert after < before, f"Expected token count to drop, got {before} → {after}"


def test_maybe_compress_snip_counts_after_change() -> None:
    """maybe_compress with snip-only path: after-token count reflects the snip."""
    messages = _build_realistic_conversation(rounds=6, large_tool_lines=300)

    # Use a small max_tokens so the snip threshold is easily crossed
    mgr = ContextManager(
        max_tokens=30_000,
        ui_bus=UIEventBus(),
        snip_keep_recent_tools=2,
    )

    before = mgr.get_context_tokens(messages)
    compressed = mgr.maybe_compress(messages)
    after = mgr.get_context_tokens(messages)

    assert compressed is True, f"maybe_compress returned False (tokens={before})"
    assert after < before, f"maybe_compress snip: {before} → {after}"


def test_snip_invalidates_token_cache() -> None:
    """After _snip_tool_outputs mutates content, _rc_token_count cache is cleared."""
    messages = _build_realistic_conversation(rounds=3, large_tool_lines=300)

    mgr = ContextManager(max_tokens=1_000_000)

    # Prime the cache
    mgr.get_context_tokens(messages)
    for m in messages:
        if m.get("role") == "tool" and len(m.get("content", "")) > 1500:
            assert "_rc_token_count" in m, "Cache should be primed"

    changed = mgr._snip_tool_outputs(messages)
    assert changed

    # All snipped messages should have their cache cleared
    for m in messages:
        if m.get("role") == "tool" and "snipped" in m.get("content", ""):
            assert "_rc_token_count" not in m, (
                f"Snipped message still has stale _rc_token_count={m.get('_rc_token_count')}"
            )
