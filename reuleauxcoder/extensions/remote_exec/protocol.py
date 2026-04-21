"""Remote execution relay protocol message models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RelayEnvelope:
    """Top-level message wrapper for all relay communications."""

    type: str
    request_id: str | None = None
    peer_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "request_id": self.request_id,
            "peer_id": self.peer_id,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RelayEnvelope":
        return cls(
            type=d["type"],
            request_id=d.get("request_id"),
            peer_id=d.get("peer_id"),
            payload=d.get("payload", {}),
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@dataclass
class RegisterRequest:
    bootstrap_token: str
    host_info_min: dict[str, Any] = field(default_factory=dict)
    cwd: str = "."
    workspace_root: str | None = None
    capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bootstrap_token": self.bootstrap_token,
            "host_info_min": self.host_info_min,
            "cwd": self.cwd,
            "workspace_root": self.workspace_root,
            "capabilities": self.capabilities,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RegisterRequest":
        return cls(
            bootstrap_token=d["bootstrap_token"],
            host_info_min=d.get("host_info_min", {}),
            cwd=d.get("cwd", "."),
            workspace_root=d.get("workspace_root"),
            capabilities=d.get("capabilities", []),
        )


@dataclass
class RegisterResponse:
    peer_id: str
    peer_token: str
    heartbeat_interval_sec: int = 10

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_id": self.peer_id,
            "peer_token": self.peer_token,
            "heartbeat_interval_sec": self.heartbeat_interval_sec,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RegisterResponse":
        return cls(
            peer_id=d["peer_id"],
            peer_token=d["peer_token"],
            heartbeat_interval_sec=d.get("heartbeat_interval_sec", 10),
        )


@dataclass
class RegisterRejected:
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RegisterRejected":
        return cls(reason=d.get("reason", "unknown"))


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

@dataclass
class Heartbeat:
    peer_token: str
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"peer_token": self.peer_token, "ts": self.ts}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Heartbeat":
        return cls(peer_token=d["peer_token"], ts=d.get("ts", 0.0))


# ---------------------------------------------------------------------------
# Chat proxy (interactive peer -> host agent)
# ---------------------------------------------------------------------------

@dataclass
class ChatRequest:
    peer_token: str
    prompt: str

    def to_dict(self) -> dict[str, Any]:
        return {"peer_token": self.peer_token, "prompt": self.prompt}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatRequest":
        return cls(peer_token=d["peer_token"], prompt=d["prompt"])


@dataclass
class ChatResponse:
    response: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"response": self.response, "error": self.error}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatResponse":
        return cls(response=d.get("response", ""), error=d.get("error"))


@dataclass
class ChatStartRequest:
    peer_token: str
    prompt: str
    session_hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "prompt": self.prompt,
            "session_hint": self.session_hint,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatStartRequest":
        return cls(
            peer_token=d["peer_token"],
            prompt=d["prompt"],
            session_hint=d.get("session_hint"),
        )


@dataclass
class ChatStartResponse:
    chat_id: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"chat_id": self.chat_id, "error": self.error}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatStartResponse":
        return cls(chat_id=d.get("chat_id", ""), error=d.get("error"))


@dataclass
class ChatStreamRequest:
    peer_token: str
    chat_id: str
    cursor: int = 0
    timeout_sec: float = 30.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "chat_id": self.chat_id,
            "cursor": self.cursor,
            "timeout_sec": self.timeout_sec,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatStreamRequest":
        return cls(
            peer_token=d["peer_token"],
            chat_id=d["chat_id"],
            cursor=int(d.get("cursor", 0)),
            timeout_sec=float(d.get("timeout_sec", 30.0)),
        )


@dataclass
class ChatStreamResponse:
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    next_cursor: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": self.events,
            "done": self.done,
            "next_cursor": self.next_cursor,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatStreamResponse":
        return cls(
            events=list(d.get("events", [])),
            done=bool(d.get("done", False)),
            next_cursor=int(d.get("next_cursor", 0)),
            error=d.get("error"),
        )


@dataclass
class ApprovalReplyRequest:
    peer_token: str
    chat_id: str
    approval_id: str
    decision: str
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "chat_id": self.chat_id,
            "approval_id": self.approval_id,
            "decision": self.decision,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ApprovalReplyRequest":
        return cls(
            peer_token=d["peer_token"],
            chat_id=d["chat_id"],
            approval_id=d["approval_id"],
            decision=d["decision"],
            reason=d.get("reason"),
        )


@dataclass
class ApprovalReplyResponse:
    ok: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "error": self.error}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ApprovalReplyResponse":
        return cls(ok=bool(d.get("ok", False)), error=d.get("error"))


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

@dataclass
class ExecToolRequest:
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    cwd: str | None = None
    timeout_sec: int = 30

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "args": self.args,
            "cwd": self.cwd,
            "timeout_sec": self.timeout_sec,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExecToolRequest":
        return cls(
            tool_name=d["tool_name"],
            args=d.get("args", {}),
            cwd=d.get("cwd"),
            timeout_sec=d.get("timeout_sec", 30),
        )


@dataclass
class ExecToolResult:
    ok: bool
    result: str = ""
    error_code: str | None = None
    error_message: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "result": self.result,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ExecToolResult":
        return cls(
            ok=d["ok"],
            result=d.get("result", ""),
            error_code=d.get("error_code"),
            error_message=d.get("error_message"),
            meta=d.get("meta", {}),
        )


# ---------------------------------------------------------------------------
# Stream chunk (MVP: shell only if needed; struct kept for forward-compat)
# ---------------------------------------------------------------------------

@dataclass
class ToolStreamChunk:
    chunk_type: str  # "stdout" | "stderr" | "exit"
    data: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"chunk_type": self.chunk_type, "data": self.data, "meta": self.meta}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolStreamChunk":
        return cls(
            chunk_type=d["chunk_type"],
            data=d.get("data", ""),
            meta=d.get("meta", {}),
        )


# ---------------------------------------------------------------------------
# Disconnect / Cleanup
# ---------------------------------------------------------------------------

@dataclass
class DisconnectNotice:
    reason: str = "peer_initiated"

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DisconnectNotice":
        return cls(reason=d.get("reason", "peer_initiated"))


@dataclass
class CleanupRequest:
    pass

    def to_dict(self) -> dict[str, Any]:
        return {}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CleanupRequest":
        return cls()


@dataclass
class CleanupResult:
    ok: bool
    removed_items: list[str] = field(default_factory=list)
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "removed_items": self.removed_items,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CleanupResult":
        return cls(
            ok=d["ok"],
            removed_items=d.get("removed_items", []),
            error_message=d.get("error_message"),
        )


# ---------------------------------------------------------------------------
# Generic error
# ---------------------------------------------------------------------------

@dataclass
class ErrorMessage:
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ErrorMessage":
        return cls(code=d["code"], message=d.get("message", ""))
