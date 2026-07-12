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
            verb = "updated" if changed else "unchanged"
            return ToolOutcome(
                summary=f"Progress {verb}",
                content=f"Progress {verb} · {state.phase} · revision {state.revision}",
                metadata={"progress_revision": state.revision, "changed": changed},
            )
        except (TypeError, ValueError, RuntimeError) as error:
            return _invalid(str(error))


@register_tool
class ReportToParentTool(_AgentControlTool):
    name = "report_to_parent"
    description = (
        "Send a non-blocking milestone, reply, amendment, or warning to the "
        "immediate parent agent. This does not finish or pause the child."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "minLength": 1},
            "kind": {
                "type": "string",
                "enum": ["reply", "milestone", "amendment", "warning"],
            },
            "reply_to": {
                "type": "string",
                "description": "Directive ID when kind=reply.",
            },
        },
        "required": ["message", "kind"],
    }

    def execute(
        self,
        message: str,
        kind: str = "milestone",
        reply_to: str | None = None,
    ) -> ToolOutcome:
        return self.run_backend(message=message, kind=kind, reply_to=reply_to)

    @backend_handler("local")
    def _execute_local(
        self,
        message: str,
        kind: str = "milestone",
        reply_to: str | None = None,
    ) -> ToolOutcome:
        try:
            self._identity()
            if self._agent is None or getattr(self._agent, "subagent_depth", 0) <= 0:
                raise RuntimeError("report_to_parent is available only to child agents")
            if kind not in {"reply", "milestone", "amendment", "warning"}:
                raise ValueError(f"unsupported report kind: {kind}")
            if kind == "reply" and not (reply_to or "").strip():
                raise ValueError("reply reports require reply_to")
            manager = getattr(self._agent, "_subagent_manager", None)
            if manager is None:
                raise RuntimeError("child has no parent mailbox")
            item = manager.queue_to_parent(
                self._agent.agent_id,
                message,
                kind=kind,
                reply_to=reply_to,
            )
            if item is None:
                raise RuntimeError("parent mailbox rejected the report")
            return ToolOutcome(
                summary="Report queued for parent",
                content=f"Report queued · item_id={item.item_id}",
                metadata={
                    "item_id": item.item_id,
                    "kind": item.kind,
                    "reply_to": item.reply_to,
                },
            )
        except (TypeError, ValueError, RuntimeError) as error:
            return _invalid(str(error))


@register_tool
class RequestGuidanceTool(_AgentControlTool):
    name = "request_guidance"
    description = (
        "Checkpoint and pause this child when it cannot safely continue without "
        "a parent or human decision. This is the only blocking child report."
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "minLength": 1},
            "context": {"type": "string"},
        },
        "required": ["question"],
    }

    def execute(self, question: str, context: str | None = None) -> ToolOutcome:
        return self.run_backend(question=question, context=context)

    @backend_handler("local")
    def _execute_local(
        self, question: str, context: str | None = None
    ) -> ToolOutcome:
        try:
            self._identity()
            if self._agent is None or getattr(self._agent, "subagent_depth", 0) <= 0:
                raise RuntimeError("request_guidance is available only to child agents")
            manager = getattr(self._agent, "_subagent_manager", None)
            if manager is None:
                raise RuntimeError("child has no parent guidance route")
            request = manager.request_guidance(
                self._agent.agent_id,
                question,
                context=context,
            )
            if request is None:
                raise RuntimeError("guidance request was rejected")
            return ToolOutcome(
                summary="Guidance requested; child will pause",
                content=(
                    "Guidance checkpoint requested · "
                    f"request_id={request.item_id}"
                ),
                metadata={
                    "park_subagent": True,
                    "guidance_request_id": request.item_id,
                },
            )
        except (TypeError, ValueError, RuntimeError) as error:
            return _invalid(str(error))


def _invalid(message: str) -> ToolOutcome:
    return ToolOutcome(
        status=ToolOutcomeStatus.FAILED,
        content=f"Error: {message}",
        error_kind=ToolErrorKind.INVALID_ARGUMENTS,
    )
