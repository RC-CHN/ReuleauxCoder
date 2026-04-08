"""Core agent - the main agent class."""

from __future__ import annotations
from collections.abc import Callable
from typing import TYPE_CHECKING, Optional, List
from dataclasses import dataclass, field

if TYPE_CHECKING:
    from reuleauxcoder.services.llm.client import LLM
    from reuleauxcoder.extensions.tools.base import Tool
    from reuleauxcoder.domain.context.manager import ContextManager

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.domain.agent.loop import AgentLoop
from reuleauxcoder.domain.agent.tool_execution import ToolExecutor
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
    ):
        self.llm = llm
        self.tools = tools if tools is not None else []
        self.max_context_tokens = max_context_tokens
        self.max_rounds = max_rounds

        # State
        self.state = AgentState()

        # Context manager
        from reuleauxcoder.domain.context.manager import ContextManager

        self.context = ContextManager(max_tokens=max_context_tokens)

        # Hook runtime
        self.hook_registry = hook_registry or HookRegistry()

        # Execution components
        self._loop = AgentLoop(self)
        self._executor = ToolExecutor(self)

        # Event handlers
        self._event_handlers: List[Callable[[AgentEvent], None]] = []

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
        self._emit_event(AgentEvent.chat_start(user_input))

        # Add user message
        self.state.messages.append({"role": "user", "content": user_input})

        # Run the loop
        result = self._loop.run()

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
