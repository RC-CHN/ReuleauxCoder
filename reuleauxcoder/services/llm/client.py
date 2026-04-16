"""LLM client - wraps OpenAI-compatible APIs."""

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from openai import OpenAI, APIConnectionError, APITimeoutError, RateLimitError

from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.domain.hooks.types import (
    AfterLLMResponseContext,
    BeforeLLMRequestContext,
    HookPoint,
)
from reuleauxcoder.domain.llm.models import ToolCall, LLMResponse
from reuleauxcoder.infrastructure.fs.paths import get_diagnostics_dir
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind
from reuleauxcoder.services.llm.diagnostics import persist_llm_error_diagnostic, snapshot_messages


MAX_DEBUG_CONTENT_CHARS = 400
MAX_DEBUG_STREAM_EVENTS = 200


def _mask_api_key(api_key: str) -> str:
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "*" * len(api_key)
    return f"{api_key[:4]}...{api_key[-4:]}"


def _trim_text(value: Any, limit: int = MAX_DEBUG_CONTENT_CHARS) -> str:
    text = str(value)
    return text[:limit] + ("..." if len(text) > limit else "")


def _extract_stream_event(chunk: Any) -> list[dict[str, Any]]:
    """Extract readable, ordered stream events from a chunk."""
    events: list[dict[str, Any]] = []
    choices = getattr(chunk, "choices", None) or []
    delta = choices[0].delta if choices else None
    if delta is not None:
        content = getattr(delta, "content", None)
        if content:
            events.append({"type": "content", "text": str(content)})
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            events.append({"type": "reasoning", "text": str(reasoning)})
        tool_calls = getattr(delta, "tool_calls", None) or []
        for tool_call in tool_calls:
            function = getattr(tool_call, "function", None)
            name = getattr(function, "name", None) if function is not None else None
            arguments = getattr(function, "arguments", None) if function is not None else None
            if name:
                events.append({"type": "tool_name", "text": str(name), "index": getattr(tool_call, "index", None)})
            if arguments:
                events.append({"type": "tool_arguments", "text": str(arguments), "index": getattr(tool_call, "index", None)})
    usage = getattr(chunk, "usage", None)
    if usage is not None:
        events.append(
            {
                "type": "usage",
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
            }
        )
    return events


def _persist_debug_trace(payload: dict[str, Any], *, session_id: str | None, trace_id: str | None) -> Path:
    diagnostics_dir = get_diagnostics_dir()
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    session_slug = session_id or "no_session"
    trace_slug = trace_id or "no_trace"
    path = diagnostics_dir / f"llm_trace_{timestamp}_{session_slug}_{trace_slug}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _sanitize_messages_for_llm(
    messages: list[dict],
    *,
    preserve_reasoning_content: bool = True,
    backfill_reasoning_content_for_tool_calls: bool = False,
) -> list[dict]:
    """Repair/trim malformed tool-call history before sending to the LLM."""
    sanitized: list[dict] = []
    tool_call_names: dict[str, str] = {}
    seen_tool_outputs: set[str] = set()
    effective_backfill = preserve_reasoning_content and backfill_reasoning_content_for_tool_calls

    for msg_index, msg in enumerate(messages):
        item = dict(msg)
        if not preserve_reasoning_content:
            item.pop("reasoning_content", None)
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
        if effective_backfill and "reasoning_content" not in item:
            item["reasoning_content"] = ""
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
        preserve_reasoning_content: bool = True,
        backfill_reasoning_content_for_tool_calls: bool = False,
        debug_trace: bool = False,
        ui_bus: UIEventBus | None = None,
    ):
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.api_key = api_key
        self.base_url = base_url
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.preserve_reasoning_content = preserve_reasoning_content
        self.backfill_reasoning_content_for_tool_calls = backfill_reasoning_content_for_tool_calls
        self.debug_trace = debug_trace
        self.ui_bus = ui_bus

    def reconfigure(
        self,
        *,
        model: str,
        api_key: str,
        base_url: Optional[str],
        temperature: float,
        max_tokens: int,
        preserve_reasoning_content: bool | None = None,
        backfill_reasoning_content_for_tool_calls: bool | None = None,
        debug_trace: bool | None = None,
    ) -> None:
        """Hot-swap runtime model/client settings."""
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.api_key = api_key
        self.base_url = base_url
        self.temperature = temperature
        self.max_tokens = max_tokens
        if preserve_reasoning_content is not None:
            self.preserve_reasoning_content = preserve_reasoning_content
        if backfill_reasoning_content_for_tool_calls is not None:
            self.backfill_reasoning_content_for_tool_calls = backfill_reasoning_content_for_tool_calls
        if debug_trace is not None:
            self.debug_trace = debug_trace

    def _emit_debug(self, message: str, **data: Any) -> None:
        """Emit a debug UI event when a bus is attached."""
        if self.ui_bus is not None:
            self.ui_bus.debug(message, kind=UIEventKind.AGENT, **data)

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
        raw_messages = [dict(msg) for msg in messages]
        messages = _sanitize_messages_for_llm(
            messages,
            preserve_reasoning_content=self.preserve_reasoning_content,
            backfill_reasoning_content_for_tool_calls=self.backfill_reasoning_content_for_tool_calls,
        )
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

        debug_stream_events: list[dict[str, Any]] = []
        debug_stream_options_enabled = False

        try:
            # stream_options is an OpenAI extension
            try:
                params["stream_options"] = {"include_usage": True}
                stream = self._call_with_retry(params)
                debug_stream_options_enabled = True
            except Exception:
                params.pop("stream_options", None)
                stream = self._call_with_retry(params)

            # Accumulate response
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            tokens: list[str] = []  # Collect streamed tokens
            tc_map: dict[int, dict] = {}  # index -> {id, name, arguments_str}
            prompt_tok = 0
            completion_tok = 0

            for chunk in stream:
                if self.debug_trace and len(debug_stream_events) < MAX_DEBUG_STREAM_EVENTS:
                    debug_stream_events.extend(_extract_stream_event(chunk))
                    if len(debug_stream_events) > MAX_DEBUG_STREAM_EVENTS:
                        debug_stream_events = debug_stream_events[:MAX_DEBUG_STREAM_EVENTS]

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

                if self.preserve_reasoning_content and getattr(delta, "reasoning_content", None):
                    reasoning_parts.append(delta.reasoning_content)

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
                reasoning_content=("".join(reasoning_parts) if self.preserve_reasoning_content else None),
                tool_calls=parsed,
                prompt_tokens=prompt_tok,
                completion_tokens=completion_tok,
                tokens=tokens,
            )

            if self.debug_trace:
                trace_payload = {
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "session_id": session_id,
                    "trace_id": trace_id,
                    "model": self.model,
                    "base_url": self.base_url,
                    "api_key_hint": _mask_api_key(self.api_key),
                    "request": {
                        "temperature": params.get("temperature"),
                        "max_tokens": params.get("max_tokens"),
                        "stream": params.get("stream"),
                        "stream_options": params.get("stream_options"),
                        "stream_options_enabled": debug_stream_options_enabled,
                        "tool_count": len(params.get("tools") or []),
                    },
                    "messages": {
                        "raw_count": len(raw_messages),
                        "sanitized_count": len(messages),
                        "raw_tail": snapshot_messages(raw_messages),
                        "sanitized_tail": snapshot_messages(messages),
                    },
                    "stream": {
                        "event_count": len(debug_stream_events),
                        "events": debug_stream_events,
                    },
                    "response": {
                        "content": _trim_text(response.content or "", 1000),
                        "reasoning_content": _trim_text(response.reasoning_content or "", 1000),
                        "tool_calls": [
                            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                            for tc in response.tool_calls
                        ],
                        "usage": {
                            "prompt_tokens": response.prompt_tokens,
                            "completion_tokens": response.completion_tokens,
                        },
                    },
                    "metadata": dict(before_context.metadata),
                }
                trace_path = _persist_debug_trace(trace_payload, session_id=session_id, trace_id=trace_id)
                self._emit_debug(
                    f"LLM trace saved: {trace_path}",
                    trace_path=str(trace_path),
                    session_id=session_id,
                    trace_id=trace_id,
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
        except Exception as e:
            diagnostic_path = persist_llm_error_diagnostic(
                model=self.model,
                base_url=self.base_url,
                session_id=session_id,
                request_params=params,
                raw_messages=raw_messages,
                sanitized_messages=messages,
                error=e,
                metadata=dict(before_context.metadata),
            )
            setattr(e, "llm_diagnostic_path", str(diagnostic_path))
            if session_id:
                before_context.metadata["llm_error_diagnostic_path"] = str(diagnostic_path)
            raise

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
