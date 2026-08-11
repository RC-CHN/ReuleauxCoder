from __future__ import annotations

import json

import httpx
import pytest

from reuleauxcoder.domain.config.models import ModelProfileConfig
from reuleauxcoder.domain.llm.protocols import LLMProtocol
from reuleauxcoder.domain.runtime.performance import RuntimePerformanceMonitor
from reuleauxcoder.services.llm.client import LLM
from reuleauxcoder.services.llm.factory import build_llm_from_settings
from reuleauxcoder.services.llm.providers import (
    AnthropicMessagesProvider,
    ProviderHTTPError,
    ProviderProtocolError,
    ProviderTransportError,
)


def _sse(*events: dict) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
        for event in events
    ).encode()


def _anthropic_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_native_anthropic_adapter_satisfies_llm_contract_and_streams_text() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {
                    "type": "message_start",
                    "message": {
                        "usage": {
                            "input_tokens": 12,
                            "cache_read_input_tokens": 4,
                        }
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": "plan"},
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": "hello"},
                },
                {"type": "message_delta", "usage": {"output_tokens": 3}},
                {"type": "message_stop"},
            ),
        )

    llm = LLM(
        model="claude-test",
        api_key="anthropic-secret",
        provider="anthropic",
    )
    llm.client = _anthropic_client(handler)
    try:
        assert isinstance(llm, LLMProtocol)
        response = llm.chat(
            [
                {"role": "system", "content": "be concise"},
                {"role": "user", "content": "hi"},
            ]
        )
    finally:
        llm.client.close()

    assert response.content == "hello"
    assert response.reasoning_content == "plan"
    assert response.prompt_tokens == 12
    assert response.completion_tokens == 3
    assert response.cached_input_tokens == 4
    request = captured["request"]
    assert request.url == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "anthropic-secret"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in request.headers
    assert captured["body"] == {
        "model": "claude-test",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        ],
        "max_tokens": 4096,
        "stream": True,
        "system": "be concise",
        "temperature": 0.0,
    }


def test_native_anthropic_adapter_translates_tools_and_streams_arguments() -> None:
    captured_body = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"file_path":',
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '"README.md"}',
                    },
                },
                {"type": "message_delta", "usage": {"output_tokens": 8}},
            ),
        )

    llm = LLM(model="claude-test", api_key="key", provider="anthropic")
    llm.client = _anthropic_client(handler)
    try:
        response = llm.chat(
            [{"role": "user", "content": "read it"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file",
                        "parameters": {
                            "type": "object",
                            "properties": {"file_path": {"type": "string"}},
                            "required": ["file_path"],
                        },
                    },
                }
            ],
        )
    finally:
        llm.client.close()

    assert response.tool_calls[0].id == "toolu_1"
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"file_path": "README.md"}
    assert captured_body["tools"] == [
        {
            "name": "read_file",
            "description": "Read a file",
            "input_schema": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        }
    ]
    assert "stream_options" not in captured_body


def test_native_anthropic_adapter_translates_prior_tool_round() -> None:
    captured_body = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "done"},
                }
            ),
        )

    provider = AnthropicMessagesProvider(
        api_key="key",
        client=_anthropic_client(handler),
    )
    stream = provider.open_stream(
        {
            "model": "claude-test",
            "messages": [
                {"role": "user", "content": "read"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "toolu_1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"file_path":"README.md"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "toolu_1",
                    "content": "contents",
                },
            ],
            "max_tokens": 20,
        }
    )
    try:
        list(stream)
    finally:
        provider.close()

    assert captured_body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "read"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "read_file",
                    "input": {"file_path": "README.md"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "contents",
                }
            ],
        },
    ]


def test_native_provider_failures_are_safe_and_retry_classified() -> None:
    secret = "provider-body-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text=secret)

    provider = AnthropicMessagesProvider(
        api_key="key",
        client=_anthropic_client(handler),
    )
    try:
        with pytest.raises(ProviderHTTPError) as raised:
            provider.open_stream(
                {"model": "claude-test", "messages": [], "max_tokens": 1}
            )
    finally:
        provider.close()

    assert provider.is_retryable(raised.value) is True
    assert raised.value.status_code == 429
    assert secret not in str(raised.value)


def test_native_provider_rejects_malformed_sse_without_echoing_content() -> None:
    secret = "malformed-provider-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {{{secret}\n\n".encode(),
        )

    provider = AnthropicMessagesProvider(
        api_key="key",
        client=_anthropic_client(handler),
    )
    stream = provider.open_stream(
        {"model": "claude-test", "messages": [], "max_tokens": 1}
    )
    try:
        with pytest.raises(ProviderProtocolError) as raised:
            next(stream)
    finally:
        provider.close()

    assert secret not in str(raised.value)


def test_native_provider_transport_failure_is_safe_and_retryable() -> None:
    secret = "transport-url-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed at https://{secret}.invalid", request=request)

    provider = AnthropicMessagesProvider(
        api_key="key",
        client=_anthropic_client(handler),
    )
    try:
        with pytest.raises(ProviderTransportError) as raised:
            provider.open_stream(
                {"model": "claude-test", "messages": [], "max_tokens": 1}
            )
    finally:
        provider.close()

    assert provider.is_retryable(raised.value) is True
    assert raised.value.kind == "transport"
    assert secret not in str(raised.value)


def test_provider_reconfigure_retains_nonfatal_cleanup_failure_fact() -> None:
    class CleanupFailure(RuntimeError):
        pass

    class BrokenProvider:
        def close(self) -> None:
            raise CleanupFailure("secret cleanup detail")

    llm = LLM(model="before", api_key="key")
    monitor = RuntimePerformanceMonitor()
    llm.performance_monitor = monitor
    llm._provider_adapter = BrokenProvider()  # type: ignore[assignment]

    llm.reconfigure(
        model="after",
        api_key="next-key",
        base_url=None,
        temperature=0.0,
        max_tokens=128,
    )
    try:
        assert llm.model == "after"
        assert llm.last_provider_cleanup_error_type == "CleanupFailure"
        sample = monitor.snapshot(category="model")[-1]
        assert sample.name == "provider_cleanup"
        assert sample.status == "error"
        assert sample.attribute_map()["error_type"] == "CleanupFailure"
        assert "secret cleanup detail" not in repr(sample)
    finally:
        llm.client.close()


def test_factory_selects_native_provider_without_opening_network() -> None:
    llm = build_llm_from_settings(
        ModelProfileConfig(
            name="native",
            model="claude-test",
            api_key="key",
            provider="anthropic",
        )
    )
    try:
        assert llm.provider_family == "anthropic"
        assert isinstance(llm.client, httpx.Client)
    finally:
        llm.client.close()


def test_unknown_provider_is_rejected_before_dispatch() -> None:
    with pytest.raises(ValueError, match="Unsupported model provider"):
        LLM(model="demo", api_key="key", provider="unknown")
