"""Sub-agent runtime manager with bounded explore concurrency."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import threading
import time
import uuid

from reuleauxcoder.services.llm.factory import build_llm_from_settings


_VALID_SUBAGENT_MODES = frozenset({"explore", "execute", "verify"})
_DEFAULT_MAX_ROUNDS = 50
_DEFAULT_TIMEOUT_SECONDS = 300
_MAX_TIMEOUT_SECONDS = 3_600


def _clamp_subagent_rounds(
    value: int | None, default: int = _DEFAULT_MAX_ROUNDS
) -> int:
    base = default if value is None else int(value)
    if base < 1:
        return 1
    if base > _DEFAULT_MAX_ROUNDS:
        return _DEFAULT_MAX_ROUNDS
    return base


def _clamp_timeout_seconds(
    value: int | None,
    default: int = _DEFAULT_TIMEOUT_SECONDS,
    maximum: int = _MAX_TIMEOUT_SECONDS,
) -> int:
    base = default if value is None else int(value)
    maximum = max(1, int(maximum))
    if base < 1:
        return 1
    if base > maximum:
        return maximum
    return base


@dataclass(slots=True)
class SubagentJob:
    """Tracked background sub-agent job."""

    id: str
    mode: str
    task: str
    status: str
    created_at: float
    parent_agent_id: str | None = None
    parent_session_id: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    timeout_seconds: int | None = None
    result: str | None = None
    error: str | None = None
    detached_due_to_timeout: bool = False
    injected_to_parent: bool = False
    generation: int = 0
    cancel_requested: bool = False


class SubagentManager:
    """Manage background/sync sub-agent runs.

    Explore-mode jobs are capped by a fixed worker pool so the parent agent
    can fan out read-only investigations safely.
    """

    def __init__(
        self,
        *,
        max_parallel_explore: int = 4,
        default_max_rounds: int = _DEFAULT_MAX_ROUNDS,
        default_timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        max_timeout_seconds: int = _MAX_TIMEOUT_SECONDS,
        parent_agent_id: str | None = None,
        initial_generation: int = 0,
    ):
        self._max_parallel_explore = max(1, int(max_parallel_explore))
        self._default_max_rounds = _clamp_subagent_rounds(default_max_rounds)
        self._max_timeout_seconds = max(1, int(max_timeout_seconds))
        self._default_timeout_seconds = _clamp_timeout_seconds(
            default_timeout_seconds,
            maximum=self._max_timeout_seconds,
        )
        self._runtime_parallel_explore = self._max_parallel_explore
        self._active_explore = 0
        self._explore_pool = ThreadPoolExecutor(max_workers=self._max_parallel_explore)
        self._lock = threading.Lock()
        self._slot_cv = threading.Condition(self._lock)
        self._jobs: dict[str, SubagentJob] = {}
        self._futures: dict[str, Future] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._parent_agent_id = parent_agent_id
        self._generation = max(0, int(initial_generation))
        self._shutdown = False

    @property
    def max_parallel_explore(self) -> int:
        return self._max_parallel_explore

    @property
    def runtime_parallel_explore(self) -> int:
        return self._runtime_parallel_explore

    @property
    def default_max_rounds(self) -> int:
        return self._default_max_rounds

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def set_runtime_parallel_explore(self, value: int) -> int:
        with self._lock:
            self._runtime_parallel_explore = max(
                1, min(self._max_parallel_explore, int(value))
            )
            self._slot_cv.notify_all()
            return self._runtime_parallel_explore

    @staticmethod
    def is_valid_mode(mode: str) -> bool:
        return mode in _VALID_SUBAGENT_MODES

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
            raise ValueError(
                "Only 'explore' mode supports background parallel execution"
            )

        parent_agent_id = getattr(parent_agent, "agent_id", None)
        if not parent_agent_id:
            raise ValueError("parent agent must expose a stable agent_id")
        parent_generation = getattr(parent_agent, "session_generation", None)
        if parent_generation is None:
            raise ValueError("parent agent must expose session_generation")

        with self._lock:
            if self._shutdown:
                raise RuntimeError("SubagentManager is shut down")
            if self._parent_agent_id is None:
                self._parent_agent_id = parent_agent_id
                self._generation = parent_generation
            if self._parent_agent_id != parent_agent_id:
                raise ValueError("SubagentManager cannot be shared across parent agents")
            if self._generation != parent_generation:
                raise ValueError(
                    "SubagentManager generation does not match the parent session"
                )

        if parallel_explore is not None:
            self.set_runtime_parallel_explore(parallel_explore)

        effective_max_rounds = _clamp_subagent_rounds(
            max_rounds, default=self._default_max_rounds
        )
        effective_timeout_seconds = _clamp_timeout_seconds(
            timeout_seconds,
            default=self._default_timeout_seconds,
            maximum=self._max_timeout_seconds,
        )
        job_id = f"sj_{uuid.uuid4().hex[:10]}"
        now = time.time()
        job = SubagentJob(
            id=job_id,
            mode=mode,
            task=task,
            status="queued",
            created_at=now,
            parent_agent_id=parent_agent_id,
            parent_session_id=getattr(parent_agent, "current_session_id", None),
            timeout_seconds=effective_timeout_seconds,
            generation=parent_generation,
        )
        cancel_event = threading.Event()
        def _runner() -> str:
            with self._slot_cv:
                while self._active_explore >= self._runtime_parallel_explore:
                    if cancel_event.is_set() or self._shutdown:
                        return "[Sub-agent finished status=cancelled]"
                    self._slot_cv.wait(timeout=0.5)
                if cancel_event.is_set() or self._shutdown:
                    return "[Sub-agent finished status=cancelled]"
                self._active_explore += 1
                tracked = self._jobs.get(job_id)
                if tracked is not None:
                    tracked.status = "running"
                    tracked.started_at = time.time()
            try:
                return run_subagent_task(
                    parent_agent=parent_agent,
                    task=task,
                    mode=mode,
                    max_rounds=effective_max_rounds,
                    timeout_seconds=effective_timeout_seconds,
                    model_profile_name=model_profile_name,
                    cancel_event=cancel_event,
                )
            finally:
                with self._slot_cv:
                    self._active_explore = max(0, self._active_explore - 1)
                    self._slot_cv.notify_all()

        # Register the job before submission. A very fast Future may invoke its
        # callback immediately; it must always find the tracked job.
        with self._lock:
            self._jobs[job_id] = job
            self._cancel_events[job_id] = cancel_event
            future = self._explore_pool.submit(_runner)
            self._futures[job_id] = future

        def _on_done(done: Future) -> None:
            tracked_for_injection = None
            with self._lock:
                tracked = self._jobs.get(job_id)
                if tracked is None:
                    return
                if tracked.detached_due_to_timeout:
                    return
                tracked.finished_at = time.time()
                if done.cancelled():
                    tracked.status = "cancelled"
                    tracked.error = "Sub-agent cancelled before it started."
                else:
                    try:
                        result = done.result()
                    except Exception as e:  # pragma: no cover - defensive
                        tracked.error = str(e)
                        tracked.status = "failed"
                        tracked_for_injection = tracked
                    else:
                        if "[Sub-agent finished status=cancelled]" in result:
                            tracked.detached_due_to_timeout = "detached" in result
                            tracked.status = (
                                "cancelled_detached"
                                if tracked.detached_due_to_timeout
                                else "cancelled"
                            )
                            tracked.error = "Sub-agent cancelled."
                        elif "[Sub-agent finished status=timeout]" in result:
                            tracked.detached_due_to_timeout = True
                            tracked.status = "timed_out_detached"
                            tracked.error = "Sub-agent timed out and detached; background thread may still be running."
                        elif tracked.generation != self._generation:
                            tracked.status = "stale"
                            tracked.error = "Sub-agent completed for an inactive session generation."
                        else:
                            tracked.result = result
                            tracked.status = "completed"
                            tracked_for_injection = tracked

            if tracked_for_injection is not None:
                inject = getattr(parent_agent, "inject_subagent_job_result", None)
                if callable(inject):
                    try:
                        inject(tracked_for_injection)
                    except Exception:
                        pass

        future.add_done_callback(_on_done)
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
        effective_max_rounds = _clamp_subagent_rounds(
            max_rounds, default=self._default_max_rounds
        )
        effective_timeout_seconds = _clamp_timeout_seconds(
            timeout_seconds,
            default=self._default_timeout_seconds,
            maximum=self._max_timeout_seconds,
        )
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

    def cancel_job(self, job_id: str) -> bool:
        """Request cancellation and prevent later parent injection."""
        with self._slot_cv:
            job = self._jobs.get(job_id)
            event = self._cancel_events.get(job_id)
            future = self._futures.get(job_id)
            if job is None or event is None or job.status in {
                "completed",
                "failed",
                "cancelled",
                "cancelled_detached",
                "timed_out_detached",
                "stale",
            }:
                return False
            job.cancel_requested = True
            event.set()
            job.status = "cancelling"
            self._slot_cv.notify_all()
        # Future.cancel() may invoke callbacks synchronously. Never call it
        # while holding the manager lock because callbacks acquire that lock.
        cancelled_before_start = future is not None and future.cancel()
        if cancelled_before_start:
            with self._lock:
                job.status = "cancelled"
                job.finished_at = job.finished_at or time.time()
        return True

    def advance_generation(
        self,
        *,
        generation: int | None = None,
        cancel_pending: bool = True,
    ) -> int:
        """Start a new parent session generation.

        Results from older generations remain inspectable but can never be
        injected into the new parent conversation.
        """
        with self._slot_cv:
            next_generation = (
                self._generation + 1 if generation is None else int(generation)
            )
            if next_generation <= self._generation:
                raise ValueError("session generation must increase monotonically")
            self._generation = next_generation
            old_ids = [
                job.id
                for job in self._jobs.values()
                if job.generation < self._generation
                and job.status in {"queued", "running", "cancelling"}
            ]
        if cancel_pending:
            for job_id in old_ids:
                self.cancel_job(job_id)
        return self._generation

    def prune(self, *, keep: int = 100) -> int:
        """Remove oldest terminal jobs while retaining recent diagnostics."""
        terminal = {
            "completed",
            "failed",
            "cancelled",
            "cancelled_detached",
            "timed_out_detached",
            "stale",
        }
        with self._lock:
            finished = sorted(
                (job for job in self._jobs.values() if job.status in terminal),
                key=lambda job: job.finished_at or job.created_at,
                reverse=True,
            )
            remove_ids = {job.id for job in finished[max(0, keep) :]}
            for job_id in remove_ids:
                self._jobs.pop(job_id, None)
                self._futures.pop(job_id, None)
                self._cancel_events.pop(job_id, None)
            return len(remove_ids)

    def shutdown(self, *, wait: bool = True) -> None:
        """Cancel all jobs and release the worker pool exactly once."""
        with self._slot_cv:
            if self._shutdown:
                return
            self._shutdown = True
            active_ids = list(self._cancel_events)
            self._slot_cv.notify_all()
        for job_id in active_ids:
            self.cancel_job(job_id)
        self._explore_pool.shutdown(wait=wait, cancel_futures=True)

    def drain_completed_for_parent(
        self, *, parent_state_lock: threading.Lock | None = None
    ) -> list[SubagentJob]:
        """Return completed/failed jobs not yet injected into parent context.

        When *parent_state_lock* is provided it is acquired (while ``self._lock``
        is already held) before reading ``injected_to_parent`` so that the read
        happens-after the write performed by ``Agent.inject_subagent_job_result``
        under the same lock.  This prevents a TOCTOU race where the background
        done-callback injects a job between drain's fast-path check and the
        follow-up ``inject_subagent_job_result`` call on the drained copy.
        """
        drained: list[SubagentJob] = []
        with self._lock:
            for job in self._jobs.values():
                if job.injected_to_parent:
                    continue
                if job.status not in {"completed", "failed"}:
                    continue
                if parent_state_lock is not None:
                    parent_state_lock.acquire()
                try:
                    # Re-check under the parent lock – the done-callback may have
                    # injected this job after our first check above.
                    if job.injected_to_parent:
                        continue
                    job.injected_to_parent = True
                    drained.append(
                        SubagentJob(
                            id=job.id,
                            mode=job.mode,
                            task=job.task,
                            status=job.status,
                            created_at=job.created_at,
                            parent_agent_id=job.parent_agent_id,
                            parent_session_id=job.parent_session_id,
                            started_at=job.started_at,
                            finished_at=job.finished_at,
                            timeout_seconds=job.timeout_seconds,
                            result=job.result,
                            error=job.error,
                            detached_due_to_timeout=job.detached_due_to_timeout,
                            injected_to_parent=False,
                            generation=job.generation,
                            cancel_requested=job.cancel_requested,
                        )
                    )
                finally:
                    if parent_state_lock is not None:
                        parent_state_lock.release()
        return sorted(drained, key=lambda item: item.finished_at or item.created_at)


def get_subagent_manager(agent) -> SubagentManager:
    manager = getattr(agent, "_subagent_manager", None)
    if isinstance(manager, SubagentManager):
        return manager

    default_rounds = getattr(agent, "max_rounds", 50)
    manager = SubagentManager(
        max_parallel_explore=4,
        default_max_rounds=default_rounds,
        parent_agent_id=getattr(agent, "agent_id", None),
        initial_generation=getattr(agent, "session_generation", 0),
    )
    agent._subagent_manager = manager
    return manager


def _create_subagent_llm(parent_agent, model_profile_name: str | None):
    config = getattr(parent_agent, "runtime_config", None)
    if config is None:
        return parent_agent.llm, None

    profiles = getattr(config, "model_profiles", {}) or {}

    def _resolve_profile_name(route: str | None) -> str | None:
        normalized = (route or "").strip().lower()
        if normalized == "main":
            return (
                getattr(config, "active_main_model_profile", None)
                or getattr(config, "active_model_profile", None)
                or getattr(config, "active_sub_model_profile", None)
            )

        return (
            getattr(config, "active_sub_model_profile", None)
            or getattr(config, "active_main_model_profile", None)
            or getattr(config, "active_model_profile", None)
        )

    profile_name = _resolve_profile_name(model_profile_name)
    if profile_name:
        profile = profiles.get(profile_name)
        if profile is not None:
            return build_llm_from_settings(
                profile,
                debug_trace=getattr(parent_agent.llm, "debug_trace", False),
            ), profile_name

    return parent_agent.llm, None


def _filter_subagent_tools(parent_agent, mode: str):
    mode_allowlist = {
        "explore": {"read_file", "glob", "grep"},
        "execute": {"read_file", "glob", "grep", "edit_file", "write_file", "shell"},
        "verify": {"read_file", "glob", "grep", "shell"},
    }
    allowed = mode_allowlist[mode]
    return [
        _clone_tool_for_subagent(tool)
        for tool in parent_agent.tools
        if tool.name in allowed and tool.name != "agent"
    ]


def _clone_tool_for_subagent(tool):
    """Materialize one Tool scope without shallow-copying runtime services."""
    clone = getattr(tool, "clone_for_scope", None)
    if not callable(clone):
        raise TypeError(f"Tool '{tool.name}' does not support scoped materialization")
    return clone("subagent")


def run_subagent_task(
    *,
    parent_agent,
    task: str,
    mode: str,
    max_rounds: int = 50,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    model_profile_name: str | None = None,
    cancel_event: threading.Event | None = None,
) -> str:
    """Run one sub-agent task with isolated message history."""
    if mode not in _VALID_SUBAGENT_MODES:
        raise ValueError(f"Unknown sub-agent mode: {mode}")

    effective_max_rounds = _clamp_subagent_rounds(max_rounds)
    effective_timeout_seconds = _clamp_timeout_seconds(timeout_seconds)

    from reuleauxcoder.domain.agent.agent import Agent
    from reuleauxcoder.extensions.subagent.approval import (
        build_subagent_approval_provider,
    )

    sub_llm, effective_model_profile = _create_subagent_llm(
        parent_agent, model_profile_name
    )

    sub = Agent(
        llm=sub_llm,
        tools=_filter_subagent_tools(parent_agent, mode),
        max_context_tokens=parent_agent.context.max_tokens,
        max_rounds=effective_max_rounds,
        hook_registry=parent_agent.hook_registry.clone(scope="subagent"),
        approval_provider=build_subagent_approval_provider(parent_agent, mode, task),
    )

    holder: dict[str, object] = {}

    def _run() -> None:
        try:
            holder["result"] = sub.chat(task)
        except BaseException as error:  # propagate worker failures to the manager
            holder["error"] = error

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    deadline = time.monotonic() + effective_timeout_seconds
    cancelled = False
    while thread.is_alive() and time.monotonic() < deadline:
        thread.join(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            sub.request_stop()
            thread.join(timeout=1.0)
            break

    if cancelled:
        suffix = " detached" if thread.is_alive() else ""
        return f"[Sub-agent cancelled{suffix}]\n[Sub-agent finished status=cancelled]"
    if thread.is_alive():
        sub.request_stop()
        return (
            f"[Sub-agent timeout][mode={mode}]\n"
            f"Sub-agent exceeded timeout after {effective_timeout_seconds}s. "
            "A cooperative stop request was sent.\n"
            "[Sub-agent finished status=timeout]"
        )

    if "error" in holder:
        raise holder["error"]  # type: ignore[misc]
    result = str(holder.get("result", ""))
    if len(result) > 5000:
        result = result[:4500] + "\n... (sub-agent output truncated)"

    status = "ok"
    if result.strip() == "(reached maximum tool-call rounds)" or any(
        marker in result
        for marker in (
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
