"""Typed command view models shared by CLI, TUI and remote presenters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


class ViewModel(Protocol):
    view_type: str

    def to_payload(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class HelpCommandViewModel:
    usage: str
    description: str


@dataclass(frozen=True, slots=True)
class HelpSectionViewModel:
    feature_id: str
    commands: tuple[HelpCommandViewModel, ...]


@dataclass(frozen=True, slots=True)
class HelpViewModel:
    sections: tuple[HelpSectionViewModel, ...]
    diagnostic: str | None = None
    view_type: str = "help"

    def to_payload(self) -> dict[str, Any]:
        return {
            "sections": [
                {
                    "feature_id": section.feature_id,
                    "commands": [
                        {
                            "usage": command.usage,
                            "description": command.description,
                        }
                        for command in section.commands
                    ],
                }
                for section in self.sections
            ],
            "diagnostic": self.diagnostic,
        }


@dataclass(frozen=True, slots=True)
class ModelProfileViewModel:
    name: str
    model: str
    active_main: bool
    active_sub: bool
    base_url: str | None
    max_tokens: int
    temperature: float
    max_context_tokens: int
    api_key_hint: str


@dataclass(frozen=True, slots=True)
class ModelListViewModel:
    active_main: str | None
    active_sub: str | None
    current_model: str
    profiles: tuple[ModelProfileViewModel, ...]
    diagnostics: tuple[str, ...] = ()
    available_actions: tuple[str, ...] = (
        "use-main",
        "use-sub",
        "set-main",
        "set-sub",
    )
    view_type: str = "model_profiles"

    def to_payload(self) -> dict[str, Any]:
        return {
            "active_main_profile": self.active_main,
            "active_sub_profile": self.active_sub,
            "current_model": self.current_model,
            "profiles": [
                {
                    "name": profile.name,
                    "model": profile.model,
                    "active": profile.active_main,
                    "active_main": profile.active_main,
                    "active_sub": profile.active_sub,
                    "base_url": profile.base_url,
                    "max_tokens": profile.max_tokens,
                    "temperature": profile.temperature,
                    "max_context_tokens": profile.max_context_tokens,
                    "api_key_hint": profile.api_key_hint,
                }
                for profile in self.profiles
            ],
            "diagnostics": list(self.diagnostics),
            "available_actions": list(self.available_actions),
        }


@dataclass(frozen=True, slots=True)
class ModeProfileViewModel:
    name: str
    active: bool
    description: str
    tools: tuple[str, ...]
    prompt_append: str
    allowed_subagent_modes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModesViewModel:
    active_mode: str | None
    modes: tuple[ModeProfileViewModel, ...]
    diagnostics: tuple[str, ...] = ()
    view_type: str = "mode_profiles"

    def to_payload(self) -> dict[str, Any]:
        return {
            "active_mode": self.active_mode,
            "modes": [
                {
                    "name": mode.name,
                    "active": mode.active,
                    "description": mode.description,
                    "tools": list(mode.tools),
                    "prompt_append": mode.prompt_append,
                    "allowed_subagent_modes": list(mode.allowed_subagent_modes),
                }
                for mode in self.modes
            ],
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class TokenUsageViewModel:
    prompt_tokens: int
    completion_tokens: int
    lifetime_total: int
    current_context_tokens: int
    max_context_tokens: int
    context_percent: float | None
    message_count: int
    snip_at: float | None
    summarize_at: float | None
    collapse_at: float | None
    snip_hit_count: int
    summarize_hit_count: int
    snip_exhausted: bool
    summarize_exhausted: bool
    max_hits: int
    view_type: str = "token_usage"

    def to_payload(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in (
                "prompt_tokens",
                "completion_tokens",
                "lifetime_total",
                "current_context_tokens",
                "max_context_tokens",
                "context_percent",
                "message_count",
                "snip_at",
                "summarize_at",
                "collapse_at",
                "snip_hit_count",
                "summarize_hit_count",
                "snip_exhausted",
                "summarize_exhausted",
                "max_hits",
            )
        }


@dataclass(frozen=True, slots=True)
class SubagentJobViewModel:
    job_id: str
    status: str
    mode: str
    task: str
    created_at: float
    started_at: float | None
    finished_at: float | None
    timeout_seconds: float | None
    error: str | None


@dataclass(frozen=True, slots=True)
class SubagentJobsViewModel:
    jobs: tuple[SubagentJobViewModel, ...]
    runtime_parallel_explore: int
    max_parallel_explore: int
    view_type: str = "subagent_jobs"

    def to_payload(self) -> dict[str, Any]:
        return {
            "runtime_parallel_explore": self.runtime_parallel_explore,
            "max_parallel_explore": self.max_parallel_explore,
            "jobs": [
                {
                    "id": job.job_id,
                    "status": job.status,
                    "mode": job.mode,
                    "task": job.task,
                    "created_at": job.created_at,
                    "started_at": job.started_at,
                    "finished_at": job.finished_at,
                    "timeout_seconds": job.timeout_seconds,
                    "error": job.error,
                }
                for job in self.jobs
            ],
        }


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


@dataclass(frozen=True, slots=True)
class EffectiveConfigRowViewModel:
    path: str
    value: str
    source: str


@dataclass(frozen=True, slots=True)
class EffectiveConfigViewModel:
    rows: tuple[EffectiveConfigRowViewModel, ...]
    diagnostics: tuple[str, ...] = ()
    view_type: str = "effective_config"

    def to_payload(self) -> dict[str, Any]:
        return {
            "rows": [
                {"path": row.path, "value": row.value, "source": row.source}
                for row in self.rows
            ],
            "diagnostics": list(self.diagnostics),
        }


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
    if view_type == "model_profiles":
        return ModelListViewModel(
            active_main=data.get("active_main_profile") or data.get("active_profile"),
            active_sub=data.get("active_sub_profile"),
            current_model=str(data.get("current_model", "")),
            profiles=tuple(
                ModelProfileViewModel(
                    name=str(profile.get("name", "")),
                    model=str(profile.get("model", "")),
                    active_main=bool(
                        profile.get("active_main", profile.get("active"))
                    ),
                    active_sub=bool(profile.get("active_sub")),
                    base_url=profile.get("base_url"),
                    max_tokens=int(profile.get("max_tokens", 0)),
                    temperature=float(profile.get("temperature", 0)),
                    max_context_tokens=int(profile.get("max_context_tokens", 0)),
                    api_key_hint=str(profile.get("api_key_hint", "")),
                )
                for profile in data.get("profiles", [])
            ),
            diagnostics=tuple(str(item) for item in data.get("diagnostics", [])),
        )
    if view_type == "mode_profiles":
        return ModesViewModel(
            active_mode=data.get("active_mode"),
            modes=tuple(
                ModeProfileViewModel(
                    name=str(mode.get("name", "")),
                    active=bool(mode.get("active")),
                    description=str(mode.get("description", "")),
                    tools=tuple(str(item) for item in mode.get("tools", [])),
                    prompt_append=str(mode.get("prompt_append", "")),
                    allowed_subagent_modes=tuple(
                        str(item)
                        for item in mode.get("allowed_subagent_modes", [])
                    ),
                )
                for mode in data.get("modes", [])
            ),
            diagnostics=tuple(str(item) for item in data.get("diagnostics", [])),
        )
    if view_type == "token_usage":
        return TokenUsageViewModel(
            prompt_tokens=int(data.get("prompt_tokens", 0)),
            completion_tokens=int(data.get("completion_tokens", 0)),
            lifetime_total=int(data.get("lifetime_total", 0)),
            current_context_tokens=int(data.get("current_context_tokens", 0)),
            max_context_tokens=int(data.get("max_context_tokens", 0)),
            context_percent=data.get("context_percent"),
            message_count=int(data.get("message_count", 0)),
            snip_at=data.get("snip_at"),
            summarize_at=data.get("summarize_at"),
            collapse_at=data.get("collapse_at"),
            snip_hit_count=int(data.get("snip_hit_count", 0)),
            summarize_hit_count=int(data.get("summarize_hit_count", 0)),
            snip_exhausted=bool(data.get("snip_exhausted")),
            summarize_exhausted=bool(data.get("summarize_exhausted")),
            max_hits=int(data.get("max_hits", 0)),
        )
    markdown = data.pop("markdown", None)
    if isinstance(markdown, str):
        return MarkdownViewModel(view_type=view_type, markdown=markdown, data=data)
    return DataViewModel(view_type=view_type, data=data)
