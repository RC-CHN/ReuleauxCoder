"""LLM diagnostic dump helpers."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from reuleauxcoder.infrastructure.fs.paths import get_diagnostics_dir


MAX_SNAPSHOT_MESSAGES = 10
MAX_CONTENT_CHARS = 500
MAX_TOOL_RESULT_CHARS = 500
MAX_ERROR_BODY_CHARS = 4000


def snapshot_messages(
    messages: list[dict], limit: int = MAX_SNAPSHOT_MESSAGES
) -> list[dict[str, Any]]:
    """Build a compact tail snapshot of messages for diagnostics."""
    tail = messages[-limit:] if len(messages) > limit else list(messages)
    snapshot: list[dict[str, Any]] = []
    start_index = max(0, len(messages) - len(tail))
    for offset, msg in enumerate(tail):
        item: dict[str, Any] = {
            "index": start_index + offset,
            "role": msg.get("role", "?"),
        }
        content = msg.get("content")
        if content is not None:
            text = str(content)
            item["content"] = text[:MAX_CONTENT_CHARS] + (
                "..." if len(text) > MAX_CONTENT_CHARS else ""
            )
        if msg.get("tool_call_id"):
            item["tool_call_id"] = msg.get("tool_call_id")
        if msg.get("tool_calls"):
            item["tool_calls"] = msg.get("tool_calls")
        snapshot.append(item)
    return snapshot


def persist_llm_error_diagnostic(
    *,
    model: str,
    base_url: str | None,
    session_id: str | None,
    request_params: dict[str, Any],
    raw_messages: list[dict],
    sanitized_messages: list[dict],
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> Path:
    """Persist an LLM error diagnostic JSON dump and return the path."""
    diagnostics_dir = get_diagnostics_dir()
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    session_slug = session_id or "no_session"
    file_path = diagnostics_dir / f"llm_error_{timestamp}_{session_slug}.json"

    body = getattr(error, "body", None)
    error_payload = {
        "type": type(error).__name__,
        "message": str(error),
    }
    if body is not None:
        body_text = (
            body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        )
        error_payload["body"] = body_text[:MAX_ERROR_BODY_CHARS]

    tool_schemas = request_params.get("tools") or []
    tool_names: list[str] = []
    for tool in tool_schemas:
        function_def = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function_def, dict):
            name = function_def.get("name")
            if isinstance(name, str) and name:
                tool_names.append(name)

    payload = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": session_id,
        "model": model,
        "base_url": base_url,
        "error": error_payload,
        "request": {
            "stream": request_params.get("stream"),
            "temperature": request_params.get("temperature"),
            "max_tokens": request_params.get("max_tokens"),
            "tool_count": len(tool_schemas),
            "tool_names": tool_names,
        },
        "messages": {
            "raw_count": len(raw_messages),
            "sanitized_count": len(sanitized_messages),
            "raw_tail": snapshot_messages(raw_messages),
            "sanitized_tail": snapshot_messages(sanitized_messages),
        },
        "metadata": dict(metadata or {}),
    }

    file_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return file_path
