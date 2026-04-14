"""LLM client - wraps OpenAI-compatible APIs."""

import json
import time
from collections.abc import Callable
from typing import Any, Optional

from openai import OpenAI, APIConnectionError, APITimeoutError, RateLimitError

from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.types import (
    AfterLLMResponseContext,
    BeforeLLMRequestContext,
    HookPoint,
)
from reuleauxcoder.domain.llm.models import ToolCall, LLMResponse


def _sanitize_messages_for_llm(messages: list[dict]) -> list[dict]:
    """Repair/trim malformed tool-call history before sending to the LLM."""
    sanitized: list[dict] = []
    tool_call_names: dict[str, str] = {}
    seen_tool_outputs: set[str] = set()

    for msg_index, msg in enumerate(messages):
        item = dict(msg)
        if item.get("role") != "assistant":
            sanitized.append(item)
            continue

        raw_tool_calls = item.get("tool_calls") or []
        if not raw_tool_calls:
            sanitized.append(item)
            continue

        repaired_tool_calls: list[dict] = []
        for tc_index, tc in enumerate(raw_tool_calls):
            tc_item = dict(tc)
            tc_id = (tc_item.get("id") or "").strip()
            if not tc_id:
                tc_id = f"recovered_tool_call_{msg_index}_{tc_index}"
                tc_item["id"] = tc_id

            fn = dict(tc_item.get("function") or {})
            tc_item["function"] = fn
            tool_call_names[tc_id] = fn.get("name") or "unknown_tool"
            repaired_tool_calls.append(tc_item)

        item["tool_calls"] = repaired_tool_calls
        sanitized.append(item)

    final_messages: list[dict] = []
    for item in sanitized:
        if item.get("role") != "tool":
            final_messages.append(item)
            continue

        tool_call_id = (item.get("tool_call_id") or "").strip()
        if not tool_call_id:
            continue
        if tool_call_id not in tool_call_names:
            continue

        content = item.get("content")
        if content is None or not str(content).strip():
            item = dict(item)
            item["content"] = f"Tool '{tool_call_names[tool_call_id]}' output missing."

        seen_tool_outputs.add(tool_call_id)
        final_messages.append(item)

    for tool_call_id, tool_name in tool_call_names.items():
        if tool_call_id in seen_tool_outputs:
            continue
        final_messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": f"Tool '{tool_name}' output missing.",
            }
        )

    return final_messages


class LLM:
    """LLM client that wraps OpenAI-compatible APIs."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ):
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.temperature = temperature
        self.max_tokens = max_tokens

    def reconfigure(
        self,
        *,
        model: str,
        api_key: str,
        base_url: Optional[str],
        temperature: float,
        max_tokens: int,
    ) -> None:
        """Hot-swap runtime model/client settings."""
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.temperature = temperature
        self.max_tokens = max_tokens

    def chat(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        on_token: Optional[Callable[[str], None]] = None,
        hook_registry: HookRegistry | None = None,
        session_id: str | None = None,
        trace_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Send messages, stream back response, handle tool calls."""
        messages = _sanitize_messages_for_llm(messages)
        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        if tools:
            params["tools"] = tools

        before_context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            request_params=dict(params),
            messages=list(messages),
            tools=list(tools) if tools else [],
            model=self.model,
            session_id=session_id,
            trace_id=trace_id,
            metadata=dict(metadata or {}),
        )

        if hook_registry is not None:
            guard_decisions = hook_registry.run_guards(HookPoint.BEFORE_LLM_REQUEST, before_context)
            denied = next((d for d in guard_decisions if not d.allowed), None)
            if denied is not None:
                raise RuntimeError(denied.reason or "LLM request blocked by guard hook")
            before_context = hook_registry.run_transforms(HookPoint.BEFORE_LLM_REQUEST, before_context)
            hook_registry.run_observers(HookPoint.BEFORE_LLM_REQUEST, before_context)

        params = dict(before_context.request_params)

        # stream_options is an OpenAI extension
        try:
            params["stream_options"] = {"include_usage": True}
            stream = self._call_with_retry(params)
        except Exception:
            params.pop("stream_options", None)
            stream = self._call_with_retry(params)

        # Accumulate response
        content_parts: list[str] = []
        tokens: list[str] = []  # Collect streamed tokens
        tc_map: dict[int, dict] = {}  # index -> {id, name, arguments_str}
        prompt_tok = 0
        completion_tok = 0

        for chunk in stream:
            # Usage info comes in the final chunk
            if chunk.usage:
                prompt_tok = chunk.usage.prompt_tokens
                completion_tok = chunk.usage.completion_tokens

            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # Accumulate text
            if delta.content:
                content_parts.append(delta.content)
                tokens.append(delta.content)
                if on_token is not None:
                    on_token(delta.content)

            # Accumulate tool calls across chunks
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tc_map:
                        tc_map[idx] = {"id": "", "name": "", "args": ""}
                    if tc_delta.id:
                        tc_map[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tc_map[idx]["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tc_map[idx]["args"] += tc_delta.function.arguments

        # Parse accumulated tool calls
        parsed: list[ToolCall] = []
        for idx in sorted(tc_map):
            raw = tc_map[idx]
            tool_call_id = raw.get("id") or f"tool_call_{idx}"
            try:
                args = json.loads(raw["args"])
            except (json.JSONDecodeError, KeyError):
                args = {}
            parsed.append(ToolCall(id=tool_call_id, name=raw["name"], arguments=args))

        response = LLMResponse(
            content="".join(content_parts),
            tool_calls=parsed,
            prompt_tokens=prompt_tok,
            completion_tokens=completion_tok,
            tokens=tokens,
        )

        after_context = AfterLLMResponseContext(
            hook_point=HookPoint.AFTER_LLM_RESPONSE,
            request_params=dict(params),
            response=response,
            model=self.model,
            session_id=session_id,
            trace_id=trace_id,
            metadata=dict(before_context.metadata),
        )

        if hook_registry is not None:
            after_context = hook_registry.run_transforms(HookPoint.AFTER_LLM_RESPONSE, after_context)
            hook_registry.run_observers(HookPoint.AFTER_LLM_RESPONSE, after_context)

        return after_context.response or response

    def _call_with_retry(self, params: dict, max_retries: int = 3):
        """Retry on transient errors with exponential backoff."""
        for attempt in range(max_retries):
            try:
                return self.client.chat.completions.create(**params)
            except (RateLimitError, APITimeoutError, APIConnectionError) as e:
                if attempt == max_retries - 1:
                    raise
                wait = 2**attempt
                time.sleep(wait)
