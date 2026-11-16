"""Compaction through real request hooks, provider projection and the agent loop."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import Config, ContextConfig
from reuleauxcoder.domain.context.summary import CompactionError
from reuleauxcoder.domain.hooks.types import HookPoint
from reuleauxcoder.domain.llm.errors import ContextWindowExceeded
from reuleauxcoder.domain.llm.context_messages import normalize_provider_message_roles
from reuleauxcoder.extensions.hooks.builtin.project_context import ProjectContextHook
from reuleauxcoder.services.llm.client import LLM
from tests.domain.context.test_summary import checkpoint_text
from tests.domain.context.test_wall_hits import history


def stream(text, *, cached=0, finish="stop"):
    yield SimpleNamespace(
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=text, reasoning_content=None, tool_calls=None),
            finish_reason=finish,
        )],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                              prompt_cache_hit_tokens=cached),
    )


def setup_agent(tmp_path, monkeypatch, *, mode="chat-completions", fraction=.85):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENT.md").write_text("Project constraint: preserve stored data.")
    config = Config(api_key="test", session_auto_save=False,
                    context=ContextConfig(reserved_output_tokens=512,
                                          safety_margin_tokens=128, summary_max_tokens=512))
    llm = LLM(model="test-model", api_key="test", max_tokens=512, request_mode=mode)
    agent = Agent(llm=llm, tools=[], config=config)
    agent.current_session_id = "compaction-test"
    agent.hook_registry.register(HookPoint.BEFORE_LLM_REQUEST, ProjectContextHook())
    for message in history():
        agent._append_message(message, source="test")
    prepared = agent._loop._projection.compaction_request()
    tokens = agent.context.estimate_request_tokens(prepared.messages, prepared.tools)
    agent.context.reconfigure(int(tokens / fraction))
    return agent, llm, prepared


@pytest.mark.parametrize("mode", ["chat-completions", "responses"])
def test_summary_uses_prepared_prefix_then_resumes_once(tmp_path, monkeypatch, mode):
    agent, llm, prepared = setup_agent(tmp_path, monkeypatch, mode=mode)
    phases = []
    llm._emit_operation_phase = lambda **phase: phases.append(phase)
    calls = []
    def send(params):
        calls.append(deepcopy(params))
        summary = 'source="context_compactor"' in params["messages"][-1]["content"]
        return stream(checkpoint_text() if summary else "done", cached=80)
    llm._call_with_retry = send
    try:
        assert agent._loop.run() == "done"
        assert len(calls) == 2
        summary, resumed = calls
        assert summary["messages"][:-1] == normalize_provider_message_roles(prepared.messages[:len(summary["messages"]) - 1])
        assert sum(str(m.get("content")).startswith("<project_context ") for m in summary["messages"]) == 1
        assert summary.get("tools", []) == resumed.get("tools", [])
        if mode == "responses":
            assert summary["_rc_session_id"] == resumed["_rc_session_id"] == "compaction-test"
            from reuleauxcoder.services.llm.providers import _responses_request
            assert _responses_request(summary, cache_mode="implicit")["prompt_cache_key"] == _responses_request(resumed, cache_mode="implicit")["prompt_cache_key"]
        assert agent.context.cache_epoch == 1
        assert agent.context.checkpoints[-1].summary_cached_input_tokens == 80
        assert agent.state.total_model_calls == 2
        assert all(phase["status"] != "failed" for phase in phases)
        assert any(event.kind == "request_attempt_deferred" for event in agent.history_ledger.events)
        assert any(event.kind == "compaction_response" for event in agent.history_ledger.events)
        assert len(agent.messages) < len(prepared.history_positions)
    finally:
        llm.client.close()


def test_explicit_provider_overflow_recovers_below_normal_threshold(tmp_path, monkeypatch):
    agent, llm, _ = setup_agent(tmp_path, monkeypatch, fraction=.5)
    calls = []
    def send(params):
        calls.append(deepcopy(params))
        if len(calls) == 1:
            raise ContextWindowExceeded("provider context limit")
        if 'source="context_compactor"' in params["messages"][-1]["content"]:
            return stream(checkpoint_text())
        return stream("recovered")
    llm._call_with_retry = send
    try:
        assert agent._loop.run() == "recovered"
        assert len(calls) == 3
        assert agent.context.checkpoints[-1].trigger == "context-overflow"
    finally:
        llm.client.close()


def test_overflow_does_not_retry_after_failed_summary(tmp_path, monkeypatch):
    agent, llm, _ = setup_agent(tmp_path, monkeypatch, fraction=.5)
    calls = []
    def send(params):
        calls.append(params)
        if len(calls) == 1:
            raise ContextWindowExceeded("original overflow")
        return stream("invalid checkpoint")
    llm._call_with_retry = send
    original = deepcopy(agent.messages)
    try:
        with pytest.raises(ContextWindowExceeded, match="original overflow"):
            agent._loop.run()
        assert len(calls) == 2
        assert normalize_provider_message_roles(agent.messages) == normalize_provider_message_roles(original)
        assert agent.context.cache_epoch == 0
    finally:
        llm.client.close()


def test_manual_compaction_preserves_failed_history(tmp_path, monkeypatch):
    agent, llm, _ = setup_agent(tmp_path, monkeypatch)
    llm._call_with_retry = lambda params: stream(checkpoint_text(), finish="length")
    original = deepcopy(agent.messages)
    try:
        with pytest.raises(CompactionError, match="truncated"):
            agent.force_compress_context("summarize", llm)
        assert agent.messages == original
        assert not agent.context.checkpoints
    finally:
        llm.client.close()


def test_provider_overflow_recovery_is_bounded(tmp_path, monkeypatch):
    agent, llm, _ = setup_agent(tmp_path, monkeypatch, fraction=.5)
    calls = []
    def send(params):
        calls.append(params)
        if 'source="context_compactor"' in params["messages"][-1]["content"]:
            return stream(checkpoint_text())
        raise ContextWindowExceeded("still too large")
    llm._call_with_retry = send
    try:
        with pytest.raises(ContextWindowExceeded, match="still too large"):
            agent._loop.run()
        assert len(calls) == 3  # request, summary, one retried request
        assert agent.context.cache_epoch == 1
    finally:
        llm.client.close()


def test_history_change_during_summary_does_not_commit(tmp_path, monkeypatch):
    agent, llm, _ = setup_agent(tmp_path, monkeypatch)
    original = normalize_provider_message_roles(deepcopy(agent.messages))
    record_response = agent._record_compaction_response
    def record_and_mutate(response):
        record_response(response)
        agent._append_message({"role": "user", "content": "new constraint"}, source="test")
    agent._record_compaction_response = record_and_mutate
    llm._call_with_retry = lambda params: stream(checkpoint_text())
    try:
        with pytest.raises(CompactionError, match="History changed"):
            agent.force_compress_context("summarize", llm)
        assert normalize_provider_message_roles(agent.messages[:-1]) == original
        assert agent.messages[-1]["content"] == "new constraint"
        assert agent.context.cache_epoch == 0
    finally:
        llm.client.close()


def test_automatic_compaction_preserves_image_projection_and_correlates_usage(tmp_path, monkeypatch):
    from reuleauxcoder.domain.images import ChatInput, ImageConfig, image_parts
    from reuleauxcoder.infrastructure.persistence.images import ImageStore
    from tests.domain.test_images import picture
    agent, llm, _ = setup_agent(tmp_path, monkeypatch)
    llm.support_modal = ("text", "image")
    agent.image_store = llm.image_store = ImageStore(tmp_path, ImageConfig())
    picture_ref = agent.image_store.import_bytes(agent.current_session_id, picture(size=(80, 60)))
    agent.messages[0]["content"] = ChatInput("preserve this image", (picture_ref,)).content("image-turn")
    agent.messages[0].pop("_rc_token_count", None)
    calls = []
    def send(params):
        calls.append(deepcopy(params))
        return stream(checkpoint_text() if len(calls) == 1 else "done")
    llm._call_with_retry = send
    try:
        assert agent._loop.run() == "done"
        assert len(calls) == 2
        assert image_parts(calls[0]["messages"])
        assert not image_parts(calls[1]["messages"])
        summary_event = next(event for event in agent.history_ledger.events if event.kind == "compaction_response")
        assert summary_event.payload["image_attempts"][0]["image_count"] == 1
        assert not any(event.kind == "image_payload_observed" for event in agent.history_ledger.events)
    finally:
        llm.client.close()


def test_final_handoff_uses_the_same_compaction_path(tmp_path, monkeypatch):
    agent, llm, _ = setup_agent(tmp_path, monkeypatch)
    agent.max_rounds = 0
    calls = []
    def send(params):
        calls.append(params)
        return stream(checkpoint_text() if len(calls) == 1 else "handoff")
    llm._call_with_retry = send
    try:
        assert agent._loop.run() == "handoff"
        assert len(calls) == 2
        assert agent.context.cache_epoch == 1
        assert 'source="context_compactor"' in calls[0]["messages"][-1]["content"]
    finally:
        llm.client.close()
