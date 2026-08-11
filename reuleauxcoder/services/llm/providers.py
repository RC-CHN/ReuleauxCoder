"""Provider transport adapters behind model-client orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator, Protocol

import httpx


ANTHROPIC_API_VERSION = "2023-06-01"
ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com/v1"


class ProviderAdapter(Protocol):
    """Small transport port; retries and response assembly stay in ``LLM``."""

    family: str
    native: bool

    @property
    def client(self) -> object: ...

    @client.setter
    def client(self, value: object) -> None: ...

    def open_stream(self, params: dict[str, Any]) -> Any: ...

    def is_retryable(self, error: BaseException) -> bool: ...

    def close(self) -> None: ...


class ProviderHTTPError(RuntimeError):
    """Content-free native-provider HTTP failure."""

    def __init__(self, family: str, status_code: int) -> None:
        self.family = family
        self.status_code = status_code
        super().__init__(f"{family} request failed with HTTP status {status_code}")


class ProviderTransportError(RuntimeError):
    """Content-free native-provider connection failure."""

    def __init__(self, family: str, kind: str) -> None:
        self.family = family
        self.kind = kind
        super().__init__(f"{family} request failed during {kind}")


class ProviderProtocolError(RuntimeError):
    """Content-free malformed native-provider stream failure."""

    def __init__(self, family: str, event_type: str = "unknown") -> None:
        self.family = family
        self.event_type = event_type
        super().__init__(f"{family} returned a malformed stream event")


class OpenAICompatibleProvider:
    """Adapter for OpenAI and wire-compatible completion clients."""

    family = "openai-compatible"
    native = False

    def __init__(
        self,
        client: object,
        retryable_errors: tuple[type[BaseException], ...],
    ) -> None:
        self._client = client
        self._retryable_errors = retryable_errors

    @property
    def client(self) -> object:
        return self._client

    @client.setter
    def client(self, value: object) -> None:
        self._client = value

    def open_stream(self, params: dict[str, Any]) -> Any:
        return self._client.chat.completions.create(**params)

    def is_retryable(self, error: BaseException) -> bool:
        return isinstance(error, self._retryable_errors)

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


@dataclass(slots=True)
class _FunctionDelta:
    name: str | None = None
    arguments: str | None = None


@dataclass(slots=True)
class _ToolCallDelta:
    index: int
    id: str | None = None
    function: _FunctionDelta | None = None


@dataclass(slots=True)
class _Delta:
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: tuple[_ToolCallDelta, ...] = ()


@dataclass(slots=True)
class _Choice:
    delta: _Delta


@dataclass(slots=True)
class _Usage:
    prompt_tokens: int
    completion_tokens: int
    cache_read_input_tokens: int | None = None


@dataclass(slots=True)
class _Chunk:
    choices: tuple[_Choice, ...] = ()
    usage: _Usage | None = None


def _content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    role = message.get("role")
    content = message.get("content")
    blocks: list[dict[str, Any]] = []
    if isinstance(content, str):
        if content:
            blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                raise TypeError("anthropic adapter supports text content blocks only")
            text = block.get("text")
            if not isinstance(text, str):
                raise TypeError("anthropic text content must be a string")
            blocks.append({"type": "text", "text": text})
    elif content is not None:
        raise TypeError("anthropic message content must be text")

    if role == "assistant":
        for call in message.get("tool_calls") or ():
            if not isinstance(call, dict):
                raise TypeError("anthropic tool call must be an object")
            function = call.get("function")
            if not isinstance(function, dict):
                raise TypeError("anthropic tool function must be an object")
            raw_input = function.get("arguments", {})
            if isinstance(raw_input, str):
                raw_input = json.loads(raw_input)
            if not isinstance(raw_input, dict):
                raise TypeError("anthropic tool input must be an object")
            blocks.append(
                {
                    "type": "tool_use",
                    "id": str(call.get("id") or ""),
                    "name": str(function.get("name") or ""),
                    "input": raw_input,
                }
            )
    return blocks


def _merge_message(
    messages: list[dict[str, Any]],
    role: str,
    blocks: list[dict[str, Any]],
) -> None:
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
    else:
        messages.append({"role": role, "content": blocks})


def _anthropic_messages(
    source: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    for message in source:
        if not isinstance(message, dict):
            raise TypeError("anthropic message must be an object")
        role = message.get("role")
        if role == "system":
            content = message.get("content")
            if not isinstance(content, str):
                raise TypeError("anthropic system message must be text")
            system_parts.append(content)
            continue
        if role == "tool":
            content = message.get("content")
            if not isinstance(content, str):
                raise TypeError("anthropic tool result must be text")
            _merge_message(
                messages,
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": str(message.get("tool_call_id") or ""),
                        "content": content,
                    }
                ],
            )
            continue
        if role not in {"user", "assistant"}:
            raise TypeError("anthropic message role is unsupported")
        blocks = _content_blocks(message)
        if not blocks:
            blocks = [{"type": "text", "text": ""}]
        _merge_message(messages, role, blocks)
    return ("\n\n".join(system_parts) or None), messages


def _anthropic_tools(source: object) -> list[dict[str, Any]]:
    if source is None:
        return []
    if not isinstance(source, list):
        raise TypeError("anthropic tools must be a list")
    result: list[dict[str, Any]] = []
    for tool in source:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict):
            raise TypeError("anthropic tool schema must be a function object")
        schema = function.get("parameters", {"type": "object"})
        if not isinstance(schema, dict):
            raise TypeError("anthropic tool input schema must be an object")
        converted = {
            "name": str(function.get("name") or ""),
            "input_schema": schema,
        }
        description = function.get("description")
        if isinstance(description, str) and description:
            converted["description"] = description
        result.append(converted)
    return result


def _anthropic_request(params: dict[str, Any]) -> dict[str, Any]:
    messages = params.get("messages")
    if not isinstance(messages, list):
        raise TypeError("anthropic messages must be a list")
    system, native_messages = _anthropic_messages(messages)
    request: dict[str, Any] = {
        "model": str(params.get("model") or ""),
        "messages": native_messages,
        "max_tokens": int(params.get("max_tokens") or 4096),
        "stream": True,
    }
    if system is not None:
        request["system"] = system
    tools = _anthropic_tools(params.get("tools"))
    if tools:
        request["tools"] = tools
    extra_body = params.get("extra_body")
    thinking = extra_body.get("thinking") if isinstance(extra_body, dict) else None
    if isinstance(thinking, dict):
        request["thinking"] = dict(thinking)
    elif params.get("temperature") is not None:
        request["temperature"] = params["temperature"]
    return request


class _AnthropicStream(Iterator[_Chunk]):
    def __init__(self, response: httpx.Response, context: Any) -> None:
        self._context = context
        self._lines = iter(response.iter_lines())
        self._closed = False
        self._input_tokens = 0
        self._output_tokens = 0
        self._cache_read_input_tokens: int | None = None

    def __iter__(self) -> "_AnthropicStream":
        return self

    def __next__(self) -> _Chunk:
        for line in self._lines:
            if not line or not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                self.close()
                raise StopIteration
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, RecursionError):
                self.close()
                raise ProviderProtocolError("anthropic") from None
            if not isinstance(event, dict):
                self.close()
                raise ProviderProtocolError("anthropic")
            try:
                chunk = self._convert(event)
            except ProviderProtocolError:
                self.close()
                raise
            except (AttributeError, OverflowError, TypeError, ValueError):
                self.close()
                raise ProviderProtocolError("anthropic") from None
            if chunk is not None:
                return chunk
        self.close()
        raise StopIteration

    def _convert(self, event: dict[str, Any]) -> _Chunk | None:
        event_type = event.get("type")
        if event_type == "error":
            raise ProviderProtocolError("anthropic", "error")
        if event_type == "message_start":
            usage = (event.get("message") or {}).get("usage") or {}
            self._input_tokens = int(usage.get("input_tokens") or 0)
            cached = usage.get("cache_read_input_tokens")
            self._cache_read_input_tokens = int(cached) if cached is not None else None
            return self._usage_chunk()
        if event_type == "message_delta":
            usage = event.get("usage") or {}
            self._output_tokens = int(
                usage.get("output_tokens") or self._output_tokens
            )
            return self._usage_chunk()
        if event_type == "content_block_start":
            block = event.get("content_block") or {}
            index = int(event.get("index") or 0)
            if block.get("type") == "text":
                text = block.get("text")
                return self._delta_chunk(content=text if isinstance(text, str) else None)
            if block.get("type") == "thinking":
                text = block.get("thinking")
                return self._delta_chunk(
                    reasoning=text if isinstance(text, str) else None
                )
            if block.get("type") == "tool_use":
                tool_input = block.get("input")
                arguments = (
                    json.dumps(tool_input, ensure_ascii=False, separators=(",", ":"))
                    if isinstance(tool_input, dict) and tool_input
                    else None
                )
                return self._tool_chunk(
                    index,
                    tool_id=str(block.get("id") or ""),
                    name=str(block.get("name") or ""),
                    arguments=arguments,
                )
            return None
        if event_type == "content_block_delta":
            delta = event.get("delta") or {}
            index = int(event.get("index") or 0)
            if delta.get("type") == "text_delta":
                text = delta.get("text")
                return self._delta_chunk(content=text if isinstance(text, str) else None)
            if delta.get("type") == "thinking_delta":
                text = delta.get("thinking")
                return self._delta_chunk(
                    reasoning=text if isinstance(text, str) else None
                )
            if delta.get("type") == "input_json_delta":
                partial = delta.get("partial_json")
                return self._tool_chunk(
                    index,
                    arguments=partial if isinstance(partial, str) else None,
                )
        return None

    def _usage_chunk(self) -> _Chunk:
        return _Chunk(
            usage=_Usage(
                prompt_tokens=self._input_tokens,
                completion_tokens=self._output_tokens,
                cache_read_input_tokens=self._cache_read_input_tokens,
            )
        )

    @staticmethod
    def _delta_chunk(
        *,
        content: str | None = None,
        reasoning: str | None = None,
    ) -> _Chunk:
        return _Chunk(
            choices=(
                _Choice(_Delta(content=content, reasoning_content=reasoning)),
            )
        )

    @staticmethod
    def _tool_chunk(
        index: int,
        *,
        tool_id: str | None = None,
        name: str | None = None,
        arguments: str | None = None,
    ) -> _Chunk:
        return _Chunk(
            choices=(
                _Choice(
                    _Delta(
                        tool_calls=(
                            _ToolCallDelta(
                                index=index,
                                id=tool_id,
                                function=_FunctionDelta(
                                    name=name,
                                    arguments=arguments,
                                ),
                            ),
                        )
                    )
                ),
            )
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._context.__exit__(None, None, None)


class AnthropicMessagesProvider:
    """Native adapter for Anthropic's Messages streaming API."""

    family = "anthropic"
    native = True

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = (base_url or ANTHROPIC_DEFAULT_BASE_URL).rstrip("/")
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(60.0, connect=10.0)
        )

    @property
    def client(self) -> object:
        return self._client

    @client.setter
    def client(self, value: object) -> None:
        if not isinstance(value, httpx.Client):
            raise TypeError("anthropic provider client must be httpx.Client")
        self._client = value

    def open_stream(self, params: dict[str, Any]) -> _AnthropicStream:
        try:
            context = self._client.stream(
                "POST",
                f"{self._base_url}/messages",
                headers={
                    "anthropic-version": ANTHROPIC_API_VERSION,
                    "content-type": "application/json",
                    "x-api-key": self._api_key,
                },
                json=_anthropic_request(params),
            )
            response = context.__enter__()
        except httpx.TimeoutException:
            raise ProviderTransportError(self.family, "timeout") from None
        except httpx.TransportError:
            raise ProviderTransportError(self.family, "transport") from None
        if response.status_code >= 400:
            status_code = response.status_code
            context.__exit__(None, None, None)
            raise ProviderHTTPError(self.family, status_code)
        return _AnthropicStream(response, context)

    def is_retryable(self, error: BaseException) -> bool:
        return isinstance(error, ProviderTransportError) or (
            isinstance(error, ProviderHTTPError)
            and error.status_code in {429, 500, 502, 503, 504}
        )

    def close(self) -> None:
        self._client.close()


def close_provider(adapter: ProviderAdapter) -> str | None:
    """Return a safe error type when reconfiguration cleanup fails."""
    try:
        adapter.close()
    except Exception as error:
        name = type(error).__name__
        if (
            name
            and len(name) <= 64
            and name.isascii()
            and name.replace("_", "").isalnum()
        ):
            return name
        return "Exception"
    return None


def normalize_provider_family(value: object) -> str:
    family = str(value or "openai-compatible").strip().lower()
    aliases = {
        "openai": "openai-compatible",
        "openai_compatible": "openai-compatible",
        "anthropic-messages": "anthropic",
    }
    return aliases.get(family, family)
