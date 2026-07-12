"""Isolated child process and parent-side scoped tool broker."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import multiprocessing
from multiprocessing.connection import Connection
from pathlib import Path
from queue import Empty, Queue
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolArchiveReference,
    ToolOutcome,
    ToolOutcomeStatus,
)
from reuleauxcoder.domain.llm.models import ToolCall
from reuleauxcoder.domain.runtime import (
    agent_event_to_runtime_event,
    runtime_event_from_dict,
    runtime_event_to_agent_event,
    runtime_event_to_dict,
    tool_outcome_from_dict,
    tool_outcome_to_dict,
)
from reuleauxcoder.extensions.subagent.worker_protocol import (
    WorkerEnvelope,
    WorkerSpec,
    WorkerToolSpec,
    ToolResultRef,
)
from reuleauxcoder.extensions.subagent.models import SubagentTranscriptStore
from reuleauxcoder.extensions.tools.base import Tool
from reuleauxcoder.services.llm.client import LLM

if TYPE_CHECKING:
    from reuleauxcoder.domain.agent.agent import Agent


@dataclass(slots=True)
class WorkerExecutionResult:
    status: str
    summary: str
    messages: list[dict[str, Any]]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    guidance_request_id: str | None = None
    model_calls: int = 0
    killed: bool = False
    usage_uncertain: bool = False
    resume_ready: bool = False


class BrokeredWorkerTool(Tool):
    """Worker-side schema adapter whose implementation lives in the parent."""

    def __init__(self, spec: WorkerToolSpec, client: "WorkerIPCClient"):
        super().__init__(backend=None)
        self.name = spec.name
        self.description = spec.description
        self.parameters = spec.parameters
        self._client = client

    def execute(self, **kwargs) -> ToolOutcome:
        outcome = self._client.request_tool(self.name, kwargs)
        if outcome.metadata.get("park_subagent") and hasattr(self, "_agent"):
            self._agent._park_request = dict(outcome.metadata)
        return outcome

    def bind_agent(self, agent) -> None:
        self._agent = agent


class WorkerIPCClient:
    """Serialize worker sends and demultiplex parent responses/directives."""

    def __init__(
        self,
        spec: WorkerSpec,
        connection: Connection,
        cancellation_event,
    ):
        self.spec = spec
        self.connection = connection
        self.cancellation_event = cancellation_event
        self._send_lock = threading.Lock()
        self._state_lock = threading.Condition()
        self._sequence = 0
        self._tool_results: dict[str, ToolOutcome] = {}
        self._directives: list[tuple[str, str]] = []
        self._park_acks: set[str] = set()
        self._closed = False
        self._receiver = threading.Thread(target=self._receive_loop, daemon=True)
        self._receiver.start()

    def send(self, message_type: str, payload: dict[str, Any]) -> None:
        with self._send_lock:
            self._sequence += 1
            envelope = WorkerEnvelope(
                type=message_type,  # type: ignore[arg-type]
                job_id=self.spec.job_id,
                agent_id=self.spec.agent_id,
                session_generation=self.spec.session_generation,
                worker_generation=self.spec.worker_generation,
                cancellation_epoch=self.spec.cancellation_epoch,
                sequence=self._sequence,
                payload=payload,
            )
            self.connection.send(envelope.to_dict())

    def request_tool(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        call_id = f"broker_{time.time_ns()}"
        self.send(
            "tool_request",
            {"call_id": call_id, "name": name, "arguments": arguments},
        )
        with self._state_lock:
            while call_id not in self._tool_results:
                if self.cancellation_event.is_set() or self._closed:
                    return ToolOutcome(
                        status=ToolOutcomeStatus.CANCELLED,
                        content=f"Tool '{name}' cancelled with child worker.",
                        error_kind=ToolErrorKind.INTERRUPTED,
                    )
                self._state_lock.wait(timeout=0.05)
            return self._tool_results.pop(call_id)

    def drain_directives(self) -> list[str]:
        with self._state_lock:
            directives = self._directives
            self._directives = []
        if directives:
            self.send(
                "directive_ack",
                {"directive_ids": [directive_id for directive_id, _text in directives]},
            )
        return [text for _directive_id, text in directives]

    def wait_for_park_ack(self, checkpoint_id: str, *, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._state_lock:
            while checkpoint_id not in self._park_acks:
                if self.cancellation_event.is_set() or self._closed:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._state_lock.wait(timeout=min(0.05, remaining))
            self._park_acks.remove(checkpoint_id)
            return True

    def _receive_loop(self) -> None:
        try:
            while True:
                envelope = WorkerEnvelope.from_dict(self.connection.recv())
                if envelope.type == "tool_result":
                    call_id = str(envelope.payload.get("call_id") or "")
                    outcome = tool_outcome_from_dict(
                        _required_object(envelope.payload.get("outcome"), "outcome")
                    )
                    if envelope.payload.get("outcome_ref") is not None:
                        reference = ToolResultRef.from_dict(
                            _required_object(
                                envelope.payload.get("outcome_ref"), "outcome_ref"
                            )
                        )
                        _validate_tool_result_ref(reference, outcome)
                    with self._state_lock:
                        self._tool_results[call_id] = outcome
                        self._state_lock.notify_all()
                elif envelope.type == "directive":
                    directive_id = str(envelope.payload.get("directive_id") or "")
                    sender = str(envelope.payload.get("sender_agent_id") or "root")
                    source = str(envelope.payload.get("source") or "parent")
                    content = str(envelope.payload.get("content") or "")
                    with self._state_lock:
                        self._directives.append(
                            (
                                directive_id,
                                f"directive_id={directive_id}\n"
                                f"sender_agent_id={sender}\n"
                                f"source={source}\n\n{content}",
                            )
                        )
                        self._state_lock.notify_all()
                elif envelope.type == "park_ack":
                    checkpoint_id = str(
                        envelope.payload.get("checkpoint_id") or ""
                    )
                    with self._state_lock:
                        self._park_acks.add(checkpoint_id)
                        self._state_lock.notify_all()
        except (EOFError, OSError, TypeError, ValueError):
            with self._state_lock:
                self._closed = True
                self._state_lock.notify_all()


class ParentToolBroker:
    """Execute worker requests through the existing scoped Agent pipeline."""

    def __init__(
        self,
        agent: "Agent",
        *,
        cancellation_event: threading.Event,
        event_sink: Callable[[AgentEvent], None] | None,
    ):
        self.agent = agent
        self.cancellation_event = cancellation_event
        self.event_sink = event_sink
        self._captured_outcomes: dict[str, ToolOutcome] = {}
        self._committed_requests: dict[str, tuple[str, str, ToolOutcome]] = {}
        self._request_lock = threading.Lock()
        agent._stop_event = cancellation_event
        for tool in agent.tools:
            backend_context = getattr(getattr(tool, "backend", None), "context", None)
            if backend_context is not None:
                backend_context.cancellation_event = cancellation_event
        agent.add_event_handler(self._capture_event)

    def execute(self, call_id: str, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        fingerprint = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._request_lock:
            committed = self._committed_requests.get(call_id)
            if committed is not None:
                previous_name, previous_fingerprint, outcome = committed
                if previous_name == name and previous_fingerprint == fingerprint:
                    return outcome
                return ToolOutcome(
                    status=ToolOutcomeStatus.FAILED,
                    content=(
                        f"Tool call id '{call_id}' was reused with a different request."
                    ),
                    error_kind=ToolErrorKind.INVALID_ARGUMENTS,
                )
        if self.cancellation_event.is_set():
            return _cancelled_outcome(name)
        manager = getattr(self.agent, "_subagent_manager", None)
        if manager is not None:
            manager.record_tool_activity(self.agent.agent_id, name)
        try:
            result = self.agent._executor.execute(
                ToolCall(id=call_id, name=name, arguments=arguments)
            )
        finally:
            if manager is not None:
                manager.record_tool_activity(self.agent.agent_id, None)
        outcome = self._captured_outcomes.pop(
            call_id,
            ToolOutcome.from_legacy(result, success=not result.startswith("Error:")),
        )
        with self._request_lock:
            self._committed_requests[call_id] = (name, fingerprint, outcome)
        return outcome

    def _capture_event(self, event: AgentEvent) -> None:
        if event.event_type is AgentEventType.TOOL_CALL_END and event.correlation_id:
            if event.tool_outcome is not None:
                self._captured_outcomes[event.correlation_id] = event.tool_outcome
            return
        if self.cancellation_event.is_set():
            return
        if event.event_type in {
            AgentEventType.TOOL_OUTPUT_DELTA,
            AgentEventType.PROGRESS_REPORTED,
            AgentEventType.DIAGNOSTIC,
            AgentEventType.APPROVAL_REQUESTED,
            AgentEventType.APPROVAL_RESOLVED,
        } and self.event_sink is not None:
            self.event_sink(event)

    def ipc_tool_result(self, outcome: ToolOutcome) -> dict[str, Any]:
        """Inline small outcomes; archive large source facts by content hash."""
        serialized = tool_outcome_to_dict(outcome)
        encoded = json.dumps(
            serialized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) <= 32_768:
            return {"outcome": serialized}

        checksum = hashlib.sha256(encoded).hexdigest()
        root = Path(
            getattr(self.agent, "runtime_working_directory", None) or Path.cwd()
        )
        directory = root / ".rcoder" / "subagents" / "tool-results"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{checksum}.json"
        if not path.exists():
            temporary = path.with_suffix(f".{time.time_ns()}.tmp")
            temporary.write_bytes(encoded)
            temporary.replace(path)
        archive = ToolArchiveReference(
            path=str(path),
            media_type="application/json",
            checksum_sha256=checksum,
            size_bytes=len(encoded),
        )
        projection = replace(
            outcome,
            content=None,
            stdout="",
            stderr="",
            diff=None,
            diagnostics=(),
            archive_reference=archive,
            model_content=outcome.model_text,
        )
        reference = ToolResultRef(
            path=str(path),
            checksum_sha256=checksum,
            size_bytes=len(encoded),
            model_view_hash=hashlib.sha256(
                projection.model_text.encode("utf-8")
            ).hexdigest(),
        )
        return {
            "outcome": tool_outcome_to_dict(projection),
            "outcome_ref": reference.to_dict(),
        }

    def is_effectful(self, tool_name: str) -> bool:
        tool = next(
            (candidate for candidate in self.agent.tools if candidate.name == tool_name),
            None,
        )
        effect_class = getattr(tool, "effect_class", None)
        return effect_class in {"workspace_write", "process_execution", "remote_effect"}


def worker_process_main(spec_data: dict[str, Any], connection: Connection, cancel) -> None:
    """Spawn target: run one child model loop and no workspace primitives."""
    from reuleauxcoder.domain.agent.agent import Agent

    spec = WorkerSpec.from_dict(spec_data)
    client = WorkerIPCClient(spec, connection, cancel)
    try:
        llm = LLM(**spec.llm_kwargs)
        tools = [BrokeredWorkerTool(tool_spec, client) for tool_spec in spec.tools]
        child = Agent(
            llm=llm,
            tools=tools,
            max_context_tokens=spec.max_context_tokens,
            max_rounds=max(0, spec.max_rounds - spec.initial_model_calls),
            max_tool_calls=spec.max_tool_calls,
            max_total_tokens=spec.max_tokens,
            agent_id=spec.agent_id,
        )
        child._stop_event = cancel
        child.current_session_id = spec.session_id
        child.session_generation = spec.session_generation
        child.subagent_depth = 1
        child.subagent_job_id = spec.job_id
        child.strict_tool_scope = True
        child.runtime_working_directory = spec.working_directory
        child._external_message_source = client.drain_directives
        for tool in tools:
            tool.bind_agent(child)
        child.add_event_handler(
            lambda event: client.send(
                "runtime_event",
                {
                    "event": runtime_event_to_dict(
                        agent_event_to_runtime_event(event)
                    )
                },
            )
        )
        if spec.replay_messages:
            child._replace_context_messages(
                list(spec.replay_messages),
                reason="isolated worker replay",
                record=False,
            )
            child.state.total_prompt_tokens = spec.initial_prompt_tokens
            child.state.total_completion_tokens = spec.initial_completion_tokens
            child.state.total_tool_calls = spec.initial_tool_calls
            child.state.total_model_calls = spec.initial_model_calls
        client.send("ready", {"tool_schema_count": len(tools)})
        if spec.replay_messages:
            child._current_turn_id = f"resume-{spec.worker_generation}"
            for directive in spec.resume_directives:
                child._append_message(
                    {
                        "role": "system",
                        "content": (
                            "[Guidance resolution]\n"
                            f"{directive}\n"
                            "[/Guidance resolution]"
                        ),
                    },
                    source="subagent_guidance",
                )
            result = (
                child._loop.run()
                if child.max_rounds > 0
                else "(sub-agent round budget exhausted)"
            )
        else:
            result = child.chat(spec.delegated_prompt)
        status = (
            "cancelled"
            if cancel.is_set()
            else "blocked"
            if child._park_request is not None
            else "ok"
        )
        if status == "blocked":
            checkpoint_state = {
                "messages": child.messages,
                "prompt_tokens": child.state.total_prompt_tokens,
                "completion_tokens": child.state.total_completion_tokens,
                "tool_calls": child.state.total_tool_calls,
                "guidance_request_id": child._park_request.get(
                    "guidance_request_id"
                ),
                "model_calls": child.state.total_model_calls,
            }
            checkpoint_id = "cp_" + _stable_hash(checkpoint_state)[:16]
            client.send(
                "checkpoint",
                {
                    "checkpoint_id": checkpoint_id,
                    **checkpoint_state,
                },
            )
            if not client.wait_for_park_ack(checkpoint_id):
                raise RuntimeError(
                    f"checkpoint {checkpoint_id} was not durably acknowledged"
                )
        client.send(
            "terminal",
            {
                "status": status,
                "summary": result,
                "messages": child.messages,
                "prompt_tokens": child.state.total_prompt_tokens,
                "completion_tokens": child.state.total_completion_tokens,
                "tool_calls": child.state.total_tool_calls,
                "guidance_request_id": (
                    child._park_request.get("guidance_request_id")
                    if child._park_request is not None
                    else None
                ),
                "model_calls": child.state.total_model_calls,
            },
        )
    except BaseException as error:
        try:
            client.send(
                "terminal",
                {
                    "status": "failed",
                    "summary": f"{type(error).__name__}: {error}",
                    "messages": [],
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                },
            )
        except (EOFError, OSError):
            pass
    finally:
        connection.close()


def run_isolated_worker(
    spec: WorkerSpec,
    broker: ParentToolBroker,
    *,
    cancel_event: threading.Event,
    timeout_seconds: int,
    directive_source: Callable[[], list] | None = None,
    event_sink: Callable[[AgentEvent], None] | None = None,
    checkpoint_sink: (
        Callable[[str, WorkerExecutionResult, dict[str, Any]], bool] | None
    ) = None,
    grace_seconds: float = 0.75,
) -> WorkerExecutionResult:
    """Supervise one spawn worker, broker tools, and enforce hard cancellation."""
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=True)
    process_cancel = context.Event()
    process = context.Process(
        target=worker_process_main,
        args=(spec.to_dict(), child_connection, process_cancel),
        daemon=True,
        name=f"rcoder-{spec.job_id}",
    )
    process.start()
    child_connection.close()
    deadline = time.monotonic() + timeout_seconds
    cancel_started: float | None = None
    parent_sequence = 0
    last_worker_sequence = 0
    terminal: WorkerExecutionResult | None = None
    checkpoint: WorkerExecutionResult | None = None
    tool_results: Queue[tuple[str, ToolOutcome]] = Queue()
    active_tool = False
    active_tool_name: str | None = None
    worker_crash_indeterminate = False
    inflight_directives: dict[str, Any] = {}

    def send(message_type: str, payload: dict[str, Any]) -> None:
        nonlocal parent_sequence
        parent_sequence += 1
        parent_connection.send(
            WorkerEnvelope(
                type=message_type,  # type: ignore[arg-type]
                job_id=spec.job_id,
                agent_id=spec.agent_id,
                session_generation=spec.session_generation,
                worker_generation=spec.worker_generation,
                cancellation_epoch=spec.cancellation_epoch,
                sequence=parent_sequence,
                payload=payload,
            ).to_dict()
        )

    def execute_tool(call_id: str, name: str, arguments: dict[str, Any]) -> None:
        tool_results.put((call_id, broker.execute(call_id, name, arguments)))

    try:
        while process.is_alive() or parent_connection.poll():
            now = time.monotonic()
            timed_out = now >= deadline
            if cancel_event.is_set() or timed_out:
                process_cancel.set()
                if cancel_started is None:
                    cancel_started = now
            if cancel_started is not None and now - cancel_started >= grace_seconds:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=0.25)
                    if process.is_alive() and hasattr(process, "kill"):
                        process.kill()
                break

            if directive_source is not None and cancel_started is None:
                for directive in directive_source():
                    inflight_directives[directive.directive_id] = directive
                    send(
                        "directive",
                        {
                            "directive_id": directive.directive_id,
                            "sender_agent_id": directive.sender_agent_id,
                            "source": directive.source,
                            "content": directive.content,
                        },
                    )

            if active_tool:
                try:
                    call_id, outcome = tool_results.get_nowait()
                except Empty:
                    pass
                else:
                    active_tool = False
                    active_tool_name = None
                    if cancel_started is None:
                        send(
                            "tool_result",
                            {
                                "call_id": call_id,
                                **broker.ipc_tool_result(outcome),
                            },
                        )

            if not parent_connection.poll(0.025):
                continue
            envelope = WorkerEnvelope.from_dict(parent_connection.recv())
            if (
                envelope.job_id != spec.job_id
                or envelope.agent_id != spec.agent_id
                or envelope.session_generation != spec.session_generation
                or envelope.worker_generation != spec.worker_generation
                or envelope.cancellation_epoch != spec.cancellation_epoch
                or envelope.sequence <= last_worker_sequence
            ):
                continue
            last_worker_sequence = envelope.sequence
            if envelope.type == "runtime_event":
                runtime = runtime_event_from_dict(
                    _required_object(envelope.payload.get("event"), "runtime event")
                )
                if event_sink is not None and cancel_started is None:
                    event_sink(runtime_event_to_agent_event(runtime))
            elif envelope.type == "directive_ack":
                directive_ids = envelope.payload.get("directive_ids") or []
                if isinstance(directive_ids, list):
                    for directive_id in directive_ids:
                        inflight_directives.pop(str(directive_id), None)
            elif envelope.type == "tool_request" and not active_tool:
                active_tool = True
                active_tool_name = str(envelope.payload.get("name") or "")
                thread = threading.Thread(
                    target=execute_tool,
                    args=(
                        str(envelope.payload.get("call_id") or ""),
                        str(envelope.payload.get("name") or ""),
                        _required_object(
                            envelope.payload.get("arguments"), "tool arguments"
                        ),
                    ),
                    daemon=True,
                )
                thread.start()
            elif envelope.type == "checkpoint":
                checkpoint_id = str(
                    envelope.payload.get("checkpoint_id") or ""
                )
                if not checkpoint_id:
                    terminal = WorkerExecutionResult(
                        status="failed",
                        summary="Worker checkpoint omitted checkpoint_id.",
                        messages=[],
                    )
                    process_cancel.set()
                    break
                checkpoint = WorkerExecutionResult(
                    status="blocked",
                    summary="Subagent parked awaiting guidance.",
                    messages=list(envelope.payload.get("messages") or []),
                    prompt_tokens=int(envelope.payload.get("prompt_tokens") or 0),
                    completion_tokens=int(
                        envelope.payload.get("completion_tokens") or 0
                    ),
                    tool_calls=int(envelope.payload.get("tool_calls") or 0),
                    guidance_request_id=(
                        str(envelope.payload.get("guidance_request_id"))
                        if envelope.payload.get("guidance_request_id")
                        else None
                    ),
                    model_calls=int(envelope.payload.get("model_calls") or 0),
                )
                for directive in inflight_directives.values():
                    checkpoint.messages.append(
                        {
                            "role": "system",
                            "content": (
                                "[Guidance resolution]\n"
                                f"{directive.model_text()}\n"
                                "[/Guidance resolution]"
                            ),
                        }
                    )
                checkpoint.resume_ready = bool(inflight_directives)
                checkpoint_payload = {
                    **envelope.payload,
                    "embedded_directive_ids": list(inflight_directives),
                }
                inflight_directives.clear()
                try:
                    reference = _persist_worker_checkpoint(
                        spec,
                        checkpoint_id=checkpoint_id,
                        checkpoint=checkpoint,
                    )
                    accepted = (
                        checkpoint_sink(reference, checkpoint, checkpoint_payload)
                        if checkpoint_sink is not None
                        else True
                    )
                    if not accepted:
                        raise RuntimeError("checkpoint sink rejected transition")
                except (OSError, TypeError, ValueError, RuntimeError) as error:
                    terminal = WorkerExecutionResult(
                        status="failed",
                        summary=f"Checkpoint persistence failed: {error}",
                        messages=checkpoint.messages,
                    )
                    process_cancel.set()
                    break
                send("park_ack", {"checkpoint_id": checkpoint_id})
            elif envelope.type == "terminal":
                terminal = WorkerExecutionResult(
                    status=str(envelope.payload.get("status") or "failed"),
                    summary=str(envelope.payload.get("summary") or ""),
                    messages=list(envelope.payload.get("messages") or []),
                    prompt_tokens=int(envelope.payload.get("prompt_tokens") or 0),
                    completion_tokens=int(
                        envelope.payload.get("completion_tokens") or 0
                    ),
                    tool_calls=int(envelope.payload.get("tool_calls") or 0),
                    guidance_request_id=(
                        str(envelope.payload.get("guidance_request_id"))
                        if envelope.payload.get("guidance_request_id")
                        else None
                    ),
                    model_calls=int(envelope.payload.get("model_calls") or 0),
                )
                if terminal.status == "blocked" and checkpoint is not None:
                    terminal.messages = checkpoint.messages
                    terminal.resume_ready = checkpoint.resume_ready
                break
    finally:
        if (
            terminal is None
            and active_tool
            and active_tool_name is not None
            and broker.is_effectful(active_tool_name)
        ):
            worker_crash_indeterminate = True
            cancel_event.set()
        if process.is_alive():
            process_cancel.set()
            process.terminate()
        process.join(timeout=0.5)
        parent_connection.close()

    if terminal is not None and cancel_started is None:
        return terminal
    if worker_crash_indeterminate:
        return WorkerExecutionResult(
            status="indeterminate",
            summary=(
                "Worker exited while effectful tool "
                f"'{active_tool_name}' had no committed outcome; manual inspection required."
            ),
            messages=checkpoint.messages if checkpoint else [],
        )
    if cancel_event.is_set():
        known = terminal or checkpoint
        return WorkerExecutionResult(
            status="killed" if process.exitcode not in {0, None} else "cancelled",
            summary="Subagent interrupted.",
            messages=known.messages if known else [],
            prompt_tokens=known.prompt_tokens if known else spec.initial_prompt_tokens,
            completion_tokens=(
                known.completion_tokens if known else spec.initial_completion_tokens
            ),
            tool_calls=known.tool_calls if known else spec.initial_tool_calls,
            model_calls=known.model_calls if known else spec.initial_model_calls,
            killed=process.exitcode not in {0, None},
            usage_uncertain=True,
        )
    if time.monotonic() >= deadline:
        known = terminal or checkpoint
        return WorkerExecutionResult(
            status="timed_out",
            summary=f"Subagent exceeded timeout after {timeout_seconds}s.",
            messages=known.messages if known else [],
            prompt_tokens=known.prompt_tokens if known else spec.initial_prompt_tokens,
            completion_tokens=(
                known.completion_tokens if known else spec.initial_completion_tokens
            ),
            tool_calls=known.tool_calls if known else spec.initial_tool_calls,
            model_calls=known.model_calls if known else spec.initial_model_calls,
            killed=True,
            usage_uncertain=True,
        )
    return terminal or checkpoint or WorkerExecutionResult(
        status="failed",
        summary=f"Worker exited without terminal frame (exit={process.exitcode}).",
        messages=[],
    )


def _cancelled_outcome(name: str) -> ToolOutcome:
    return ToolOutcome(
        status=ToolOutcomeStatus.CANCELLED,
        content=f"Tool '{name}' cancelled with child worker.",
        error_kind=ToolErrorKind.INTERRUPTED,
    )


def _required_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"{label} must be an object")
    return dict(value)


def _validate_tool_result_ref(
    reference: ToolResultRef, outcome: ToolOutcome
) -> None:
    path = Path(reference.path)
    payload = path.read_bytes()
    if len(payload) != reference.size_bytes:
        raise ValueError("tool result archive size mismatch")
    if hashlib.sha256(payload).hexdigest() != reference.checksum_sha256:
        raise ValueError("tool result archive checksum mismatch")
    if (
        hashlib.sha256(outcome.model_text.encode("utf-8")).hexdigest()
        != reference.model_view_hash
    ):
        raise ValueError("tool result model view checksum mismatch")


def _persist_worker_checkpoint(
    spec: WorkerSpec,
    *,
    checkpoint_id: str,
    checkpoint: WorkerExecutionResult,
) -> str:
    root = Path(spec.working_directory or Path.cwd())
    metadata = {
        "status": "blocked",
        "checkpoint_id": checkpoint_id,
        "session_generation": spec.session_generation,
        "worker_generation": spec.worker_generation,
        "cancellation_epoch": spec.cancellation_epoch,
        "tool_schema_hash": _stable_hash(
            [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                for tool in spec.tools
            ]
        ),
        "model_settings_hash": _stable_hash(spec.llm_kwargs),
        "working_directory": spec.working_directory,
        "usage": {
            "prompt_tokens": checkpoint.prompt_tokens,
            "completion_tokens": checkpoint.completion_tokens,
            "tool_calls": checkpoint.tool_calls,
            "model_calls": checkpoint.model_calls,
        },
        "guidance_request_id": checkpoint.guidance_request_id,
        "tool_adjacency_complete": True,
    }
    return SubagentTranscriptStore(root).write(
        spec.job_id,
        checkpoint.messages,
        metadata,
    )


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
