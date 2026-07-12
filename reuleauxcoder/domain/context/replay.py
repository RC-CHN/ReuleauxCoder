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
    instructions: tuple[dict, ...]
    tools: tuple[dict, ...]
    items: tuple[dict, ...]
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
        instructions: list[dict],
        tools: list[dict],
        items: list[dict],
    ) -> "ReplayEnvelope":
        core = {
            "schema_version": 1,
            "session_id": session_id,
            "cache_epoch": cache_epoch,
            "history_version": history_version,
            "model_profile": model_profile,
            "provider_family": provider_family,
            "request_mode": request_mode,
            "instructions": instructions,
            "tools": tools,
            "items": items,
        }
        stable = content_hash(
            {
                "model_profile": model_profile,
                "provider_family": provider_family,
                "request_mode": request_mode,
                "instructions": instructions,
                "tools": tools,
                "items": items,
            }
        )
        return cls(
            schema_version=1,
            session_id=session_id,
            view_id=f"rv_{uuid.uuid4().hex[:12]}",
            cache_epoch=cache_epoch,
            history_version=history_version,
            model_profile=model_profile,
            provider_family=provider_family,
            request_mode=request_mode,
            instructions=tuple(canonicalize(instructions)),
            tools=tuple(canonicalize(tools)),
            items=tuple(canonicalize(items)),
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
            instructions=tuple(dict(item) for item in data.get("instructions", [])),
            tools=tuple(dict(item) for item in data.get("tools", [])),
            items=tuple(dict(item) for item in data.get("items", [])),
            stable_prefix_hash=str(data.get("stable_prefix_hash") or ""),
            canonical_payload_hash=str(data.get("canonical_payload_hash") or ""),
        )

    def validate(self) -> bool:
        rebuilt = ReplayEnvelope.create(
            session_id=self.session_id,
            cache_epoch=self.cache_epoch,
            history_version=self.history_version,
            model_profile=self.model_profile,
            provider_family=self.provider_family,
            request_mode=self.request_mode,
            instructions=list(self.instructions),
            tools=list(self.tools),
            items=list(self.items),
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
                {"replay": replay.to_dict(), "overlay": overlay}
            ),
            plan_revision=max(0, int(plan_revision)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
