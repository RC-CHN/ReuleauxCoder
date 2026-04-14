"""Sub-agent spawning tool."""

from __future__ import annotations

from reuleauxcoder.extensions.subagent.manager import get_subagent_manager
from reuleauxcoder.extensions.tools.base import Tool


class AgentTool(Tool):
    name = "agent"
    description = (
        "Spawn one or more sub-agents to handle complex sub-tasks independently. "
        "Each sub-agent has isolated context and tool access. "
        "Provide either 'task' or 'tasks', but not both. "
        "Batch 'tasks' requires mode='explore' and run_in_background=true. "
        "Optionally set 'model' to 'sub' or 'main'. "
        "'sub' uses the configured default sub-agent model; 'main' uses the configured main-agent model. "
        "If omitted or invalid, 'sub' is used. "
        "parallel_explore sets the runtime explore parallelism cap (1-4)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": (
                    "Single sub-agent task. Use this for one job only. "
                    "Mutually exclusive with 'tasks' - do not provide both."
                ),
            },
            "tasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Batch tasks for parallel/background explore jobs. "
                    "Mutually exclusive with 'task' - do not provide both. "
                    "Requires mode='explore' and run_in_background=true."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["explore", "execute", "verify"],
                "description": "Sub-agent mode (default: explore)",
            },
            "run_in_background": {
                "type": "boolean",
                "description": "Run in background; only supported for explore mode",
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
        },
        "required": [],
    }

    _parent_agent = None

    def preflight_validate(self, **kwargs) -> str | None:
        task = kwargs.get("task")
        tasks = kwargs.get("tasks")
        mode = kwargs.get("mode", "explore")
        run_in_background = kwargs.get("run_in_background", False)

        single_task = (task or "").strip() if isinstance(task, str) else ""
        batch_tasks = [item.strip() for item in (tasks or []) if isinstance(item, str) and item.strip()]

        if not single_task and not batch_tasks:
            return "Error: provide either 'task' or non-empty 'tasks'."

        if single_task and batch_tasks:
            return "Error: use either 'task' or 'tasks', not both."

        if batch_tasks and (mode != "explore" or not run_in_background):
            return (
                "Error: batch 'tasks' currently requires mode='explore' "
                "and run_in_background=true."
            )

        return None

    def execute(
        self,
        task: str | None = None,
        tasks: list[str] | None = None,
        mode: str = "explore",
        run_in_background: bool = False,
        max_rounds: int = 50,
        timeout_seconds: int = 300,
        parallel_explore: int | None = None,
        model: str | None = None,
    ) -> str:
        if self._parent_agent is None:
            return "Error: agent tool not initialized (no parent agent)"

        parent = self._parent_agent
        manager = get_subagent_manager(parent)
        effective_max_rounds = max(1, int(max_rounds or manager.default_max_rounds))
        effective_timeout_seconds = max(1, int(timeout_seconds or 300))

        if parallel_explore is not None:
            manager.set_runtime_parallel_explore(parallel_explore)

        if not manager.is_valid_mode(mode):
            return f"Error: unknown sub-agent mode '{mode}'. Use explore|execute|verify."

        if not _is_subagent_mode_allowed(parent, mode):
            current_mode = getattr(parent, "active_mode", None) or "default"
            return (
                f"Sub-agent mode '{mode}' is not allowed in current mode '{current_mode}'."
            )

        single_task = (task or "").strip() if isinstance(task, str) else ""
        batch_tasks = [item.strip() for item in (tasks or []) if isinstance(item, str) and item.strip()]

        validation_error = self.preflight_validate(
            task=task,
            tasks=tasks,
            mode=mode,
            run_in_background=run_in_background,
        )
        if validation_error:
            return validation_error

        if batch_tasks:
            if mode != "explore" or not run_in_background:
                return (
                    "Error: batch 'tasks' currently requires mode='explore' "
                    "and run_in_background=true."
                )

            try:
                job_ids: list[str] = []
                for item in batch_tasks:
                    job_id = manager.submit_background(
                        parent_agent=parent,
                        task=item,
                        mode=mode,
                        max_rounds=effective_max_rounds,
                        timeout_seconds=effective_timeout_seconds,
                        parallel_explore=parallel_explore,
                        model_profile_name=model,
                    )
                    job_ids.append(job_id)
                return (
                    f"[Sub-agent batch queued] count={len(job_ids)} mode={mode} "
                    f"max_rounds={effective_max_rounds} timeout_s={effective_timeout_seconds} "
                    f"parallel_explore={manager.runtime_parallel_explore} "
                    f"model={model or 'sub-default'} "
                    f"job_ids={','.join(job_ids)}"
                )
            except Exception as e:
                return f"Sub-agent batch error: {e}"

        try:
            if run_in_background:
                job_id = manager.submit_background(
                    parent_agent=parent,
                    task=single_task,
                    mode=mode,
                    max_rounds=effective_max_rounds,
                    timeout_seconds=effective_timeout_seconds,
                    parallel_explore=parallel_explore,
                    model_profile_name=model,
                )
                return (
                    f"[Sub-agent queued] mode={mode} max_rounds={effective_max_rounds} "
                    f"timeout_s={effective_timeout_seconds} "
                    f"parallel_explore={manager.runtime_parallel_explore} "
                    f"model={model or 'sub-default'} "
                    f"job_id={job_id}"
                )

            return manager.run_sync(
                parent_agent=parent,
                task=single_task,
                mode=mode,
                max_rounds=effective_max_rounds,
                timeout_seconds=effective_timeout_seconds,
                model_profile_name=model,
            )
        except Exception as e:
            return f"Sub-agent error: {e}"


def _is_subagent_mode_allowed(parent_agent, sub_mode: str) -> bool:
    active_mode = parent_agent.get_active_mode_config()
    if active_mode is None:
        return True

    allowed = list(getattr(active_mode, "allowed_subagent_modes", []) or [])
    if not allowed:
        return True

    # Backward compatibility: early configs used main-agent mode names here.
    legacy_tokens = {"coder", "planner", "debugger"}
    if any(token in legacy_tokens for token in allowed):
        return True

    return sub_mode in set(allowed)
