from copy import deepcopy

import pytest

from reuleauxcoder.domain.context.manager import ContextManager
from reuleauxcoder.domain.context.replay import canonicalize
from reuleauxcoder.domain.context.summary import CompactionError, CompactionRequest
from reuleauxcoder.app.ui_events import UIEventBus
from tests.domain.context.test_summary import SummaryLLM, checkpoint_text


def history(rounds=30):
    messages = [{"role": "user", "content": "Implement storage; must keep the existing file format."}]
    for index in range(rounds):
        messages += [
            {"role": "assistant", "content": f"step {index}", "tool_calls": [
                {"id": f"call-{index}", "type": "function",
                 "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"call-{index}",
             "content": "source detail " * 140},
        ]
    return messages


def request(messages, system="stable system"):
    return CompactionRequest(
        messages=[{"role": "system", "content": system}, *deepcopy(messages),
                  {"role": "user", "content": "<execution_state>now</execution_state>"}],
        tools=[{"type": "function", "function": {"name": "read", "parameters": {}}}],
        history_positions=tuple(range(1, len(messages) + 1)),
        output_tokens=512, session_id="session",
    )


def manager_for(messages, fraction=.85, **kwargs):
    sample = request(messages)
    estimate = ContextManager().estimate_request_tokens(sample.messages, sample.tools)
    return ContextManager(max_tokens=int(estimate / fraction),
                          reserved_output_tokens=512, safety_margin_tokens=128,
                          summary_max_tokens=512, **kwargs)


def test_no_early_snip_or_cache_rewrite():
    messages = history()
    original = deepcopy(messages)
    manager = manager_for(messages, fraction=.65)
    llm = SummaryLLM()
    assert not manager.maybe_compress(messages, llm, request=request(messages))
    assert messages == original
    assert not llm.calls
    assert manager.cache_epoch == 0


def test_one_user_many_tool_rounds_compacts_and_preserves_exact_recent_tail():
    messages = history()
    original = deepcopy(messages)
    ui = UIEventBus()
    manager = manager_for(messages, ui_bus=ui)
    llm = SummaryLLM()
    assert manager.maybe_compress(messages, llm, request=request(messages))
    assert len(llm.calls) == 1
    assert messages[0]["content"].startswith("<context_summary")
    assert messages[-8:] == original[-8:]
    assert llm.calls[0]["messages"][0]["content"] == "stable system"
    assert all("context-snipped" not in str(item) for item in llm.calls[0]["messages"])
    checkpoint = manager.checkpoints[-1]
    assert checkpoint.tokens_after < manager.pressure_threshold(512)
    assert checkpoint.tokens_after < checkpoint.tokens_before
    assert checkpoint.preserved_rounds > 0
    assert checkpoint.summary_cached_input_tokens == 80
    assert manager.cache_epoch == 1
    assert [event.data["phase"] for event in ui.history_snapshot()] == ["before", "after"]


def test_disabled_auto_does_not_disable_manual_checkpoint():
    messages = history()
    manager = manager_for(messages, auto_compact=False)
    assert not manager.maybe_compress(messages, SummaryLLM(), request=request(messages))
    assert manager.force_compress(messages, "summarize", SummaryLLM(), request=request(messages))


@pytest.mark.parametrize("llm", [
    SummaryLLM(checkpoint_text("large repeated summary " * 10_000)),
    SummaryLLM("invalid output"),
    SummaryLLM(checkpoint_text(), finish_reason="length"),
])
def test_rejected_summary_preserves_history_and_epoch(llm):
    messages = history()
    original = deepcopy(messages)
    manager = manager_for(messages)
    with pytest.raises(CompactionError):
        manager.maybe_compress(messages, llm, request=request(messages))
    assert messages == original
    assert manager.cache_epoch == 0
    assert not manager.checkpoints


def test_model_failure_preserves_history_and_reports_failure():
    class Failure:
        def chat(self, **kwargs):
            raise RuntimeError("offline")
    messages = history()
    original = deepcopy(messages)
    ui = UIEventBus()
    manager = manager_for(messages, ui_bus=ui)
    with pytest.raises(CompactionError, match="RuntimeError"):
        manager.maybe_compress(messages, Failure(), request=request(messages))
    assert messages == original
    assert ui.history_snapshot()[-1].data["phase"] == "failed"


def test_complete_request_overhead_drives_pressure_and_no_unbounded_retry():
    messages = history()
    original = deepcopy(messages)
    manager = ContextManager(max_tokens=40_000, reserved_output_tokens=512,
                             safety_margin_tokens=128, summary_max_tokens=512,
                             threshold_ratio=.3, retain_ratio=.1, compaction_retries=1)
    llm = SummaryLLM()
    with pytest.raises(CompactionError):
        manager.maybe_compress(messages, llm, request=request(messages, system="fixed " * 10_000))
    assert len(llm.calls) <= 2
    assert messages == original
    assert manager.cache_epoch == 0


def test_usage_calibration_survives_rewrite():
    messages = history()
    manager = manager_for(messages)
    prepared = request(messages)
    local = manager.estimate_request_tokens(prepared.messages, prepared.tools)
    manager.observe_usage(actual_prompt_tokens=local, cached_input_tokens=100,
                          local_request_estimate=local,
                          local_history_estimate=manager.get_context_tokens(messages),
                          request_boundary="turn:0", model_profile="model")
    assert manager.predict_request_tokens(prepared.messages, prepared.tools, complete=True) == local
    assert manager.maybe_compress(messages, SummaryLLM(), request=prepared)
    assert manager._estimate_scale_by_profile["model"] == 1
    assert manager.checkpoints[-1].cached_input_tokens == 100


def test_source_references_and_checkpoint_round_trip():
    from reuleauxcoder.domain.history import HistoryLedger
    from reuleauxcoder.domain.context.checkpoint import CompactionCheckpoint
    messages = history()
    ledger = HistoryLedger(session_id="session")
    events = [ledger.append_message(message, source="test") for message in messages]
    manager = manager_for(messages)
    assert manager.maybe_compress(messages, SummaryLLM(), request=request(messages),
                                  history_events=ledger.events)
    checkpoint = manager.checkpoints[-1]
    assert events[0].event_id in checkpoint.source_event_ids
    assert CompactionCheckpoint.from_dict(checkpoint.to_dict()) == checkpoint


def test_cancellation_after_summary_does_not_commit():
    import threading
    from reuleauxcoder.domain.llm.errors import LLMRequestCancelled
    cancelled = threading.Event()
    class CancelAfterResponse(SummaryLLM):
        def chat(self, **kwargs):
            response = super().chat(**{k: v for k, v in kwargs.items() if k != "cancellation_event"})
            cancelled.set()
            return response
    messages = history()
    original = deepcopy(messages)
    manager = manager_for(messages)
    with pytest.raises(LLMRequestCancelled):
        manager.maybe_compress(messages, CancelAfterResponse(), request=request(messages),
                               cancellation_event=cancelled)
    assert messages == original
    assert manager.cache_epoch == 0


def test_second_compaction_receives_prior_checkpoint_and_subsequent_work():
    messages = history()
    manager = manager_for(messages)
    first = SummaryLLM(checkpoint_text("Original constraint: preserve the file format."))
    assert manager.force_compress(messages, "summarize", first, request=request(messages))
    checkpoint = deepcopy(messages[0])
    messages.extend(history())
    second = SummaryLLM()
    assert manager.force_compress(messages, "summarize", second, request=request(messages))
    assert canonicalize(second.calls[0]["messages"][1]) == canonicalize(checkpoint)
    assert any(item.get("role") == "tool" for item in second.calls[0]["messages"])
    assert manager.cache_epoch == 2
