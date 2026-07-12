"""Sub-agent runtime manager with bounded explore concurrency."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from collections import deque
from pathlib import Path
import hashlib
import json
import threading
import time
import uuid
from typing import Literal

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.services.llm.factory import build_llm_from_settings
from reuleauxcoder.extensions.subagent.context import project_parent_context
from reuleauxcoder.extensions.subagent.models import (
    SubagentResult,
    SubagentTranscriptStore,
)
from reuleauxcoder.extensions.subagent.isolation import create_worktree, remove_worktree


_VALID_SUBAGENT_MODES = frozenset({"explore", "execute", "verify"})
_DEFAULT_MAX_ROUNDS = 50
_DEFAULT_TIMEOUT_SECONDS = 300
_MAX_TIMEOUT_SECONDS = 3_600
SubagentDelivery = Literal["awaited", "detached"]
SubagentMessageKind = Literal[
    "milestone", "blocked", "approval_needed", "partial", "amendment"
]


def _subagent_item_hash(**payload) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _publish_job_event(parent_agent, job: "SubagentJob") -> None:
    """Best-effort lifecycle projection; execution never depends on a UI."""
    ledger = getattr(parent_agent, "history_ledger", None)
    if ledger is not None:
        ledger.append(
            "subagent_job_changed",
            {
                "job_id": job.id,
                "mode": job.mode,
                "task": job.task,
                "status": job.status,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "timeout_seconds": job.timeout_seconds,
                "result": job.result,
                "error": job.error,
                "generation": job.generation,
                "parent_session_id": job.parent_session_id,
                "parent_job_id": job.parent_job_id,
                "depth": job.depth,
                "context_mode": job.context_mode,
                "delivery": job.delivery,
                "worktree_path": job.worktree_path,
            },
            agent_id=getattr(parent_agent, "agent_id", None),
            parent_agent_id=job.parent_agent_id,
            job_id=job.id,
            turn_id=getattr(parent_agent, "_current_turn_id", None),
        )
        persist = getattr(parent_agent, "persist_runtime_snapshot", None)
        if callable(persist):
            persist()
    emit = getattr(parent_agent, "_emit_event", None)
    if not callable(emit):
        return
    emit(
        AgentEvent.subagent_completed(
            job_id=job.id,
            mode=job.mode,
            task=job.task,
            status=job.status,
            result=job.result if job.status == "completed" else None,
            error=job.error,
        )
    )


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


def _optional_int(value) -> int | None:
    return None if value is None else int(value)


def _optional_float(value) -> float | None:
    return None if value is None else float(value)


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
    parent_job_id: str | None = None
    depth: int = 0
    context_mode: str = "recent"
    structured_result: SubagentResult | None = None
    progress: tuple[str, ...] = ()
    worktree_path: str | None = None
    max_tool_calls: int | None = None
    max_tokens: int | None = None
    delivery: SubagentDelivery = "awaited"
    completion_seq: int | None = None


@dataclass(frozen=True, slots=True)
class SubagentCommunication:
    item_id: str
    seq: int
    sender_agent_id: str
    sender_job_id: str | None
    recipient_agent_id: str
    content: str
    created_at: float
    generation: int
    kind: SubagentMessageKind = "milestone"
    content_hash: str = ""


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
        max_depth: int = 1,
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
        self._max_depth = max(0, int(max_depth))
        self._completion_mailbox: deque[str] = deque()
        self._registered_agents: dict[str, int] = {}
        self._message_queues: dict[str, deque[str]] = {}
        self._agent_jobs: dict[str, str] = {}
        self._agent_parents: dict[str, str] = {}
        self._agent_generations: dict[str, int] = {}
        self._parent_messages: deque[SubagentCommunication] = deque()
        self._next_sequence = 1
        if parent_agent_id:
            self._registered_agents[parent_agent_id] = 0

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
    def max_depth(self) -> int:
        return self._max_depth

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
        context_mode: str = "recent",
        parent_job_id: str | None = None,
        depth: int = 0,
        worktree: bool = False,
        resume_reference: str | None = None,
        max_tool_calls: int | None = 80,
        max_tokens: int | None = None,
        detached: bool = False,
    ) -> str:
        if depth > self._max_depth:
            raise ValueError(f"Sub-agent depth limit reached ({self._max_depth})")

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
            if (
                self._parent_agent_id != parent_agent_id
                and parent_agent_id not in self._registered_agents
            ):
                raise ValueError(
                    "SubagentManager cannot be shared across parent agents"
                )
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
            parent_job_id=parent_job_id,
            depth=depth,
            context_mode=context_mode,
            max_tool_calls=max_tool_calls,
            max_tokens=max_tokens,
            delivery="detached" if detached else "awaited",
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
            if tracked is not None:
                _publish_job_event(parent_agent, tracked)
            try:
                return run_subagent_task(
                    parent_agent=parent_agent,
                    task=task,
                    mode=mode,
                    max_rounds=effective_max_rounds,
                    timeout_seconds=effective_timeout_seconds,
                    model_profile_name=model_profile_name,
                    cancel_event=cancel_event,
                    job_id=job_id,
                    context_mode=context_mode,
                    worktree=worktree,
                    resume_reference=resume_reference,
                    max_tool_calls=max_tool_calls,
                    max_tokens=max_tokens,
                )
            finally:
                with self._slot_cv:
                    self._active_explore = max(0, self._active_explore - 1)
                    self._slot_cv.notify_all()

        # Register the job before submission. A very fast Future may invoke its
        # callback immediately; it must always find the tracked job.
        with self._lock:
            self._jobs[job_id] = job
            self._message_queues[job_id] = deque()
            self._cancel_events[job_id] = cancel_event
        _publish_job_event(parent_agent, job)
        with self._lock:
            future = self._explore_pool.submit(_runner)
            self._futures[job_id] = future

        def _on_done(done: Future) -> None:
            with self._slot_cv:
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
                    else:
                        result_text = (
                            result.model_text()
                            if isinstance(result, SubagentResult)
                            else str(result)
                        )
                        if isinstance(result, SubagentResult) and result.status in {
                            "cancelled",
                            "cancelled_detached",
                            "timeout",
                            "timed_out_detached",
                        }:
                            tracked.structured_result = result
                            tracked.result = result.summary
                            tracked.detached_due_to_timeout = result.status.endswith(
                                "_detached"
                            )
                            tracked.status = result.status
                            tracked.error = result.summary
                        elif "[Sub-agent finished status=cancelled]" in result_text:
                            tracked.detached_due_to_timeout = "detached" in result_text
                            tracked.status = (
                                "cancelled_detached"
                                if tracked.detached_due_to_timeout
                                else "cancelled"
                            )
                            tracked.error = "Sub-agent cancelled."
                        elif "[Sub-agent finished status=timeout]" in result_text:
                            tracked.detached_due_to_timeout = True
                            tracked.status = "timed_out_detached"
                            tracked.error = "Sub-agent timed out and detached; background thread may still be running."
                        elif tracked.generation != self._generation:
                            tracked.status = "stale"
                            tracked.error = "Sub-agent completed for an inactive session generation."
                        else:
                            structured = _coerce_subagent_result(result)
                            tracked.structured_result = structured
                            tracked.result = structured.summary
                            tracked.worktree_path = structured.worktree_path
                            tracked.status = "completed"
                if (
                    tracked.generation == self._generation
                    and self._is_actionable_terminal(tracked)
                ):
                    self._enqueue_completion_locked(tracked)
                self._slot_cv.notify_all()
            _publish_job_event(parent_agent, tracked)

            # The parent drains this mailbox at an API-safe boundary. Worker
            # callbacks never mutate parent history directly.

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
        context_mode: str = "recent",
        depth: int = 0,
        worktree: bool = False,
        max_tool_calls: int | None = 80,
        max_tokens: int | None = None,
    ) -> str:
        if depth > self._max_depth:
            raise ValueError(f"Sub-agent depth limit reached ({self._max_depth})")
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
                context_mode=context_mode,
                depth=depth,
                worktree=worktree,
                max_tool_calls=max_tool_calls,
                max_tokens=max_tokens,
            )
            return future.result()
        return run_subagent_task(
            parent_agent=parent_agent,
            task=task,
            mode=mode,
            max_rounds=effective_max_rounds,
            timeout_seconds=effective_timeout_seconds,
            model_profile_name=model_profile_name,
            context_mode=context_mode,
            depth=depth,
            worktree=worktree,
            max_tool_calls=max_tool_calls,
            max_tokens=max_tokens,
        )

    def list_jobs(self) -> list[SubagentJob]:
        with self._lock:
            jobs = list(self._jobs.values())
        return sorted(jobs, key=lambda item: item.created_at, reverse=True)

    def restore_from_history(self, parent_agent, events) -> int:
        """Rebuild inspectable jobs; live workers never survive process resume."""
        with self._lock:
            self._jobs.clear()
            self._futures.clear()
            self._message_queues.clear()
            self._cancel_events.clear()
            self._parent_messages.clear()
            self._completion_mailbox.clear()
            self._parent_agent_id = getattr(parent_agent, "agent_id", None)
            self._generation = getattr(parent_agent, "session_generation", 0)
        latest: dict[str, dict] = {}
        for event in events:
            if getattr(event, "kind", None) != "subagent_job_changed":
                continue
            payload = getattr(event, "payload", {})
            job_id = str(payload.get("job_id") or "")
            if job_id:
                latest[job_id] = dict(payload)
        if not latest:
            return 0

        terminal = {
            "completed",
            "failed",
            "cancelled",
            "cancelled_detached",
            "timeout",
            "timed_out_detached",
            "stale",
        }
        visible_context = "\n".join(
            str(message.get("content") or "")
            for message in getattr(parent_agent, "messages", ())
        )
        restored: list[SubagentJob] = []
        for job_id, payload in latest.items():
            status = str(payload.get("status") or "stale")
            error = payload.get("error")
            if status not in terminal:
                status = "stale"
                error = "Worker was not recoverable after process resume."
            job = SubagentJob(
                id=job_id,
                mode=str(payload.get("mode") or "explore"),
                task=str(payload.get("task") or "restored sub-agent task"),
                status=status,
                created_at=float(payload.get("created_at") or time.time()),
                parent_agent_id=getattr(parent_agent, "agent_id", None),
                parent_session_id=(
                    payload.get("parent_session_id")
                    or getattr(parent_agent, "current_session_id", None)
                ),
                started_at=_optional_float(payload.get("started_at")),
                finished_at=_optional_float(payload.get("finished_at")),
                timeout_seconds=_optional_int(payload.get("timeout_seconds")),
                result=payload.get("result"),
                error=error,
                generation=getattr(parent_agent, "session_generation", 0),
                parent_job_id=payload.get("parent_job_id"),
                depth=int(payload.get("depth") or 0),
                context_mode=str(payload.get("context_mode") or "recent"),
                worktree_path=payload.get("worktree_path"),
                delivery=(
                    "detached"
                    if payload.get("delivery") == "detached"
                    else "awaited"
                ),
                injected_to_parent=f"id={job_id}" in visible_context,
            )
            restored.append(job)

        with self._lock:
            self._parent_agent_id = getattr(parent_agent, "agent_id", None)
            self._generation = getattr(parent_agent, "session_generation", 0)
            for job in restored:
                self._jobs[job.id] = job
                self._message_queues.setdefault(job.id, deque())
                self._cancel_events.setdefault(job.id, threading.Event())
        return len(restored)

    def get_job(self, job_id: str) -> SubagentJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def register_child_agent(
        self,
        agent_id: str,
        depth: int,
        *,
        parent_agent_id: str,
        job_id: str | None = None,
    ) -> None:
        with self._lock:
            if depth > self._max_depth:
                raise ValueError(f"Sub-agent depth limit reached ({self._max_depth})")
            self._registered_agents[agent_id] = depth
            self._agent_parents[agent_id] = parent_agent_id
            self._agent_generations[agent_id] = self._generation
            if job_id:
                self._agent_jobs[agent_id] = job_id

    def send_to_parent(
        self,
        sender_agent_id: str,
        message: str,
        *,
        kind: SubagentMessageKind = "milestone",
    ) -> bool:
        """Queue one child report for its immediate parent agent."""

        text = message.strip()
        with self._lock:
            recipient = self._agent_parents.get(sender_agent_id)
            if (
                not text
                or recipient is None
                or self._agent_generations.get(sender_agent_id) != self._generation
            ):
                return False
            seq = self._allocate_sequence_locked()
            self._parent_messages.append(
                SubagentCommunication(
                    item_id=f"sc_{uuid.uuid4().hex[:12]}",
                    seq=seq,
                    sender_agent_id=sender_agent_id,
                    sender_job_id=self._agent_jobs.get(sender_agent_id),
                    recipient_agent_id=recipient,
                    content=text,
                    created_at=time.time(),
                    generation=self._generation,
                    kind=kind,
                    content_hash=_subagent_item_hash(
                        sender_agent_id=sender_agent_id,
                        sender_job_id=self._agent_jobs.get(sender_agent_id),
                        recipient_agent_id=recipient,
                        generation=self._generation,
                        kind=kind,
                        content=text,
                    ),
                )
            )
            self._slot_cv.notify_all()
            return True

    def drain_parent_messages(self, parent_agent_id: str) -> list[SubagentCommunication]:
        with self._lock:
            selected: list[SubagentCommunication] = []
            retained: deque[SubagentCommunication] = deque()
            while self._parent_messages:
                message = self._parent_messages.popleft()
                if (
                    message.recipient_agent_id == parent_agent_id
                    and message.generation == self._generation
                ):
                    selected.append(message)
                elif message.generation == self._generation:
                    retained.append(message)
            self._parent_messages = retained
            return sorted(selected, key=lambda item: item.seq)

    def has_awaited_jobs(self, parent_agent_id: str) -> bool:
        """Return whether this parent still depends on a non-terminal child."""
        with self._lock:
            return any(
                job.parent_agent_id == parent_agent_id
                and job.generation == self._generation
                and job.delivery == "awaited"
                and job.status
                not in {
                    "completed",
                    "failed",
                    "cancelled",
                    "cancelled_detached",
                    "timeout",
                    "timed_out_detached",
                    "stale",
                }
                for job in self._jobs.values()
            )

    def wait_for_parent_activity(
        self, parent_agent_id: str, *, timeout: float = 0.1
    ) -> bool:
        """Wait briefly for an actionable completion or child-to-parent message."""
        with self._slot_cv:
            if self._has_parent_activity_locked(parent_agent_id):
                return True
            self._slot_cv.wait(timeout=max(0.0, timeout))
            return self._has_parent_activity_locked(parent_agent_id)

    def _has_parent_activity_locked(self, parent_agent_id: str) -> bool:
        return any(
            self._jobs.get(job_id) is not None
            and self._jobs[job_id].parent_agent_id == parent_agent_id
            and self._jobs[job_id].generation == self._generation
            for job_id in self._completion_mailbox
        ) or any(
            item.recipient_agent_id == parent_agent_id
            and item.generation == self._generation
            for item in self._parent_messages
        )

    def _allocate_sequence_locked(self) -> int:
        seq = self._next_sequence
        self._next_sequence += 1
        return seq

    @staticmethod
    def _is_actionable_terminal(job: SubagentJob) -> bool:
        if job.status == "stale":
            return False
        if job.delivery == "awaited":
            return job.status in {
                "completed",
                "failed",
                "cancelled",
                "cancelled_detached",
                "timeout",
                "timed_out_detached",
            }
        return job.status in {
            "failed",
            "cancelled_detached",
            "timeout",
            "timed_out_detached",
        }

    def _enqueue_completion_locked(self, job: SubagentJob) -> None:
        if job.id in self._completion_mailbox:
            return
        job.completion_seq = self._allocate_sequence_locked()
        self._completion_mailbox.append(job.id)

    def send_message(self, job_id: str, message: str) -> bool:
        """Queue a message for a running worker; it is consumed next model round."""

        text = message.strip()
        if not text:
            return False
        with self._lock:
            job = self._jobs.get(job_id)
            queue = self._message_queues.get(job_id)
            if job is None or queue is None or job.status not in {"queued", "running"}:
                return False
            queue.append(text)
            return True

    def drain_messages(self, job_id: str) -> list[str]:
        with self._lock:
            queue = self._message_queues.get(job_id)
            if queue is None:
                return []
            messages = list(queue)
            queue.clear()
            return messages

    def follow_up(
        self,
        *,
        parent_agent,
        job_id: str,
        message: str,
        timeout_seconds: int | None = None,
    ) -> str:
        """Resume a completed explore agent transcript as a new invocation."""

        previous = self.get_job(job_id)
        if previous is None or previous.status != "completed":
            raise ValueError("follow-up requires a completed sub-agent job")
        reference = (
            previous.structured_result.transcript_ref
            if previous.structured_result is not None
            else None
        )
        return self.submit_background(
            parent_agent=parent_agent,
            task=message,
            mode="explore",
            timeout_seconds=timeout_seconds,
            context_mode="minimal",
            parent_job_id=job_id,
            depth=previous.depth,
            resume_reference=reference,
        )

    def cleanup_worktree(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        if job is None or not job.worktree_path:
            return False
        remove_worktree(job.worktree_path)
        job.worktree_path = None
        return True

    def wait_job(self, job_id: str, timeout: float | None = None) -> SubagentJob | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            future = self._futures.get(job_id)
        if future is None:
            return None

        try:
            future.result(timeout=timeout)
        except FutureTimeoutError:
            return self.get_job(job_id)
        except Exception:
            # The done callback owns the public failed/stale terminal state.
            pass

        terminal = {
            "completed",
            "failed",
            "cancelled",
            "cancelled_detached",
            "timeout",
            "timed_out_detached",
            "stale",
        }
        with self._slot_cv:
            job = self._jobs.get(job_id)
            while job is not None and job.status not in terminal:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                self._slot_cv.wait(timeout=remaining)
                job = self._jobs.get(job_id)
            return job

    def cancel_job(self, job_id: str) -> bool:
        """Request cancellation and prevent later parent injection."""
        with self._slot_cv:
            job = self._jobs.get(job_id)
            event = self._cancel_events.get(job_id)
            future = self._futures.get(job_id)
            if (
                job is None
                or event is None
                or job.status
                in {
                    "completed",
                    "failed",
                    "cancelled",
                    "cancelled_detached",
                    "timeout",
                    "timed_out_detached",
                    "stale",
                }
            ):
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
            self._parent_messages.clear()
            self._completion_mailbox.clear()
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
            "timeout",
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
                self._message_queues.pop(job_id, None)
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
        self,
        *,
        parent_state_lock: threading.Lock | None = None,
        parent_agent_id: str | None = None,
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
            mailbox_ids: list[str] = []
            retained_mailbox: deque[str] = deque()
            while self._completion_mailbox:
                candidate = self._completion_mailbox.popleft()
                job = self._jobs.get(candidate)
                if job is not None and job.generation != self._generation:
                    continue
                if (
                    parent_agent_id is None
                    or job is None
                    or job.parent_agent_id == parent_agent_id
                ):
                    mailbox_ids.append(candidate)
                else:
                    retained_mailbox.append(candidate)
            self._completion_mailbox = retained_mailbox
            # Compatibility for restored/older jobs that predate the mailbox.
            mailbox_ids.extend(
                job.id
                for job in self._jobs.values()
                if self._is_actionable_terminal(job)
                and (parent_agent_id is None or job.parent_agent_id == parent_agent_id)
                and job.generation == self._generation
                and not job.injected_to_parent
                and job.id not in mailbox_ids
            )
            for job_id in mailbox_ids:
                job = self._jobs.get(job_id)
                if job is None:
                    continue
                if job.injected_to_parent:
                    continue
                if job.status not in {
                    "completed",
                    "failed",
                    "cancelled",
                    "cancelled_detached",
                    "timeout",
                    "timed_out_detached",
                }:
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
                            parent_job_id=job.parent_job_id,
                            depth=job.depth,
                            context_mode=job.context_mode,
                            structured_result=job.structured_result,
                            progress=job.progress,
                            worktree_path=job.worktree_path,
                            max_tool_calls=job.max_tool_calls,
                            max_tokens=job.max_tokens,
                            delivery=job.delivery,
                            completion_seq=job.completion_seq,
                        )
                    )
                finally:
                    if parent_state_lock is not None:
                        parent_state_lock.release()
        return sorted(
            drained,
            key=lambda item: (
                item.completion_seq
                if item.completion_seq is not None
                else float("inf")
            ),
        )


def get_subagent_manager(agent) -> SubagentManager:
    manager = getattr(agent, "_subagent_manager", None)
    if isinstance(manager, SubagentManager):
        return manager

    default_rounds = getattr(agent, "max_rounds", 50)
    manager = SubagentManager(
        max_parallel_explore=2,
        default_max_rounds=default_rounds,
        parent_agent_id=getattr(agent, "agent_id", None),
        initial_generation=getattr(agent, "session_generation", 0),
        max_depth=1,
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


def _filter_subagent_tools(parent_agent, mode: str, *, include_agent: bool = False):
    mode_allowlist = {
        "explore": {"read_file", "glob", "grep"},
        "execute": {"read_file", "glob", "grep", "edit_file", "write_file", "shell"},
        "verify": {"read_file", "glob", "grep", "shell"},
    }
    allowed = mode_allowlist[mode]
    allowed.update({"update_plan", "report_progress"})
    if include_agent:
        allowed.add("agent")
    return [
        _clone_tool_for_subagent(tool)
        for tool in parent_agent.tools
        if tool.name in allowed
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
    job_id: str | None = None,
    context_mode: str = "recent",
    depth: int = 0,
    worktree: bool = False,
    resume_reference: str | None = None,
    max_tool_calls: int | None = 80,
    max_tokens: int | None = None,
) -> str | SubagentResult:
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

    lease = None
    manager = get_subagent_manager(parent_agent)
    sub_tools = _filter_subagent_tools(
        parent_agent, mode, include_agent=True
    )
    if worktree:
        if mode != "execute":
            raise ValueError("worktree isolation is only supported for execute mode")
        if not job_id:
            raise ValueError("worktree isolation requires a tracked job id")
        root = getattr(parent_agent, "runtime_working_directory", None) or Path.cwd()
        lease = create_worktree(root, job_id)
        _retarget_tools(sub_tools, lease.path)
    sub = Agent(
        llm=sub_llm,
        tools=sub_tools,
        max_context_tokens=parent_agent.context.max_tokens,
        max_rounds=effective_max_rounds,
        max_tool_calls=max_tool_calls,
        max_total_tokens=max_tokens,
        hook_registry=parent_agent.hook_registry.clone(scope="subagent"),
        approval_provider=build_subagent_approval_provider(parent_agent, mode, task),
    )
    sub.runtime_config = getattr(parent_agent, "runtime_config", None)
    sub.current_session_id = getattr(parent_agent, "current_session_id", None)
    sub.history_ledger.bind_context(
        session_id=sub.current_session_id,
        agent_id=sub.agent_id,
    )
    sub.session_generation = getattr(parent_agent, "session_generation", 0)
    sub.subagent_depth = depth
    sub._subagent_manager = manager
    # Child activity remains attributed to the child, but uses the root event
    # transport so every UI can observe chunks/tools without polling workers.
    parent_event_sink = getattr(parent_agent, "_emit_event", None)
    if callable(parent_event_sink):
        sub.add_event_handler(parent_event_sink)
    if job_id:
        sub._external_message_source = lambda: manager.drain_messages(job_id)
    manager.register_child_agent(
        sub.agent_id,
        depth,
        parent_agent_id=parent_agent.agent_id,
        job_id=job_id,
    )
    for tool in sub.tools:
        bind_agent = getattr(tool, "bind_agent", None)
        if callable(bind_agent):
            bind_agent(sub)
        if getattr(tool, "name", None) == "agent":
            tool._parent_agent = sub
    if resume_reference:
        root = getattr(parent_agent, "runtime_working_directory", None) or Path.cwd()
        sub._replace_context_messages(
            SubagentTranscriptStore(root).read(resume_reference),
            reason="subagent transcript resume",
            record=False,
        )

    parent_context = project_parent_context(parent_agent, context_mode)
    delegated_prompt = (
        "You are a delegated worker. Stay within the assigned scope. "
        "Return the conclusion first, then evidence, relevant files, changes, "
        "and unresolved issues. Do not delegate unless the task explicitly "
        "requires independent parallel work.\n\n"
        f"[Parent context mode={context_mode}]\n{parent_context}\n"
        "[/Parent context]\n\n"
        + (
            f"[Isolated worktree]\n{lease.path}\nRe-read files because inherited paths may be stale.\n[/Isolated worktree]\n"
            if lease is not None
            else ""
        )
        + f"[Assigned task]\n{task}\n[/Assigned task]"
    )

    holder: dict[str, object] = {}

    def _run() -> None:
        try:
            holder["result"] = sub.chat(delegated_prompt)
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
        partial = _result_from_agent(
            sub,
            status="cancelled_detached" if suffix else "cancelled",
            summary=str(holder.get("result", "Sub-agent cancelled.")),
            started_at=deadline - effective_timeout_seconds,
            job_id=job_id,
            parent_agent=parent_agent,
            partial=True,
        )
        partial.worktree_path = str(lease.path) if lease is not None else None
        return partial
    if thread.is_alive():
        sub.request_stop()
        timeout_result = _result_from_agent(
                sub,
                status="timed_out_detached",
                summary=f"Sub-agent exceeded timeout after {effective_timeout_seconds}s.",
                started_at=deadline - effective_timeout_seconds,
                job_id=job_id,
                parent_agent=parent_agent,
                partial=True,
            )
        timeout_result.worktree_path = str(lease.path) if lease is not None else None
        return timeout_result

    if "error" in holder:
        raise holder["error"]  # type: ignore[misc]
    result = str(holder.get("result", ""))

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

    final_result = _result_from_agent(
        sub,
        status=status,
        summary=result,
        started_at=deadline - effective_timeout_seconds,
        job_id=job_id,
        parent_agent=parent_agent,
    )
    final_result.worktree_path = str(lease.path) if lease is not None else None
    return final_result


def _coerce_subagent_result(value: object) -> SubagentResult:
    if isinstance(value, SubagentResult):
        return value
    return SubagentResult(status="ok", summary=str(value))


def _result_from_agent(
    sub,
    *,
    status: str,
    summary: str,
    started_at: float,
    job_id: str | None,
    parent_agent,
    partial: bool = False,
) -> SubagentResult:
    import re

    messages = list(getattr(sub, "messages", []))
    files = sorted(
        {
            match.group(0)
            for match in re.finditer(r"(?:[\w.-]+/)+[\w.-]+", summary)
        }
    )
    result = SubagentResult(
        status=status,
        summary=summary,
        files=files[:100],
        duration_seconds=max(0.0, time.monotonic() - started_at),
        partial=partial,
    )
    if job_id:
        root = (
            getattr(parent_agent, "runtime_working_directory", None) or Path.cwd()
        )
        try:
            result.transcript_ref = SubagentTranscriptStore(root).write(
                job_id,
                messages,
                {"status": status, "partial": partial},
            )
        except OSError:
            pass
    return result


def _retarget_tools(tools: list, cwd: Path) -> None:
    """Point cloned local tool adapters at an isolated worktree."""

    from reuleauxcoder.infrastructure.workspace import LocalWorkspacePort

    for tool in tools:
        backend = getattr(tool, "backend", None)
        context = getattr(backend, "context", None)
        if context is None:
            continue
        context.cwd = str(cwd)
        context.workspace_root = str(cwd)
        if getattr(backend, "backend_id", None) == "local":
            backend.workspace = LocalWorkspacePort(cwd, cwd=cwd)
        if hasattr(tool, "_cwd"):
            tool._cwd = str(cwd)
