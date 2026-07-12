"""Sub-agent spawning tool."""

from __future__ import annotations

from reuleauxcoder.extensions.subagent.manager import get_subagent_manager
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool


@register_tool
class AgentTool(Tool):
    name = "agent"
    description = (
        "Spawn one or more sub-agents to handle complex sub-tasks independently. "
        "Each sub-agent has isolated context and tool access. "
        "Pass a list of task strings via the 'tasks' parameter: "
        "a single-element list for one sub-agent, multiple elements for batch parallel jobs. "
        "Single tasks support any mode (sync or background). "
        "Batch tasks (multiple elements) always run as explore-mode background jobs. "
        "Optionally set 'model' to 'sub' or 'main' to choose the sub-agent model profile "
        "(defaults to 'sub' if omitted or invalid). "
        "parallel_explore sets the runtime explore parallelism cap (1-4)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of sub-agent task descriptions. "
                    "Pass a single-item list for one sub-agent, "
                    "or multiple items for batch parallel explore jobs."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["explore", "execute", "verify"],
                "description": "Sub-agent mode (default: explore)",
            },
            "run_in_background": {
                "type": "boolean",
                "description": "Run in background and receive a completion notification.",
            },
            "max_rounds": {
                "type": "integer",
                "description": "Maximum sub-agent rounds (default: 50)",
                "minimum": 1,
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Sub-agent timeout in seconds (default: 300)",
                "minimum": 1,
            },
            "max_tool_calls": {
                "type": "integer",
                "description": "Maximum tool calls across the sub-agent invocation (default: 80).",
                "minimum": 1,
            },
            "max_tokens": {
                "type": "integer",
                "description": "Optional total prompt plus completion token budget.",
                "minimum": 1,
            },
            "parallel_explore": {
                "type": "integer",
                "description": "Runtime explore parallelism cap for this parent (1-4)",
                "minimum": 1,
                "maximum": 4,
            },
            "model": {
                "type": "string",
                "enum": ["sub", "main"],
                "description": (
                    "Optional model route for the sub-agent. "
                    "'sub' uses the configured default sub-agent model; "
                    "'main' uses the configured main-agent model. "
                    "If omitted, defaults to 'sub'."
                ),
            },
            "context": {
                "type": "string",
                "enum": ["minimal", "recent", "full"],
                "description": "Parent context projection (default: recent).",
            },
            "resume_job_id": {
                "type": "string",
                "description": "Resume a completed background agent with the single task as follow-up.",
            },
            "isolation": {
                "type": "string",
                "enum": ["worktree"],
                "description": "Run an execute agent in a detached git worktree.",
            },
            "action": {
                "type": "string",
                "enum": ["spawn", "message", "cancel", "status"],
                "description": "Control-plane action (default: spawn).",
            },
            "target_job_id": {
                "type": "string",
                "description": "Target job for message, cancel, or status actions.",
            },
        },
        "required": [],
    }

    _parent_agent = None

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())

    def preflight_validate(self, **kwargs) -> str | None:
        tasks = kwargs.get("tasks")
        mode = kwargs.get("mode", "explore")
        run_in_background = kwargs.get("run_in_background", False)
        action = kwargs.get("action", "spawn")

        task_list = [
            item.strip()
            for item in (tasks or [])
            if isinstance(item, str) and item.strip()
        ]

        if action in {"status", "cancel"}:
            if not kwargs.get("target_job_id"):
                return f"Error: action='{action}' requires target_job_id."
            return None
        if action == "message":
            if not kwargs.get("target_job_id") or len(task_list) != 1:
                return "Error: action='message' requires target_job_id and one tasks entry."
            return None
        if action != "spawn":
            return f"Error: unsupported sub-agent action '{action}'."
        if not task_list:
            return "Error: 'tasks' must be a non-empty list of task strings."

        if len(task_list) > 1 and (mode != "explore" or not run_in_background):
            return (
                "Error: batch tasks (multiple items) require "
                "mode='explore' and run_in_background=true."
            )
        isolation = kwargs.get("isolation")
        if isolation == "worktree" and (mode != "execute" or not run_in_background):
            return (
                "Error: isolation='worktree' requires mode='execute' and "
                "run_in_background=true."
            )

        if (
            not get_subagent_manager(self._parent_agent).is_valid_mode(mode)
            if self._parent_agent is not None
            else False
        ):
            return f"Error: unsupported sub-agent mode '{mode}'."

        return None

    def execute(
        self,
        tasks: list[str] | None = None,
        mode: str = "explore",
        run_in_background: bool = False,
        max_rounds: int = 50,
        timeout_seconds: int = 300,
        parallel_explore: int | None = None,
        model: str | None = None,
        context: str = "recent",
        resume_job_id: str | None = None,
        isolation: str | None = None,
        max_tool_calls: int = 80,
        max_tokens: int | None = None,
        action: str = "spawn",
        target_job_id: str | None = None,
    ) -> str:
        if self._parent_agent is None:
            return "Error: agent tool not initialized (no parent agent)"

        return self.run_backend(
            tasks=tasks,
            mode=mode,
            run_in_background=run_in_background,
            max_rounds=max_rounds,
            timeout_seconds=timeout_seconds,
            parallel_explore=parallel_explore,
            model=model,
            context=context,
            resume_job_id=resume_job_id,
            isolation=isolation,
            max_tool_calls=max_tool_calls,
            max_tokens=max_tokens,
            action=action,
            target_job_id=target_job_id,
        )

    @backend_handler("local")
    def _execute_local(
        self,
        tasks: list[str] | None = None,
        mode: str = "explore",
        run_in_background: bool = False,
        max_rounds: int = 50,
        timeout_seconds: int = 300,
        parallel_explore: int | None = None,
        model: str | None = None,
        context: str = "recent",
        resume_job_id: str | None = None,
        isolation: str | None = None,
        max_tool_calls: int = 80,
        max_tokens: int | None = None,
        action: str = "spawn",
        target_job_id: str | None = None,
    ) -> str:
        parent = self._parent_agent
        if parent is None:
            return "Error: agent tool not initialized (no parent agent)"

        manager = get_subagent_manager(parent)
        effective_max_rounds = max(1, int(max_rounds or manager.default_max_rounds))
        effective_timeout = max(1, int(timeout_seconds or 300))
        model_route = (model or "sub").strip().lower()
        if model_route not in {"sub", "main"}:
            model_route = "sub"

        task_list = [
            item.strip()
            for item in (tasks or [])
            if isinstance(item, str) and item.strip()
        ]
        if action == "status":
            job = manager.get_job(target_job_id or "")
            if job is None:
                return "Error: unknown sub-agent job."
            return (
                f"Sub-agent {job.id}: status={job.status}, mode={job.mode}, "
                f"depth={job.depth}, task={job.task}"
            )
        if action == "cancel":
            return (
                f"Sub-agent cancellation requested: {target_job_id}"
                if manager.cancel_job(target_job_id or "")
                else "Error: sub-agent job is not cancellable."
            )
        if action == "message":
            if len(task_list) != 1:
                return "Error: message action requires one tasks entry as the message."
            return (
                f"Message queued for sub-agent: {target_job_id}"
                if manager.send_message(target_job_id or "", task_list[0])
                else "Error: target sub-agent is not running."
            )
        if action != "spawn":
            return f"Error: unsupported sub-agent action '{action}'."
        if not task_list:
            return "Error: 'tasks' must be a non-empty list of task strings."
        if context not in {"minimal", "recent", "full"}:
            return f"Error: unsupported context projection '{context}'."
        if resume_job_id:
            if len(task_list) != 1:
                return "Error: resume_job_id requires exactly one follow-up task."
            try:
                resumed = manager.follow_up(
                    parent_agent=parent,
                    job_id=resume_job_id,
                    message=task_list[0],
                    timeout_seconds=effective_timeout,
                )
            except ValueError as error:
                return f"Error: {error}"
            return f"Sub-agent follow-up started in background: {resumed}"

        child_depth = int(getattr(parent, "subagent_depth", 0)) + 1
        use_worktree = isolation == "worktree"

        # Single task: run in the requested mode (sync or background)
        if len(task_list) == 1:
            single_task = task_list[0]
            if run_in_background:
                job_id = manager.submit_background(
                    parent_agent=parent,
                    task=single_task,
                    mode=mode,
                    max_rounds=effective_max_rounds,
                    timeout_seconds=effective_timeout,
                    parallel_explore=parallel_explore,
                    model_profile_name=model_route,
                    context_mode=context,
                    depth=child_depth,
                    worktree=use_worktree,
                    max_tool_calls=max_tool_calls,
                    max_tokens=max_tokens,
                )
                return f"Sub-agent job started in background: {job_id}"

            result = manager.run_sync(
                parent_agent=parent,
                task=single_task,
                mode=mode,
                max_rounds=effective_max_rounds,
                timeout_seconds=effective_timeout,
                model_profile_name=model_route,
                context_mode=context,
                depth=child_depth,
                worktree=use_worktree,
                max_tool_calls=max_tool_calls,
                max_tokens=max_tokens,
            )
            return result.model_text() if hasattr(result, "model_text") else str(result)

        # Multiple tasks: always run as explore-mode background jobs
        job_ids: list[str] = []
        for item in task_list:
            job_id = manager.submit_background(
                parent_agent=parent,
                task=item,
                mode="explore",
                max_rounds=effective_max_rounds,
                timeout_seconds=effective_timeout,
                parallel_explore=parallel_explore,
                model_profile_name=model_route,
                context_mode=context,
                depth=child_depth,
                max_tool_calls=max_tool_calls,
                max_tokens=max_tokens,
            )
            job_ids.append(job_id)
        return f"Started {len(job_ids)} background sub-agent jobs: {', '.join(job_ids)}"
