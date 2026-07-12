"""Framework-neutral projection for the persistent execution panel."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import time

from reuleauxcoder.domain.runtime.events import (
    ApprovalRequested,
    ApprovalResolved,
    AssistantContentDelta,
    ChatCompleted,
    ChatStarted,
    ErrorOccurred,
    PlanUpdated,
    ProgressReported,
    NotificationRaised,
    ReasoningDelta,
    RuntimeEvent,
    RuntimeStateChanged,
    StreamChunk,
    SubagentJobChanged,
    ToolCallFinished,
    ToolCallStarted,
    ToolOutputDelta,
    TurnFinished,
    TurnStarted,
)


@dataclass(frozen=True, slots=True)
class ExecutionPlanItem:
    step: str
    active_form: str
    status: str


@dataclass(slots=True)
class ExecutionAgentState:
    agent_id: str
    label: str
    task: str = ""
    status: str = "idle"
    activity: str = ""
    last_activity_at: float | None = None
    animation_lease_until: float = 0.0
    output_tail: deque[str] = field(default_factory=lambda: deque(maxlen=5))

    def is_animating(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) < self.animation_lease_until


@dataclass(frozen=True, slots=True)
class AttentionItem:
    request_id: str
    title: str
    source_agent_id: str | None
    preview: str | None = None


@dataclass(slots=True)
class ExecutionViewState:
    phase: str = "investigating"
    plan_revision: int = 0
    plan: tuple[ExecutionPlanItem, ...] = ()
    plan_explanation: str | None = None
    progress_summary: str = ""
    progress_next: str | None = None
    progress_revision: int = 0
    agents: dict[str, ExecutionAgentState] = field(default_factory=dict)
    attention: dict[str, AttentionItem] = field(default_factory=dict)
    runtime_state: str = "idle"
    seen_event_ids: set[str] = field(default_factory=set)
    session_generations: dict[tuple[str, str | None], int] = field(default_factory=dict)

    @property
    def completed_plan_items(self) -> int:
        return sum(item.status == "completed" for item in self.plan)

    @property
    def active_plan_item(self) -> ExecutionPlanItem | None:
        return next((item for item in self.plan if item.status == "in_progress"), None)


class ExecutionViewReducer:
    """Reduce runtime facts into a source-backed execution status model.

    Animation is a short lease renewed only by real runtime events.  A quiet
    lease expiry deliberately does not infer that a worker is stale.
    """

    def __init__(
        self,
        state: ExecutionViewState | None = None,
        *,
        animation_lease_seconds: float = 0.8,
        root_agent_id: str | None = None,
    ) -> None:
        self.state = state or ExecutionViewState()
        self.animation_lease_seconds = animation_lease_seconds
        self.root_agent_id = root_agent_id

    def apply(self, event: RuntimeEvent) -> bool:
        if event.event_id in self.state.seen_event_ids:
            return False
        self.state.seen_event_ids.add(event.event_id)
        if self._is_stale_generation(event):
            return False

        payload = event.payload
        if isinstance(payload, PlanUpdated):
            if payload.revision < self.state.plan_revision:
                return False
            self.state.plan_revision = payload.revision
            self.state.plan = tuple(
                ExecutionPlanItem(
                    step=str(item.get("step", "")),
                    active_form=str(item.get("active_form") or item.get("step", "")),
                    status=str(item.get("status", "pending")),
                )
                for item in payload.items
            )
            self.state.plan_explanation = payload.explanation
            return True
        if isinstance(payload, ProgressReported):
            if payload.revision < self.state.progress_revision:
                return False
            self.state.progress_revision = payload.revision
            self.state.phase = payload.phase
            self.state.progress_summary = payload.summary
            self.state.progress_next = payload.next
            self._touch(event, payload.summary, status="working")
            return True
        if isinstance(payload, (TurnStarted, ChatStarted)):
            self.state.runtime_state = "running"
            self._touch(event, "thinking", status="thinking")
            return True
        if isinstance(payload, (AssistantContentDelta, ReasoningDelta, StreamChunk)):
            self._touch(event, "thinking", status="thinking")
            return True
        if isinstance(payload, ToolCallStarted):
            self._touch(event, _tool_activity(payload.tool_name, payload.arguments), status="tool")
            return True
        if isinstance(payload, ToolOutputDelta):
            agent = self._touch(event, "tool output", status="tool")
            for line in payload.text.splitlines():
                agent.output_tail.append(line)
            return True
        if isinstance(payload, ToolCallFinished):
            status = "working" if payload.outcome.success else "failed"
            self._touch(event, f"{payload.tool_name} {'done' if payload.outcome.success else 'failed'}", status=status)
            return True
        if isinstance(payload, SubagentJobChanged):
            agent = self.state.agents.get(payload.job_id)
            if agent is None:
                agent = ExecutionAgentState(
                    agent_id=payload.job_id,
                    label=_short_agent_label(payload.job_id),
                )
                self.state.agents[payload.job_id] = agent
            agent.task = payload.task
            agent.status = payload.status
            agent.activity = payload.error or _subagent_activity(payload.status)
            agent.last_activity_at = event.timestamp
            agent.animation_lease_until = event.timestamp + self.animation_lease_seconds
            attention_id = f"job:{payload.job_id}"
            if payload.status in {"failed", "stale", "blocked", "timed_out_detached"}:
                self.state.attention[attention_id] = AttentionItem(
                    request_id=attention_id,
                    title=f"{agent.label}: {payload.error or payload.status}",
                    source_agent_id=event.agent_id,
                )
            elif payload.status in {"completed", "cancelled"}:
                self.state.attention.pop(attention_id, None)
            return True
        if isinstance(payload, ApprovalRequested):
            self.state.attention[payload.request_id] = AttentionItem(
                request_id=payload.request_id,
                title=payload.title,
                source_agent_id=event.agent_id,
                preview=payload.preview,
            )
            self._touch(event, "waiting for approval", status="waiting_approval")
            return True
        if isinstance(payload, ApprovalResolved):
            self.state.attention.pop(payload.request_id, None)
            self._touch(event, "approval resolved", status="working")
            return True
        if isinstance(payload, (TurnFinished, ChatCompleted)):
            self.state.runtime_state = "idle"
            self._touch(event, "ready", status="idle")
            return True
        if isinstance(payload, ErrorOccurred):
            self.state.runtime_state = "failed"
            self._touch(event, payload.message, status="failed")
            return True
        if isinstance(payload, NotificationRaised) and payload.code == "subagent.conflict":
            attention_id = f"notice:{event.event_id}"
            self.state.attention[attention_id] = AttentionItem(
                request_id=attention_id,
                title=payload.message,
                source_agent_id=event.agent_id,
            )
            return True
        if isinstance(payload, RuntimeStateChanged):
            self.state.runtime_state = payload.state
            self._touch(event, payload.reason or payload.state, status=payload.state)
            return True
        return False

    def _touch(
        self,
        event: RuntimeEvent,
        activity: str,
        *,
        status: str,
    ) -> ExecutionAgentState:
        agent_id = event.agent_id or "main"
        agent = self.state.agents.get(agent_id)
        if agent is None:
            agent = ExecutionAgentState(
                agent_id=agent_id,
                label=(
                    "MAIN"
                    if agent_id in {"main", self.root_agent_id}
                    else _short_agent_label(agent_id)
                ),
            )
            self.state.agents[agent_id] = agent
        agent.activity = activity
        agent.status = status
        agent.last_activity_at = event.timestamp
        agent.animation_lease_until = event.timestamp + self.animation_lease_seconds
        return agent

    def _is_stale_generation(self, event: RuntimeEvent) -> bool:
        if event.agent_id is None or event.session_generation is None:
            return False
        key = (event.agent_id, event.session_id)
        current = self.state.session_generations.get(key)
        if current is not None and event.session_generation < current:
            return True
        if current is None or event.session_generation > current:
            self.state.session_generations[key] = event.session_generation
        return False


def execution_panel_lines(
    state: ExecutionViewState,
    *,
    width: int,
    now: float | None = None,
) -> tuple[str, ...]:
    """Project execution state to width-aware plain lines for any terminal UI."""
    width = max(20, width)
    now = time.time() if now is None else now
    phase = state.phase.upper()
    plan_count = f"{state.completed_plan_items}/{len(state.plan)}" if state.plan else "—"
    attention = " · NEEDS YOU" if state.attention else ""
    header = _fit(f"FORGE · {phase} · PLAN {plan_count}{attention}", width)
    active = state.active_plan_item
    plan_line = f"PLAN  {'● ' + active.active_form if active else '○ no active step'}"

    active_agents = [
        agent
        for agent in state.agents.values()
        if agent.status not in {"idle", "completed", "succeeded"}
    ]
    if width < 60:
        activity = active_agents[0].activity if active_agents else state.progress_summary
        return (header, _fit(plan_line, width), _fit(activity or "ready", width))

    lines = [header, _fit(plan_line, width)]
    for index, agent in enumerate(active_agents[:4]):
        marker = "◉" if agent.is_animating(now) else ("!" if agent.status in {"failed", "blocked", "stale"} else "○")
        branch = "├─" if index < len(active_agents[:4]) - 1 else "└─"
        task = agent.task or agent.activity or "working"
        lines.append(_fit(f"{branch} {marker} {agent.label}  {task}", width))
        if agent.output_tail:
            lines.append(_fit(f"   └ {agent.output_tail[-1]}", width))
    if state.attention:
        first = next(iter(state.attention.values()))
        lines.append(_fit(f"! {first.title}", width))
    return tuple(lines)


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def _short_agent_label(agent_id: str) -> str:
    compact = agent_id.replace("subagent-", "").replace("job-", "")
    return compact[:8].upper() or "AGENT"


def _subagent_activity(status: str) -> str:
    return {
        "completed": "complete",
        "failed": "failed",
        "cancelled": "cancelled",
        "running": "working",
        "queued": "queued",
    }.get(status, status)


def _tool_activity(name: str, arguments: dict) -> str:
    target = next(
        (
            str(arguments[key])
            for key in ("file_path", "path", "pattern", "command", "cmd")
            if arguments.get(key)
        ),
        "",
    )
    target = target.replace("\n", " ")[:80]
    return f"{name} {target}".rstrip()
