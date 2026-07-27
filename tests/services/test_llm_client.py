import json
import threading
import time

from reuleauxcoder.domain.llm.models import (
    EMPTY_ASSISTANT_CONTENT_PLACEHOLDER,
    LLMResponse,
    ToolCall,
)
from reuleauxcoder.interfaces.events import UIEventBus, UIEventLevel
from reuleauxcoder.interfaces.events import RuntimeEventPayload
from reuleauxcoder.domain.runtime.events import OperationPhaseChanged
from reuleauxcoder.services.llm.client import LLM, LLMRequestCancelled
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
        )
    except LLMRequestCancelled:
        pass
    else:
        raise AssertionError("slow dispatch must raise LLMRequestCancelled")
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert llm.last_dispatched_request is None

    # The abandoned provider call may return later, but it never transfers
    # ownership back to this turn and is closed by its detached worker.
    release_open.set()
    assert late_stream_closed.wait(timeout=1)
    assert llm.last_dispatched_request is None


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
