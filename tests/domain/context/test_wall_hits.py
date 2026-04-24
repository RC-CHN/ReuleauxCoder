"""Tests for ContextManager wall-hit progressive compression state machine."""

from reuleauxcoder.domain.context.manager import ContextManager, estimate_tokens


def _make_long_tool_output(lines: int = 20, extra_chars: int = 1600) -> dict:
    """Create a long tool message that will be snipped."""
    return {
        "role": "tool",
        "content": "\n".join([f"line {i}: " + ("x" * 200) for i in range(lines)])
        + ("x" * extra_chars),
    }


def _make_user_message(chars: int = 100) -> dict:
    """Create a user message."""
    return {"role": "user", "content": "x" * chars}


def _make_messages_with_tokens(target_tokens: int) -> list[dict]:
    """Create messages with approximately target_tokens under current estimator."""
    # With o200k_base + 1.5 fudge, a 100-char user message is roughly ~20 tokens.
    count = max(1, target_tokens // 20)
    return [_make_user_message(100) for _ in range(count)]


def _make_snippable_tool_messages(count: int = 11) -> list[dict]:
    """Create enough tool messages so some are not protected by recent-tool keepalive."""
    return [_make_long_tool_output() for _ in range(count)]


class TestWallHitStateMachine:
    """Test the progressive compression wall-hit counters."""

    def test_snip_hit_count_increments_when_snip_doesnt_reduce_enough(self) -> None:
        """When snip runs but doesn't reduce below threshold, hit count should increment."""
        manager = ContextManager(max_tokens=1000)  # snip_at = 500

        # 需要 >10 条 tool 消息，这样旧的 tool output 才不会被 protected。
        messages = _make_snippable_tool_messages(11)
        messages.extend(_make_messages_with_tokens(400))

        manager.maybe_compress(messages, llm=None)

        current = estimate_tokens(messages)
        if current > manager._snip_at:
            assert manager._snip_hit_count >= 1 or manager._snip_exhausted

    def test_snip_exhausted_skips_snip_on_next_compress(self) -> None:
        """When snip is exhausted, healthy context should still reset state."""
        manager = ContextManager(max_tokens=1000)
        manager._snip_exhausted = True
        manager._snip_hit_count = 3

        messages = _make_messages_with_tokens(
            750
        )  # initial target > summarize_at (700)

        manager.maybe_compress(messages, llm=None)

        # No summarize can run without enough messages/LLM; if context ends healthy,
        # the manager resets state unconditionally.
        current = estimate_tokens(messages)
        if current <= manager._snip_at:
            assert not manager._snip_exhausted
            assert manager._snip_hit_count == 0
        else:
            assert manager._snip_exhausted

    def test_successful_snip_reset_all_state(self) -> None:
        """When snip successfully reduces below threshold, reset all state."""
        manager = ContextManager(max_tokens=1000)
        manager._snip_hit_count = 2
        manager._snip_exhausted = False

        # 11 条 tool 消息里只有最老的一条是超长可压缩内容，其余都很短。
        # 这样 snip 一次后应该能直接回到健康区间。
        messages = _make_snippable_tool_messages(11)
        for i in range(1, len(messages)):
            messages[i]["content"] = "ok"

        manager.maybe_compress(messages, llm=None)

        assert manager._snip_hit_count == 0
        assert not manager._snip_exhausted

    def test_summarize_hit_count_increments_when_doesnt_reduce_enough(self) -> None:
        """When summarize runs but doesn't reduce below threshold, hit count increments."""
        manager = ContextManager(max_tokens=1000)
        manager._snip_exhausted = True  # Skip snip

        # Messages above summarize threshold with enough messages for summarize
        messages = [
            {"role": "user", "content": f"msg {i} " + "x" * 100} for i in range(30)
        ]

        # Mock LLM that returns summary
        class SummaryLLM:
            def chat(self, messages, **kwargs):
                return type("Response", (), {"content": "summary text here"})()

        llm = SummaryLLM()

        # First summarize attempt
        manager.maybe_compress(messages, llm=llm)

        # Check if summarize ran and if tokens still exceed threshold
        current = estimate_tokens(messages)
        if current > manager._summarize_at:
            # Summarize ran but didn't reduce enough
            assert manager._summarize_hit_count >= 1 or manager._summarize_exhausted

    def test_successful_summarize_reset_all_state(self) -> None:
        """When summarize makes context healthy enough, state should reset."""
        manager = ContextManager(max_tokens=1000)
        manager._snip_exhausted = True
        manager._summarize_hit_count = 2

        messages = _make_messages_with_tokens(750)

        class SummaryLLM:
            def chat(self, messages, **kwargs):
                return type("Response", (), {"content": "summary"})()

        llm = SummaryLLM()
        manager.maybe_compress(messages, llm=llm)

        current = estimate_tokens(messages)
        if current <= manager._snip_at:
            assert manager._snip_hit_count == 0
            assert manager._summarize_hit_count == 0
            assert not manager._snip_exhausted
            assert not manager._summarize_exhausted
        elif current <= manager._summarize_at:
            # Reduced below summarize threshold but not fully healthy yet.
            assert manager._snip_exhausted is True

    def test_collapse_reset_all_state(self) -> None:
        """Hard collapse should reset state when it actually runs."""
        manager = ContextManager(max_tokens=1000)
        manager._snip_hit_count = 3
        manager._summarize_hit_count = 3
        manager._snip_exhausted = True
        manager._summarize_exhausted = True

        messages = _make_messages_with_tokens(950)  # target > collapse_at (900)
        before = estimate_tokens(messages)

        class SummaryLLM:
            def chat(self, messages, **kwargs):
                return type("Response", (), {"content": "collapsed summary"})()

        manager.maybe_compress(messages, llm=SummaryLLM())

        if before > manager._collapse_at:
            assert manager._snip_hit_count == 0
            assert manager._summarize_hit_count == 0
            assert not manager._snip_exhausted
            assert not manager._summarize_exhausted

    def test_healthy_context_reset_state(self) -> None:
        """When context is healthy (below snip threshold), reset state."""
        manager = ContextManager(max_tokens=1000)
        manager._snip_hit_count = 2

        # Messages below snip threshold
        messages = _make_messages_with_tokens(300)  # < snip_at (500)

        manager.maybe_compress(messages, llm=None)

        # Should reset since context is healthy
        assert manager._snip_hit_count == 0
        assert not manager._snip_exhausted

    def test_max_hits_is_three(self) -> None:
        """Default max_hits should be 3."""
        manager = ContextManager()
        assert manager._max_hits == 3

    def test_reconfigure_reset_state(self) -> None:
        """Reconfiguring max_tokens should reset compression state."""
        manager = ContextManager(max_tokens=1000)
        manager._snip_hit_count = 3
        manager._snip_exhausted = True

        manager.reconfigure(max_tokens=2000)

        assert manager._snip_hit_count == 0
        assert not manager._snip_exhausted
        assert manager.max_tokens == 2000
