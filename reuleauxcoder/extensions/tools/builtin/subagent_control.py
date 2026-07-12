"""Root-only Codex-style subagent lifecycle tools."""

from __future__ import annotations

import time

from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.extensions.subagent.manager import get_subagent_manager
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool


_TERMINAL = {
    "completed",
    "failed",
    "cancelled",
    "killed",
    "timed_out",
    "indeterminate",
    "stale",
}


class _RootSubagentTool(Tool):
    effect_class = "control_plane_internal"

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())
        self._agent = None

    def bind_agent(self, agent) -> None:
        self._agent = agent

    def _root(self):
        if self._agent is None:
            raise RuntimeError(f"{self.name} is not bound to an agent")
        if getattr(self._agent, "subagent_depth", 0) > 0:
            raise RuntimeError(f"{self.name} is available only to the root agent")
        return self._agent


@register_tool
class SpawnAgentTool(_RootSubagentTool):
    name = "spawn_agent"
    effect_class = "model_delegation"
    description = (
        "Create one subagent and return its job ID immediately. The child runs "
        "asynchronously; call wait_agent only when there is no other useful work."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "minLength": 1},
            "mode": {
                "type": "string",
                "enum": ["explore", "execute", "verify"],
            },
            "model": {"type": "string", "enum": ["sub", "main"]},
            "context": {
                "type": "string",
                "enum": ["minimal", "recent", "full"],
            },
            "max_rounds": {"type": "integer", "minimum": 1, "maximum": 50},
            "timeout_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3600,
            },
            "max_tool_calls": {"type": "integer", "minimum": 1},
            "max_tokens": {"type": "integer", "minimum": 1},
            "isolation": {"type": "string", "enum": ["worktree"]},
        },
        "required": ["message"],
    }

    def execute(self, **kwargs) -> ToolOutcome:
        return self.run_backend(**kwargs)

    @backend_handler("local")
    def _execute_local(
        self,
        message: str,
        mode: str = "explore",
        model: str = "sub",
        context: str = "recent",
        max_rounds: int = 50,
        timeout_seconds: int = 300,
        max_tool_calls: int = 80,
        max_tokens: int | None = None,
        isolation: str | None = None,
    ) -> ToolOutcome:
        try:
            root = self._root()
            text = message.strip()
            if not text:
                raise ValueError("message must be non-empty")
            if mode not in {"explore", "execute", "verify"}:
                raise ValueError(f"unsupported subagent mode: {mode}")
            mode_config = root.get_active_mode_config()
            allowed_modes = set(
                getattr(mode_config, "allowed_subagent_modes", ()) or ()
            )
            if allowed_modes and mode not in allowed_modes:
                raise ValueError(
                    f"subagent mode '{mode}' is unavailable in the active mode"
                )
            if isolation == "worktree" and mode != "execute":
                raise ValueError("worktree isolation requires mode='execute'")
            job_id = get_subagent_manager(root).submit_background(
                parent_agent=root,
                task=text,
                mode=mode,
                max_rounds=max_rounds,
                timeout_seconds=timeout_seconds,
                model_profile_name=model,
                context_mode=context,
                depth=1,
                worktree=isolation == "worktree",
                max_tool_calls=max_tool_calls,
                max_tokens=max_tokens,
            )
            if not isinstance(job_id, str):
                raise RuntimeError("subagent scheduler did not return a job ID")
            return ToolOutcome(
                summary=f"Spawned {job_id}",
                content=f"Subagent created · job_id={job_id}",
                metadata={"job_id": job_id, "mode": mode},
            )
        except (TypeError, ValueError, RuntimeError) as error:
            return _invalid(str(error))


@register_tool
class SendMessageTool(_RootSubagentTool):
    name = "send_message"
    description = "Queue a directive for one running child without waiting for a reply."
    parameters = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "message": {"type": "string", "minLength": 1},
        },
        "required": ["job_id", "message"],
    }

    def execute(self, job_id: str, message: str) -> ToolOutcome:
        try:
            root = self._root()
            directive = get_subagent_manager(root).queue_message(
                job_id,
                message,
                sender_agent_id=root.agent_id,
            )
            if directive is None:
                raise ValueError("target child is not running or does not exist")
            return ToolOutcome(
                summary=f"Message queued for {job_id}",
                content=f"Directive queued · directive_id={directive.directive_id}",
                metadata={
                    "job_id": job_id,
                    "directive_id": directive.directive_id,
                },
            )
        except (TypeError, ValueError, RuntimeError) as error:
            return _invalid(str(error))


@register_tool
class ListAgentsTool(_RootSubagentTool):
    name = "list_agents"
    description = "Return a compact snapshot of visible child jobs and their states."
    parameters = {"type": "object", "properties": {}}

    def execute(self) -> ToolOutcome:
        try:
            jobs = get_subagent_manager(self._root()).list_jobs()
            now = time.time()
            rows = [_agent_snapshot(job, now=now) for job in jobs]
            lines = [_agent_snapshot_line(row) for row in rows]
            content = "\n".join(lines) if lines else "No subagents."
            return ToolOutcome(
                summary=f"{len(jobs)} subagent{'s' if len(jobs) != 1 else ''}",
                content=content,
                metadata={"count": len(jobs), "agents": rows},
            )
        except RuntimeError as error:
            return _invalid(str(error))


@register_tool
class WaitAgentTool(_RootSubagentTool):
    name = "wait_agent"
    description = (
        "Wait for subagent mailbox activity. New human input interrupts this wait; "
        "timeout does not cancel any child."
    )
    parameters = {
        "type": "object",
        "properties": {
            "timeout_ms": {
                "type": "integer",
                "minimum": 100,
                "maximum": 300000,
            }
        },
    }

    def execute(self, timeout_ms: int = 30000) -> ToolOutcome:
        try:
            root = self._root()
            timeout_ms = max(100, min(int(timeout_ms), 300000))
            manager = get_subagent_manager(root)
            deadline = time.monotonic() + timeout_ms / 1000
            outcome = "timed_out"
            while time.monotonic() < deadline:
                if root._has_user_steering():
                    outcome = "steered"
                    break
                if manager.wait_for_parent_activity(root.agent_id, timeout=0.1):
                    outcome = "activity"
                    break
                if root.stop_requested():
                    outcome = "cancelled"
                    break
            return ToolOutcome(
                summary={
                    "activity": "Subagent activity available",
                    "steered": "Wait interrupted by user input",
                    "cancelled": "Wait cancelled",
                    "timed_out": "Wait timed out",
                }[outcome],
                content=f"wait_agent · {outcome}",
                metadata={"outcome": outcome, "timed_out": outcome == "timed_out"},
            )
        except (TypeError, ValueError, RuntimeError) as error:
            return _invalid(str(error))


@register_tool
class InterruptAgentTool(_RootSubagentTool):
    name = "interrupt_agent"
    description = "Stop one child job and prevent its late result entering root context."
    parameters = {
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    }

    def execute(self, job_id: str) -> ToolOutcome:
        try:
            manager = get_subagent_manager(self._root())
            if not manager.cancel_job(job_id):
                raise ValueError("job does not exist or is already terminal")
            job = manager.wait_job(job_id, timeout=2.0)
            status = getattr(job, "status", "cancelling")
            if status not in _TERMINAL:
                return ToolOutcome(
                    status=ToolOutcomeStatus.FAILED,
                    summary=f"Cancellation still pending for {job_id}",
                    content=f"interrupt_agent · job_id={job_id} · status={status}",
                    error_kind=ToolErrorKind.EXECUTION,
                    metadata={"job_id": job_id, "status": status},
                )
            return ToolOutcome(
                summary=f"Interrupted {job_id}",
                content=f"interrupt_agent · job_id={job_id} · status={status}",
                metadata={"job_id": job_id, "status": status},
            )
        except (TypeError, ValueError, RuntimeError) as error:
            return _invalid(str(error))


def _invalid(message: str) -> ToolOutcome:
    return ToolOutcome(
        status=ToolOutcomeStatus.FAILED,
        content=f"Error: {message}",
        error_kind=ToolErrorKind.INVALID_ARGUMENTS,
    )


def _clip(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _agent_snapshot(job, *, now: float) -> dict:
    started_at = getattr(job, "started_at", None)
    finished_at = getattr(job, "finished_at", None)
    elapsed = max(0.0, (finished_at or now) - started_at) if started_at else 0.0
    prompt_tokens = int(getattr(job, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(job, "completion_tokens", 0) or 0)
    max_tokens = getattr(job, "max_tokens", None)
    tool_calls = int(getattr(job, "tool_calls", 0) or 0)
    max_tool_calls = getattr(job, "max_tool_calls", None)
    progress = tuple(getattr(job, "progress", ()) or ())
    current_tool = getattr(job, "current_tool", None)
    blocker = (
        getattr(job, "error", None)
        or getattr(job, "guidance_request_id", None)
        if getattr(job, "status", "") == "blocked"
        else None
    )
    return {
        "job_id": job.id,
        "status": job.status,
        "mode": job.mode,
        "task": _clip(job.task, 100),
        "activity": (
            f"running {current_tool}"
            if current_tool
            else _clip(progress[1] if len(progress) > 1 else progress[-1], 80)
            if progress
            else None
        ),
        "elapsed_seconds": round(elapsed, 1),
        "tool_calls": tool_calls,
        "max_tool_calls": max_tool_calls,
        "tokens": prompt_tokens + completion_tokens,
        "max_tokens": max_tokens,
        "blocker": blocker,
    }


def _agent_snapshot_line(row: dict) -> str:
    budget = f"tools {row['tool_calls']}"
    if row["max_tool_calls"] is not None:
        budget += f"/{row['max_tool_calls']}"
    budget += f" · tok {row['tokens']}"
    if row["max_tokens"] is not None:
        budget += f"/{row['max_tokens']}"
    detail = row["blocker"] or row["activity"] or row["task"]
    return (
        f"{row['job_id']} · {row['status']} · {row['mode']} · "
        f"{row['elapsed_seconds']:.1f}s · {budget} · {_clip(str(detail), 100)}"
    )
