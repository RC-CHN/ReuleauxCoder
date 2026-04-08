"""Tool execution - handles tool calls."""

from __future__ import annotations
from typing import TYPE_CHECKING, List
import concurrent.futures

if TYPE_CHECKING:
    from reuleauxcoder.domain.agent.agent import Agent
    from reuleauxcoder.domain.llm.models import ToolCall

from reuleauxcoder.domain.agent.events import AgentEvent
from reuleauxcoder.domain.approval import ApprovalRequest
from reuleauxcoder.domain.hooks.types import (
    AfterToolExecuteContext,
    BeforeToolExecuteContext,
    HookPoint,
)


class ToolExecutor:
    """Handles tool execution for the agent."""

    def __init__(self, agent: "Agent"):
        self.agent = agent

    def execute(self, tc: "ToolCall") -> str:
        """Execute a single tool call."""
        tool = self.agent.get_tool(tc.name)
        if tool is None:
            from reuleauxcoder.extensions.tools.registry import get_tool

            tool = get_tool(tc.name)

        before_context = BeforeToolExecuteContext(
            hook_point=HookPoint.BEFORE_TOOL_EXECUTE,
            tool_call=tc,
            round_index=self.agent.state.current_round,
            metadata={
                "tool_source": getattr(tool, "tool_source", "builtin" if tool is not None else "unknown"),
                "mcp_server": getattr(tool, "server_name", None),
                "tool_description": getattr(tool, "description", None),
                "tool_schema": getattr(tool, "parameters", None),
            },
        )

        guard_decisions = self.agent.hook_registry.run_guards(
            HookPoint.BEFORE_TOOL_EXECUTE,
            before_context,
        )
        denied = next((d for d in guard_decisions if not d.allowed), None)
        if denied is not None:
            return denied.reason or f"Tool '{tc.name}' blocked by guard hook"

        approval_required = next((d for d in guard_decisions if d.requires_approval), None)
        if approval_required is not None:
            provider = self.agent.approval_provider
            if provider is None:
                return (
                    approval_required.reason
                    or f"Tool '{tc.name}' requires approval, but no approval provider is configured"
                )
            decision = provider.request_approval(
                ApprovalRequest(
                    tool_name=tc.name,
                    tool_args=dict(tc.arguments),
                    reason=approval_required.reason,
                )
            )
            if not decision.approved:
                return decision.reason or f"Tool '{tc.name}' denied by approval provider"

        before_context = self.agent.hook_registry.run_transforms(
            HookPoint.BEFORE_TOOL_EXECUTE,
            before_context,
        )
        self.agent.hook_registry.run_observers(HookPoint.BEFORE_TOOL_EXECUTE, before_context)

        tool_call = before_context.tool_call or tc

        # First check agent's tools, then fall back to global registry
        tool = self.agent.get_tool(tool_call.name)
        if tool is None:
            from reuleauxcoder.extensions.tools.registry import get_tool

            tool = get_tool(tool_call.name)

        if tool is None:
            return f"Error: unknown tool '{tool_call.name}'"

        try:
            result = tool.execute(**tool_call.arguments)
            after_context = AfterToolExecuteContext(
                hook_point=HookPoint.AFTER_TOOL_EXECUTE,
                tool_call=tool_call,
                result=result,
                round_index=self.agent.state.current_round,
            )
            after_context = self.agent.hook_registry.run_transforms(
                HookPoint.AFTER_TOOL_EXECUTE,
                after_context,
            )
            self.agent.hook_registry.run_observers(HookPoint.AFTER_TOOL_EXECUTE, after_context)
            self.agent._emit_event(AgentEvent.tool_call_end(tool_call.name, after_context.result))
            return after_context.result
        except TypeError as e:
            return f"Error: bad arguments for {tool_call.name}: {e}"
        except Exception as e:
            self.agent._emit_event(AgentEvent.error(f"Error executing {tool_call.name}: {e}"))
            return f"Error executing {tool_call.name}: {e}"

    def execute_parallel(self, tool_calls: List["ToolCall"]) -> List[str]:
        """Execute multiple tool calls in parallel."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.execute, tc) for tc in tool_calls]
            return [f.result() for f in futures]