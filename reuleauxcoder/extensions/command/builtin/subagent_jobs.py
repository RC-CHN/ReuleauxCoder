"""Builtin sub-agent job management commands."""

from __future__ import annotations

from dataclasses import dataclass
import time

from reuleauxcoder.app.commands.matchers import match_template
from reuleauxcoder.app.commands.models import CommandResult
from reuleauxcoder.app.commands.module_registry import register_command_module
from reuleauxcoder.app.commands.params import ParamParseError
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.shared import TEXT_REQUIRED, UI_TARGETS, non_empty_text, slash_trigger
from reuleauxcoder.app.commands.specs import ActionSpec
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


def _format_age(ts: float | None) -> str:
    if ts is None:
        return "-"
    seconds = max(0, int(time.time() - ts))
    return f"{seconds}s ago"


def _handle_list_jobs(command, ctx) -> CommandResult:
    manager = get_subagent_manager(ctx.agent)
    jobs = manager.list_jobs()

    if not jobs:
        ctx.ui_bus.info("No sub-agent jobs yet.", kind=UIEventKind.COMMAND)
        return CommandResult(action="continue", payload={"jobs": []})

    ctx.ui_bus.info(
        (
            f"Sub-agent jobs ({len(jobs)} total, "
            f"parallel explore={manager.runtime_parallel_explore}/{manager.max_parallel_explore}):"
        ),
        kind=UIEventKind.COMMAND,
    )
    for job in jobs[:20]:
        ctx.ui_bus.info(
            (
                f"- {job.id} [{job.status}] mode={job.mode}, timeout={job.timeout_seconds or '-'}s, "
                f"created={_format_age(job.created_at)}"
            ),
            kind=UIEventKind.COMMAND,
            job_id=job.id,
            status=job.status,
            mode=job.mode,
        )

    return CommandResult(
        action="continue",
        payload={
            "jobs": [
                {
                    "id": job.id,
                    "status": job.status,
                    "mode": job.mode,
                    "task": job.task,
                    "created_at": job.created_at,
                    "started_at": job.started_at,
                    "finished_at": job.finished_at,
                    "timeout_seconds": job.timeout_seconds,
                    "error": job.error,
                }
                for job in jobs
            ]
        },
    )


def _handle_get_job(command, ctx) -> CommandResult:
    manager = get_subagent_manager(ctx.agent)
    job = manager.get_job(command.job_id)
    if job is None:
        ctx.ui_bus.error(f"Sub-agent job '{command.job_id}' not found.", kind=UIEventKind.COMMAND)
        return CommandResult(action="continue")

    ctx.ui_bus.info(
        (
            f"Job {job.id}: status={job.status}, mode={job.mode}, "
            f"timeout={job.timeout_seconds or '-'}s, task={job.task}"
        ),
        kind=UIEventKind.COMMAND,
        job_id=job.id,
        status=job.status,
        mode=job.mode,
    )
    if job.error:
        ctx.ui_bus.error(f"Job {job.id} error: {job.error}", kind=UIEventKind.COMMAND, job_id=job.id)
    if job.result:
        ctx.ui_bus.success(
            f"Job {job.id} result:\n{job.result}",
            kind=UIEventKind.COMMAND,
            job_id=job.id,
        )

    return CommandResult(
        action="continue",
        payload={
            "job": {
                "id": job.id,
                "status": job.status,
                "mode": job.mode,
                "task": job.task,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "timeout_seconds": job.timeout_seconds,
                "result": job.result,
                "error": job.error,
            }
        },
    )


def _handle_wait_job(command, ctx) -> CommandResult:
    manager = get_subagent_manager(ctx.agent)
    job = manager.wait_job(command.job_id)
    if job is None:
        ctx.ui_bus.error(f"Sub-agent job '{command.job_id}' not found.", kind=UIEventKind.COMMAND)
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

    return CommandResult(action="continue", payload={"job_id": job.id, "status": job.status})


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
