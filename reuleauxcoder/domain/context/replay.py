"""Canonical replay and full-request audit envelopes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
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
