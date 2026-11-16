from copy import deepcopy

import pytest

from reuleauxcoder.domain.context.summary import (
    CompactionError, SUMMARY_SECTIONS, generate_summary,
)
from reuleauxcoder.domain.llm.models import LLMResponse


def checkpoint_text(detail="Preserve the original task and its constraints."):
    return "\n\n".join(f"## {heading}\n- {detail}" for heading in SUMMARY_SECTIONS)


class SummaryLLM:
    def __init__(self, content=None, *, finish_reason="stop", tool_calls=()):
        self.calls = []
        self.content = checkpoint_text() if content is None else content
        self.finish_reason = finish_reason
        self.tool_calls = list(tool_calls)

    def chat(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return LLMResponse(content=self.content, finish_reason=self.finish_reason,
                           tool_calls=self.tool_calls, prompt_tokens=100,
                           completion_tokens=50, cached_input_tokens=80)


def test_summary_reuses_full_prefix_tools_route_metadata_and_native_reasoning():
    prefix = [{"role": "system", "content": "original system"},
              {"role": "user", "content": "<context_summary>old constraints</context_summary>"}]
    prefix += [{"role": "assistant", "content": f"round {i} " + "detail " * 500}
               for i in range(30)]
    prefix[-1]["provider_data"] = {"request_mode": "responses",
                                  "items": [{"type": "reasoning", "encrypted_content": "opaque"}]}
    original = deepcopy(prefix)
    tools = [{"type": "function", "function": {"name": "read", "parameters": {}}}]
    llm = SummaryLLM()
    response = generate_summary(prefix, llm, tools=tools, session_id="same-session",
                                metadata={"image_turn_id": "turn"}, max_output_tokens=8192)
    call = llm.calls[0]
    assert prefix == original
    assert call["messages"][:-1] == prefix
    assert call["tools"] == tools
    assert call["session_id"] == "same-session"
    assert call["metadata"] == {"image_turn_id": "turn", "purpose": "compaction", "volatile_tail": True}
    assert "PRIOR checkpoint" in call["messages"][-1]["content"]
    assert response.cached_input_tokens == 80
    assert len(llm.calls) == 1


@pytest.mark.parametrize("content,finish,tools", [
    ("", "stop", []),
    ("just some text", "stop", []),
    ("\n".join(f"## {heading}" for heading in SUMMARY_SECTIONS), "stop", []),
    (checkpoint_text(), "length", []),
    (checkpoint_text(), "max_tokens", []),
    (checkpoint_text(), "incomplete", []),
    (checkpoint_text(), "stop", [object()]),
])
def test_incomplete_or_non_summary_output_never_falls_back(content, finish, tools):
    llm = SummaryLLM(content, finish_reason=finish, tool_calls=tools)
    observed = []
    with pytest.raises(CompactionError):
        generate_summary([], llm, tools=[], max_output_tokens=1024, on_response=observed.append)
    assert len(llm.calls) == 1
    assert len(observed) == 1


def test_accounting_failure_propagates_instead_of_becoming_summary_failure():
    def broken_accounting(response):
        raise OSError("ledger unavailable")
    with pytest.raises(OSError, match="ledger unavailable"):
        generate_summary([], SummaryLLM(), tools=[], max_output_tokens=1024,
                         on_response=broken_accounting)
