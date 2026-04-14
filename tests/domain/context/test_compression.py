from reuleauxcoder.domain.context.compression import HardCollapseStrategy, SummarizeStrategy, ToolOutputSnipStrategy


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
    assert strategy.compress([{"role": "user", "content": "one"}], llm=DummyLLM("summary")) is False
    assert strategy.compress([{"role": "user", "content": "one"}] * 20, llm=None) is False


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
