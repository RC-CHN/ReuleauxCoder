"""Remote execution relay protocol message models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

REMOTE_PROTOCOL_VERSION = 2
REMOTE_PROTOCOL_MIN_VERSION = 1


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


@dataclass(frozen=True, slots=True)
class TerminalCapabilities:
    width: int = 80
    color_level: str = "none"
    unicode: bool = True
    interactive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "color_level": self.color_level,
            "unicode": self.unicode,
            "interactive": self.interactive,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TerminalCapabilities":
        values = data or {}
        width = int(values.get("width", 80) or 80)
        color_level = str(values.get("color_level", "none"))
        if color_level not in {"none", "standard", "256", "truecolor"}:
            color_level = "none"
        return cls(
            width=max(20, min(width, 500)),
            color_level=color_level,
            unicode=bool(values.get("unicode", True)),
            interactive=bool(values.get("interactive", False)),
        )


@dataclass
class RegisterRequest:
    bootstrap_token: str
    host_info_min: dict[str, Any] = field(default_factory=dict)
    cwd: str = "."
    workspace_root: str | None = None
    capabilities: list[str] = field(default_factory=list)
    protocol_version: int = REMOTE_PROTOCOL_MIN_VERSION
    terminal: TerminalCapabilities = field(default_factory=TerminalCapabilities)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bootstrap_token": self.bootstrap_token,
            "host_info_min": self.host_info_min,
            "cwd": self.cwd,
            "workspace_root": self.workspace_root,
            "capabilities": self.capabilities,
            "protocol_version": self.protocol_version,
            "terminal": self.terminal.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RegisterRequest":
        return cls(
            bootstrap_token=d["bootstrap_token"],
            host_info_min=d.get("host_info_min", {}),
            cwd=d.get("cwd", "."),
            workspace_root=d.get("workspace_root"),
            capabilities=d.get("capabilities", []),
            protocol_version=int(
                d.get("protocol_version", REMOTE_PROTOCOL_MIN_VERSION)
            ),
            terminal=TerminalCapabilities.from_dict(d.get("terminal")),
        )


@dataclass
class RegisterResponse:
    peer_id: str
    peer_token: str
    heartbeat_interval_sec: int = 10
    protocol_version: int = REMOTE_PROTOCOL_MIN_VERSION
    host_capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_id": self.peer_id,
            "peer_token": self.peer_token,
            "heartbeat_interval_sec": self.heartbeat_interval_sec,
            "protocol_version": self.protocol_version,
            "host_capabilities": self.host_capabilities,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RegisterResponse":
        return cls(
            peer_id=d["peer_id"],
            peer_token=d["peer_token"],
            heartbeat_interval_sec=d.get("heartbeat_interval_sec", 10),
            protocol_version=int(
                d.get("protocol_version", REMOTE_PROTOCOL_MIN_VERSION)
            ),
            host_capabilities=[
                str(item) for item in (d.get("host_capabilities") or [])
            ],
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
    terminal: TerminalCapabilities | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"peer_token": self.peer_token, "ts": self.ts}
        if self.terminal is not None:
            result["terminal"] = self.terminal.to_dict()
        return result

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Heartbeat":
        return cls(
            peer_token=d["peer_token"],
            ts=d.get("ts", 0.0),
            terminal=(
                TerminalCapabilities.from_dict(d["terminal"])
                if isinstance(d.get("terminal"), dict)
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class TokenRefreshRequest:
    peer_token: str

    def to_dict(self) -> dict[str, Any]:
        return {"peer_token": self.peer_token}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TokenRefreshRequest":
        return cls(peer_token=str(data["peer_token"]))


@dataclass(frozen=True, slots=True)
class TokenRefreshResponse:
    ok: bool
    peer_token: str | None = None
    expires_in_sec: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "peer_token": self.peer_token,
            "expires_in_sec": self.expires_in_sec,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TokenRefreshResponse":
        return cls(
            ok=bool(data.get("ok")),
            peer_token=data.get("peer_token"),
            expires_in_sec=int(data.get("expires_in_sec", 0)),
            error=data.get("error"),
        )


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


@dataclass(frozen=True, slots=True)
class ChatCancelRequest:
    peer_token: str
    chat_id: str
    reason: str = "user_interrupt"

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "chat_id": self.chat_id,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatCancelRequest":
        return cls(
            peer_token=str(data["peer_token"]),
            chat_id=str(data["chat_id"]),
            reason=str(data.get("reason", "user_interrupt")),
        )


@dataclass(frozen=True, slots=True)
class ChatCancelResponse:
    ok: bool
    already_done: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "already_done": self.already_done,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatCancelResponse":
        return cls(
            ok=bool(data.get("ok")),
            already_done=bool(data.get("already_done")),
            error=data.get("error"),
        )


@dataclass(frozen=True, slots=True)
class ChatControlRequest:
    peer_token: str
    chat_id: str
    control_id: str
    action: str
    content: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "chat_id": self.chat_id,
            "control_id": self.control_id,
            "action": self.action,
            "content": self.content,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatControlRequest":
        peer_token = str(data["peer_token"]).strip()
        chat_id = str(data["chat_id"]).strip()
        control_id = str(data["control_id"]).strip()
        action = str(data["action"]).strip()
        if not peer_token or not chat_id or not control_id or not action:
            raise ValueError(
                "peer_token, chat_id, control_id, and action are required"
            )
        return cls(
            peer_token=peer_token,
            chat_id=chat_id,
            control_id=control_id,
            action=action,
            content=(
                str(data["content"]) if data.get("content") is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ChatControlResponse:
    ok: bool
    control_id: str
    outcome: str
    steering_id: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "control_id": self.control_id,
            "outcome": self.outcome,
            "steering_id": self.steering_id,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChatControlResponse":
        return cls(
            ok=bool(data.get("ok")),
            control_id=str(data.get("control_id", "")),
            outcome=str(data.get("outcome", "rejected")),
            steering_id=data.get("steering_id"),
            reason=data.get("reason"),
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


@dataclass
class InteractionReplyRequest:
    peer_token: str
    chat_id: str
    request_id: str
    value: Any = None
    cancelled: bool = False
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "peer_token": self.peer_token,
            "chat_id": self.chat_id,
            "request_id": self.request_id,
            "value": self.value,
            "cancelled": self.cancelled,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InteractionReplyRequest":
        return cls(
            peer_token=data["peer_token"],
            chat_id=data["chat_id"],
            request_id=data["request_id"],
            value=data.get("value"),
            cancelled=bool(data.get("cancelled", False)),
            reason=data.get("reason"),
        )


@dataclass
class InteractionReplyResponse:
    ok: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "error": self.error}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InteractionReplyResponse":
        return cls(ok=bool(data.get("ok", False)), error=data.get("error"))


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


@dataclass
class WorkspaceRequest:
    operation: str
    args: dict[str, Any] = field(default_factory=dict)
    cwd: str | None = None
    timeout_sec: int = 30

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "args": self.args,
            "cwd": self.cwd,
            "timeout_sec": self.timeout_sec,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkspaceRequest":
        return cls(
            operation=data["operation"],
            args=dict(data.get("args", {})),
            cwd=data.get("cwd"),
            timeout_sec=int(data.get("timeout_sec", 30)),
        )


@dataclass
class WorkspaceResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkspaceResult":
        return cls(
            ok=bool(data["ok"]),
            data=dict(data.get("data", {})),
            error_code=data.get("error_code"),
            error_message=data.get("error_message"),
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


@dataclass(frozen=True, slots=True)
class DisconnectRequest:
    peer_token: str
    reason: str = "peer_initiated"

    def to_dict(self) -> dict[str, Any]:
        return {"peer_token": self.peer_token, "reason": self.reason}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DisconnectRequest":
        return cls(
            peer_token=str(data["peer_token"]),
            reason=str(data.get("reason", "peer_initiated")),
        )


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
