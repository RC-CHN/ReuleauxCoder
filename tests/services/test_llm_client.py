import json

from reuleauxcoder.interfaces.events import UIEventBus, UIEventLevel
from reuleauxcoder.services.llm.client import LLM, _sanitize_messages_for_llm


def test_sanitize_messages_backfills_reasoning_content_for_assistant_tool_calls() -> None:
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

    sanitized = _sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        backfill_reasoning_content_for_tool_calls=True,
    )

    assert sanitized[0]["reasoning_content"] == ""


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

    sanitized = _sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=True,
        backfill_reasoning_content_for_tool_calls=False,
    )

    assert "reasoning_content" not in sanitized[0]


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

    sanitized = _sanitize_messages_for_llm(
        messages,
        preserve_reasoning_content=False,
        backfill_reasoning_content_for_tool_calls=True,
    )

    assert "reasoning_content" not in sanitized[0]


class _FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeDelta:
    def __init__(self, content: str = "", reasoning_content: str | None = None, tool_calls=None):
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


def test_llm_debug_trace_persists_trace_and_emits_ui_event(tmp_path, monkeypatch) -> None:
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
                _FakeChunk(usage=_FakeUsage(prompt_tokens=12, completion_tokens=3)),
            ]
        )

    llm._call_with_retry = _fake_call_with_retry  # type: ignore[method-assign]
    response = llm.chat([{"role": "user", "content": "Hi"}], session_id="session_test", trace_id="trace_1")

    assert response.content == "Hello"
    debug_events = [event for event in seen if event.level == UIEventLevel.DEBUG]
    assert debug_events
    trace_path = debug_events[-1].data.get("trace_path")
    assert trace_path

    payload = json.loads(open(trace_path, encoding="utf-8").read())
    assert payload["model"] == "demo-model"
    assert payload["request"]["stream"] is True
    assert payload["stream"]["event_count"] >= 2
    assert payload["stream"]["events"][0]["type"] == "content"
    assert payload["stream"]["events"][0]["text"] == "Hello"
    assert payload["response"]["usage"]["prompt_tokens"] == 12
    assert payload["response"]["usage"]["completion_tokens"] == 3
