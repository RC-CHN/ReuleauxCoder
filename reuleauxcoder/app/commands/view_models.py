"""Typed command view models shared by CLI, TUI and remote presenters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


class ViewModel(Protocol):
    view_type: str

    def to_payload(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class MarkdownViewModel:
    view_type: str
    markdown: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {**self.data, "markdown": self.markdown}


@dataclass(frozen=True, slots=True)
class MCPServerViewModel:
    name: str
    enabled: bool
    runtime_connected: bool


@dataclass(frozen=True, slots=True)
class MCPServersViewModel:
    servers: tuple[MCPServerViewModel, ...]
    view_type: str = "mcp_servers"

    def to_payload(self) -> dict[str, Any]:
        return {
            "servers": [
                {
                    "name": server.name,
                    "enabled": server.enabled,
                    "runtime_connected": server.runtime_connected,
                }
                for server in self.servers
            ]
        }


@dataclass(frozen=True, slots=True)
class SessionSummaryViewModel:
    session_id: str
    model: str
    saved_at: str
    preview: str
    fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class SessionsViewModel:
    fingerprint: str | None
    show_all: bool
    sessions: tuple[SessionSummaryViewModel, ...]
    view_type: str = "sessions"

    def to_payload(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "show_all": self.show_all,
            "sessions": [
                {
                    "id": session.session_id,
                    "model": session.model,
                    "saved_at": session.saved_at,
                    "preview": session.preview,
                    "fingerprint": session.fingerprint,
                }
                for session in self.sessions
            ],
        }


@dataclass(frozen=True, slots=True)
class DataViewModel:
    view_type: str
    data: Mapping[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return dict(self.data)


def view_model_from_payload(
    view_type: str, payload: Mapping[str, Any] | None
) -> ViewModel:
    """Single legacy payload adapter used while command builders migrate."""
    data = dict(payload or {})
    if view_type == "mcp_servers":
        return MCPServersViewModel(
            servers=tuple(
                MCPServerViewModel(
                    name=str(server.get("name", "")),
                    enabled=bool(server.get("enabled")),
                    runtime_connected=bool(server.get("runtime_connected")),
                )
                for server in data.get("servers", [])
            )
        )
    if view_type == "sessions":
        return SessionsViewModel(
            fingerprint=data.get("fingerprint"),
            show_all=bool(data.get("show_all")),
            sessions=tuple(
                SessionSummaryViewModel(
                    session_id=str(session.get("id", "")),
                    model=str(session.get("model", "")),
                    saved_at=str(session.get("saved_at", "")),
                    preview=str(session.get("preview", "")),
                    fingerprint=session.get("fingerprint"),
                )
                for session in data.get("sessions", [])
            ),
        )
    markdown = data.pop("markdown", None)
    if isinstance(markdown, str):
        return MarkdownViewModel(view_type=view_type, markdown=markdown, data=data)
    return DataViewModel(view_type=view_type, data=data)
