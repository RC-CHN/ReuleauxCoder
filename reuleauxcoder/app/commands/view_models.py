"""Typed command view models shared by CLI, TUI and remote presenters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


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
    actual_prompt_tokens: int | None
    cached_input_tokens: int | None
    planning_at: int
    quality_wall: int
    rewrite_target: int
    emergency_at: int
    cache_epoch: int
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
                "actual_prompt_tokens",
                "cached_input_tokens",
                "planning_at",
                "quality_wall",
                "rewrite_target",
                "emergency_at",
                "cache_epoch",
            )
        }


@dataclass(frozen=True, slots=True)
class SubagentJobViewModel:
    job_id: str
    parent_agent_id: str | None
    parent_session_id: str | None
    status: str
    mode: str
    task: str
    created_at: float
    started_at: float | None
    finished_at: float | None
    timeout_seconds: float | None
    generation: int
    result: str | None
    error: str | None
    depth: int = 0
    parent_job_id: str | None = None
    context_mode: str = "recent"
    transcript_ref: str | None = None
    worktree_path: str | None = None


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
                    "parent_agent_id": job.parent_agent_id,
                    "parent_session_id": job.parent_session_id,
                    "status": job.status,
                    "mode": job.mode,
                    "task": job.task,
                    "created_at": job.created_at,
                    "started_at": job.started_at,
                    "finished_at": job.finished_at,
                    "timeout_seconds": job.timeout_seconds,
                    "generation": job.generation,
                    "result": job.result,
                    "error": job.error,
                    "depth": job.depth,
                    "parent_job_id": job.parent_job_id,
                    "context_mode": job.context_mode,
                    "transcript_ref": job.transcript_ref,
                    "worktree_path": job.worktree_path,
                }
                for job in self.jobs
            ],
        }


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
    position: int | None = None
    active: bool = False


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
                    "position": session.position,
                    "model": session.model,
                    "saved_at": session.saved_at,
                    "preview": session.preview,
                    "fingerprint": session.fingerprint,
                    "active": session.active,
                }
                for session in self.sessions
            ],
        }


@dataclass(frozen=True, slots=True)
class SessionTranscriptEntryViewModel:
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class SessionResumeViewModel:
    session_id: str
    model: str
    saved_at: str
    active_mode: str | None
    entries: tuple[SessionTranscriptEntryViewModel, ...]
    view_type: str = "session_resume"

    def to_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "model": self.model,
            "saved_at": self.saved_at,
            "active_mode": self.active_mode,
            "entries": [
                {"role": entry.role, "content": entry.content} for entry in self.entries
            ],
        }


@dataclass(frozen=True, slots=True)
class EffectiveConfigRowViewModel:
    path: str
    value: str
    source: str


@dataclass(frozen=True, slots=True)
class EffectiveConfigViewModel:
    rows: tuple[EffectiveConfigRowViewModel, ...]
    diagnostics: tuple[str, ...] = ()
    extension_graph: tuple[str, ...] = ()
    extension_scopes: tuple[str, ...] = ()
    lsp_scopes: tuple[str, ...] = ()
    peer_capabilities: tuple[str, ...] = ()
    active_jobs: tuple[str, ...] = ()
    view_type: str = "effective_config"

    def to_payload(self) -> dict[str, Any]:
        return {
            "rows": [
                {"path": row.path, "value": row.value, "source": row.source}
                for row in self.rows
            ],
            "diagnostics": list(self.diagnostics),
            "extension_graph": list(self.extension_graph),
            "extension_scopes": list(self.extension_scopes),
            "lsp_scopes": list(self.lsp_scopes),
            "peer_capabilities": list(self.peer_capabilities),
            "active_jobs": list(self.active_jobs),
        }
