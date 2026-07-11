"""Tool execution - handles tool calls."""

from __future__ import annotations
from typing import TYPE_CHECKING, List
import concurrent.futures

if TYPE_CHECKING:
    from reuleauxcoder.domain.agent.agent import Agent
    from reuleauxcoder.domain.llm.models import ToolCall

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.agent.tool_outcome import ToolErrorKind, ToolOutcome
from reuleauxcoder.domain.approval import ApprovalRequest
from reuleauxcoder.domain.approval_preview import build_approval_preview
from reuleauxcoder.domain.hooks.types import (
    AfterToolExecuteContext,
    BeforeToolExecuteContext,
    HookPoint,
)
from reuleauxcoder.extensions.tools.registry import get_tool


class ToolExecutor:
    """Handles tool execution for the agent."""

    def __init__(self, agent: "Agent"):
        self.agent = agent

    def execute(self, tc: "ToolCall") -> str:
        """Execute a single tool call."""
        tool = self.agent.get_tool(tc.name)
        if tool is None:
            tool = get_tool(tc.name)

        before_context = BeforeToolExecuteContext(
            hook_point=HookPoint.BEFORE_TOOL_EXECUTE,
            agent_id=self.agent.agent_id,
            session_generation=self.agent.session_generation,
            session_id=self.agent.current_session_id,
            turn_id=self.agent._current_turn_id,
            tool_call=tc,
            round_index=self.agent.state.current_round,
            metadata={
                "tool_source": getattr(
                    tool, "tool_source", "builtin" if tool is not None else "unknown"
                ),
                "mcp_server": getattr(tool, "server_name", None),
                "tool_description": getattr(tool, "description", None),
                "tool_schema": getattr(tool, "parameters", None),
            },
        )

        # Fixed core pipeline: authorize -> validate -> approve -> contribute
        # -> execute -> process outcome -> observe -> publish. Extension code
        # cannot reorder or bypass the core validation and approval stages.
        guard_decisions = self.agent.extension_runtime.authorize_tool(before_context)
        denied = next((d for d in guard_decisions if not d.allowed), None)
        if denied is not None:
            message = denied.reason or f"Tool '{tc.name}' blocked by guard hook"
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tc.name,
                    message,
                    success=False,
                    tool_call_id=tc.id,
                    outcome=ToolOutcome.from_legacy(
                        message, success=False, error_kind=ToolErrorKind.DENIED
                    ),
                )
            )
            return message

        for decision in guard_decisions:
            if decision.warning:
                self.agent._emit_event(
                    AgentEvent.diagnostic(
                        decision.warning,
                        code="tool.guard_warning",
                        details={"tool_name": tc.name, "tool_call_id": tc.id},
                    )
                )

        preflight_error = (
            tool.preflight_validate(**tc.arguments) if tool is not None else None
        )
        if preflight_error:
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tc.name,
                    preflight_error,
                    success=False,
                    tool_call_id=tc.id,
                    outcome=ToolOutcome.from_legacy(
                        preflight_error,
                        success=False,
                        error_kind=ToolErrorKind.INVALID_ARGUMENTS,
                    ),
                )
            )
            return preflight_error

        if not self.agent.is_tool_allowed_in_mode(tc.name):
            mode_name = self.agent.active_mode or "default"
            suggested_modes = self.agent.suggest_modes_for_tool(tc.name)
            if suggested_modes:
                suggestions = ", ".join(
                    f"/mode switch {name}" for name in suggested_modes
                )
                message = (
                    f"Tool '{tc.name}' is not available in current mode '{mode_name}'. "
                    f"Ask user to switch mode first: {suggestions}"
                )
            else:
                message = (
                    f"Tool '{tc.name}' is not available in current mode '{mode_name}'"
                )
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tc.name, message, success=False, tool_call_id=tc.id
                )
            )
            return message

        approval_required = next(
            (d for d in guard_decisions if d.requires_approval), None
        )
        if approval_required is not None:
            provider = self.agent.approval_provider
            if provider is None:
                message = (
                    approval_required.reason
                    or f"Tool '{tc.name}' requires approval, but no approval provider is configured"
                )
                self.agent._emit_event(
                    AgentEvent.tool_call_end(
                        tc.name, message, success=False, tool_call_id=tc.id
                    )
                )
                return message
            try:
                approval_request = ApprovalRequest(
                    tool_name=tc.name,
                    tool_args=dict(tc.arguments),
                    tool_source=getattr(tool, "tool_source", "builtin_tool")
                    if tool is not None
                    else "unknown",
                    reason=approval_required.reason,
                )
                backend = getattr(tool, "backend", None)
                approval_request.preview = build_approval_preview(
                    approval_request,
                    workspace=getattr(backend, "workspace", None),
                )
                decision = provider.request_approval(approval_request)
            except (KeyboardInterrupt, EOFError):
                message = f"Tool '{tc.name}' approval interrupted by user"
                self.agent._emit_event(
                    AgentEvent.tool_call_end(
                        tc.name, message, success=False, tool_call_id=tc.id
                    )
                )
                return message

            if not decision.approved:
                message = (
                    decision.reason or f"Tool '{tc.name}' denied by approval provider"
                )
                self.agent._emit_event(
                    AgentEvent.tool_call_end(
                        tc.name, message, success=False, tool_call_id=tc.id
                    )
                )
                return message

        try:
            before_context = self.agent.extension_runtime.contribute_tool_context(
                before_context
            )
        except Exception as exc:
            message = f"Tool '{tc.name}' context contribution failed: {exc}"
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tc.name,
                    message,
                    success=False,
                    tool_call_id=tc.id,
                    outcome=ToolOutcome.from_legacy(
                        message,
                        success=False,
                        error_kind=ToolErrorKind.INTERNAL,
                    ),
                )
            )
            return message
        self.agent.extension_runtime.observe(
            HookPoint.BEFORE_TOOL_EXECUTE, before_context
        )

        tool_call = before_context.tool_call or tc

        # First check agent's tools, then fall back to global registry
        tool = self.agent.get_tool(tool_call.name)
        if tool is None:
            tool = get_tool(tool_call.name)

        if tool is None:
            message = f"Error: unknown tool '{tool_call.name}'"
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tool_call.name, message, success=False, tool_call_id=tc.id
                )
            )
            return message

        try:
            raw_result = tool.execute(**tool_call.arguments)
            outcome = (
                raw_result
                if isinstance(raw_result, ToolOutcome)
                else ToolOutcome.from_legacy(raw_result)
            )
            if (shell_cwd := getattr(tool, "_cwd", None)) is not None:
                self.agent.runtime_working_directory = str(shell_cwd)
            after_context = AfterToolExecuteContext(
                hook_point=HookPoint.AFTER_TOOL_EXECUTE,
                agent_id=self.agent.agent_id,
                session_generation=self.agent.session_generation,
                session_id=self.agent.current_session_id,
                turn_id=self.agent._current_turn_id,
                tool_call=tool_call,
                result=outcome.model_text,
                outcome=outcome,
                round_index=self.agent.state.current_round,
            )
            after_context = self.agent.extension_runtime.process_tool_outcome(
                after_context
            )
            outcome = after_context.outcome or ToolOutcome.from_legacy(
                after_context.result
            )
            # Legacy transforms may still replace ``result``.  Preserve that
            # compatibility at this single hook boundary.
            if after_context.result != outcome.model_text:
                outcome = outcome.with_model_projection(after_context.result)
                after_context.outcome = outcome
            self.agent.extension_runtime.observe(
                HookPoint.AFTER_TOOL_EXECUTE, after_context
            )
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tool_call.name,
                    outcome.model_text,
                    tool_call_id=tc.id,
                    outcome=outcome,
                )
            )
            return outcome.model_text
        except KeyboardInterrupt:
            message = f"Tool '{tool_call.name}' interrupted by user."
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tool_call.name,
                    message,
                    success=False,
                    tool_call_id=tc.id,
                    outcome=ToolOutcome.from_legacy(
                        message,
                        success=False,
                        error_kind=ToolErrorKind.INTERRUPTED,
                    ),
                )
            )
            if not self.agent.stop_requested():
                self.agent.request_stop()
            raise
        except TypeError as e:
            message = f"Error: bad arguments for {tool_call.name}: {e}"
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tool_call.name,
                    message,
                    success=False,
                    tool_call_id=tc.id,
                    outcome=ToolOutcome.from_legacy(
                        message,
                        success=False,
                        error_kind=ToolErrorKind.INVALID_ARGUMENTS,
                    ),
                )
            )
            return message
        except Exception as e:
            message = f"Error executing {tool_call.name}: {e}"
            self.agent._emit_event(
                AgentEvent.tool_call_end(
                    tool_call.name,
                    message,
                    success=False,
                    tool_call_id=tc.id,
                    outcome=ToolOutcome.from_legacy(
                        message,
                        success=False,
                        error_kind=ToolErrorKind.EXECUTION,
                    ),
                )
            )
            return message

    def execute_parallel(self, tool_calls: List["ToolCall"]) -> List[str]:
        """Execute multiple tool calls in parallel."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.execute, tc) for tc in tool_calls]
            return [f.result() for f in futures]
