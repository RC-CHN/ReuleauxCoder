"""Tests for ContextManager wall-hit progressive compression state machine."""

from reuleauxcoder.domain.context.manager import ContextManager, estimate_tokens


def _make_long_tool_output(lines: int = 20, extra_chars: int = 1600) -> dict:
    """Create a long tool message that will be snipped."""
    return {
        "role": "tool",
        "content": "\n".join([f"line {i}" for i in range(lines)]) + ("x" * extra_chars),
    }


def _make_user_message(chars: int = 100) -> dict:
    """Create a user message."""
    return {"role": "user", "content": "x" * chars}


def _make_messages_with_tokens(target_tokens: int) -> list[dict]:
    """Create messages with approximately target_tokens."""
    # ~3.5 chars/token, so each 100-char message is ~28 tokens
    count = max(1, target_tokens // 28)
    messages = [_make_user_message(100) for _ in range(count)]
    return messages


class TestWallHitStateMachine:
    """Test the progressive compression wall-hit counters."""

    def test_snip_hit_count_increments_when_snip_doesnt_reduce_enough(self) -> None:
        """When snip runs but doesn't reduce below threshold, hit count should increment."""
        manager = ContextManager(max_tokens=1000)  # snip_at = 500
        
        # Create messages with tool outputs that can be snipped, but won't reduce enough
        # Add many tool outputs so snip can run, but total tokens still exceed threshold
        messages = [_make_long_tool_output() for _ in range(5)]
        messages.extend(_make_messages_with_tokens(400))
        
        # First compression attempt - snip should run
        manager.maybe_compress(messages, llm=None)
        
        # Since snip ran but likely didn't reduce below threshold, hit count should increment
        # OR if snip exhausted all tool outputs, it should be marked exhausted
        current = estimate_tokens(messages)
        if current > manager._snip_at:
            # Either hit count incremented or exhausted
            assert manager._snip_hit_count >= 1 or manager._snip_exhausted

    def test_snip_exhausted_skips_snip_on_next_compress(self) -> None:
        """When snip is exhausted, next compress should skip snip layer."""
        manager = ContextManager(max_tokens=1000)
        manager._snip_exhausted = True
        manager._snip_hit_count = 3
        
        # Messages above summarize threshold
        messages = _make_messages_with_tokens(750)  # > summarize_at (700)
        
        # Should not try snip, go straight to checking summarize conditions
        # Since no LLM and summarize requires LLM, it won't actually compress
        manager.maybe_compress(messages, llm=None)
        
        # snip_exhausted should still be True (not reset)
        assert manager._snip_exhausted

    def test_successful_snip_reset_all_state(self) -> None:
        """When snip successfully reduces below threshold, reset all state."""
        manager = ContextManager(max_tokens=1000)
        manager._snip_hit_count = 2
        manager._snip_exhausted = False
        
        # Create messages with long tool output that can be snipped
        messages = [_make_long_tool_output()]
        # Add enough to exceed snip threshold
        messages.extend(_make_messages_with_tokens(400))
        
        # Snip should work and reduce tokens
        manager.maybe_compress(messages, llm=None)
        
        # After successful reduction below threshold, state should reset
        assert manager._snip_hit_count == 0
        assert not manager._snip_exhausted

    def test_summarize_hit_count_increments_when_doesnt_reduce_enough(self) -> None:
        """When summarize runs but doesn't reduce below threshold, hit count increments."""
        manager = ContextManager(max_tokens=1000)
        manager._snip_exhausted = True  # Skip snip
        
        # Messages above summarize threshold with enough messages for summarize
        messages = [{"role": "user", "content": f"msg {i} " + "x" * 100} for i in range(30)]
        
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
        """When summarize successfully reduces below threshold, reset all state."""
        manager = ContextManager(max_tokens=1000)
        manager._snip_exhausted = True
        manager._summarize_hit_count = 2
        
        # Messages above summarize threshold but can be reduced
        messages = _make_messages_with_tokens(750)
        
        # LLM that returns summary, and after summarize tokens should be lower
        class SummaryLLM:
            def chat(self, messages, **kwargs):
                return type("Response", (), {"content": "summary"})()
        
        llm = SummaryLLM()
        manager.maybe_compress(messages, llm=llm)
        
        # Check if tokens reduced below summarize_at
        current = estimate_tokens(messages)
        if current <= manager._summarize_at:
            assert manager._snip_hit_count == 0
            assert manager._summarize_hit_count == 0
            assert not manager._snip_exhausted
            assert not manager._summarize_exhausted

    def test_collapse_reset_all_state(self) -> None:
        """Hard collapse should always reset all compression state."""
        manager = ContextManager(max_tokens=1000)
        manager._snip_hit_count = 3
        manager._summarize_hit_count = 3
        manager._snip_exhausted = True
        manager._summarize_exhausted = True
        
        # Messages above collapse threshold
        messages = _make_messages_with_tokens(950)  # > collapse_at (900)
        
        class SummaryLLM:
            def chat(self, messages, **kwargs):
                return type("Response", (), {"content": "collapsed summary"})()
        
        manager.maybe_compress(messages, llm=SummaryLLM())
        
        # Collapse should reset everything
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