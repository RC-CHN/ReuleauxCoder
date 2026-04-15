"""Sub-agent runtime manager with bounded explore concurrency."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import threading
import time
import uuid

from reuleauxcoder.interfaces.events import UIEventKind
from reuleauxcoder.services.llm.client import LLM


_SUBAGENT_MODES = {"explore", "execute", "verify"}
_SUBAGENT_MAX_ROUNDS = 50
_SUBAGENT_DEFAULT_TIMEOUT_SECONDS = 300
_SUBAGENT_MAX_TIMEOUT_SECONDS = 3_600


def _emit_subagent_ui_event(parent_agent, *, status: str, job_id: str, mode: str, task: str, level: str = "info") -> None:
    ui_bus = getattr(parent_agent, "ui_bus", None) or getattr(getattr(parent_agent, "context", None), "_ui_bus", None)
    if ui_bus is None:
        return

    emit = getattr(ui_bus, level, None) or ui_bus.info
    task_preview = (task or "").strip()
    if len(task_preview) > 240:
        task_preview = task_preview[:237] + "..."
    message = (
        "[SUBAGENT]\n"
        f"status={status}\n"
        f"id={job_id}\n"
        f"mode={mode}"
    )
    if task_preview:
        message += f"\n\n{task_preview}"
    emit(message, kind=UIEventKind.AGENT)


def _clamp_subagent_rounds(value: int | None, default: int = _SUBAGENT_MAX_ROUNDS) -> int:
    base = default if value is None else int(value)
    if base < 1:
        return 1
    if base > _SUBAGENT_MAX_ROUNDS:
        return _SUBAGENT_MAX_ROUNDS
    return base


def _clamp_timeout_seconds(value: int | None, default: int = _SUBAGENT_DEFAULT_TIMEOUT_SECONDS) -> int:
    base = default if value is None else int(value)
    if base < 1:
        return 1
    if base > _SUBAGENT_MAX_TIMEOUT_SECONDS:
        return _SUBAGENT_MAX_TIMEOUT_SECONDS
    return base


@dataclass(slots=True)
class SubagentJob:
    """Tracked background sub-agent job."""

    id: str
    mode: str
    task: str
    status: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    timeout_seconds: int | None = None
    result: str | None = None
    error: str | None = None
    detached_due_to_timeout: bool = False
    injected_to_parent: bool = False


class SubagentManager:
    """Manage background/sync sub-agent runs.

    Explore-mode jobs are capped by a fixed worker pool so the parent agent
    can fan out read-only investigations safely.
    """

    def __init__(self, *, max_parallel_explore: int = 4, default_max_rounds: int = 50):
        self._max_parallel_explore = max(1, int(max_parallel_explore))
        self._default_max_rounds = _clamp_subagent_rounds(default_max_rounds)
        self._runtime_parallel_explore = self._max_parallel_explore
        self._active_explore = 0
        self._explore_pool = ThreadPoolExecutor(max_workers=self._max_parallel_explore)
        self._lock = threading.Lock()
        self._slot_cv = threading.Condition(self._lock)
        self._jobs: dict[str, SubagentJob] = {}
        self._futures: dict[str, Future] = {}

    @property
    def max_parallel_explore(self) -> int:
        return self._max_parallel_explore

    @property
    def runtime_parallel_explore(self) -> int:
        return self._runtime_parallel_explore

    @property
    def default_max_rounds(self) -> int:
        return self._default_max_rounds

    def set_runtime_parallel_explore(self, value: int) -> int:
        with self._lock:
            self._runtime_parallel_explore = max(1, min(self._max_parallel_explore, int(value)))
            self._slot_cv.notify_all()
            return self._runtime_parallel_explore

    @staticmethod
    def is_valid_mode(mode: str) -> bool:
        return mode in _SUBAGENT_MODES

    def submit_background(
        self,
        *,
        parent_agent,
        task: str,
        mode: str,
        max_rounds: int | None = None,
        timeout_seconds: int | None = None,
        parallel_explore: int | None = None,
        model_profile_name: str | None = None,
    ) -> str:
        if mode != "explore":
            raise ValueError("Only 'explore' mode supports background parallel execution")

        if parallel_explore is not None:
            self.set_runtime_parallel_explore(parallel_explore)

        effective_max_rounds = _clamp_subagent_rounds(max_rounds, default=self._default_max_rounds)
        effective_timeout_seconds = _clamp_timeout_seconds(timeout_seconds)
        job_id = f"sj_{uuid.uuid4().hex[:10]}"
        now = time.time()
        job = SubagentJob(
            id=job_id,
            mode=mode,
            task=task,
            status="queued",
            created_at=now,
            timeout_seconds=effective_timeout_seconds,
        )
        _emit_subagent_ui_event(
            parent_agent,
            status="queued",
            job_id=job_id,
            mode=mode,
            task=task,
        )

        def _runner() -> str:
            with self._slot_cv:
                while self._active_explore >= self._runtime_parallel_explore:
                    self._slot_cv.wait(timeout=0.5)
                self._active_explore += 1
                tracked = self._jobs.get(job_id)
                if tracked is not None:
                    tracked.status = "running"
                    tracked.started_at = time.time()
            _emit_subagent_ui_event(
                parent_agent,
                status="running",
                job_id=job_id,
                mode=mode,
                task=task,
            )

            try:
                return run_subagent_task(
                    parent_agent=parent_agent,
                    task=task,
                    mode=mode,
                    max_rounds=effective_max_rounds,
                    timeout_seconds=effective_timeout_seconds,
                    model_profile_name=model_profile_name,
                )
            finally:
                with self._slot_cv:
                    self._active_explore = max(0, self._active_explore - 1)
                    self._slot_cv.notify_all()

        future = self._explore_pool.submit(_runner)

        def _on_done(done: Future) -> None:
            with self._lock:
                tracked = self._jobs.get(job_id)
                if tracked is None:
                    return
                if tracked.detached_due_to_timeout:
                    return
                tracked.finished_at = time.time()
                try:
                    result = done.result()
                    tracked.result = result
                    if "[Sub-agent finished status=timeout]" in result:
                        tracked.detached_due_to_timeout = True
                        tracked.status = "timed_out_detached"
                        tracked.error = (
                            "Sub-agent timed out and detached; background thread may still be running."
                        )
                        emit_status = "timed_out_detached"
                        emit_level = "warning"
                    else:
                        tracked.status = "completed"
                        emit_status = "completed"
                        emit_level = "success"
                except Exception as e:  # pragma: no cover - defensive
                    tracked.error = str(e)
                    tracked.status = "failed"
                    emit_status = "failed"
                    emit_level = "error"

            _emit_subagent_ui_event(
                parent_agent,
                status=emit_status,
                job_id=job_id,
                mode=mode,
                task=task,
                level=emit_level,
            )

        future.add_done_callback(_on_done)

        with self._lock:
            self._jobs[job_id] = job
            self._futures[job_id] = future
        return job_id

    def run_sync(
        self,
        *,
        parent_agent,
        task: str,
        mode: str,
        max_rounds: int | None = None,
        timeout_seconds: int | None = None,
        model_profile_name: str | None = None,
    ) -> str:
        effective_max_rounds = _clamp_subagent_rounds(max_rounds, default=self._default_max_rounds)
        effective_timeout_seconds = _clamp_timeout_seconds(timeout_seconds)
        if mode == "explore":
            future = self._explore_pool.submit(
                run_subagent_task,
                parent_agent=parent_agent,
                task=task,
                mode=mode,
                max_rounds=effective_max_rounds,
                timeout_seconds=effective_timeout_seconds,
                model_profile_name=model_profile_name,
            )
            return future.result()
        return run_subagent_task(
            parent_agent=parent_agent,
            task=task,
            mode=mode,
            max_rounds=effective_max_rounds,
            timeout_seconds=effective_timeout_seconds,
            model_profile_name=model_profile_name,
        )

    def list_jobs(self) -> list[SubagentJob]:
        with self._lock:
            jobs = list(self._jobs.values())
        return sorted(jobs, key=lambda item: item.created_at, reverse=True)

    def get_job(self, job_id: str) -> SubagentJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def wait_job(self, job_id: str, timeout: float | None = None) -> SubagentJob | None:
        with self._lock:
            future = self._futures.get(job_id)
        if future is None:
            return None

        try:
            future.result(timeout=timeout)
        except Exception:
            pass
        return self.get_job(job_id)

    def drain_completed_for_parent(self) -> list[SubagentJob]:
        """Return completed/failed jobs not yet injected into parent context."""
        drained: list[SubagentJob] = []
        with self._lock:
            for job in self._jobs.values():
                if job.injected_to_parent:
                    continue
                if job.status not in {"completed", "failed"}:
                    continue
                job.injected_to_parent = True
                drained.append(
                    SubagentJob(
                        id=job.id,
                        mode=job.mode,
                        task=job.task,
                        status=job.status,
                        created_at=job.created_at,
                        started_at=job.started_at,
                        finished_at=job.finished_at,
                        timeout_seconds=job.timeout_seconds,
                        result=job.result,
                        error=job.error,
                        detached_due_to_timeout=job.detached_due_to_timeout,
                        injected_to_parent=True,
                    )
                )
        return sorted(drained, key=lambda item: item.finished_at or item.created_at)


def get_subagent_manager(agent) -> SubagentManager:
    manager = getattr(agent, "_subagent_manager", None)
    if isinstance(manager, SubagentManager):
        return manager

    default_rounds = getattr(agent, "max_rounds", 50)
    manager = SubagentManager(max_parallel_explore=4, default_max_rounds=default_rounds)
    setattr(agent, "_subagent_manager", manager)
    return manager


def _create_subagent_llm(parent_agent, model_profile_name: str | None):
    config = getattr(parent_agent, "runtime_config", None)
    if config is None:
        return parent_agent.llm, None

    profiles = getattr(config, "model_profiles", {}) or {}

    def _resolve_profile_name(route: str | None) -> str | None:
        normalized = (route or "").strip().lower()
        if normalized == "main":
            return getattr(config, "active_main_model_profile", None) or getattr(
                config, "active_model_profile", None
            ) or getattr(config, "active_sub_model_profile", None)

        return getattr(config, "active_sub_model_profile", None) or getattr(
            config, "active_main_model_profile", None
        ) or getattr(config, "active_model_profile", None)

    profile_name = _resolve_profile_name(model_profile_name)
    if profile_name:
        profile = profiles.get(profile_name)
        if profile is not None:
            return LLM(
                model=profile.model,
                api_key=profile.api_key,
                base_url=profile.base_url,
                temperature=profile.temperature,
                max_tokens=profile.max_tokens,
                preserve_reasoning_content=profile.preserve_reasoning_content,
                backfill_reasoning_content_for_tool_calls=profile.backfill_reasoning_content_for_tool_calls,
            ), profile_name

    return parent_agent.llm, None


def _filter_subagent_tools(parent_agent, mode: str):
    mode_allowlist = {
        "explore": {"read_file", "glob", "grep"},
        "execute": {"read_file", "glob", "grep", "edit_file", "write_file", "bash"},
        "verify": {"read_file", "glob", "grep", "bash"},
    }
    allowed = mode_allowlist[mode]
    return [tool for tool in parent_agent.tools if tool.name in allowed and tool.name != "agent"]


def run_subagent_task(
    *,
    parent_agent,
    task: str,
    mode: str,
    max_rounds: int = 50,
    timeout_seconds: int = _SUBAGENT_DEFAULT_TIMEOUT_SECONDS,
    model_profile_name: str | None = None,
) -> str:
    """Run one sub-agent task with isolated message history."""
    if mode not in _SUBAGENT_MODES:
        raise ValueError(f"Unknown sub-agent mode: {mode}")

    effective_max_rounds = _clamp_subagent_rounds(max_rounds)
    effective_timeout_seconds = _clamp_timeout_seconds(timeout_seconds)

    from reuleauxcoder.domain.agent.agent import Agent
    from reuleauxcoder.extensions.subagent.approval import build_subagent_approval_provider

    sub_llm, effective_model_profile = _create_subagent_llm(parent_agent, model_profile_name)

    sub = Agent(
        llm=sub_llm,
        tools=_filter_subagent_tools(parent_agent, mode),
        max_context_tokens=parent_agent.context.max_tokens,
        max_rounds=effective_max_rounds,
        hook_registry=parent_agent.hook_registry.clone(),
        approval_provider=build_subagent_approval_provider(parent_agent, mode, task),
    )

    holder: dict[str, str] = {}

    def _run() -> None:
        holder["result"] = sub.chat(task)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=effective_timeout_seconds)
    if thread.is_alive():
        sub.request_stop()
        return (
            f"[Sub-agent timeout][mode={mode}]\n"
            f"Sub-agent exceeded timeout after {effective_timeout_seconds}s. "
            "A cooperative stop request was sent.\n"
            "[Sub-agent finished status=timeout]"
        )

    result = holder.get("result", "")
    if len(result) > 5000:
        result = result[:4500] + "\n... (sub-agent output truncated)"

    status = "ok"
    if result.strip() == "(reached maximum tool-call rounds)" or any(
        marker in result for marker in (
            "Maximum tool-call rounds reached.",
            "Max rounds reached.",
            "Reached maximum tool-call rounds",
        )
    ):
        status = "max_rounds"

    return (
        f"[Sub-agent completed][mode={mode}][model={effective_model_profile or getattr(sub_llm, 'model', 'inherited')}]\n{result}\n"
        f"[Sub-agent finished status={status} max_rounds={effective_max_rounds} timeout_s={effective_timeout_seconds}]"
    )
