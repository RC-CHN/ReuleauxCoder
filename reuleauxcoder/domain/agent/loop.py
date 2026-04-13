"""Agent loop - the main conversation loop."""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.domain.agent.agent import Agent

from reuleauxcoder.services.prompt.builder import system_prompt
from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType


class AgentLoop:
    """Manages the agent's conversation loop."""

    def __init__(self, agent: "Agent"):
        self.agent = agent

    def _full_messages(self) -> list[dict]:
        """Get full messages including system prompt."""
        mode = self.agent.get_active_mode_config()
        active_tools = self.agent.get_active_tools()
        blocked = self.agent.get_blocked_tools()
        blocked_tools = [tool.name for tool in blocked]

        suggested_modes: list[str] = []
        for tool in blocked:
            for mode_name in self.agent.suggest_modes_for_tool(tool.name):
                if mode_name != self.agent.active_mode and mode_name not in suggested_modes:
                    suggested_modes.append(mode_name)

        available_modes = [
            (name, mode_cfg.description)
            for name, mode_cfg in sorted(self.agent.available_modes.items())
        ]

        system = system_prompt(
            active_tools,
            mode_name=self.agent.active_mode,
            mode_prompt_append=mode.prompt_append if mode is not None else "",
            blocked_tools=blocked_tools,
            mode_switch_hints=suggested_modes,
            available_modes=available_modes,
            skills_catalog=getattr(self.agent, "skills_catalog", ""),
        )
        return [{"role": "system", "content": system}] + self.agent.state.messages

    def _tool_schemas(self) -> list[dict]:
        """Get tool schemas for LLM."""
        return [t.schema() for t in self.agent.get_active_tools()]

    def run(self) -> str:
        """Run the conversation loop."""
        # Compress if needed
        self.agent.context.maybe_compress(
            self.agent.state.messages,
            self.agent.llm,
        )

        for round_num in range(self.agent.max_rounds):
            self.agent.state.current_round = round_num

            # Call LLM
            resp = self.agent.llm.chat(
                messages=self._full_messages(),
                tools=self._tool_schemas(),
                on_token=lambda token: self.agent._emit_event(AgentEvent.stream_token(token)),
                hook_registry=self.agent.hook_registry,
            )

            # Update token counts
            self.agent.state.total_prompt_tokens += resp.prompt_tokens
            self.agent.state.total_completion_tokens += resp.completion_tokens

            # No tool calls -> done
            if not resp.tool_calls:
                self.agent.state.messages.append(resp.message)
                return resp.content

            # Tool calls -> execute
            self.agent.state.messages.append(resp.message)

            if len(resp.tool_calls) == 1:
                tc = resp.tool_calls[0]
                self.agent._emit_event(AgentEvent.tool_call_start(tc.name, tc.arguments))
                result = self.agent._executor.execute(tc)
                self.agent.state.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    }
                )
            else:
                # If approval is interactive, run sequentially to keep terminal UX stable.
                if self.agent.approval_provider is not None:
                    for tc in resp.tool_calls:
                        self.agent._emit_event(AgentEvent.tool_call_start(tc.name, tc.arguments))
                        result = self.agent._executor.execute(tc)
                        self.agent.state.messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": result,
                            }
                        )
                else:
                    # No interactive approval needed: keep parallel execution.
                    for tc in resp.tool_calls:
                        self.agent._emit_event(AgentEvent.tool_call_start(tc.name, tc.arguments))
                    results = self.agent._executor.execute_parallel(resp.tool_calls)
                    for tc, result in zip(resp.tool_calls, results):
                        self.agent.state.messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": result,
                            }
                        )

            # Compress if tool outputs are big
            self.agent.context.maybe_compress(
                self.agent.state.messages,
                self.agent.llm,
            )

        return "(reached maximum tool-call rounds)"