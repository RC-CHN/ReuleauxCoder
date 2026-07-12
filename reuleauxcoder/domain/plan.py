"""Authoritative agent-scoped execution checklist and semantic progress."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import threading
from typing import Literal

PlanStatus = Literal["pending", "in_progress", "completed"]
ProgressPhase = Literal[
    "investigating", "implementing", "verifying", "ready", "blocked"
]


@dataclass(frozen=True, slots=True)
class PlanItem:
    step: str
    active_form: str
    status: PlanStatus


@dataclass(frozen=True, slots=True)
class PlanState:
    owner_agent_id: str
    session_generation: int
    revision: int = 0
    items: tuple[PlanItem, ...] = ()
    explanation: str | None = None
    event_id: str | None = None

    @property
    def completed(self) -> int:
        return sum(item.status == "completed" for item in self.items)

    @property
    def active_index(self) -> int | None:
        return next(
            (index for index, item in enumerate(self.items) if item.status == "in_progress"),
            None,
        )

    def to_dict(self) -> dict:
        return {
            "owner_agent_id": self.owner_agent_id,
            "session_generation": self.session_generation,
            "revision": self.revision,
            "items": [asdict(item) for item in self.items],
            "explanation": self.explanation,
            "event_id": self.event_id,
        }


@dataclass(frozen=True, slots=True)
class ProgressState:
    phase: ProgressPhase = "investigating"
    summary: str = ""
    next: str | None = None
    revision: int = 0
    event_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class PlanController:
    MAX_ITEMS = 20
    MAX_ITEM_CHARS = 240
    MAX_SERIALIZED_CHARS = 5_000

    def __init__(self, agent) -> None:
        self._agent = agent
        self._lock = threading.RLock()
        self._state = PlanState(
            owner_agent_id=agent.agent_id,
            session_generation=agent.session_generation,
        )
        self._progress = ProgressState()
        self._committed_calls: dict[str, tuple[str, int]] = {}

    @property
    def state(self) -> PlanState:
        with self._lock:
            return self._state

    @property
    def progress(self) -> ProgressState:
        with self._lock:
            return self._progress

    def update(
        self,
        raw_items: list[dict],
        *,
        explanation: str | None,
        tool_call_id: str,
        session_generation: int,
    ) -> tuple[PlanState, bool]:
        items = self._validate_items(raw_items)
        explanation = (explanation or "").strip()[:500] or None
        fingerprint = self._fingerprint(items, explanation)
        with self._lock:
            cached = self._committed_calls.get(tool_call_id)
            if cached is not None:
                if cached[0] != fingerprint:
                    raise ValueError("tool_call_id was already committed with different plan data")
                return self._state, False
            if session_generation != self._agent.session_generation:
                raise ValueError("session generation changed before plan commit")
            revision = self._state.revision + 1
            payload = {
                "owner_agent_id": self._agent.agent_id,
                "session_generation": session_generation,
                "revision": revision,
                "items": [asdict(item) for item in items],
                "explanation": explanation,
                "tool_call_id": tool_call_id,
            }
            event = self._agent.history_ledger.append("plan_updated", payload)
            self._state = PlanState(
                owner_agent_id=self._agent.agent_id,
                session_generation=session_generation,
                revision=revision,
                items=items,
                explanation=explanation,
                event_id=event.event_id,
            )
            self._committed_calls[tool_call_id] = (fingerprint, revision)
            self._agent.persist_runtime_snapshot()
            return self._state, True

    def report(
        self,
        *,
        phase: str,
        summary: str,
        next_step: str | None,
        tool_call_id: str,
        session_generation: int,
    ) -> tuple[ProgressState, bool]:
        if phase not in {
            "investigating",
            "implementing",
            "verifying",
            "ready",
            "blocked",
        }:
            raise ValueError("invalid progress phase")
        summary = summary.strip()
        if not summary or len(summary) > 500:
            raise ValueError("summary must contain 1-500 characters")
        next_step = (next_step or "").strip()[:500] or None
        fingerprint = json.dumps(
            [phase, summary, next_step], ensure_ascii=False, separators=(",", ":")
        )
        with self._lock:
            cached = self._committed_calls.get(tool_call_id)
            if cached is not None:
                if cached[0] != fingerprint:
                    raise ValueError("tool_call_id was already committed with different progress data")
                return self._progress, False
            if session_generation != self._agent.session_generation:
                raise ValueError("session generation changed before progress commit")
            revision = self._progress.revision + 1
            event = self._agent.history_ledger.append(
                "progress_reported",
                {
                    "owner_agent_id": self._agent.agent_id,
                    "session_generation": session_generation,
                    "revision": revision,
                    "phase": phase,
                    "summary": summary,
                    "next": next_step,
                    "tool_call_id": tool_call_id,
                },
            )
            self._progress = ProgressState(
                phase=phase,  # type: ignore[arg-type]
                summary=summary,
                next=next_step,
                revision=revision,
                event_id=event.event_id,
            )
            self._committed_calls[tool_call_id] = (fingerprint, revision)
            self._agent.persist_runtime_snapshot()
            return self._progress, True

    def restore(self, plan: dict | None, progress: dict | None) -> None:
        with self._lock:
            if plan:
                items = self._validate_items(list(plan.get("items") or []))
                self._state = PlanState(
                    owner_agent_id=self._agent.agent_id,
                    session_generation=self._agent.session_generation,
                    revision=max(0, int(plan.get("revision", 0))),
                    items=items,
                    explanation=plan.get("explanation"),
                    event_id=plan.get("event_id"),
                )
            else:
                self._state = PlanState(
                    owner_agent_id=self._agent.agent_id,
                    session_generation=self._agent.session_generation,
                )
            if progress:
                phase = str(progress.get("phase") or "investigating")
                if phase not in {
                    "investigating",
                    "implementing",
                    "verifying",
                    "ready",
                    "blocked",
                }:
                    phase = "investigating"
                self._progress = ProgressState(
                    phase=phase,  # type: ignore[arg-type]
                    summary=str(progress.get("summary") or "")[:500],
                    next=str(progress.get("next") or "")[:500] or None,
                    revision=max(0, int(progress.get("revision", 0))),
                    event_id=progress.get("event_id"),
                )
            else:
                self._progress = ProgressState()
            self._committed_calls.clear()

    def reset(self) -> None:
        self.restore(None, None)

    @classmethod
    def _validate_items(cls, raw_items: list[dict]) -> tuple[PlanItem, ...]:
        if not isinstance(raw_items, list) or len(raw_items) > cls.MAX_ITEMS:
            raise ValueError(f"plan must contain at most {cls.MAX_ITEMS} items")
        items: list[PlanItem] = []
        active = 0
        for index, raw in enumerate(raw_items):
            if not isinstance(raw, dict):
                raise ValueError(f"plan[{index}] must be an object")
            step = str(raw.get("step") or "").strip()
            active_form = str(raw.get("active_form") or step).strip()
            status = str(raw.get("status") or "")
            if not step or len(step) > cls.MAX_ITEM_CHARS:
                raise ValueError(f"plan[{index}].step must contain 1-{cls.MAX_ITEM_CHARS} characters")
            if not active_form or len(active_form) > cls.MAX_ITEM_CHARS:
                raise ValueError(
                    f"plan[{index}].active_form must contain 1-{cls.MAX_ITEM_CHARS} characters"
                )
            if status not in {"pending", "in_progress", "completed"}:
                raise ValueError(
                    f"plan[{index}].status must be pending, in_progress, or completed"
                )
            active += status == "in_progress"
            items.append(PlanItem(step, active_form, status))  # type: ignore[arg-type]
        if active > 1:
            raise ValueError("plan may contain at most one in_progress item")
        encoded = json.dumps(
            [asdict(item) for item in items], ensure_ascii=False, separators=(",", ":")
        )
        if len(encoded) > cls.MAX_SERIALIZED_CHARS:
            raise ValueError("serialized plan exceeds 5000 characters")
        return tuple(items)

    @staticmethod
    def _fingerprint(items: tuple[PlanItem, ...], explanation: str | None) -> str:
        return json.dumps(
            {"items": [asdict(item) for item in items], "explanation": explanation},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
