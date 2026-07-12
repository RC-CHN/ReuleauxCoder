"""Append-only semantic history independent from the bounded model context."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import threading
import time
import uuid
import os
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class HistoryEvent:
    schema_version: int
    seq: int
    event_id: str
    kind: str
    created_at: float
    session_generation: int
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HistoryEvent":
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            seq=int(data.get("seq", 0)),
            event_id=str(data.get("event_id") or f"he_{uuid.uuid4().hex[:12]}"),
            kind=str(data.get("kind") or "unknown"),
            created_at=float(data.get("created_at", 0.0)),
            session_generation=int(data.get("session_generation", 0)),
            payload=dict(data.get("payload") or {}),
        )


class HistoryLedger:
    """Thread-safe in-memory ledger whose persisted form is JSONL."""

    def __init__(
        self,
        events: Iterable[HistoryEvent | dict[str, Any]] = (),
        *,
        generation: int = 0,
        sink_path: str | Path | None = None,
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

    @property
    def events(self) -> tuple[HistoryEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def append(self, kind: str, payload: dict[str, Any]) -> HistoryEvent:
        with self._lock:
            event = HistoryEvent(
                schema_version=1,
                seq=self._next_seq,
                event_id=f"he_{uuid.uuid4().hex[:12]}",
                kind=kind,
                created_at=time.time(),
                session_generation=self._generation,
                payload=json.loads(json.dumps(payload, ensure_ascii=False, default=str)),
            )
            self._next_seq += 1
            self._events.append(event)
            self._append_to_sink(event)
            return event

    def append_message(self, message: dict, *, source: str) -> HistoryEvent:
        canonical_message = {
            key: value
            for key, value in message.items()
            if key != "_rc_token_count"
        }
        return self.append(
            "message_committed", {"source": source, "message": canonical_message}
        )

    def append_context_view(
        self,
        messages: list[dict],
        *,
        reason: str,
        history_version: int,
        checkpoint_id: str | None = None,
    ) -> HistoryEvent:
        return self.append(
            "context_view_committed",
            {
                "reason": reason,
                "history_version": history_version,
                "checkpoint_id": checkpoint_id,
                "items": [
                    {key: value for key, value in item.items() if key != "_rc_token_count"}
                    for item in messages
                ],
            },
        )

    def advance_generation(self, generation: int) -> None:
        with self._lock:
            self._generation = generation

    def bind_jsonl(self, path: str | Path) -> None:
        with self._lock:
            self._sink_path = Path(path)
            self._sink_path.parent.mkdir(parents=True, exist_ok=True)

    def _append_to_sink(self, event: HistoryEvent) -> None:
        if self._sink_path is None:
            return
        self._sink_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(event.to_dict(), ensure_ascii=False) + "\n"
        with self._sink_path.open("a", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
