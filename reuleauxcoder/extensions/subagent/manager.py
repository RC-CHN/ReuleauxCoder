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
from typing import Callable, Literal

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.services.llm.factory import llm_runtime_kwargs
from reuleauxcoder.extensions.subagent.context import project_parent_context
from reuleauxcoder.extensions.subagent.models import (
    SubagentResult,
    SubagentTranscriptStore,
)
from reuleauxcoder.extensions.subagent.scoped_tools import materialize_subagent_tool
from reuleauxcoder.extensions.subagent.worker_protocol import (
    WorkerSpec,
    WorkerToolSpec,
)
from reuleauxcoder.extensions.subagent.worker_runtime import (
    ParentToolBroker,
    run_isolated_worker,
)
from reuleauxcoder.extensions.subagent.isolation import create_worktree, remove_worktree


_VALID_SUBAGENT_MODES = frozenset({"explore", "execute", "verify"})
_DEFAULT_MAX_ROUNDS = 50
_DEFAULT_TIMEOUT_SECONDS = 300
_MAX_TIMEOUT_SECONDS = 3_600
SubagentMessageKind = Literal[
    "reply",
    "milestone",
    "amendment",
    "warning",
    "approval_needed",
    "partial",
    "guidance",
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
                "worktree_path": job.worktree_path,
                "verification_job_id": job.verification_job_id,
                "verification_for": job.verification_for,
                "working_directory": job.working_directory,
                "guidance_request_id": job.guidance_request_id,
                "resume_reference": job.resume_reference,
                "prompt_tokens": job.prompt_tokens,
                "completion_tokens": job.completion_tokens,
                "tool_calls": job.tool_calls,
                "worker_generation": job.worker_generation,
                "model_calls": job.model_calls,
                "cancellation_epoch": job.cancellation_epoch,
                "progress": list(job.progress),
                "current_tool": job.current_tool,
                "last_activity_at": job.last_activity_at,
                "max_rounds": job.max_rounds,
                "model_profile_name": job.model_profile_name,
                "auto_verify": job.auto_verify,
                "agent_id": job.agent_id,
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
            activity=(job.progress[1] if len(job.progress) > 1 else None),
            current_tool=job.current_tool,
            tool_calls=job.tool_calls,
            max_tool_calls=job.max_tool_calls,
            tokens=job.prompt_tokens + job.completion_tokens,
            max_tokens=job.max_tokens,
            blocker=(
                job.error or job.guidance_request_id
                if job.status == "blocked"
                else None
            ),
        )
    )


def _publish_communication_event(
    parent_agent, item: "SubagentCommunication", *, status: str
) -> None:
    """Persist mailbox enqueue/ack so crash recovery is exactly-once."""
    ledger = getattr(parent_agent, "history_ledger", None)
    if ledger is None:
        return
    ledger.append(
        f"subagent_communication_{status}",
        {
            "item_id": item.item_id,
            "seq": item.seq,
            "sender_agent_id": item.sender_agent_id,
            "sender_job_id": item.sender_job_id,
            "recipient_agent_id": item.recipient_agent_id,
            "content": item.content,
            "created_at": item.created_at,
            "generation": item.generation,
            "kind": item.kind,
            "reply_to": item.reply_to,
            "content_hash": item.content_hash,
            "direction": "child_to_parent",
        },
        agent_id=item.sender_agent_id,
        parent_agent_id=item.recipient_agent_id,
        job_id=item.sender_job_id,
        turn_id=getattr(parent_agent, "_current_turn_id", None),
    )
    persist = getattr(parent_agent, "persist_runtime_snapshot", None)
    if callable(persist):
        persist()


def _publish_directive_event(
    parent_agent,
    *,
    directive_id: str,
    target_job_id: str,
    sender_agent_id: str,
    content: str,
    generation: int,
    status: str,
    seq: int | None = None,
    content_hash: str | None = None,
    source: str = "parent",
) -> None:
    ledger = getattr(parent_agent, "history_ledger", None)
    if ledger is None:
        return
    ledger.append(
        f"subagent_communication_{status}",
        {
            "item_id": directive_id,
            "direction": "parent_to_child",
            "target_job_id": target_job_id,
            "sender_agent_id": sender_agent_id,
            "content": content,
            "generation": generation,
            "seq": seq,
            "content_hash": content_hash,
            "source": source,
        },
        agent_id=sender_agent_id,
        job_id=target_job_id,
        turn_id=getattr(parent_agent, "_current_turn_id", None),
    )
    persist = getattr(parent_agent, "persist_runtime_snapshot", None)
    if callable(persist):
        persist()


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
    completion_seq: int | None = None
    verification_job_id: str | None = None
    verification_for: str | None = None
    working_directory: str | None = None
    guidance_request_id: str | None = None
    resume_reference: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    worker_generation: int = 0
    model_calls: int = 0
    cancellation_epoch: int = 0
    current_tool: str | None = None
    last_activity_at: float | None = None
    max_rounds: int = _DEFAULT_MAX_ROUNDS
    model_profile_name: str | None = None
    auto_verify: bool = True
    agent_id: str | None = None


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
    reply_to: str | None = None
    content_hash: str = ""


@dataclass(frozen=True, slots=True)
class SubagentDirective:
    directive_id: str
    seq: int
    target_job_id: str
    sender_agent_id: str
    content: str
    created_at: float
    generation: int
    content_hash: str
    source: Literal["parent", "human"] = "parent"

    def model_text(self) -> str:
        return (
            f"directive_id={self.directive_id}\n"
            f"sender_agent_id={self.sender_agent_id}\n"
            f"source={self.source}\n\n"
            f"{self.content}"
        )


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
        self._job_schedulers: dict[str, Callable[[], bool]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._parent_agent_id = parent_agent_id
        self._generation = max(0, int(initial_generation))
        self._shutdown = False
        self._max_depth = max(0, int(max_depth))
        self._completion_mailbox: deque[str] = deque()
        self._registered_agents: dict[str, int] = {}
        self._message_queues: dict[str, deque[SubagentDirective]] = {}
        self._agent_jobs: dict[str, str] = {}
        self._agent_parents: dict[str, str] = {}
        self._agent_generations: dict[str, int] = {}
        self._parent_messages: deque[SubagentCommunication] = deque()
        self._claimed_parent_messages: set[str] = set()
        self._next_sequence = 1
        self._root_agent = None
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

    def bind_root_agent(self, agent) -> None:
        """Bind only the root owner used for ledger persistence and recovery."""
        if getattr(agent, "agent_id", None) == self._parent_agent_id:
            self._root_agent = agent

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
        depth: int = 1,
        worktree: bool = False,
        resume_reference: str | None = None,
        max_tool_calls: int | None = 80,
        max_tokens: int | None = None,
        auto_verify: bool = True,
        working_directory: str | None = None,
    ) -> str | SubagentResult:
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
        self.bind_root_agent(parent_agent)

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
            verification_for=parent_job_id if mode == "verify" else None,
            working_directory=working_directory,
            resume_reference=resume_reference,
            max_rounds=effective_max_rounds,
            model_profile_name=model_profile_name,
            auto_verify=auto_verify,
            agent_id=f"sa_{job_id}",
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
                    tracked.last_activity_at = tracked.started_at
                    tracked.finished_at = None
                    tracked.worker_generation += 1
                    active_resume_reference = tracked.resume_reference
                    initial_prompt_tokens = tracked.prompt_tokens
                    initial_completion_tokens = tracked.completion_tokens
                    initial_tool_calls = tracked.tool_calls
                    initial_model_calls = tracked.model_calls
                    worker_generation = tracked.worker_generation
                    cancellation_epoch = tracked.cancellation_epoch
                else:
                    active_resume_reference = None
                    initial_prompt_tokens = 0
                    initial_completion_tokens = 0
                    initial_tool_calls = 0
                    initial_model_calls = 0
                    worker_generation = 1
                    cancellation_epoch = 0
            if tracked is not None:
                _publish_job_event(parent_agent, tracked)
            resume_directives = (
                tuple(
                    directive.model_text()
                    for directive in self.drain_messages(job_id)
                )
                if active_resume_reference
                else ()
            )
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
                    resume_reference=active_resume_reference,
                    resume_directives=resume_directives,
                    initial_prompt_tokens=initial_prompt_tokens,
                    initial_completion_tokens=initial_completion_tokens,
                    initial_tool_calls=initial_tool_calls,
                    initial_model_calls=initial_model_calls,
                    worker_generation=worker_generation,
                    cancellation_epoch=cancellation_epoch,
                    max_tool_calls=max_tool_calls,
                    max_tokens=max_tokens,
                    working_directory=working_directory,
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

        def _on_done(done: Future) -> None:
            schedule_verify = False
            resume_immediately = False
            with self._slot_cv:
                tracked = self._jobs.get(job_id)
                if tracked is None:
                    return
                tracked.finished_at = time.time()
                tracked.last_activity_at = tracked.finished_at
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
                        if tracked.cancel_requested:
                            tracked.status = (
                                result.status
                                if isinstance(result, SubagentResult)
                                and result.status in {"cancelled", "killed"}
                                else "cancelled"
                            )
                            tracked.result = None
                            tracked.error = "Sub-agent cancelled; late result quarantined."
                            result = None
                        result_text = (
                            result.model_text()
                            if isinstance(result, SubagentResult)
                            else str(result)
                        )
                        if result is None:
                            pass
                        elif isinstance(result, SubagentResult) and result.status in {
                            "cancelled",
                            "killed",
                            "timed_out",
                        }:
                            tracked.structured_result = result
                            tracked.result = result.summary
                            tracked.status = result.status
                            tracked.error = result.summary
                        elif (
                            isinstance(result, SubagentResult)
                            and result.status == "blocked"
                        ):
                            tracked.structured_result = result
                            tracked.result = result.summary
                            tracked.resume_reference = result.transcript_ref
                            tracked.prompt_tokens = result.prompt_tokens
                            tracked.completion_tokens = result.completion_tokens
                            tracked.tool_calls = result.tool_uses
                            tracked.model_calls = result.model_calls
                            tracked.status = "blocked"
                            tracked.finished_at = None
                            tracked.error = None
                            resume_immediately = bool(
                                self._message_queues.get(job_id)
                            )
                            if resume_immediately:
                                tracked.status = "resuming"
                                tracked.guidance_request_id = None
                        elif isinstance(result, SubagentResult) and result.status in {
                            "failed",
                            "error",
                            "unverified",
                            "indeterminate",
                        }:
                            tracked.structured_result = result
                            tracked.result = result.summary
                            tracked.worktree_path = result.worktree_path
                            tracked.status = (
                                "indeterminate"
                                if result.status == "indeterminate"
                                else "failed"
                            )
                            tracked.error = result.summary
                        elif "[Sub-agent finished status=cancelled]" in result_text:
                            tracked.status = "cancelled"
                            tracked.error = "Sub-agent cancelled."
                        elif tracked.generation != self._generation:
                            tracked.status = "stale"
                            tracked.error = "Sub-agent completed for an inactive session generation."
                        else:
                            structured = _coerce_subagent_result(result)
                            tracked.structured_result = structured
                            tracked.result = structured.summary
                            tracked.worktree_path = structured.worktree_path
                            tracked.status = "completed"
                            schedule_verify = tracked.mode == "execute" and auto_verify
                if (
                    tracked.generation == self._generation
                    and self._is_actionable_terminal(tracked)
                    and not schedule_verify
                ):
                    dependency = (
                        self._jobs.get(tracked.parent_job_id or "")
                        if tracked.mode == "verify"
                        else None
                    )
                    if dependency is not None and self._is_actionable_terminal(
                        dependency
                    ):
                        self._enqueue_completion_locked(dependency)
                    self._enqueue_completion_locked(tracked)
                self._slot_cv.notify_all()
            _publish_job_event(parent_agent, tracked)

            if resume_immediately:
                scheduler = self._job_schedulers.get(job_id)
                if callable(scheduler):
                    scheduler()

            if schedule_verify:
                verification_root = tracked.worktree_path or (
                    getattr(parent_agent, "runtime_working_directory", None)
                    or str(Path.cwd())
                )
                verification_task = (
                    f"Verify execute job {tracked.id}. Inspect its reported changes, "
                    "run the narrowest relevant tests or diagnostics, and report "
                    "objective evidence. Do not modify implementation files. "
                    f"Execution root: {verification_root}"
                )
                try:
                    verification_id = self.submit_background(
                        parent_agent=parent_agent,
                        task=verification_task,
                        mode="verify",
                        max_rounds=min(effective_max_rounds, 20),
                        timeout_seconds=effective_timeout_seconds,
                        model_profile_name=model_profile_name,
                        context_mode="minimal",
                        parent_job_id=tracked.id,
                        depth=tracked.depth,
                        max_tool_calls=max_tool_calls,
                        max_tokens=max_tokens,
                        auto_verify=False,
                        working_directory=verification_root,
                    )
                except Exception as error:  # fail visible, never strand dependency
                    with self._slot_cv:
                        tracked.error = (
                            "Automatic verification could not start: " + str(error)
                        )
                        if self._is_actionable_terminal(tracked):
                            self._enqueue_completion_locked(tracked)
                        self._slot_cv.notify_all()
                    _publish_job_event(parent_agent, tracked)
                else:
                    with self._lock:
                        tracked.verification_job_id = verification_id
                    _publish_job_event(parent_agent, tracked)

            # The parent drains this mailbox at an API-safe boundary. Worker
            # callbacks never mutate parent history directly.

        def _schedule() -> bool:
            with self._lock:
                tracked = self._jobs.get(job_id)
                current = self._futures.get(job_id)
                if (
                    tracked is None
                    or self._shutdown
                    or tracked.status not in {"queued", "resuming"}
                    or (current is not None and not current.done())
                ):
                    return False
                future = self._explore_pool.submit(_runner)
                self._futures[job_id] = future
            future.add_done_callback(_on_done)
            return True

        with self._lock:
            self._job_schedulers[job_id] = _schedule
        _schedule()
        return job_id

    def list_jobs(self) -> list[SubagentJob]:
        with self._lock:
            jobs = list(self._jobs.values())
        return sorted(jobs, key=lambda item: item.created_at, reverse=True)

    def restore_from_history(self, parent_agent, events) -> int:
        """Rebuild inspectable jobs; live workers never survive process resume."""
        events = list(events)
        self.bind_root_agent(parent_agent)
        with self._lock:
            self._jobs.clear()
            self._futures.clear()
            self._job_schedulers.clear()
            self._message_queues.clear()
            self._cancel_events.clear()
            self._parent_messages.clear()
            self._claimed_parent_messages.clear()
            self._completion_mailbox.clear()
            self._parent_agent_id = getattr(parent_agent, "agent_id", None)
            self._generation = getattr(parent_agent, "session_generation", 0)
        latest: dict[str, dict] = {}
        queued_messages: dict[str, dict] = {}
        delivered_messages: set[str] = set()
        queued_directives: dict[str, dict] = {}
        delivered_directives: set[str] = set()
        for event in events:
            kind = getattr(event, "kind", None)
            payload = getattr(event, "payload", {})
            if kind == "subagent_job_changed":
                job_id = str(payload.get("job_id") or "")
                if job_id:
                    latest[job_id] = dict(payload)
            elif kind == "subagent_communication_queued":
                if payload.get("direction") == "parent_to_child":
                    item_id = str(payload.get("item_id") or "")
                    if item_id:
                        queued_directives[item_id] = dict(payload)
                    continue
                item_id = str(payload.get("item_id") or "")
                if item_id:
                    queued_messages[item_id] = dict(payload)
            elif kind == "subagent_communication_delivered":
                if payload.get("direction") == "parent_to_child":
                    item_id = str(payload.get("item_id") or "")
                    if item_id:
                        delivered_directives.add(item_id)
                    continue
                item_id = str(payload.get("item_id") or "")
                if item_id:
                    delivered_messages.add(item_id)

        terminal = {
            "completed",
            "failed",
            "cancelled",
            "killed",
            "timed_out",
            "indeterminate",
            "stale",
        }
        visible_context = "\n".join(
            str(message.get("content") or "")
            for message in getattr(parent_agent, "messages", ())
        )
        restored: list[SubagentJob] = []
        stale_jobs: list[SubagentJob] = []
        for job_id, payload in latest.items():
            status = str(payload.get("status") or "stale")
            error = payload.get("error")
            if status not in terminal and status != "blocked":
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
                injected_to_parent=f"id={job_id}" in visible_context,
                verification_job_id=payload.get("verification_job_id"),
                verification_for=payload.get("verification_for"),
                working_directory=payload.get("working_directory"),
                guidance_request_id=payload.get("guidance_request_id"),
                resume_reference=payload.get("resume_reference"),
                prompt_tokens=int(payload.get("prompt_tokens") or 0),
                completion_tokens=int(payload.get("completion_tokens") or 0),
                tool_calls=int(payload.get("tool_calls") or 0),
                worker_generation=int(payload.get("worker_generation") or 0),
                model_calls=int(payload.get("model_calls") or 0),
                cancellation_epoch=int(payload.get("cancellation_epoch") or 0),
                progress=tuple(str(item) for item in payload.get("progress") or ()),
                current_tool=payload.get("current_tool"),
                last_activity_at=_optional_float(payload.get("last_activity_at")),
                max_rounds=int(payload.get("max_rounds") or _DEFAULT_MAX_ROUNDS),
                model_profile_name=payload.get("model_profile_name"),
                auto_verify=bool(payload.get("auto_verify", True)),
                agent_id=payload.get("agent_id") or f"sa_{job_id}",
            )
            restored.append(job)
            if status == "stale" and str(payload.get("status")) != "stale":
                stale_jobs.append(job)

        pending_messages: list[SubagentCommunication] = []
        for item_id, payload in queued_messages.items():
            if item_id in delivered_messages or f"item_id={item_id}" in visible_context:
                continue
            sender = str(payload.get("sender_agent_id") or "restored-child")
            recipient = str(
                payload.get("recipient_agent_id")
                or getattr(parent_agent, "agent_id", "root")
            )
            content = str(payload.get("content") or "").strip()
            if not content:
                continue
            kind = str(payload.get("kind") or "milestone")
            if kind not in {
                "reply",
                "milestone",
                "amendment",
                "warning",
                "approval_needed",
                "partial",
                "guidance",
            }:
                kind = "milestone"
            pending_messages.append(
                SubagentCommunication(
                    item_id=item_id,
                    seq=max(1, int(payload.get("seq") or 1)),
                    sender_agent_id=sender,
                    sender_job_id=payload.get("sender_job_id"),
                    recipient_agent_id=recipient,
                    content=content,
                    created_at=float(payload.get("created_at") or time.time()),
                    generation=getattr(parent_agent, "session_generation", 0),
                    kind=kind,  # type: ignore[arg-type]
                    reply_to=payload.get("reply_to"),
                    content_hash=_subagent_item_hash(
                        sender_agent_id=sender,
                        sender_job_id=payload.get("sender_job_id"),
                        recipient_agent_id=recipient,
                        generation=getattr(parent_agent, "session_generation", 0),
                        kind=kind,
                        reply_to=payload.get("reply_to"),
                        content=content,
                    ),
                )
            )

        pending_directives: list[SubagentDirective] = []
        for directive_id, payload in queued_directives.items():
            if directive_id in delivered_directives:
                continue
            target_job_id = str(payload.get("target_job_id") or "")
            content = str(payload.get("content") or "").strip()
            source = str(payload.get("source") or "parent")
            if not target_job_id or not content or source not in {"parent", "human"}:
                continue
            seq = max(1, int(payload.get("seq") or 1))
            sender = str(
                payload.get("sender_agent_id")
                or self._parent_agent_id
                or "root"
            )
            pending_directives.append(
                SubagentDirective(
                    directive_id=directive_id,
                    seq=seq,
                    target_job_id=target_job_id,
                    sender_agent_id=sender,
                    content=content,
                    created_at=float(payload.get("created_at") or time.time()),
                    generation=getattr(parent_agent, "session_generation", 0),
                    content_hash=str(payload.get("content_hash") or ""),
                    source=source,  # type: ignore[arg-type]
                )
            )

        with self._lock:
            self._parent_agent_id = getattr(parent_agent, "agent_id", None)
            self._generation = getattr(parent_agent, "session_generation", 0)
            for job in restored:
                self._jobs[job.id] = job
                self._message_queues.setdefault(job.id, deque())
                self._cancel_events.setdefault(job.id, threading.Event())
            for directive in sorted(pending_directives, key=lambda item: item.seq):
                queue = self._message_queues.get(directive.target_job_id)
                if queue is not None:
                    queue.append(directive)
            self._parent_messages.extend(
                sorted(pending_messages, key=lambda item: item.seq)
            )
            if pending_messages or pending_directives:
                self._next_sequence = max(
                    self._next_sequence,
                    max(
                        [item.seq for item in pending_messages]
                        + [item.seq for item in pending_directives]
                    )
                    + 1,
                )
        for job in restored:
            if job.status == "blocked" and job.resume_reference:
                self._install_restored_blocked_scheduler(parent_agent, job.id)
        for job in stale_jobs:
            _publish_job_event(parent_agent, job)
        return len(restored)

    def _install_restored_blocked_scheduler(self, parent_agent, job_id: str) -> None:
        """Recreate a dormant resume closure without starting the worker."""
        cancel_event = self._cancel_events[job_id]

        def _runner():
            with self._slot_cv:
                while self._active_explore >= self._runtime_parallel_explore:
                    if cancel_event.is_set() or self._shutdown:
                        return "[Sub-agent finished status=cancelled]"
                    self._slot_cv.wait(timeout=0.5)
                job = self._jobs[job_id]
                if cancel_event.is_set() or self._shutdown:
                    return "[Sub-agent finished status=cancelled]"
                self._active_explore += 1
                job.status = "running"
                job.started_at = time.time()
                job.last_activity_at = job.started_at
                job.finished_at = None
                job.worker_generation += 1
            _publish_job_event(parent_agent, job)
            directives = tuple(
                directive.model_text() for directive in self.drain_messages(job_id)
            )
            try:
                return run_subagent_task(
                    parent_agent=parent_agent,
                    task=job.task,
                    mode=job.mode,
                    max_rounds=job.max_rounds,
                    timeout_seconds=job.timeout_seconds or self._default_timeout_seconds,
                    model_profile_name=job.model_profile_name,
                    cancel_event=cancel_event,
                    job_id=job.id,
                    context_mode=job.context_mode,
                    depth=job.depth,
                    resume_reference=job.resume_reference,
                    resume_directives=directives,
                    initial_prompt_tokens=job.prompt_tokens,
                    initial_completion_tokens=job.completion_tokens,
                    initial_tool_calls=job.tool_calls,
                    initial_model_calls=job.model_calls,
                    worker_generation=job.worker_generation,
                    cancellation_epoch=job.cancellation_epoch,
                    max_tool_calls=job.max_tool_calls,
                    max_tokens=job.max_tokens,
                    working_directory=job.worktree_path or job.working_directory,
                )
            finally:
                with self._slot_cv:
                    self._active_explore = max(0, self._active_explore - 1)
                    self._slot_cv.notify_all()

        def _on_done(done: Future) -> None:
            resume_immediately = False
            with self._slot_cv:
                job = self._jobs.get(job_id)
                if job is None:
                    return
                try:
                    result = done.result()
                except Exception as error:  # pragma: no cover - defensive
                    job.status = "failed"
                    job.error = str(error)
                    job.finished_at = time.time()
                    job.last_activity_at = job.finished_at
                else:
                    if job.cancel_requested:
                        job.status = "cancelled"
                        job.result = None
                        job.error = "Sub-agent cancelled; late result quarantined."
                        job.finished_at = time.time()
                    elif isinstance(result, SubagentResult) and result.status == "blocked":
                        job.status = "blocked"
                        job.structured_result = result
                        job.result = result.summary
                        job.resume_reference = result.transcript_ref
                        job.prompt_tokens = result.prompt_tokens
                        job.completion_tokens = result.completion_tokens
                        job.tool_calls = result.tool_uses
                        job.model_calls = result.model_calls
                        job.finished_at = None
                        resume_immediately = bool(self._message_queues.get(job_id))
                        if resume_immediately:
                            job.status = "resuming"
                            job.guidance_request_id = None
                    else:
                        structured = _coerce_subagent_result(result)
                        job.structured_result = structured
                        job.result = structured.summary
                        job.worktree_path = structured.worktree_path or job.worktree_path
                        job.status = (
                            "completed" if structured.status == "ok" else structured.status
                        )
                        if not self._is_actionable_terminal(job):
                            job.status = "failed"
                        job.error = (
                            None if job.status == "completed" else structured.summary
                        )
                        job.finished_at = time.time()
                if self._is_actionable_terminal(job):
                    self._enqueue_completion_locked(job)
                self._slot_cv.notify_all()
            _publish_job_event(parent_agent, job)
            if resume_immediately:
                _schedule()

        def _schedule() -> bool:
            with self._lock:
                job = self._jobs.get(job_id)
                current = self._futures.get(job_id)
                if (
                    job is None
                    or self._shutdown
                    or job.status != "resuming"
                    or (current is not None and not current.done())
                ):
                    return False
                future = self._explore_pool.submit(_runner)
                self._futures[job_id] = future
            future.add_done_callback(_on_done)
            return True

        with self._lock:
            self._job_schedulers[job_id] = _schedule

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
        reply_to: str | None = None,
    ) -> bool:
        """Queue one child report for its immediate parent agent."""

        return self.queue_to_parent(
            sender_agent_id,
            message,
            kind=kind,
            reply_to=reply_to,
        ) is not None

    def queue_to_parent(
        self,
        sender_agent_id: str,
        message: str,
        *,
        kind: SubagentMessageKind = "milestone",
        reply_to: str | None = None,
    ) -> SubagentCommunication | None:
        """Queue and return one typed child report for its immediate parent."""

        text = message.strip()
        queued: SubagentCommunication | None = None
        with self._lock:
            recipient = self._agent_parents.get(sender_agent_id)
            if (
                not text
                or recipient is None
                or self._agent_generations.get(sender_agent_id) != self._generation
            ):
                return None
            seq = self._allocate_sequence_locked()
            queued = SubagentCommunication(
                item_id=f"sc_{uuid.uuid4().hex[:12]}",
                seq=seq,
                sender_agent_id=sender_agent_id,
                sender_job_id=self._agent_jobs.get(sender_agent_id),
                recipient_agent_id=recipient,
                content=text,
                created_at=time.time(),
                generation=self._generation,
                kind=kind,
                reply_to=reply_to,
                content_hash=_subagent_item_hash(
                    sender_agent_id=sender_agent_id,
                    sender_job_id=self._agent_jobs.get(sender_agent_id),
                    recipient_agent_id=recipient,
                    generation=self._generation,
                    kind=kind,
                    reply_to=reply_to,
                    content=text,
                ),
            )
            self._parent_messages.append(queued)
            self._slot_cv.notify_all()
        if self._root_agent is not None:
            _publish_communication_event(self._root_agent, queued, status="queued")
        return queued

    def request_guidance(
        self,
        sender_agent_id: str,
        question: str,
        *,
        context: str | None = None,
    ) -> SubagentCommunication | None:
        """Begin a ledgered parking transition and publish one blocker."""
        text = question.strip()
        if context and context.strip():
            text = f"{text}\n\nContext:\n{context.strip()}"
        with self._lock:
            job_id = self._agent_jobs.get(sender_agent_id)
            job = self._jobs.get(job_id or "")
            if job is None or job.status != "running":
                return None
            job.status = "parking"
        item = self.queue_to_parent(
            sender_agent_id,
            text,
            kind="guidance",
        )
        if item is None:
            with self._lock:
                if job.status == "parking":
                    job.status = "running"
            return None
        with self._lock:
            job.guidance_request_id = item.item_id
        if self._root_agent is not None:
            _publish_job_event(self._root_agent, job)
        return item

    def record_progress(
        self,
        sender_agent_id: str,
        *,
        phase: str,
        summary: str,
        next_step: str | None = None,
    ) -> bool:
        """Project child-owned progress into its authoritative job snapshot."""
        with self._lock:
            job = self._jobs.get(self._agent_jobs.get(sender_agent_id, ""))
            if job is None or job.status not in {"running", "parking"}:
                return False
            job.progress = tuple(
                part for part in (phase.strip(), summary.strip(), next_step) if part
            )
            job.last_activity_at = time.time()
        if self._root_agent is not None:
            _publish_job_event(self._root_agent, job)
        return True

    def record_tool_activity(
        self, sender_agent_id: str, tool_name: str | None
    ) -> bool:
        """Record the currently brokered tool without exposing its output."""
        with self._lock:
            job = self._jobs.get(self._agent_jobs.get(sender_agent_id, ""))
            if job is None or job.status not in {"running", "parking"}:
                return False
            job.current_tool = tool_name
            job.last_activity_at = time.time()
        if self._root_agent is not None:
            _publish_job_event(self._root_agent, job)
        return True

    def commit_worker_checkpoint(
        self,
        job_id: str,
        reference: str,
        checkpoint,
    ) -> bool:
        """Durably project a provider-safe checkpoint before ParkAck."""
        with self._lock:
            job = self._jobs.get(job_id)
            if (
                job is None
                or job.generation != self._generation
                or job.status not in {"parking", "running"}
                or job.cancel_requested
            ):
                return False
            if (
                job.guidance_request_id
                and checkpoint.guidance_request_id != job.guidance_request_id
            ):
                return False
            job.status = "blocked"
            job.resume_reference = reference
            job.prompt_tokens = checkpoint.prompt_tokens
            job.completion_tokens = checkpoint.completion_tokens
            job.tool_calls = checkpoint.tool_calls
            job.model_calls = checkpoint.model_calls
            job.current_tool = None
            job.last_activity_at = time.time()
        if self._root_agent is not None:
            _publish_job_event(self._root_agent, job)
        return True

    def drain_parent_messages(self, parent_agent_id: str) -> list[SubagentCommunication]:
        with self._lock:
            selected = [
                message
                for message in self._parent_messages
                if (
                    message.recipient_agent_id == parent_agent_id
                    and message.generation == self._generation
                    and message.item_id not in self._claimed_parent_messages
                )
            ]
            self._claimed_parent_messages.update(item.item_id for item in selected)
            return sorted(selected, key=lambda item: item.seq)

    def acknowledge_parent_message(self, item_id: str) -> bool:
        """Remove a safely injected mailbox item and persist its watermark."""
        acknowledged = None
        with self._lock:
            retained: deque[SubagentCommunication] = deque()
            while self._parent_messages:
                item = self._parent_messages.popleft()
                if acknowledged is None and item.item_id == item_id:
                    acknowledged = item
                else:
                    retained.append(item)
            self._parent_messages = retained
            self._claimed_parent_messages.discard(item_id)
        if acknowledged is None:
            return False
        if self._root_agent is not None:
            _publish_communication_event(
                self._root_agent, acknowledged, status="delivered"
            )
        return True

    def release_parent_message(self, item_id: str) -> None:
        """Return an uncommitted claim to the mailbox for a later safe boundary."""
        with self._lock:
            self._claimed_parent_messages.discard(item_id)
            self._slot_cv.notify_all()

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
            and item.item_id not in self._claimed_parent_messages
            for item in self._parent_messages
        )

    def _allocate_sequence_locked(self) -> int:
        seq = self._next_sequence
        self._next_sequence += 1
        return seq

    @staticmethod
    def _is_actionable_terminal(job: SubagentJob) -> bool:
        return job.status in {
            "completed",
            "failed",
            "cancelled",
            "killed",
            "timed_out",
            "indeterminate",
        }

    def _enqueue_completion_locked(self, job: SubagentJob) -> None:
        if job.id in self._completion_mailbox:
            return
        job.completion_seq = self._allocate_sequence_locked()
        self._completion_mailbox.append(job.id)

    def send_message(
        self,
        job_id: str,
        message: str,
        *,
        sender_agent_id: str | None = None,
        source: Literal["parent", "human"] = "parent",
    ) -> bool:
        """Queue a message for a running worker; it is consumed next model round."""

        return self.queue_message(
            job_id,
            message,
            sender_agent_id=sender_agent_id,
            source=source,
        ) is not None

    def queue_message(
        self,
        job_id: str,
        message: str,
        *,
        sender_agent_id: str | None = None,
        source: Literal["parent", "human"] = "parent",
    ) -> SubagentDirective | None:
        """Queue and return one typed directive for a running child."""

        text = message.strip()
        if not text or source not in {"parent", "human"}:
            return None
        directive_id = f"sd_{uuid.uuid4().hex[:12]}"
        sender = sender_agent_id or self._parent_agent_id or "root"
        resume = False
        with self._lock:
            job = self._jobs.get(job_id)
            queue = self._message_queues.get(job_id)
            if job is None or queue is None or job.status not in {
                "queued",
                "running",
                "parking",
                "blocked",
                "resuming",
            }:
                return None
            if job.status == "blocked" and (
                not job.resume_reference or job_id not in self._job_schedulers
            ):
                return None
            seq = self._allocate_sequence_locked()
            directive = SubagentDirective(
                directive_id=directive_id,
                seq=seq,
                target_job_id=job_id,
                sender_agent_id=sender,
                content=text,
                created_at=time.time(),
                generation=self._generation,
                source=source,
                content_hash=_subagent_item_hash(
                    directive_id=directive_id,
                    seq=seq,
                    target_job_id=job_id,
                    sender_agent_id=sender,
                    content=text,
                    generation=self._generation,
                    source=source,
                ),
            )
            if source == "human" and job.status in {
                "parking",
                "blocked",
                "resuming",
            }:
                queue.appendleft(directive)
            else:
                queue.append(directive)
            if job.status == "blocked":
                job.status = "resuming"
                job.guidance_request_id = None
                resume = True
        if self._root_agent is not None:
            _publish_directive_event(
                self._root_agent,
                directive_id=directive_id,
                target_job_id=job_id,
                sender_agent_id=sender,
                content=text,
                generation=self._generation,
                status="queued",
                seq=directive.seq,
                content_hash=directive.content_hash,
                source=directive.source,
            )
        if resume:
            if self._root_agent is not None:
                _publish_job_event(self._root_agent, job)
            scheduler = self._job_schedulers.get(job_id)
            if callable(scheduler):
                scheduler()
        return directive

    def drain_messages(self, job_id: str) -> list[SubagentDirective]:
        with self._lock:
            queue = self._message_queues.get(job_id)
            if queue is None:
                return []
            messages = list(queue)
            queue.clear()
        for directive in messages:
            if self._root_agent is not None:
                _publish_directive_event(
                    self._root_agent,
                    directive_id=directive.directive_id,
                    target_job_id=job_id,
                    sender_agent_id=directive.sender_agent_id,
                    content=directive.content,
                    generation=self._generation,
                    status="delivered",
                    seq=directive.seq,
                    content_hash=directive.content_hash,
                    source=directive.source,
                )
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
            "killed",
            "timed_out",
            "indeterminate",
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
                    "killed",
                    "timed_out",
                    "indeterminate",
                    "stale",
                }
            ):
                return False
            job.cancel_requested = True
            job.cancellation_epoch += 1
            event.set()
            was_blocked = job.status == "blocked"
            job.status = "cancelled" if was_blocked else "cancelling"
            if was_blocked:
                job.finished_at = time.time()
                job.guidance_request_id = None
                job.error = "Sub-agent cancelled while awaiting guidance."
                self._enqueue_completion_locked(job)
            self._slot_cv.notify_all()
        self._cancel_job_approvals(job_id)
        if was_blocked:
            if self._root_agent is not None:
                _publish_job_event(self._root_agent, job)
            return True
        # Future.cancel() may invoke callbacks synchronously. Never call it
        # while holding the manager lock because callbacks acquire that lock.
        cancelled_before_start = future is not None and future.cancel()
        if cancelled_before_start:
            with self._lock:
                job.status = "cancelled"
                job.finished_at = job.finished_at or time.time()
        return True

    def _cancel_job_approvals(self, job_id: str) -> int:
        root = self._root_agent
        provider = getattr(root, "approval_provider", None)
        coordinator = getattr(provider, "coordinator", None)
        cancel_matching = getattr(coordinator, "cancel_matching", None)
        if not callable(cancel_matching):
            return 0
        request_ids = cancel_matching(
            lambda request: request.metadata.get("subagent_job_id") == job_id,
            reason=f"sub-agent {job_id} cancelled",
        )
        interactor = getattr(root, "ui_interactor", None)
        cancel_interaction = getattr(interactor, "cancel", None)
        if callable(cancel_interaction):
            for request_id in request_ids:
                cancel_interaction(
                    request_id,
                    reason=f"sub-agent {job_id} cancelled",
                )
        return len(request_ids)

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
            self._claimed_parent_messages.clear()
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
            "killed",
            "timed_out",
            "indeterminate",
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
                self._job_schedulers.pop(job_id, None)
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
                    "killed",
                    "timed_out",
                    "indeterminate",
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
                            completion_seq=job.completion_seq,
                            verification_job_id=job.verification_job_id,
                            verification_for=job.verification_for,
                            working_directory=job.working_directory,
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
        manager.bind_root_agent(agent)
        return manager

    default_rounds = getattr(agent, "max_rounds", 50)
    manager = SubagentManager(
        max_parallel_explore=2,
        default_max_rounds=default_rounds,
        parent_agent_id=getattr(agent, "agent_id", None),
        initial_generation=getattr(agent, "session_generation", 0),
        max_depth=1,
    )
    manager.bind_root_agent(agent)
    agent._subagent_manager = manager
    return manager


def _resolve_subagent_settings(parent_agent, route: str | None):
    config = getattr(parent_agent, "runtime_config", None)
    if config is None:
        return parent_agent.llm, None
    profiles = getattr(config, "model_profiles", {}) or {}
    normalized = (route or "").strip().lower()
    if normalized == "main":
        profile_name = (
            getattr(config, "active_main_model_profile", None)
            or getattr(config, "active_model_profile", None)
            or getattr(config, "active_sub_model_profile", None)
        )
    else:
        profile_name = (
            getattr(config, "active_sub_model_profile", None)
            or getattr(config, "active_main_model_profile", None)
            or getattr(config, "active_model_profile", None)
        )
    profile = profiles.get(profile_name) if profile_name else None
    return (profile, profile_name) if profile is not None else (parent_agent.llm, None)


def _subagent_llm_kwargs(parent_agent, route: str | None) -> dict:
    settings, _profile_name = _resolve_subagent_settings(parent_agent, route)
    kwargs = llm_runtime_kwargs(
        settings,
        debug_trace=getattr(parent_agent.llm, "debug_trace", False),
    )
    for field in ("reasoning_effort_values", "reasoning_effort_param"):
        if hasattr(settings, field):
            kwargs[field] = getattr(settings, field)
    return kwargs


def _filter_subagent_tools(parent_agent, mode: str):
    mode_allowlist = {
        "explore": {"read_file", "list_file", "glob", "grep", "lsp"},
        "execute": {
            "read_file",
            "list_file",
            "glob",
            "grep",
            "lsp",
            "edit_file",
            "write_file",
            "shell",
        },
        "verify": {"read_file", "list_file", "glob", "grep", "lsp", "shell"},
    }
    allowed = mode_allowlist[mode]
    allowed.update(
        {"report_progress", "report_to_parent", "request_guidance"}
    )
    return [
        materialize_subagent_tool(tool)
        for tool in parent_agent.tools
        if tool.name in allowed
    ]


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
    depth: int = 1,
    worktree: bool = False,
    resume_reference: str | None = None,
    resume_directives: tuple[str, ...] = (),
    initial_prompt_tokens: int = 0,
    initial_completion_tokens: int = 0,
    initial_tool_calls: int = 0,
    initial_model_calls: int = 0,
    worker_generation: int = 1,
    cancellation_epoch: int = 0,
    max_tool_calls: int | None = 80,
    max_tokens: int | None = None,
    working_directory: str | None = None,
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

    lease = None
    manager = get_subagent_manager(parent_agent)
    sub_tools = _filter_subagent_tools(parent_agent, mode)
    if worktree:
        if mode != "execute":
            raise ValueError("worktree isolation is only supported for execute mode")
        if not job_id:
            raise ValueError("worktree isolation requires a tracked job id")
        root = getattr(parent_agent, "runtime_working_directory", None) or Path.cwd()
        lease = create_worktree(root, job_id)
        _retarget_tools(sub_tools, lease.path)
    elif working_directory:
        _retarget_tools(sub_tools, Path(working_directory))
    child_agent_id = f"sa_{job_id or uuid.uuid4().hex[:12]}"
    broker_hooks = parent_agent.hook_registry.clone(scope="subagent")
    broker_hooks.bind_runtime_service(
        "lsp_manager", getattr(parent_agent, "lsp_manager", None)
    )
    sub = Agent(
        llm=parent_agent.llm,
        tools=sub_tools,
        max_context_tokens=parent_agent.context.max_tokens,
        max_rounds=effective_max_rounds,
        max_tool_calls=max_tool_calls,
        max_total_tokens=max_tokens,
        hook_registry=broker_hooks,
        approval_provider=build_subagent_approval_provider(parent_agent, mode, task),
        agent_id=child_agent_id,
    )
    sub.runtime_config = getattr(parent_agent, "runtime_config", None)
    sub.runtime_working_directory = (
        str(lease.path)
        if lease is not None
        else working_directory
        or getattr(parent_agent, "runtime_working_directory", None)
    )
    sub.current_session_id = getattr(parent_agent, "current_session_id", None)
    sub.history_ledger.bind_context(
        session_id=sub.current_session_id,
        agent_id=sub.agent_id,
    )
    sub.session_generation = getattr(parent_agent, "session_generation", 0)
    sub.subagent_depth = depth
    sub.parent_agent_id = parent_agent.agent_id
    sub.subagent_job_id = job_id
    sub.subagent_mode = mode
    sub.subagent_task = task
    sub.strict_tool_scope = True
    sub._subagent_manager = manager
    parent_event_sink = getattr(parent_agent, "_emit_event", None)
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
    replay_messages: list[dict] = []
    if resume_reference:
        root = getattr(parent_agent, "runtime_working_directory", None) or Path.cwd()
        replay_messages = SubagentTranscriptStore(root).read(resume_reference)

    delegated_prompt = build_delegated_prompt(
        task=task,
        parent_context=project_parent_context(parent_agent, context_mode),
        context_mode=context_mode,
        worktree_path=str(lease.path) if lease is not None else None,
        working_directory=(
            working_directory if working_directory and lease is None else None
        ),
    )

    cancellation = cancel_event or threading.Event()
    spec = WorkerSpec(
        job_id=job_id or f"sj_{uuid.uuid4().hex[:10]}",
        agent_id=child_agent_id,
        session_id=getattr(parent_agent, "current_session_id", None),
        session_generation=getattr(parent_agent, "session_generation", 0),
        worker_generation=worker_generation,
        cancellation_epoch=cancellation_epoch,
        delegated_prompt=delegated_prompt,
        llm_kwargs=_subagent_llm_kwargs(parent_agent, model_profile_name),
        tools=tuple(
            WorkerToolSpec(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
            )
            for tool in sub_tools
        ),
        max_context_tokens=parent_agent.context.max_tokens,
        max_rounds=effective_max_rounds,
        max_tool_calls=max_tool_calls,
        max_tokens=max_tokens,
        working_directory=sub.runtime_working_directory,
        replay_messages=tuple(replay_messages),
        resume_directives=resume_directives,
        initial_prompt_tokens=initial_prompt_tokens,
        initial_completion_tokens=initial_completion_tokens,
        initial_tool_calls=initial_tool_calls,
        initial_model_calls=initial_model_calls,
    )
    broker = ParentToolBroker(
        sub,
        cancellation_event=cancellation,
        event_sink=parent_event_sink if callable(parent_event_sink) else None,
    )
    started_at = time.monotonic()
    execution = run_isolated_worker(
        spec,
        broker,
        cancel_event=cancellation,
        timeout_seconds=effective_timeout_seconds,
        directive_source=(
            (lambda: manager.drain_messages(job_id)) if job_id else None
        ),
        event_sink=parent_event_sink if callable(parent_event_sink) else None,
        checkpoint_sink=(
            (
                lambda reference, checkpoint, _payload: (
                    manager.commit_worker_checkpoint(
                        job_id, reference, checkpoint
                    )
                )
            )
            if job_id
            else None
        ),
    )
    sub.state.messages = list(execution.messages)
    sub.state.total_prompt_tokens = execution.prompt_tokens
    sub.state.total_completion_tokens = execution.completion_tokens
    sub.state.total_tool_calls = execution.tool_calls
    sub.state.total_model_calls = execution.model_calls
    result = execution.summary
    status = execution.status
    if status == "ok" and (
        result.strip() == "(reached maximum tool-call rounds)" or any(
        marker in result
        for marker in (
            "Maximum tool-call rounds reached.",
            "Max rounds reached.",
            "Reached maximum tool-call rounds",
        )
    )):
        status = "max_rounds"

    final_result = _result_from_agent(
        sub,
        status=status,
        summary=result,
        started_at=started_at,
        job_id=job_id,
        parent_agent=parent_agent,
        partial=status
        in {
            "blocked",
            "cancelled",
            "killed",
            "timed_out",
            "failed",
            "indeterminate",
        },
    )
    final_result.worktree_path = str(lease.path) if lease is not None else None
    return final_result


def build_delegated_prompt(
    *,
    task: str,
    parent_context: str,
    context_mode: str,
    worktree_path: str | None = None,
    working_directory: str | None = None,
) -> str:
    """Build the stable child contract used by fresh and resumed workers."""
    sections = [
        (
            "You are a delegated worker with a narrow assigned scope. Do not "
            "create or delegate to other agents and do not modify the root plan. "
            "Use report_progress only for low-frequency human-visible status, "
            "report_to_parent for non-blocking findings/replies, and "
            "request_guidance only when you cannot safely continue without a decision."
        ),
        f"[Parent context mode={context_mode}]\n{parent_context}\n[/Parent context]",
    ]
    if worktree_path:
        sections.append(
            "[Isolated worktree]\n"
            f"{worktree_path}\n"
            "Re-read relevant files because inherited paths or contents may be stale.\n"
            "[/Isolated worktree]"
        )
    elif working_directory:
        sections.append(
            f"[Execution root]\n{working_directory}\n[/Execution root]"
        )
    sections.extend(
        [
            f"[Assigned task]\n{task}\n[/Assigned task]",
            (
                "When the task is complete, return one final assistant response with "
                "no tool calls. Use exactly these sections in this order:\n"
                "1. Conclusion — answer the assigned task directly.\n"
                "2. Evidence — cite actual reads, commands, tests, or diagnostics.\n"
                "3. Changes and artifacts — list files/artifacts/worktree state, or None.\n"
                "4. Unresolved issues — list blockers/risks/parent decisions, or None.\n"
                "5. Confidence — high, medium, or low, including why confidence is reduced."
            ),
        ]
    )
    return "\n\n".join(sections)


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
    tool_facts = [
        event
        for event in getattr(getattr(sub, "history_ledger", None), "events", ())
        if event.kind == "tool_call_finished"
    ]
    failures = [event for event in tool_facts if not event.payload.get("success")]
    evidence = [
        (
            f"tool {event.payload.get('tool_name') or 'unknown'}: "
            f"{event.payload.get('status') or 'unknown'}"
            + (
                f" (exit {event.payload['exit_code']})"
                if event.payload.get("exit_code") is not None
                else ""
            )
        )
        for event in tool_facts[-20:]
    ]
    if getattr(sub, "subagent_mode", None) == "verify" and failures:
        status = "failed"
        summary = (
            "Verification observed one or more failed tool outcomes.\n" + summary
        ).strip()
    result = SubagentResult(
        status=status,
        summary=summary,
        evidence=evidence,
        files=files[:100],
        duration_seconds=max(0.0, time.monotonic() - started_at),
        partial=partial,
        tool_uses=int(getattr(sub.state, "total_tool_calls", 0)),
        prompt_tokens=int(getattr(sub.state, "total_prompt_tokens", 0)),
        completion_tokens=int(getattr(sub.state, "total_completion_tokens", 0)),
        model_calls=int(getattr(sub.state, "total_model_calls", 0)),
    )
    if job_id:
        root = (
            getattr(parent_agent, "runtime_working_directory", None) or Path.cwd()
        )
        try:
            result.transcript_ref = SubagentTranscriptStore(root).write(
                job_id,
                messages,
                {
                    "status": status,
                    "partial": partial,
                    "failed_tool_outcomes": len(failures),
                },
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
