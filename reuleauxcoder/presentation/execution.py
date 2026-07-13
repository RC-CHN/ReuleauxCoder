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
    budget: str = ""
    blocker: str | None = None
    child_agent_id: str | None = None
    is_subagent: bool = False

    def is_animating(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) < self.animation_lease_until


@dataclass(frozen=True, slots=True)
class AttentionItem:
    request_id: str
    title: str
    source_agent_id: str | None
    preview: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionPanelAgent:
    """Stable semantic agent row; terminal renderers choose its styling."""

    label: str
    status: str
    task: str
    activity: str
    budget: str
    marker: str
    is_subagent: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionPanelView:
    """Framework-neutral snapshot for compact and expanded execution panels."""

    phase: str
    plan_completed: int
    plan_total: int
    runtime_state: str
    is_live: bool
    active_plan: str
    plan: tuple[ExecutionPlanItem, ...]
    main: ExecutionPanelAgent
    subagents: tuple[ExecutionPanelAgent, ...]
    attention: tuple[AttentionItem, ...]
    progress_summary: str
    progress_next: str | None


@dataclass(slots=True)
class ExecutionViewState:
    phase: str = "investigating"
    plan_revision: int = 0
    plan: tuple[ExecutionPlanItem, ...] = ()
    plan_explanation: str | None = None
    plan_updated_at: float = 0.0
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
            self.state.plan_updated_at = event.timestamp
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
            self._touch(
                event,
                _tool_activity(payload.tool_name, payload.arguments),
                status="tool",
            )
            return True
        if isinstance(payload, ToolOutputDelta):
            agent = self._touch(event, "tool output", status="tool")
            for line in payload.text.splitlines():
                agent.output_tail.append(line)
            return True
        if isinstance(payload, ToolCallFinished):
            status = "working" if payload.outcome.success else "failed"
            self._touch(
                event,
                f"{payload.tool_name} {'done' if payload.outcome.success else 'failed'}",
                status=status,
            )
            return True
        if isinstance(payload, SubagentJobChanged):
            is_new_job = payload.job_id not in self.state.agents
            if is_new_job and payload.status in {"queued", "running"}:
                self._remove_terminal_subagents()

            agent = self.state.agents.get(payload.job_id)
            child_agent_id = payload.child_agent_id or f"sa_{payload.job_id}"
            duplicate = self.state.agents.pop(child_agent_id, None)
            if agent is None and duplicate is not None:
                agent = duplicate
            if agent is None:
                agent = ExecutionAgentState(
                    agent_id=payload.job_id,
                    label=_short_agent_label(payload.job_id),
                )
            agent.agent_id = payload.job_id
            agent.label = _short_agent_label(payload.job_id)
            agent.child_agent_id = child_agent_id
            agent.is_subagent = True
            self.state.agents[payload.job_id] = agent
            agent.task = payload.task
            agent.status = payload.status
            agent.activity = (
                (f"running {payload.current_tool}" if payload.current_tool else None)
                or payload.activity
                or payload.error
                or _subagent_activity(payload.status)
            )
            agent.budget = _budget_text(payload)
            agent.blocker = payload.blocker
            agent.last_activity_at = event.timestamp
            agent.animation_lease_until = event.timestamp + self.animation_lease_seconds
            attention_id = f"job:{payload.job_id}"
            if payload.status in {
                "failed",
                "stale",
                "blocked",
                "killed",
                "timed_out",
                "indeterminate",
            }:
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
        if (
            isinstance(payload, NotificationRaised)
            and payload.code == "subagent.conflict"
        ):
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
        state_key = self._state_key_for_agent(agent_id)
        agent = self.state.agents.get(state_key)
        if agent is None:
            agent = ExecutionAgentState(
                agent_id=agent_id,
                label=(
                    "MAIN"
                    if agent_id in {"main", self.root_agent_id}
                    else _short_agent_label(agent_id)
                ),
            )
            self.state.agents[state_key] = agent
        agent.activity = activity
        agent.status = status
        agent.last_activity_at = event.timestamp
        agent.animation_lease_until = event.timestamp + self.animation_lease_seconds
        return agent

    def _state_key_for_agent(self, agent_id: str) -> str:
        for key, agent in self.state.agents.items():
            if agent.child_agent_id == agent_id:
                return key
        if agent_id.startswith("sa_") and agent_id[3:] in self.state.agents:
            return agent_id[3:]
        return agent_id

    def _remove_terminal_subagents(self) -> None:
        terminal = {
            "completed",
            "succeeded",
            "failed",
            "cancelled",
            "killed",
            "timed_out",
            "indeterminate",
            "stale",
        }
        retired = [
            key
            for key, agent in self.state.agents.items()
            if agent.is_subagent and agent.status in terminal
        ]
        for key in retired:
            self.state.agents.pop(key, None)
            self.state.attention.pop(f"job:{key}", None)

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


def execution_panel_view(
    state: ExecutionViewState,
    *,
    now: float | None = None,
) -> ExecutionPanelView:
    """Project runtime facts without committing to a terminal layout."""
    now = time.time() if now is None else now
    active = state.active_plan_item
    projected = tuple(_panel_agent(agent, now=now) for agent in state.agents.values())
    main = next(
        (agent for agent in projected if not agent.is_subagent),
        ExecutionPanelAgent(
            label="MAIN",
            status=state.runtime_state,
            task="",
            activity=state.progress_summary or "ready",
            budget="",
            marker="○",
        ),
    )
    subagents = tuple(agent for agent in projected if agent.is_subagent)
    active_statuses = {
        "thinking",
        "tool",
        "working",
        "running",
        "queued",
        "waiting_approval",
        "cancelling",
    }
    return ExecutionPanelView(
        phase=state.phase.upper(),
        plan_completed=state.completed_plan_items,
        plan_total=len(state.plan),
        runtime_state=state.runtime_state,
        is_live=(
            state.runtime_state != "idle"
            or main.status in active_statuses
            or any(agent.status in active_statuses for agent in subagents)
        ),
        active_plan=active.active_form if active else "no active step",
        plan=state.plan,
        main=main,
        subagents=subagents,
        attention=tuple(state.attention.values()),
        progress_summary=state.progress_summary,
        progress_next=state.progress_next,
    )


def execution_panel_lines(
    state: ExecutionViewState,
    *,
    width: int,
    now: float | None = None,
    expanded: bool = False,
) -> tuple[str, ...]:
    """Compatibility/plain-text projection backed by the structured view."""
    width = max(20, width)
    view = execution_panel_view(state, now=now)
    plan_count = (
        f"{view.plan_completed}/{view.plan_total}" if view.plan_total else "—"
    )
    live = "LIVE" if view.is_live else "IDLE"
    need = f" · NEED {len(view.attention)}" if view.attention else ""
    if width < 60:
        final = (
            f"NEED  ! {view.attention[0].title}"
            if view.attention
            else _agent_line(view.subagents[0])
            if view.subagents
            else f"MAIN  {view.main.marker} {view.main.activity or 'ready'}"
        )
        return (
            _fit(f"{view.phase} · PLAN {plan_count} · A {len(view.subagents)}{need}", width),
            _fit(f"PLAN  {'●' if view.plan_total else '○'} {view.active_plan}", width),
            _fit(final, width),
        )

    lines = [
        _fit(
            f"STATUS  {view.phase} · PLAN {plan_count} · "
            f"AGENTS {len(view.subagents)} · {live}{need}",
            width,
        ),
        _fit(f"PLAN  {'●' if view.plan_total else '○'} {view.active_plan}", width),
        _fit(f"MAIN  {view.main.marker} {view.main.activity or 'ready'}", width),
    ]
    if view.attention:
        child = (
            f" · SUB {_agent_line(view.subagents[0])}" if view.subagents else ""
        )
        lines.append(_fit(f"NEED  ! {view.attention[0].title}{child}", width))
    elif view.subagents:
        lines.append(_fit(f"SUB   {_agent_line(view.subagents[0])}", width))
    else:
        next_step = view.progress_next or view.progress_summary or "ready"
        lines.append(_fit(f"NEXT  {next_step}", width))

    if expanded:
        for item in view.plan:
            marker = {
                "completed": "✓",
                "in_progress": "●",
                "pending": "○",
            }.get(item.status, "○")
            label = item.active_form if item.status == "in_progress" else item.step
            lines.append(_fit(f"PLAN  {marker} {label}", width))
        for agent in view.subagents:
            lines.append(_fit(f"SUB   {_agent_line(agent)}", width))
    return tuple(lines)


def _panel_agent(agent: ExecutionAgentState, *, now: float) -> ExecutionPanelAgent:
    marker = (
        ("◐", "◓", "◑", "◒")[int(now * 8) % 4]
        if agent.is_animating(now)
        else (
            "✓"
            if agent.status in {"completed", "succeeded"}
            else "!"
            if agent.status
            in {"failed", "blocked", "stale", "indeterminate", "timed_out"}
            else "○"
        )
    )
    return ExecutionPanelAgent(
        label=agent.label,
        status=agent.status,
        task=agent.task,
        activity=agent.activity,
        budget=agent.budget,
        marker=marker,
        is_subagent=agent.is_subagent,
    )


def _agent_line(agent: ExecutionPanelAgent) -> str:
    task = agent.task or "working"
    activity = f" · {agent.activity}" if agent.activity else ""
    budget = f" · {agent.budget}" if agent.budget else ""
    return f"{agent.marker} {agent.label}  {task}{activity}{budget}"


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


def _budget_text(payload: SubagentJobChanged) -> str:
    tools = f"tools {payload.tool_calls}"
    if payload.max_tool_calls is not None:
        tools += f"/{payload.max_tool_calls}"
    tokens = f"tok {payload.tokens}"
    if payload.max_tokens is not None:
        tokens += f"/{payload.max_tokens}"
    return f"{tools} · {tokens}"


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
