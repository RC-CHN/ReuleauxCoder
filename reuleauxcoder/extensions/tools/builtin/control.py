"""Agent-scoped Plan and semantic progress control tools."""

from __future__ import annotations

import threading

from reuleauxcoder.domain.agent.tool_outcome import ToolErrorKind, ToolOutcome, ToolOutcomeStatus
from reuleauxcoder.extensions.tools.backend import LocalToolBackend, ToolBackend
from reuleauxcoder.extensions.tools.base import Tool, backend_handler
from reuleauxcoder.extensions.tools.registry import register_tool


class _AgentControlTool(Tool):
    effect_class = "control_plane_internal"

    def __init__(self, backend: ToolBackend | None = None):
        super().__init__(backend or LocalToolBackend())
        self._agent = None
        self._execution = threading.local()

    def bind_agent(self, agent) -> None:
        self._agent = agent

    def bind_execution(self, *, tool_call_id: str, session_generation: int) -> None:
        self._execution.tool_call_id = tool_call_id
        self._execution.session_generation = session_generation

    def _identity(self) -> tuple[str, int]:
        tool_call_id = getattr(self._execution, "tool_call_id", None)
        generation = getattr(self._execution, "session_generation", None)
        if self._agent is None or not tool_call_id or generation is None:
            raise RuntimeError("control tool is not bound to an agent execution")
        return str(tool_call_id), int(generation)


@register_tool
class UpdatePlanTool(_AgentControlTool):
    name = "update_plan"
    description = (
        "Replace this agent's complete execution checklist. Use only when the plan "
        "changes semantically; at most one item may be in_progress."
    )
    parameters = {
        "type": "object",
        "properties": {
            "explanation": {"type": "string"},
            "plan": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "string"},
                        "active_form": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["step", "status"],
                },
            },
        },
        "required": ["plan"],
    }

    def execute(
        self, plan: list[dict], explanation: str | None = None
    ) -> ToolOutcome:
        return self.run_backend(plan=plan, explanation=explanation)

    @backend_handler("local")
    def _execute_local(
        self, plan: list[dict], explanation: str | None = None
    ) -> ToolOutcome:
        try:
            tool_call_id, generation = self._identity()
            state, changed = self._agent.plan_controller.update(
                plan,
                explanation=explanation,
                tool_call_id=tool_call_id,
                session_generation=generation,
            )
            active = state.active_index
            suffix = (
                f" · step {active + 1} active" if active is not None else ""
            )
            verb = "updated" if changed else "unchanged"
            return ToolOutcome(
                summary=f"Plan {verb}",
                content=(
                    f"Plan {verb} · {state.completed}/{len(state.items)} completed"
                    f"{suffix} · revision {state.revision}"
                ),
                metadata={"plan_revision": state.revision, "changed": changed},
            )
        except (TypeError, ValueError, RuntimeError) as error:
            return _invalid(str(error))


@register_tool
class ReportProgressTool(_AgentControlTool):
    name = "report_progress"
    description = (
        "Update the human-readable execution phase at a meaningful boundary. "
        "Do not call before every tool or atomic action."
    )
    parameters = {
        "type": "object",
        "properties": {
            "phase": {
                "type": "string",
                "enum": [
                    "investigating",
                    "implementing",
                    "verifying",
                    "ready",
                    "blocked",
                ],
            },
            "summary": {"type": "string"},
            "next": {"type": "string"},
        },
        "required": ["phase", "summary"],
    }

    def execute(
        self, phase: str, summary: str, next: str | None = None
    ) -> ToolOutcome:
        return self.run_backend(phase=phase, summary=summary, next=next)

    @backend_handler("local")
    def _execute_local(
        self, phase: str, summary: str, next: str | None = None
    ) -> ToolOutcome:
        try:
            tool_call_id, generation = self._identity()
            state, changed = self._agent.plan_controller.report(
                phase=phase,
                summary=summary,
                next_step=next,
                tool_call_id=tool_call_id,
                session_generation=generation,
            )
            if phase == "blocked" and getattr(self._agent, "subagent_depth", 0) > 0:
                manager = getattr(self._agent, "_subagent_manager", None)
                if manager is not None:
                    manager.send_to_parent(
                        self._agent.agent_id, summary, kind="blocked"
                    )
            verb = "updated" if changed else "unchanged"
            return ToolOutcome(
                summary=f"Progress {verb}",
                content=f"Progress {verb} · {state.phase} · revision {state.revision}",
                metadata={"progress_revision": state.revision, "changed": changed},
            )
        except (TypeError, ValueError, RuntimeError) as error:
            return _invalid(str(error))


def _invalid(message: str) -> ToolOutcome:
    return ToolOutcome(
        status=ToolOutcomeStatus.FAILED,
        content=f"Error: {message}",
        error_kind=ToolErrorKind.INVALID_ARGUMENTS,
    )
