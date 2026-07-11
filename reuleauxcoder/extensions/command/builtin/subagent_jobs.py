"""Builtin sub-agent job management commands."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.matchers import match_template
from reuleauxcoder.app.commands.models import CommandResult
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


def _parse_list_jobs(user_input: str, parse_ctx):
    if match_template(user_input, "/jobs") is not None:
        return ListSubagentJobsCommand()
    return None


def _parse_get_job(user_input: str, parse_ctx):
    captures = match_template(user_input, "/jobs get {job_id+}")
    if captures is None:
        return None

    try:
        job_id = non_empty_text().parse(captures["job_id"])
    except ParamParseError:
        return None
    return GetSubagentJobCommand(job_id=job_id)


def _parse_wait_job(user_input: str, parse_ctx):
    captures = match_template(user_input, "/jobs wait {job_id+}")
    if captures is None:
        return None

    try:
        job_id = non_empty_text().parse(captures["job_id"])
    except ParamParseError:
        return None
    return WaitSubagentJobCommand(job_id=job_id)


def _build_jobs_view(manager, jobs) -> SubagentJobsViewModel:
    return SubagentJobsViewModel(
        jobs=tuple(
            SubagentJobViewModel(
                job_id=job.id,
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
            )
            for job in jobs
        ),
        runtime_parallel_explore=manager.runtime_parallel_explore,
        max_parallel_explore=manager.max_parallel_explore,
    )


def _handle_list_jobs(command, ctx) -> CommandResult:
    manager = get_subagent_manager(ctx.agent)
    jobs = manager.list_jobs()
    view = _build_jobs_view(manager, jobs)
    ctx.ui_bus.open_view(
        view.view_type,
        title="Sub-agent Jobs",
        view_model=view,
        reuse_key=view.view_type,
    )
    return CommandResult(action="continue", payload=view.to_payload())


def _handle_get_job(command, ctx) -> CommandResult:
    manager = get_subagent_manager(ctx.agent)
    job = manager.get_job(command.job_id)
    if job is None:
        ctx.ui_bus.error(
            f"Sub-agent job '{command.job_id}' not found.", kind=UIEventKind.COMMAND
        )
        return CommandResult(action="continue")

    view = _build_jobs_view(manager, [job])
    ctx.ui_bus.open_view(
        view.view_type,
        title=f"Sub-agent Job {job.id}",
        view_model=view,
        reuse_key=view.view_type,
    )
    return CommandResult(action="continue", payload=view.to_payload())


def _handle_wait_job(command, ctx) -> CommandResult:
    manager = get_subagent_manager(ctx.agent)
    job = manager.wait_job(command.job_id)
    if job is None:
        ctx.ui_bus.error(
            f"Sub-agent job '{command.job_id}' not found.", kind=UIEventKind.COMMAND
        )
        return CommandResult(action="continue")

    if job.status == "completed":
        ctx.ui_bus.success(
            f"Job {job.id} completed.\n{job.result or ''}",
            kind=UIEventKind.COMMAND,
            job_id=job.id,
        )
    elif job.status == "failed":
        ctx.ui_bus.error(
            f"Job {job.id} failed: {job.error or 'unknown error'}",
            kind=UIEventKind.COMMAND,
            job_id=job.id,
        )
    else:
        ctx.ui_bus.warning(
            f"Job {job.id} status: {job.status}",
            kind=UIEventKind.COMMAND,
            job_id=job.id,
        )

    view = _build_jobs_view(manager, manager.list_jobs())
    ctx.ui_bus.refresh_view(
        view.view_type,
        title="Sub-agent Jobs",
        view_model=view,
        reuse_key=view.view_type,
    )
    return CommandResult(action="continue", payload=view.to_payload())


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
                triggers=(slash_trigger("/jobs"),),
                parser=_parse_list_jobs,
                handler=_handle_list_jobs,
            ),
            ActionSpec(
                action_id="subagent.jobs.get",
                feature_id="subagent",
                description="[session] Show sub-agent job details for this session runtime",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/jobs get <id>"),),
                parser=_parse_get_job,
                handler=_handle_get_job,
            ),
            ActionSpec(
                action_id="subagent.jobs.wait",
                feature_id="subagent",
                description="[session] Wait for a sub-agent job started from this session runtime",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(slash_trigger("/jobs wait <id>"),),
                parser=_parse_wait_job,
                handler=_handle_wait_job,
            ),
        ]
    )
