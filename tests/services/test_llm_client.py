import json
import threading
import time
from types import SimpleNamespace

import reuleauxcoder.services.llm.client as llm_client_module
from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.types import BeforeLLMRequestContext, HookPoint
from reuleauxcoder.domain.llm.models import (
    EMPTY_ASSISTANT_CONTENT_PLACEHOLDER,
    LLMResponse,
    ToolCall,
)
from reuleauxcoder.interfaces.events import UIEventBus, UIEventLevel
from reuleauxcoder.interfaces.events import RuntimeEventPayload
from reuleauxcoder.domain.runtime.events import OperationPhaseChanged
from reuleauxcoder.domain.runtime.performance import RuntimePerformanceMonitor
from reuleauxcoder.services.llm.client import (
    LLM,
    LLMDispatchCallbackError,
    LLMRequestCancelled,
)
from reuleauxcoder.services.llm.sanitizer import sanitize_messages_for_llm


def test_llm_response_message_backfills_empty_assistant_content() -> None:
    response = LLMResponse(reasoning_content="reasoning only")

    assert response.message["content"] == EMPTY_ASSISTANT_CONTENT_PLACEHOLDER
    assert response.message["reasoning_content"] == "reasoning only"
    assert "tool_calls" not in response.message


def test_llm_response_message_keeps_null_content_for_tool_calls() -> None:
    response = LLMResponse(
        tool_calls=[ToolCall(id="tool_1", name="glob", arguments={})],
    )

    assert response.message["content"] is None
    assert response.message["tool_calls"][0]["id"] == "tool_1"


def test_sanitize_messages_backfills_reasoning_only_assistant_content() -> None:
    messages = [
        {
            "role": "assistant",
            "reasoning_content": "reasoning only",
        }
    ]

    sanitized = sanitize_messages_for_llm(messages, preserve_reasoning_content=True)

    assert sanitized[0]["content"] == EMPTY_ASSISTANT_CONTENT_PLACEHOLDER
    assert "reasoning_content" not in sanitized[0]


def test_sanitize_messages_backfills_empty_assistant_content_in_tool_turn() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "need tool first",
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
        {
            "role": "assistant",
            "reasoning_content": "reasoning only after tool",
        },
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        reasoning_replay_mode="tool_calls",
    )

    assert sanitized[2]["content"] == EMPTY_ASSISTANT_CONTENT_PLACEHOLDER
    assert sanitized[2]["reasoning_content"] == "reasoning only after tool"


def test_sanitize_messages_backfills_reasoning_content_for_assistant_tool_calls() -> (
    None
):
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        backfill_reasoning_content_for_tool_calls=True,
    )

    assert sanitized[0]["reasoning_content"] == "[PLACE_HOLDER]"


def test_sanitize_messages_does_not_backfill_when_disabled() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        backfill_reasoning_content_for_tool_calls=False,
    )

    assert "reasoning_content" not in sanitized[0]


def test_sanitize_messages_drops_reasoning_for_non_tool_assistant_by_default() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "done",
            "reasoning_content": "private thoughts",
        }
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
    )

    assert "reasoning_content" not in sanitized[0]


def test_sanitize_messages_keeps_reasoning_for_tool_assistant_when_present() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "need to inspect files first",
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        require_reasoning_content_for_tool_calls=True,
    )

    assert sanitized[0]["reasoning_content"] == "need to inspect files first"


def test_sanitize_messages_does_not_require_tool_reasoning_without_replay_mode() -> (
    None
):
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        reasoning_replay_mode="none",
    )

    assert "reasoning_content" not in sanitized[0]


def test_sanitize_messages_replays_tool_reasoning_by_mode() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        reasoning_replay_mode="tool_calls",
    )

    assert sanitized[0]["reasoning_content"] == "[PLACE_HOLDER]"


def test_sanitize_messages_replays_all_assistant_reasoning_within_tool_turn() -> None:
    messages = [
        {"role": "user", "content": "question 1"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "need tool first",
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
        {
            "role": "assistant",
            "content": "final answer",
            "reasoning_content": "now I can answer",
        },
        {"role": "user", "content": "question 2"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        reasoning_replay_mode="tool_calls",
    )

    assert sanitized[1]["reasoning_content"] == "need tool first"
    assert sanitized[3]["reasoning_content"] == "now I can answer"


def test_sanitize_messages_does_not_replay_non_tool_turn_reasoning_by_mode() -> None:
    messages = [
        {"role": "user", "content": "question 1"},
        {
            "role": "assistant",
            "content": "plain answer",
            "reasoning_content": "private thoughts",
        },
        {"role": "user", "content": "question 2"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        reasoning_replay_mode="tool_calls",
    )

    assert "reasoning_content" not in sanitized[1]


def test_sanitize_messages_backfills_missing_reasoning_for_non_tool_assistant_in_tool_turn() -> (
    None
):
    """Non-tool-call assistant lacking reasoning_content in a tool-call turn
    (e.g. injected sub-agent results) must receive a placeholder when
    reasoning_replay_mode='tool_calls'."""
    messages = [
        {"role": "user", "content": "send sub-agents"},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "I need background jobs",
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {
                        "name": "agent",
                        "arguments": '{"tasks":["..."],"run_in_background":true}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "started jobs"},
        {
            "role": "assistant",
            "content": "[Background sub-agent completed]...",
            # No reasoning_content — injected by system
        },
        {"role": "user", "content": "are they done?"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        reasoning_replay_mode="tool_calls",
    )

    # Tool-call assistant keeps its original reasoning
    assert sanitized[1]["reasoning_content"] == "I need background jobs"
    # Injected non-tool-call assistant gets backfilled
    assert sanitized[3]["reasoning_content"] == "[PLACE_HOLDER]"


def test_sanitize_messages_fallbacks_empty_reasoning_for_tool_assistant_when_required() -> (
    None
):
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        require_reasoning_content_for_tool_calls=True,
    )

    assert sanitized[0]["reasoning_content"] == "[PLACE_HOLDER]"


def test_sanitize_messages_strips_reasoning_content_when_preserve_disabled() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "hidden",
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "glob", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "ok"},
    ]

    sanitized = sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=False,
        backfill_reasoning_content_for_tool_calls=True,
    )

    assert "reasoning_content" not in sanitized[0]


def test_sanitize_messages_repairs_tool_responses_appended_after_session_exit() -> None:
    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tool_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
                {
                    "id": "tool_2",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "tool_1", "content": "first"},
        {"role": "user", "content": "[SESSION_EXIT]"},
        {"role": "tool", "tool_call_id": "tool_2", "content": "recovered"},
        {"role": "user", "content": "[SESSION_RESUME] next"},
    ]

    sanitized = sanitize_messages_for_llm(messages)

    assert [message["role"] for message in sanitized] == [
        "assistant",
        "tool",
        "tool",
        "user",
        "user",
    ]
    assert [sanitized[1]["tool_call_id"], sanitized[2]["tool_call_id"]] == [
        "tool_1",
        "tool_2",
    ]


class _FakeUsage:
    def __init__(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int | None = None,
    ):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.prompt_tokens_details = (
            {"cached_tokens": cached_tokens} if cached_tokens is not None else None
        )


class _FakeDelta:
    def __init__(
        self, content: str = "", reasoning_content: str | None = None, tool_calls=None
    ):
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, delta):
        self.delta = delta


class _FakeChunk:
    def __init__(self, *, content: str = "", usage=None):
        self.usage = usage
        self.choices = [_FakeChoice(_FakeDelta(content=content))]


class _DeferredDispatchProbe(TransformHook[BeforeLLMRequestContext]):
    def __init__(self, callback) -> None:
        super().__init__(name="deferred_dispatch_probe")
        self._callback = callback

    def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
        context.defer_until_dispatch(self._callback)
        return context


class _RejectingRequestTransform(TransformHook[BeforeLLMRequestContext]):
    def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
        del context
        raise RuntimeError("request rejected after build")


def test_llm_commits_deferred_callback_once_before_provider_handoff() -> None:
    events: list[str] = []
    registry = HookRegistry()
    registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _DeferredDispatchProbe(lambda _context: events.append("callback")),
    )
    llm = LLM(model="demo-model", api_key="sk-test-12345678")

    def open_stream(_params):
        events.append("stream_opened")
        return iter([_FakeChunk(content="ok")])

    llm._call_with_retry = open_stream  # type: ignore[method-assign]

    response = llm.chat(
        [{"role": "user", "content": "Hi"}],
        hook_registry=registry,
    )

    assert response.content == "ok"
    assert events == ["callback", "stream_opened"]


def test_llm_keeps_dispatch_commit_when_provider_open_fails_after_handoff(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    callbacks: list[str] = []
    registry = HookRegistry()
    registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _DeferredDispatchProbe(lambda _context: callbacks.append("callback")),
    )
    llm = LLM(model="demo-model", api_key="sk-test-12345678")

    def fail_open(_params):
        raise RuntimeError("provider stream open failed")

    llm._call_with_retry = fail_open  # type: ignore[method-assign]

    try:
        llm.chat(
            [{"role": "user", "content": "Hi"}],
            hook_registry=registry,
        )
    except RuntimeError as error:
        assert str(error) == "provider stream open failed"
    else:
        raise AssertionError("provider stream open failure must propagate")

    assert callbacks == ["callback"]


def test_deferred_dispatch_failure_is_safe_terminal_and_blocks_provider(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    sentinel = "callback-secret-must-not-leak"
    agent_diagnostics = []
    registry = HookRegistry(diagnostic_sink=agent_diagnostics.append)

    def fail(_context: BeforeLLMRequestContext) -> None:
        raise RuntimeError(f"dispatch feedback failed: {sentinel}")

    registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _DeferredDispatchProbe(fail),
    )
    bus = UIEventBus()
    phases: list[OperationPhaseChanged] = []

    def capture(event) -> None:
        if isinstance(event.payload, RuntimeEventPayload) and isinstance(
            event.payload.event.payload, OperationPhaseChanged
        ):
            phases.append(event.payload.event.payload)

    bus.subscribe(capture, replay_history=False)
    llm = LLM(
        model="demo-model",
        api_key="sk-test-12345678",
        ui_bus=bus,
    )
    llm.performance_monitor = RuntimePerformanceMonitor()
    provider_calls: list[dict] = []

    def open_stream(params):
        provider_calls.append(params)
        return iter([_FakeChunk(content="must-not-run")])

    llm._call_with_retry = open_stream  # type: ignore[method-assign]

    try:
        llm.chat(
            [{"role": "user", "content": "Hi"}],
            hook_registry=registry,
        )
    except LLMDispatchCallbackError as error:
        assert error.phase == "dispatch_callback"
        assert error.error_type == "RuntimeError"
        assert str(error) == (
            "LLM request failed before provider dispatch "
            "(phase=dispatch_callback, error_type=RuntimeError)"
        )
        assert error.__cause__ is None
    else:
        raise AssertionError("dispatch callback failure must terminate the request")

    assert provider_calls == []
    assert llm.last_dispatched_request is None
    diagnostics = registry.drain_diagnostics()
    assert len(diagnostics) == 1
    assert agent_diagnostics == [diagnostics[0]]
    assert diagnostics[0].hook_name == "deferred_dispatch"
    assert diagnostics[0].severity == "error"
    assert diagnostics[0].message == (
        "Deferred dispatch callback failed "
        "(phase=dispatch_callback, error_type=RuntimeError)"
    )
    assert phases[-1].phase == "dispatch_callback"
    assert phases[-1].status == "failed"
    assert phases[-1].error_type == "RuntimeError"

    samples = llm.performance_monitor.snapshot()
    dispatch_sample = next(
        sample for sample in samples if sample.name == "dispatch_callback"
    )
    assert dispatch_sample.status == "error"
    assert dispatch_sample.attribute_map()["error_type"] == "RuntimeError"
    request_total = next(sample for sample in samples if sample.name == "request_total")
    assert request_total.status == "error"
    assert request_total.attribute_map()["error_type"] == "RuntimeError"

    diagnostic_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / ".rcoder" / "diagnostics").glob("*.json")
    )
    observable_text = "\n".join(
        [
            *(diagnostic.message for diagnostic in diagnostics),
            *(str(event) for event in phases),
            *(str(sample) for sample in samples),
            diagnostic_text,
        ]
    )
    assert sentinel not in observable_text


def test_deferred_dispatch_failure_survives_diagnostic_reporting_failures(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    callback_secret = "primary-callback-secret"
    reporting_secret = "secondary-reporting-secret"
    registry = HookRegistry()

    def fail_callback(_context: BeforeLLMRequestContext) -> None:
        raise ValueError(f"callback failed: {callback_secret}")

    registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _DeferredDispatchProbe(fail_callback),
    )

    def fail_report(_diagnostic) -> None:
        raise RuntimeError(f"diagnostic sink failed: {reporting_secret}")

    def fail_persist(**_kwargs):
        raise OSError(f"diagnostic persistence failed: {reporting_secret}")

    monkeypatch.setattr(registry, "report_diagnostic", fail_report)
    monkeypatch.setattr(
        llm_client_module,
        "persist_llm_error_diagnostic",
        fail_persist,
    )
    provider_calls: list[dict] = []
    llm = LLM(model="demo-model", api_key="sk-test-12345678")

    def open_stream(params):
        provider_calls.append(params)
        return iter([_FakeChunk(content="must-not-run")])

    llm._call_with_retry = open_stream  # type: ignore[method-assign]

    try:
        llm.chat(
            [{"role": "user", "content": "Hi"}],
            hook_registry=registry,
        )
    except LLMDispatchCallbackError as error:
        rendered = str(error)
        assert error.phase == "dispatch_callback"
        assert error.error_type == "ValueError"
    else:
        raise AssertionError("callback failure must survive diagnostic failures")

    assert provider_calls == []
    assert callback_secret not in rendered
    assert reporting_secret not in rendered


def test_deferred_dispatch_effect_disables_ambiguous_provider_retry(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)

    class TransientConnectionError(RuntimeError):
        pass

    monkeypatch.setattr(
        llm_client_module,
        "APIConnectionError",
        TransientConnectionError,
    )
    attempts: list[int] = []
    callbacks: list[str] = []

    def create(**_params):
        attempts.append(len(attempts) + 1)
        raise TransientConnectionError("ambiguous provider failure")

    registry = HookRegistry()
    registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _DeferredDispatchProbe(lambda _context: callbacks.append("committed")),
    )
    llm = LLM(model="demo-model", api_key="sk-test-12345678")
    llm.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    try:
        llm.chat(
            [{"role": "user", "content": "Hi"}],
            hook_registry=registry,
        )
    except TransientConnectionError as error:
        assert str(error) == "ambiguous provider failure"
    else:
        raise AssertionError("provider failure must propagate")

    assert attempts == [1]
    assert callbacks == ["committed"]


def test_cancel_after_dispatch_commit_still_hands_payload_to_provider() -> None:
    cancellation = threading.Event()
    provider_called = threading.Event()
    callback_calls: list[str] = []
    registry = HookRegistry()

    def commit(_context: BeforeLLMRequestContext) -> None:
        callback_calls.append("committed")
        cancellation.set()

    registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _DeferredDispatchProbe(commit),
    )
    llm = LLM(model="demo-model", api_key="sk-test-12345678")

    def create(**_params):
        provider_called.set()
        return iter([_FakeChunk(content="ignored")])

    llm.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    try:
        llm.chat(
            [{"role": "user", "content": "Hi"}],
            hook_registry=registry,
            cancellation_event=cancellation,
        )
    except LLMRequestCancelled:
        pass
    else:
        raise AssertionError("post-commit cancellation must detach the consumer")

    assert callback_calls == ["committed"]
    assert provider_called.wait(timeout=1)


def test_llm_request_transform_rejection_terminates_operation_telemetry() -> None:
    bus = UIEventBus()
    seen: list[OperationPhaseChanged] = []

    def capture(event) -> None:
        if isinstance(event.payload, RuntimeEventPayload) and isinstance(
            event.payload.event.payload, OperationPhaseChanged
        ):
            seen.append(event.payload.event.payload)

    bus.subscribe(capture, replay_history=False)
    registry = HookRegistry()
    registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _RejectingRequestTransform(name="reject_request"),
    )
    llm = LLM(model="demo-model", api_key="sk-test-12345678", ui_bus=bus)
    llm.performance_monitor = RuntimePerformanceMonitor()

    try:
        llm.chat(
            [{"role": "user", "content": "Hi"}],
            hook_registry=registry,
        )
    except RuntimeError as error:
        assert str(error) == "request rejected after build"
    else:
        raise AssertionError("request transform rejection must propagate")

    assert [event.phase for event in seen] == ["request_build", "failed"]
    assert seen[-1].status == "failed"
    samples = llm.performance_monitor.snapshot()
    assert [sample.name for sample in samples] == [
        "request_build",
        "request_total",
    ]
    assert [sample.status for sample in samples] == ["error", "error"]


def test_llm_disables_sdk_retries_on_create_and_reconfigure(monkeypatch) -> None:
    client_options: list[dict] = []

    def create_client(**options):
        client_options.append(options)
        return object()

    monkeypatch.setattr(llm_client_module, "OpenAI", create_client)
    llm = LLM(model="first", api_key="key", base_url="https://first.invalid")

    llm.reconfigure(
        model="second",
        api_key="new-key",
        base_url="https://second.invalid",
        temperature=0.2,
        max_tokens=100,
    )

    assert [options["max_retries"] for options in client_options] == [0, 0]


def test_llm_does_not_retry_without_stream_options_on_transport_error(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    llm = LLM(model="demo-model", api_key="sk-test-12345678")
    attempts: list[bool] = []

    def fail(params):
        attempts.append("stream_options" in params)
        raise RuntimeError("connection dropped")

    llm._call_with_retry = fail  # type: ignore[method-assign]

    try:
        llm.chat([{"role": "user", "content": "Hi"}])
    except RuntimeError as error:
        assert str(error) == "connection dropped"
    else:
        raise AssertionError("transport error must propagate")

    assert attempts == [True]


def test_llm_retries_without_stream_options_only_when_provider_rejects_it() -> None:
    llm = LLM(model="demo-model", api_key="sk-test-12345678")
    attempts: list[bool] = []
    callbacks: list[str] = []
    registry = HookRegistry()
    registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _DeferredDispatchProbe(lambda _context: callbacks.append("committed")),
    )

    class UnsupportedStreamOptionsError(RuntimeError):
        status_code = 400

    def open_stream(params):
        has_stream_options = "stream_options" in params
        attempts.append(has_stream_options)
        if has_stream_options:
            raise UnsupportedStreamOptionsError(
                "stream_options is an unsupported extra field"
            )
        return iter([_FakeChunk(content="fallback")])

    llm._call_with_retry = open_stream  # type: ignore[method-assign]

    response = llm.chat(
        [{"role": "user", "content": "Hi"}],
        hook_registry=registry,
    )

    assert response.content == "fallback"
    assert attempts == [True, False]
    assert callbacks == ["committed"]


def test_llm_emits_non_replayable_request_phases() -> None:
    bus = UIEventBus()
    seen: list[OperationPhaseChanged] = []

    def capture(event) -> None:
        if isinstance(event.payload, RuntimeEventPayload) and isinstance(
            event.payload.event.payload, OperationPhaseChanged
        ):
            seen.append(event.payload.event.payload)

    bus.subscribe(capture, replay_history=False)
    llm = LLM(model="demo-model", api_key="sk-test-12345678", ui_bus=bus)
    llm.performance_monitor = RuntimePerformanceMonitor()
    llm._call_with_retry = lambda _params: iter(  # type: ignore[method-assign]
        [_FakeChunk(content="hello")]
    )

    response = llm.chat(
        [{"role": "user", "content": "Hi"}],
        trace_id="request-1",
        metadata={"agent_id": "main", "turn_id": "turn-1"},
    )

    assert response.content == "hello"
    assert [event.phase for event in seen] == [
        "request_build",
        "connect",
        "await_first_chunk",
        "streaming",
        "completed",
    ]
    assert seen[-1].status == "completed"
    assert bus.history_snapshot() == ()
    samples = llm.performance_monitor.snapshot()
    assert [sample.name for sample in samples] == [
        "request_build",
        "connect",
        "await_first_chunk",
        "streaming",
        "request_total",
    ]
    assert samples[-1].attribute_map()["turn_id"] == "turn-1"


def test_llm_stream_closes_when_agent_scope_is_cancelled() -> None:
    cancellation = threading.Event()
    llm = LLM(model="demo-model", api_key="sk-test-12345678")

    class Stream:
        def __init__(self) -> None:
            self._chunks = iter(
                [_FakeChunk(content="first"), _FakeChunk(content="late")]
            )
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._chunks)

        def close(self) -> None:
            self.closed = True

    stream = Stream()
    llm._call_with_retry = lambda params: stream  # type: ignore[method-assign]

    try:
        llm.chat(
            [{"role": "user", "content": "Hi"}],
            on_token=lambda token: cancellation.set(),
            cancellation_event=cancellation,
        )
    except LLMRequestCancelled:
        pass
    else:
        raise AssertionError("cancelled stream must raise LLMRequestCancelled")

    assert stream.closed is True


def test_llm_stream_cancel_aborts_during_silent_gap() -> None:
    cancellation = threading.Event()
    llm = LLM(model="demo-model", api_key="sk-test-12345678")

    class SilentStream:
        def __init__(self) -> None:
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self):
            time.sleep(30)
            raise StopIteration

        def close(self) -> None:
            self.closed = True

    stream = SilentStream()
    llm._call_with_retry = lambda params: stream  # type: ignore[method-assign]

    def cancel_soon() -> None:
        time.sleep(0.2)
        cancellation.set()

    threading.Thread(target=cancel_soon, daemon=True).start()
    started = time.monotonic()
    try:
        llm.chat(
            [{"role": "user", "content": "Hi"}],
            cancellation_event=cancellation,
        )
    except LLMRequestCancelled:
        pass
    else:
        raise AssertionError("silent stream must raise LLMRequestCancelled")
    elapsed = time.monotonic() - started

    assert stream.closed is True
    # Cancel is observed by polling, not by waiting for the next chunk.
    assert elapsed < 2.0


def test_llm_cancel_drops_slow_stream_open_and_discards_late_result() -> None:
    cancellation = threading.Event()
    release_open = threading.Event()
    late_stream_closed = threading.Event()
    callbacks: list[str] = []
    registry = HookRegistry()
    registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _DeferredDispatchProbe(lambda _context: callbacks.append("committed")),
    )
    llm = LLM(model="demo-model", api_key="sk-test-12345678")

    class LateStream:
        def close(self) -> None:
            late_stream_closed.set()

    def slow_open(_params):
        release_open.wait(timeout=5)
        return LateStream()

    llm._call_with_retry = slow_open  # type: ignore[method-assign]

    def cancel_soon() -> None:
        time.sleep(0.1)
        cancellation.set()

    threading.Thread(target=cancel_soon, daemon=True).start()
    started = time.monotonic()
    try:
        llm.chat(
            [{"role": "user", "content": "Hi"}],
            cancellation_event=cancellation,
            hook_registry=registry,
        )
    except LLMRequestCancelled:
        pass
    else:
        raise AssertionError("slow dispatch must raise LLMRequestCancelled")
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert llm.last_dispatched_request is not None
    assert callbacks == ["committed"]

    # The abandoned provider call may return later, but it never transfers
    # ownership back to this turn and is closed by its detached worker.
    release_open.set()
    assert late_stream_closed.wait(timeout=1)
    assert llm.last_dispatched_request is not None
    # Once the final payload is handed to the provider SDK, its irreversible
    # side effects stay committed even if the local consumer is abandoned.
    assert callbacks == ["committed"]


def test_cancelled_detached_retry_cannot_revive_terminal_operation(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)

    class TransientConnectionError(RuntimeError):
        pass

    monkeypatch.setattr(
        llm_client_module,
        "APIConnectionError",
        TransientConnectionError,
    )
    cancellation = threading.Event()
    create_started = threading.Event()
    release_create = threading.Event()
    create_failed = threading.Event()
    attempts: list[int] = []
    bus = UIEventBus()
    seen: list[OperationPhaseChanged] = []

    def capture(event) -> None:
        if isinstance(event.payload, RuntimeEventPayload) and isinstance(
            event.payload.event.payload, OperationPhaseChanged
        ):
            seen.append(event.payload.event.payload)

    def create(**_params):
        attempts.append(len(attempts) + 1)
        create_started.set()
        release_create.wait(timeout=5)
        create_failed.set()
        raise TransientConnectionError("late transient failure")

    bus.subscribe(capture, replay_history=False)
    llm = LLM(model="demo-model", api_key="sk-test-12345678", ui_bus=bus)
    llm.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    def cancel_after_open_starts() -> None:
        assert create_started.wait(timeout=1)
        cancellation.set()

    threading.Thread(target=cancel_after_open_starts, daemon=True).start()
    try:
        llm.chat(
            [{"role": "user", "content": "Hi"}],
            cancellation_event=cancellation,
        )
    except LLMRequestCancelled:
        pass
    else:
        raise AssertionError("cancelled dispatch must raise LLMRequestCancelled")

    terminal_phases = [event.phase for event in seen]
    assert terminal_phases[-1] == "cancelled"
    release_create.set()
    assert create_failed.wait(timeout=1)
    time.sleep(0.1)

    assert attempts == [1]
    assert [event.phase for event in seen] == terminal_phases


def test_llm_cancel_does_not_wait_for_slow_stream_close() -> None:
    cancellation = threading.Event()
    release_close = threading.Event()
    close_started = threading.Event()
    close_finished = threading.Event()
    llm = LLM(model="demo-model", api_key="sk-test-12345678")

    class SlowCloseStream:
        def __iter__(self):
            return self

        def __next__(self):
            time.sleep(30)
            raise StopIteration

        def close(self) -> None:
            close_started.set()
            release_close.wait(timeout=5)
            close_finished.set()

    llm._call_with_retry = lambda _params: SlowCloseStream()  # type: ignore[method-assign]

    def cancel_soon() -> None:
        time.sleep(0.1)
        cancellation.set()

    threading.Thread(target=cancel_soon, daemon=True).start()
    started = time.monotonic()
    try:
        llm.chat(
            [{"role": "user", "content": "Hi"}],
            cancellation_event=cancellation,
        )
    except LLMRequestCancelled:
        pass
    else:
        raise AssertionError("slow stream must raise LLMRequestCancelled")
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert close_started.wait(timeout=1)
    assert close_finished.is_set() is False

    release_close.set()
    assert close_finished.wait(timeout=1)


def test_llm_chat_sends_explicit_thinking_enabled_state() -> None:
    captured = {}

    llm = LLM(
        model="demo-model",
        api_key="sk-test-12345678",
        thinking_enabled=True,
    )

    def _fake_call_with_retry(params):
        captured.update(params)
        return iter(
            [
                _FakeChunk(content="Hello"),
                _FakeChunk(usage=_FakeUsage(prompt_tokens=1, completion_tokens=1)),
            ]
        )

    llm._call_with_retry = _fake_call_with_retry  # type: ignore[method-assign]
    response = llm.chat([{"role": "user", "content": "Hi"}])

    assert response.content == "Hello"
    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}


def test_llm_chat_sends_explicit_thinking_disabled_state() -> None:
    captured = {}

    llm = LLM(
        model="demo-model",
        api_key="sk-test-12345678",
        thinking_enabled=False,
    )

    def _fake_call_with_retry(params):
        captured.update(params)
        return iter(
            [
                _FakeChunk(content="Hello"),
                _FakeChunk(usage=_FakeUsage(prompt_tokens=1, completion_tokens=1)),
            ]
        )

    llm._call_with_retry = _fake_call_with_retry  # type: ignore[method-assign]
    response = llm.chat([{"role": "user", "content": "Hi"}])

    assert response.content == "Hello"
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


def test_llm_chat_omits_thinking_state_when_unset() -> None:
    captured = {}

    llm = LLM(
        model="demo-model",
        api_key="sk-test-12345678",
        thinking_enabled=None,
    )

    def _fake_call_with_retry(params):
        captured.update(params)
        return iter(
            [
                _FakeChunk(content="Hello"),
                _FakeChunk(usage=_FakeUsage(prompt_tokens=1, completion_tokens=1)),
            ]
        )

    llm._call_with_retry = _fake_call_with_retry  # type: ignore[method-assign]
    response = llm.chat([{"role": "user", "content": "Hi"}])

    assert response.content == "Hello"
    assert "extra_body" not in captured


def test_llm_debug_trace_persists_trace_and_emits_ui_event(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    ui_bus = UIEventBus()
    seen = []
    ui_bus.subscribe(seen.append, replay_history=False)

    llm = LLM(
        model="demo-model",
        api_key="sk-test-12345678",
        base_url="https://example.com/v1",
        debug_trace=True,
        ui_bus=ui_bus,
    )

    def _fake_call_with_retry(params):
        return iter(
            [
                _FakeChunk(content="Hello"),
                _FakeChunk(
                    usage=_FakeUsage(
                        prompt_tokens=12, completion_tokens=3, cached_tokens=8
                    )
                ),
            ]
        )

    llm._call_with_retry = _fake_call_with_retry  # type: ignore[method-assign]
    response = llm.chat(
        [{"role": "user", "content": "Hi"}],
        session_id="session_test",
        trace_id="trace_1",
    )

    assert response.content == "Hello"
    assert response.cached_input_tokens == 8
    debug_events = [event for event in seen if event.level == UIEventLevel.DEBUG]
    assert debug_events
    trace_path = debug_events[-1].data.get("trace_path")
    assert trace_path

    payload = json.loads(open(trace_path, encoding="utf-8").read())
    assert payload["model"] == "demo-model"
    assert "api_key_hint" not in payload
    assert payload["request"]["stream"] is True
    assert payload["request"]["reasoning_effort"] is None
    assert payload["request"]["reasoning_replay_mode"] == "none"
    assert payload["request"]["thinking_enabled"] is None
    assert payload["request"]["thinking_type"] is None
    assert payload["dispatched_request"]["messages"] == [
        {"role": "user", "content": "Hi"}
    ]
    assert "api_key" not in payload["dispatched_request"]
    assert payload["stream"]["event_count"] >= 2
    assert payload["stream"]["events"][0]["type"] == "content"
    assert payload["stream"]["events"][0]["text"] == "Hello"
    assert payload["response"]["usage"]["prompt_tokens"] == 12
    assert payload["response"]["usage"]["completion_tokens"] == 3
    assert payload["response"]["content"] == "Hello"
    assert llm.last_debug_trace_path == trace_path
