"""Builtin sub-agent job management commands."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.matchers import match_template
from reuleauxcoder.app.commands.models import CommandEffect
from reuleauxcoder.app.commands.module_registry import register_command_module
from reuleauxcoder.app.commands.params import ParamParseError
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.shared import (
    TEXT_REQUIRED,
    UI_TARGETS,
    non_empty_text,
    slash_trigger,
)
from reuleauxcoder.app.commands.specs import ActionSpec
from reuleauxcoder.app.commands.view_models import (
    SubagentJobsViewModel,
    SubagentJobViewModel,
)
from reuleauxcoder.extensions.subagent.manager import get_subagent_manager
from reuleauxcoder.interfaces.events import UIEventKind


@dataclass(frozen=True, slots=True)
class ListSubagentJobsCommand:
    pass


@dataclass(frozen=True, slots=True)
class GetSubagentJobCommand:
    job_id: str


@dataclass(frozen=True, slots=True)
class WaitSubagentJobCommand:
    job_id: str


@dataclass(frozen=True, slots=True)
class ControlSubagentJobCommand:
    action: str
    job_id: str
    message: str = ""


def _parse_list_jobs(user_input: str, parse_ctx):
    for root in ("/agents", "/jobs"):
        if match_template(user_input, root) is not None:
            return ListSubagentJobsCommand()
    return None


def _parse_get_job(user_input: str, parse_ctx):
    captures = _match_agent_command(user_input, "get {job_id+}")
    if captures is None:
        return None

    try:
        job_id = non_empty_text().parse(captures["job_id"])
    except ParamParseError:
        return None
    return GetSubagentJobCommand(job_id=job_id)


def _parse_wait_job(user_input: str, parse_ctx):
    captures = _match_agent_command(user_input, "wait {job_id+}")
    if captures is None:
        return None

    try:
        job_id = non_empty_text().parse(captures["job_id"])
    except ParamParseError:
        return None
    return WaitSubagentJobCommand(job_id=job_id)


def _parse_control_job(user_input: str, parse_ctx):
    for action in ("cancel", "stop", "cleanup"):
        captures = _match_agent_command(user_input, f"{action} {{job_id+}}")
        if captures is not None:
            return ControlSubagentJobCommand(
                action=action, job_id=captures["job_id"].strip()
            )
    for action in ("message", "resume"):
        captures = _match_agent_command(
            user_input, f"{action} {{job_id}} {{message+}}"
        )
        if captures is not None:
            return ControlSubagentJobCommand(
                action=action,
                job_id=captures["job_id"].strip(),
                message=captures["message"].strip(),
            )
    return None


def _match_agent_command(user_input: str, suffix: str):
    for root in ("/agents", "/jobs"):
        captures = match_template(user_input, f"{root} {suffix}")
        if captures is not None:
            return captures
    return None


def _build_jobs_view(manager, jobs) -> SubagentJobsViewModel:
    return SubagentJobsViewModel(
        jobs=tuple(
            SubagentJobViewModel(
                job_id=job.id,
                parent_agent_id=job.parent_agent_id,
                parent_session_id=job.parent_session_id,
                status=job.status,
                mode=job.mode,
                task=job.task,
                created_at=job.created_at,
                started_at=job.started_at,
                finished_at=job.finished_at,
                timeout_seconds=job.timeout_seconds,
                generation=job.generation,
                result=job.result,
                error=job.error,
                depth=job.depth,
                parent_job_id=job.parent_job_id,
                context_mode=job.context_mode,
                transcript_ref=(
                    job.structured_result.transcript_ref
                    if job.structured_result is not None
                    else None
                ),
                worktree_path=job.worktree_path,
            )
            for job in jobs
        ),
        runtime_parallel_explore=manager.runtime_parallel_explore,
        max_parallel_explore=manager.max_parallel_explore,
    )


def _handle_list_jobs(command, ctx) -> CommandEffect:
    manager = get_subagent_manager(ctx.agent)
    jobs = manager.list_jobs()
    view = _build_jobs_view(manager, jobs)
    ctx.effect.open_view(
        view.view_type,
        title="Sub-agent Jobs",
        view_model=view,
        reuse_key=view.view_type,
    )
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _handle_get_job(command, ctx) -> CommandEffect:
    manager = get_subagent_manager(ctx.agent)
    job = manager.get_job(command.job_id)
    if job is None:
        ctx.effect.error(
            f"Sub-agent job '{command.job_id}' not found.", kind=UIEventKind.COMMAND
        )
        return ctx.effect.finish(control="continue")

    view = _build_jobs_view(manager, [job])
    ctx.effect.open_view(
        view.view_type,
        title=f"Sub-agent Job {job.id}",
        view_model=view,
        reuse_key=view.view_type,
    )
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _handle_wait_job(command, ctx) -> CommandEffect:
    manager = get_subagent_manager(ctx.agent)
    job = manager.wait_job(command.job_id)
    if job is None:
        ctx.effect.error(
            f"Sub-agent job '{command.job_id}' not found.", kind=UIEventKind.COMMAND
        )
        return ctx.effect.finish(control="continue")

    if job.status == "completed":
        ctx.effect.success(
            f"Job {job.id} completed.\n{job.result or ''}",
            kind=UIEventKind.COMMAND,
            job_id=job.id,
        )
    elif job.status == "failed":
        ctx.effect.error(
            f"Job {job.id} failed: {job.error or 'unknown error'}",
            kind=UIEventKind.COMMAND,
            job_id=job.id,
        )
    else:
        ctx.effect.warning(
            f"Job {job.id} status: {job.status}",
            kind=UIEventKind.COMMAND,
            job_id=job.id,
        )

    view = _build_jobs_view(manager, manager.list_jobs())
    ctx.effect.refresh_view(
        view.view_type,
        title="Sub-agent Jobs",
        view_model=view,
        reuse_key=view.view_type,
    )
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _handle_control_job(command, ctx) -> CommandEffect:
    manager = get_subagent_manager(ctx.agent)
    try:
        if command.action in {"cancel", "stop"}:
            ok = manager.cancel_job(command.job_id)
            message = "Cancellation requested"
        elif command.action == "cleanup":
            ok = manager.cleanup_worktree(command.job_id)
            message = "Worktree removed"
        elif command.action == "message":
            ok = manager.send_message(
                command.job_id,
                command.message,
                source="human",
            )
            message = "Message queued"
        else:
            resumed_id = manager.follow_up(
                parent_agent=ctx.agent,
                job_id=command.job_id,
                message=command.message,
            )
            ok = True
            message = f"Follow-up started as {resumed_id}"
    except (OSError, RuntimeError, ValueError) as error:
        ok = False
        message = str(error)

    if ok:
        ctx.effect.success(
            f"{message}: {command.job_id}", kind=UIEventKind.COMMAND
        )
    else:
        ctx.effect.error(
            f"Sub-agent {command.action} failed: {message}", kind=UIEventKind.COMMAND
        )
    return ctx.effect.finish(control="continue")


@register_command_module
def register_actions(registry: ActionRegistry) -> None:
    registry.register_many(
        [
            ActionSpec(
                action_id="subagent.jobs.list",
                feature_id="subagent",
                description="[session] List sub-agent background jobs spawned from this session runtime",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/agents"), slash_trigger("/jobs")),
                parser=_parse_list_jobs,
                handler=_handle_list_jobs,
            ),
            ActionSpec(
                action_id="subagent.jobs.get",
                feature_id="subagent",
                description="[session] Show sub-agent job details for this session runtime",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(
                    slash_trigger("/agents get <id>"),
                    slash_trigger("/jobs get <id>"),
                ),
                parser=_parse_get_job,
                handler=_handle_get_job,
            ),
            ActionSpec(
                action_id="subagent.jobs.wait",
                feature_id="subagent",
                description="[session] Wait for a sub-agent job started from this session runtime",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(
                    slash_trigger("/agents wait <id>"),
                    slash_trigger("/jobs wait <id>"),
                ),
                parser=_parse_wait_job,
                handler=_handle_wait_job,
            ),
            ActionSpec(
                action_id="subagent.jobs.control",
                feature_id="subagent",
                description="[session] Cancel, message, resume, or clean up a sub-agent job",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(
                    slash_trigger("/agents cancel <id>"),
                    slash_trigger("/agents stop <id>"),
                    slash_trigger("/agents message <id> <text>"),
                    slash_trigger("/agents resume <id> <text>"),
                    slash_trigger("/agents cleanup <id>"),
                    slash_trigger("/jobs cancel <id>"),
                    slash_trigger("/jobs message <id> <text>"),
                    slash_trigger("/jobs resume <id> <text>"),
                    slash_trigger("/jobs cleanup <id>"),
                ),
                parser=_parse_control_job,
                handler=_handle_control_job,
            ),
        ]
    )
