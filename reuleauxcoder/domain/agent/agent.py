"""Core agent - the main agent class."""

from __future__ import annotations
from collections.abc import Callable
from typing import TYPE_CHECKING, Optional, List
from dataclasses import dataclass, field

if TYPE_CHECKING:
    from reuleauxcoder.domain.approval import ApprovalProvider
    from reuleauxcoder.services.llm.client import LLM
    from reuleauxcoder.extensions.tools.base import Tool
    from reuleauxcoder.domain.context.manager import ContextManager

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.domain.agent.loop import AgentLoop
from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
from reuleauxcoder.domain.config.models import ModeConfig
from reuleauxcoder.domain.hooks import HookBase, HookPoint, HookRegistry


@dataclass
class AgentState:
    """State of the agent."""

    messages: list[dict] = field(default_factory=list)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    current_round: int = 0


class Agent:
    """The main agent class - orchestrates LLM and tools."""

    def __init__(
        self,
        llm: "LLM",
        tools: Optional[List["Tool"]] = None,
        max_context_tokens: int = 128_000,
        max_rounds: int = 50,
        hook_registry: HookRegistry | None = None,
        approval_provider: "ApprovalProvider" | None = None,
        available_modes: dict[str, ModeConfig] | None = None,
        active_mode: str | None = None,
    ):
        self.llm = llm
        self.tools = tools if tools is not None else []
        self.max_context_tokens = max_context_tokens
        self.max_rounds = max_rounds

        # Mode state
        self.available_modes: dict[str, ModeConfig] = dict(available_modes or {})
        self.active_mode: str | None = None

        # State
        self.state = AgentState()

        # Context manager
        from reuleauxcoder.domain.context.manager import ContextManager

        self.context = ContextManager(max_tokens=max_context_tokens)

        # Hook runtime
        self.hook_registry = hook_registry or HookRegistry()

        # Execution components
        self.approval_provider = approval_provider
        self._loop = AgentLoop(self)
        self._executor = ToolExecutor(self)

        # Event handlers
        self._event_handlers: List[Callable[[AgentEvent], None]] = []

        # Activate initial mode if available
        if self.available_modes:
            default_mode = active_mode or next(iter(self.available_modes.keys()), None)
            if default_mode in self.available_modes:
                self.active_mode = default_mode

    def _collect_pending_tool_calls(self) -> list[tuple[str, str]]:
        """Collect assistant tool calls that do not yet have matching tool outputs."""
        completed_ids = {
            msg.get("tool_call_id")
            for msg in self.state.messages
            if msg.get("role") == "tool" and msg.get("tool_call_id")
        }

        pending: list[tuple[str, str]] = []
        seen: set[str] = set()
        for msg in self.state.messages:
            if msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id")
                fn = tc.get("function") or {}
                tc_name = fn.get("name") or "unknown_tool"
                if not tc_id or tc_id in completed_ids or tc_id in seen:
                    continue
                pending.append((tc_id, tc_name))
                seen.add(tc_id)

        return pending

    def reconcile_pending_tool_calls(self, reason: str | None = None) -> int:
        """Append fallback tool outputs for any dangling assistant tool calls.

        Returns the number of synthetic tool results appended.
        """
        pending = self._collect_pending_tool_calls()
        if not pending:
            return 0

        suffix = f" {reason}" if reason else ""
        for tc_id, tc_name in pending:
            self.state.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": f"Tool '{tc_name}' interrupted before returning output.{suffix}",
                }
            )
        return len(pending)

    def get_active_mode_config(self) -> ModeConfig | None:
        """Return active mode config if mode is enabled."""
        if not self.active_mode:
            return None
        return self.available_modes.get(self.active_mode)

    def set_mode(self, mode_name: str) -> None:
        """Switch active mode.

        Raises:
            ValueError: If mode does not exist.
        """
        if mode_name not in self.available_modes:
            raise ValueError(f"Unknown mode: {mode_name}")
        self.active_mode = mode_name

    def get_active_tools(self) -> list["Tool"]:
        """Return tools visible to the LLM in current mode."""
        mode = self.get_active_mode_config()
        if mode is None:
            return self.tools

        if not mode.tools or "*" in mode.tools:
            return self.tools

        allowed = set(mode.tools)
        return [tool for tool in self.tools if tool.name in allowed]

    def get_blocked_tools(self) -> list["Tool"]:
        """Return tools hidden/blocked by current mode."""
        mode = self.get_active_mode_config()
        if mode is None or not mode.tools or "*" in mode.tools:
            return []
        allowed = set(mode.tools)
        return [tool for tool in self.tools if tool.name not in allowed]

    def suggest_modes_for_tool(self, tool_name: str) -> list[str]:
        """Return mode names that allow the given tool."""
        suggestions: list[str] = []
        for mode_name, mode in self.available_modes.items():
            if not mode.tools or "*" in mode.tools or tool_name in set(mode.tools):
                suggestions.append(mode_name)
        return suggestions

    def is_tool_allowed_in_mode(self, tool_name: str) -> bool:
        """Return whether a tool can execute in current mode."""
        mode = self.get_active_mode_config()
        if mode is None:
            return True
        if not mode.tools or "*" in mode.tools:
            return True
        return tool_name in set(mode.tools)

    def add_event_handler(self, handler: Callable[[AgentEvent], None]) -> None:
        """Add an event handler."""
        self._event_handlers.append(handler)

    def _emit_event(self, event: AgentEvent) -> None:
        """Emit an event to all handlers."""
        for handler in self._event_handlers:
            try:
                handler(event)
            except Exception:
                pass  # Don't let handler errors break execution

    def register_hook(self, hook_point: HookPoint, hook: HookBase[object]) -> None:
        """Register a hook on the agent-scoped hook registry."""
        self.hook_registry.register(hook_point, hook)

    def list_hooks(self, hook_point: HookPoint | None = None) -> dict[str, list[str]]:
        """List registered hooks from the agent-scoped hook registry."""
        return self.hook_registry.list_hooks(hook_point)

    def add_tools(self, tools: List["Tool"]) -> None:
        """Add additional tools."""
        self.tools.extend(tools)

    def get_tool(self, name: str) -> Optional["Tool"]:
        """Look up a tool by name."""
        for t in self.tools:
            if t.name == name:
                return t
        return None

    def chat(self, user_input: str) -> str:
        """Process one user message."""
        # Repair stale dangling tool calls (e.g. after previous crash/interruption)
        self.reconcile_pending_tool_calls(reason="Recovered from previous interrupted turn.")

        self._emit_event(AgentEvent.chat_start(user_input))

        # Add user message
        self.state.messages.append({"role": "user", "content": user_input})

        # Run the loop
        try:
            result = self._loop.run()
        except BaseException as e:
            # Ensure tool-call/response parity before bubbling the failure upward.
            self.reconcile_pending_tool_calls(reason=f"Interrupted due to {type(e).__name__}.")
            raise

        self._emit_event(AgentEvent.chat_end(result))
        return result

    def reset(self) -> None:
        """Clear conversation history."""
        self.state.messages.clear()
        self.state.total_prompt_tokens = 0
        self.state.total_completion_tokens = 0
        self.state.current_round = 0

    @property
    def messages(self) -> list[dict]:
        """Get messages (for compatibility)."""
        return self.state.messages
