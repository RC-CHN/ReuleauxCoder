"""Canonical replay and full-request audit envelopes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re
import unicodedata
import uuid
from typing import Any


def canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): canonicalize(child)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
            if key != "_rc_token_count"
        }
    if isinstance(value, list | tuple):
        return [canonicalize(item) for item in value]
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value.replace("\r\n", "\n"))
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


SUPPORTED_REPLAY_SCHEMA_VERSIONS = frozenset({1, 2, 3})
_PROVIDER_MESSAGE_ROLES = frozenset(
    {"system", "developer", "user", "assistant", "tool"}
)
_MAX_REPLAY_COUNTER = (1 << 63) - 1


def _validate_json_value(value: object, *, depth: int = 0) -> None:
    if depth > 32:
        raise ValueError("provider value nesting is too deep")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("provider value contains a non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("provider object keys must be strings")
        for item in value.values():
            _validate_json_value(item, depth=depth + 1)
        return
    raise TypeError("provider value is not JSON-compatible")


def validate_provider_message(message: object) -> None:
    """Validate a persisted chat-completions message without coercion."""
    if not isinstance(message, dict):
        raise TypeError("provider message must be an object")
    role = message.get("role")
    if role not in _PROVIDER_MESSAGE_ROLES:
        raise ValueError("provider message role is invalid")
    if "content" not in message:
        raise ValueError("provider message content is missing")
    content = message["content"]
    if content is not None and not isinstance(content, (str, list)):
        raise TypeError("provider message content is invalid")
    if content is None and role != "assistant":
        raise ValueError("only assistant content may be null")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                raise TypeError("provider content part must be an object")
            part_type = part.get("type")
            if not isinstance(part_type, str) or not part_type:
                raise ValueError("provider content part type is invalid")
            if part_type in {"text", "input_text", "output_text"} and not isinstance(
                part.get("text"), str
            ):
                raise TypeError("provider text content is invalid")
            _validate_json_value(part)

    tool_call_id = message.get("tool_call_id")
    if tool_call_id is not None and (
        not isinstance(tool_call_id, str) or not tool_call_id
    ):
        raise ValueError("provider tool_call_id is invalid")
    if role == "tool" and not isinstance(tool_call_id, str):
        raise ValueError("tool message requires tool_call_id")
    name = message.get("name")
    if name is not None and (not isinstance(name, str) or not name):
        raise ValueError("provider message name is invalid")

    tool_calls = message.get("tool_calls")
    if tool_calls is not None:
        if role != "assistant" or not isinstance(tool_calls, list):
            raise TypeError("provider tool_calls is invalid")
        for call in tool_calls:
            if not isinstance(call, dict):
                raise TypeError("provider tool call must be an object")
            call_id = call.get("id")
            call_type = call.get("type", "function")
            function = call.get("function")
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_type != "function"
                or not isinstance(function, dict)
                or not isinstance(function.get("name"), str)
                or not function["name"]
                or not isinstance(function.get("arguments"), str)
            ):
                raise ValueError("provider function tool call is invalid")
            _validate_json_value(call)
    _validate_json_value(message)


def validate_replay_payload(
    payload: object, *, expected_session_id: str | None = None
) -> None:
    """Validate raw replay semantics before the compatibility constructor."""
    if not isinstance(payload, dict):
        raise TypeError("replay must be an object")
    schema_version = payload.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in SUPPORTED_REPLAY_SCHEMA_VERSIONS
    ):
        raise ValueError("replay schema version is unsupported")
    required = {
        "schema_version",
        "session_id",
        "view_id",
        "cache_epoch",
        "history_version",
        "model_profile",
        "provider_family",
        "request_mode",
        "instructions",
        "tools",
        "items",
        "stable_prefix_hash",
        "canonical_payload_hash",
    }
    if schema_version >= 2:
        required.add("request_settings")
    if schema_version >= 3:
        required.add("item_provenance")
    if not required.issubset(payload):
        raise ValueError("replay required field is missing")

    session_id = payload["session_id"]
    if schema_version >= 3:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("replay session id is invalid")
    elif session_id is not None and (
        not isinstance(session_id, str) or not session_id
    ):
        raise ValueError("legacy replay session id is invalid")
    if expected_session_id is not None and (
        session_id != expected_session_id
        if schema_version >= 3
        else session_id not in (None, expected_session_id)
    ):
        raise ValueError("replay session id does not match")

    for key in ("cache_epoch", "history_version"):
        value = payload[key]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value > _MAX_REPLAY_COUNTER
        ):
            raise ValueError("replay counter is invalid")
    for key in ("view_id", "model_profile", "provider_family", "request_mode"):
        value = payload[key]
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ValueError("replay text field is invalid")
    for key in ("stable_prefix_hash", "canonical_payload_hash"):
        value = payload[key]
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("replay hash is invalid")

    request_settings = payload.get("request_settings", {})
    if not isinstance(request_settings, dict):
        raise TypeError("replay request settings must be an object")
    _validate_json_value(request_settings)
    for key in ("instructions", "items"):
        messages = payload[key]
        if not isinstance(messages, list):
            raise TypeError("replay messages must be a list")
        for message in messages:
            validate_provider_message(message)
    tools = payload["tools"]
    if not isinstance(tools, list) or not all(isinstance(tool, dict) for tool in tools):
        raise TypeError("replay tools must be a list of objects")
    for tool in tools:
        tool_type = tool.get("type")
        if tool_type is not None and (
            not isinstance(tool_type, str) or not tool_type
        ):
            raise ValueError("replay tool type is invalid")
        if tool_type == "function" or "function" in tool:
            function = tool.get("function")
            if (
                not isinstance(function, dict)
                or not isinstance(function.get("name"), str)
                or not function["name"]
            ):
                raise ValueError("replay function tool is invalid")
            description = function.get("description")
            parameters = function.get("parameters")
            strict = function.get("strict")
            if description is not None and not isinstance(description, str):
                raise TypeError("replay tool description is invalid")
            if parameters is not None and not isinstance(parameters, dict):
                raise TypeError("replay tool parameters are invalid")
            if strict is not None and not isinstance(strict, bool):
                raise TypeError("replay tool strict flag is invalid")
    _validate_json_value(tools)

    provenance = payload.get("item_provenance", [])
    if not isinstance(provenance, list):
        raise TypeError("replay provenance must be a list")
    if schema_version >= 3 and len(provenance) != len(payload["items"]):
        raise ValueError("replay provenance does not align with items")
    for item in provenance:
        if not isinstance(item, dict):
            raise TypeError("replay provenance item must be an object")
        for key in ("source_event_ids", "artifact_refs"):
            refs = item.get(key, [])
            if not isinstance(refs, list) or not all(
                isinstance(ref, str) and ref for ref in refs
            ):
                raise ValueError("replay provenance references are invalid")
        checkpoint_id = item.get("checkpoint_id")
        if checkpoint_id is not None and (
            not isinstance(checkpoint_id, str) or not checkpoint_id
        ):
            raise ValueError("replay checkpoint id is invalid")
        _validate_json_value(item)


@dataclass(frozen=True, slots=True)
class ReplayEnvelope:
    schema_version: int
    session_id: str | None
    view_id: str
    cache_epoch: int
    history_version: int
    model_profile: str
    provider_family: str
    request_mode: str
    request_settings: dict[str, Any]
    instructions: tuple[dict, ...]
    tools: tuple[dict, ...]
    items: tuple[dict, ...]
    item_provenance: tuple[dict, ...]
    stable_prefix_hash: str
    canonical_payload_hash: str

    @classmethod
    def create(
        cls,
        *,
        session_id: str | None,
        cache_epoch: int,
        history_version: int,
        model_profile: str,
        provider_family: str,
        request_mode: str,
        request_settings: dict[str, Any] | None = None,
        instructions: list[dict],
        tools: list[dict],
        items: list[dict],
        item_provenance: list[dict] | None = None,
    ) -> "ReplayEnvelope":
        provenance = list(item_provenance or ({} for _ in items))
        if len(provenance) != len(items):
            raise ValueError("item_provenance must align one-to-one with items")
        settings = canonicalize(request_settings or {})
        core = {
            "schema_version": 3,
            "session_id": session_id,
            "cache_epoch": cache_epoch,
            "history_version": history_version,
            "model_profile": model_profile,
            "provider_family": provider_family,
            "request_mode": request_mode,
            "request_settings": settings,
            "instructions": instructions,
            "tools": tools,
            "items": items,
            "item_provenance": provenance,
        }
        stable = content_hash(
            {
                "model_profile": model_profile,
                "provider_family": provider_family,
                "request_mode": request_mode,
                "request_settings": settings,
                "instructions": instructions,
                "tools": tools,
                "items": items,
            }
        )
        return cls(
            schema_version=3,
            session_id=session_id,
            view_id=f"rv_{uuid.uuid4().hex[:12]}",
            cache_epoch=cache_epoch,
            history_version=history_version,
            model_profile=model_profile,
            provider_family=provider_family,
            request_mode=request_mode,
            request_settings=settings,
            instructions=tuple(canonicalize(instructions)),
            tools=tuple(canonicalize(tools)),
            items=tuple(canonicalize(items)),
            item_provenance=tuple(canonicalize(provenance)),
            stable_prefix_hash=stable,
            canonical_payload_hash=content_hash(core),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReplayEnvelope":
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            session_id=data.get("session_id"),
            view_id=str(data.get("view_id") or "legacy"),
            cache_epoch=int(data.get("cache_epoch", 0)),
            history_version=int(data.get("history_version", 0)),
            model_profile=str(data.get("model_profile") or "unknown"),
            provider_family=str(data.get("provider_family") or "openai-compatible"),
            request_mode=str(data.get("request_mode") or "chat-completions"),
            request_settings=canonicalize(data.get("request_settings") or {}),
            instructions=tuple(dict(item) for item in data.get("instructions", [])),
            tools=tuple(dict(item) for item in data.get("tools", [])),
            items=tuple(dict(item) for item in data.get("items", [])),
            item_provenance=tuple(
                dict(item) for item in data.get("item_provenance", [])
            ),
            stable_prefix_hash=str(data.get("stable_prefix_hash") or ""),
            canonical_payload_hash=str(data.get("canonical_payload_hash") or ""),
        )

    def validate(self) -> bool:
        if self.schema_version < 2:
            legacy_core = {
                "schema_version": 1,
                "session_id": self.session_id,
                "cache_epoch": self.cache_epoch,
                "history_version": self.history_version,
                "model_profile": self.model_profile,
                "provider_family": self.provider_family,
                "request_mode": self.request_mode,
                "instructions": list(self.instructions),
                "tools": list(self.tools),
                "items": list(self.items),
            }
            legacy_stable = {
                key: legacy_core[key]
                for key in (
                    "model_profile",
                    "provider_family",
                    "request_mode",
                    "instructions",
                    "tools",
                    "items",
                )
            }
            return (
                content_hash(legacy_stable) == self.stable_prefix_hash
                and content_hash(legacy_core) == self.canonical_payload_hash
            )
        if self.schema_version == 2:
            legacy_core = {
                "schema_version": 2,
                "session_id": self.session_id,
                "cache_epoch": self.cache_epoch,
                "history_version": self.history_version,
                "model_profile": self.model_profile,
                "provider_family": self.provider_family,
                "request_mode": self.request_mode,
                "request_settings": self.request_settings,
                "instructions": list(self.instructions),
                "tools": list(self.tools),
                "items": list(self.items),
            }
            legacy_stable = {
                key: legacy_core[key]
                for key in (
                    "model_profile",
                    "provider_family",
                    "request_mode",
                    "request_settings",
                    "instructions",
                    "tools",
                    "items",
                )
            }
            return (
                content_hash(legacy_stable) == self.stable_prefix_hash
                and content_hash(legacy_core) == self.canonical_payload_hash
            )
        if len(self.item_provenance) != len(self.items):
            return False
        rebuilt = ReplayEnvelope.create(
            session_id=self.session_id,
            cache_epoch=self.cache_epoch,
            history_version=self.history_version,
            model_profile=self.model_profile,
            provider_family=self.provider_family,
            request_mode=self.request_mode,
            request_settings=self.request_settings,
            instructions=list(self.instructions),
            tools=list(self.tools),
            items=list(self.items),
            item_provenance=list(self.item_provenance),
        )
        return (
            rebuilt.stable_prefix_hash == self.stable_prefix_hash
            and rebuilt.canonical_payload_hash == self.canonical_payload_hash
        )

    def validate_protocol(self) -> bool:
        """Require exact assistant tool-call/result adjacency for L1 replay."""
        from reuleauxcoder.domain.llm.tool_history import reconcile_tool_call_adjacency

        items = [dict(item) for item in self.items]
        repaired, synthesized = reconcile_tool_call_adjacency(items)
        return synthesized == 0 and canonical_json(repaired) == canonical_json(items)


def align_item_provenance(
    items: list[dict], events, *, fallback_event_id: str | None = None
) -> list[dict[str, Any]]:
    """Trace each exact model item to a message/view event without altering it."""
    result: list[dict[str, Any] | None] = [None] * len(items)
    event_list = list(events)
    latest_view = next(
        (
            event
            for event in reversed(event_list)
            if getattr(event, "kind", None) == "context_view_committed"
        ),
        None,
    )
    if latest_view is not None:
        view_items = list(getattr(latest_view, "payload", {}).get("items") or [])
        cursor = 0
        for index, item in enumerate(items):
            while cursor < len(view_items):
                candidate = view_items[cursor]
                cursor += 1
                if content_hash(candidate) == content_hash(item):
                    result[index] = {
                        "source_event_ids": [latest_view.event_id],
                        "artifact_refs": list(
                            getattr(latest_view, "artifact_refs", ())
                        ),
                        "checkpoint_id": getattr(latest_view, "payload", {}).get(
                            "checkpoint_id"
                        ),
                    }
                    break

    message_events: dict[str, list] = {}
    for event in event_list:
        if getattr(event, "kind", None) != "message_committed":
            continue
        message = getattr(event, "payload", {}).get("message")
        if isinstance(message, dict):
            message_events.setdefault(content_hash(message), []).append(event)
    used_event_ids: set[str] = set()
    for index, item in enumerate(items):
        if result[index] is not None:
            continue
        candidates = message_events.get(content_hash(item), [])
        event = next(
            (
                candidate
                for candidate in reversed(candidates)
                if candidate.event_id not in used_event_ids
            ),
            None,
        )
        if event is not None:
            used_event_ids.add(event.event_id)
            result[index] = {
                "source_event_ids": [event.event_id],
                "artifact_refs": list(getattr(event, "artifact_refs", ())),
                "checkpoint_id": None,
            }
        else:
            result[index] = {
                "source_event_ids": [fallback_event_id] if fallback_event_id else [],
                "artifact_refs": [],
                "checkpoint_id": None,
            }
    return [dict(item or {}) for item in result]


@dataclass(frozen=True, slots=True)
class RequestEnvelope:
    schema_version: int
    request_id: str
    replay_envelope_hash: str
    execution_overlay_revision: int
    execution_overlay_hash: str
    execution_overlay_tokens: int
    canonical_request_hash: str
    plan_revision: int = 0

    @classmethod
    def create(
        cls,
        *,
        replay: ReplayEnvelope,
        overlay: dict,
        overlay_revision: int,
        overlay_tokens: int,
        plan_revision: int = 0,
        canonical_request_payload: dict[str, Any] | None = None,
    ) -> "RequestEnvelope":
        overlay_hash = content_hash(overlay)
        return cls(
            schema_version=1,
            request_id=f"rq_{uuid.uuid4().hex[:12]}",
            replay_envelope_hash=replay.canonical_payload_hash,
            execution_overlay_revision=overlay_revision,
            execution_overlay_hash=overlay_hash,
            execution_overlay_tokens=overlay_tokens,
            canonical_request_hash=content_hash(
                canonical_request_payload
                if canonical_request_payload is not None
                else {"replay": replay.to_dict(), "overlay": overlay}
            ),
            plan_revision=max(0, int(plan_revision)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
