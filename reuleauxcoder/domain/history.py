"""Append-only semantic history independent from the bounded model context."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
import json
import threading
import time
import uuid
import os
from pathlib import Path
from typing import Any, Iterable

from reuleauxcoder.domain.llm.context_messages import is_synthetic_context_message
from reuleauxcoder.domain.runtime.performance import RuntimePerformanceMonitor


@dataclass(frozen=True, slots=True)
class HistoryEvent:
    schema_version: int
    seq: int
    event_id: str
    kind: str
    created_at: float
    session_generation: int
    payload: dict[str, Any]
    session_id: str | None = None
    agent_id: str | None = None
    parent_agent_id: str | None = None
    job_id: str | None = None
    turn_id: str | None = None
    api_round_id: str | None = None
    role: str | None = None
    artifact_refs: tuple[str, ...] = ()
    supersedes_event_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["timestamp"] = self.created_at
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HistoryEvent":
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            seq=int(data.get("seq", 0)),
            event_id=str(data.get("event_id") or f"he_{uuid.uuid4().hex[:12]}"),
            kind=str(data.get("kind") or "unknown"),
            created_at=float(data.get("timestamp", data.get("created_at", 0.0))),
            session_generation=int(data.get("session_generation", 0)),
            payload=dict(data.get("payload") or {}),
            session_id=_optional_str(data.get("session_id")),
            agent_id=_optional_str(data.get("agent_id")),
            parent_agent_id=_optional_str(data.get("parent_agent_id")),
            job_id=_optional_str(data.get("job_id")),
            turn_id=_optional_str(data.get("turn_id")),
            api_round_id=_optional_str(data.get("api_round_id")),
            role=_optional_str(data.get("role")),
            artifact_refs=tuple(str(item) for item in data.get("artifact_refs", ())),
            supersedes_event_ids=tuple(
                str(item) for item in data.get("supersedes_event_ids", ())
            ),
        )


class HistoryLedger:
    """Thread-safe in-memory ledger whose persisted form is JSONL."""

    def __init__(
        self,
        events: Iterable[HistoryEvent | dict[str, Any]] = (),
        *,
        generation: int = 0,
        sink_path: str | Path | None = None,
        session_id: str | None = None,
        agent_id: str | None = None,
        performance_monitor: RuntimePerformanceMonitor | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._events = [
            event if isinstance(event, HistoryEvent) else HistoryEvent.from_dict(event)
            for event in events
        ]
        self._events.sort(key=lambda event: event.seq)
        self._next_seq = max((event.seq for event in self._events), default=0) + 1
        self._generation = generation
        self._sink_path = Path(sink_path) if sink_path is not None else None
        self._session_id = session_id
        self._agent_id = agent_id
        self._sink_batch_depth = 0
        self._pending_sink_events: list[HistoryEvent] = []
        self._unbound_events: list[HistoryEvent] = []
        self._performance_monitor = performance_monitor

    @property
    def events(self) -> tuple[HistoryEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def append(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        agent_id: str | None = None,
        parent_agent_id: str | None = None,
        job_id: str | None = None,
        turn_id: str | None = None,
        api_round_id: str | None = None,
        role: str | None = None,
        artifact_refs: Iterable[str] = (),
        supersedes_event_ids: Iterable[str] = (),
    ) -> HistoryEvent:
        with self._lock:
            event = HistoryEvent(
                schema_version=2,
                seq=self._next_seq,
                event_id=f"he_{uuid.uuid4().hex[:12]}",
                kind=kind,
                created_at=time.time(),
                session_generation=self._generation,
                payload=json.loads(
                    json.dumps(payload, ensure_ascii=False, default=str)
                ),
                session_id=self._session_id,
                agent_id=agent_id or self._agent_id,
                parent_agent_id=parent_agent_id,
                job_id=job_id,
                turn_id=turn_id,
                api_round_id=api_round_id,
                role=role,
                artifact_refs=tuple(artifact_refs),
                supersedes_event_ids=tuple(supersedes_event_ids),
            )
            self._next_seq += 1
            self._events.append(event)
            self._append_to_sink(event)
            return event

    def append_message(
        self,
        message: dict,
        *,
        source: str,
        agent_id: str | None = None,
        turn_id: str | None = None,
        api_round_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> HistoryEvent:
        canonical_message = {
            key: value for key, value in message.items() if key != "_rc_token_count"
        }
        role = _optional_str(message.get("role"))
        with self._lock:
            with self._batch_sink_writes():
                payload = {"source": source, "message": canonical_message}
                if metadata:
                    payload.update(metadata)
                committed = self.append(
                    "message_committed",
                    payload,
                    agent_id=agent_id,
                    turn_id=turn_id,
                    api_round_id=api_round_id,
                    role=role,
                )
                semantic_kind = _message_semantic_kind(message, source)
                if semantic_kind is not None:
                    self.append(
                        semantic_kind,
                        {
                            "message_event_id": committed.event_id,
                            "source": source,
                            "tool_call_ids": _message_tool_call_ids(message),
                            "tool_call_id": message.get("tool_call_id"),
                        },
                        agent_id=agent_id,
                        turn_id=turn_id,
                        api_round_id=api_round_id,
                        role=role,
                    )
            return committed

    def append_context_view(
        self,
        messages: list[dict],
        *,
        reason: str,
        history_version: int,
        checkpoint_id: str | None = None,
    ) -> HistoryEvent:
        with self._lock:
            with self._batch_sink_writes():
                committed = self.append(
                    "context_view_committed",
                    {
                        "reason": reason,
                        "history_version": history_version,
                        "checkpoint_id": checkpoint_id,
                        "items": [
                            {
                                key: value
                                for key, value in item.items()
                                if key != "_rc_token_count"
                            }
                            for item in messages
                        ],
                    },
                )
                if checkpoint_id is not None:
                    self.append(
                        "context_checkpoint",
                        {
                            "checkpoint_id": checkpoint_id,
                            "context_view_event_id": committed.event_id,
                            "history_version": history_version,
                        },
                        supersedes_event_ids=(committed.event_id,),
                    )
            return committed

    def advance_generation(self, generation: int) -> None:
        with self._lock:
            self._generation = generation

    def bind_jsonl(self, path: str | Path) -> None:
        with self._lock:
            previous_path = self._sink_path
            self._sink_path = Path(path)
            self._sink_path.parent.mkdir(parents=True, exist_ok=True)
            if self._unbound_events:
                pending = tuple(self._unbound_events)
                try:
                    self._write_sink_events(pending)
                except Exception:
                    self._sink_path = previous_path
                    raise
                else:
                    del self._unbound_events[: len(pending)]

    def unbind_jsonl(self) -> None:
        """Stop appending events to the previously bound session ledger."""
        with self._lock:
            self._sink_path = None
            self._pending_sink_events.clear()

    def bind_context(
        self, *, session_id: str | None = None, agent_id: str | None = None
    ) -> None:
        """Bind stable event attribution before live writes begin."""
        with self._lock:
            if session_id is not None:
                self._session_id = session_id
            if agent_id is not None:
                self._agent_id = agent_id

    def set_performance_monitor(
        self, monitor: RuntimePerformanceMonitor | None
    ) -> None:
        with self._lock:
            self._performance_monitor = monitor

    def _append_to_sink(self, event: HistoryEvent) -> None:
        if self._sink_path is None:
            self._unbound_events.append(event)
            return
        if self._sink_batch_depth:
            self._pending_sink_events.append(event)
            return
        self._write_sink_events((event,))

    def _write_sink_events(self, events: tuple[HistoryEvent, ...]) -> None:
        if self._sink_path is None or not events:
            return
        started = time.monotonic()
        encode_ms = 0.0
        fsync_ms = 0.0
        encoded_bytes = 0
        status = "ok"
        try:
            encode_started = time.monotonic()
            encoded = "".join(
                json.dumps(event.to_dict(), ensure_ascii=False) + "\n"
                for event in events
            )
            encoded_data = encoded.encode("utf-8")
            encode_ms = (time.monotonic() - encode_started) * 1000
            encoded_bytes = len(encoded_data)
            self._sink_path.parent.mkdir(parents=True, exist_ok=True)
            with self._sink_path.open("ab") as stream:
                stream.write(encoded_data)
                stream.flush()
                fsync_started = time.monotonic()
                os.fsync(stream.fileno())
                fsync_ms = (time.monotonic() - fsync_started) * 1000
        except BaseException:
            status = "error"
            raise
        finally:
            monitor = self._performance_monitor
            if monitor is not None:
                monitor.record(
                    "persistence",
                    "history_ledger_write",
                    (time.monotonic() - started) * 1000,
                    status=status,
                    attributes={
                        "event_count": len(events),
                        "encoded_bytes": encoded_bytes,
                        "encode_ms": round(encode_ms, 3),
                        "fsync_ms": round(fsync_ms, 3),
                    },
                )

    @contextmanager
    def _batch_sink_writes(self):
        self._sink_batch_depth += 1
        try:
            yield
        finally:
            self._sink_batch_depth -= 1
            if self._sink_batch_depth == 0 and self._pending_sink_events:
                pending = tuple(self._pending_sink_events)
                self._write_sink_events(pending)
                del self._pending_sink_events[: len(pending)]


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _message_semantic_kind(message: dict, source: str) -> str | None:
    role = message.get("role")
    if source in {"subagent_result", "subagent_deferred"}:
        return "subagent_result"
    if source == "subagent_communication":
        return "subagent_communication"
    if source == "subagent_conflict":
        return "attention_raised"
    if source in {"session_exit", "session_resume"} or str(
        message.get("content") or ""
    ).startswith("[SESSION_"):
        return "session_lifecycle"
    if role == "user" and not is_synthetic_context_message(message):
        return "user_message"
    if role == "assistant" and message.get("tool_calls"):
        return "tool_call"
    if role == "assistant":
        return "assistant_response"
    if role == "tool":
        return "tool_result"
    return None


def _message_tool_call_ids(message: dict) -> list[str]:
    result: list[str] = []
    for call in message.get("tool_calls") or ():
        if isinstance(call, dict) and call.get("id"):
            result.append(str(call["id"]))
    return result
