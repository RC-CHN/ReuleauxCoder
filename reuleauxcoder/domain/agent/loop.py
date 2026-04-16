"""Agent loop - the main conversation loop."""

from __future__ import annotations

import os
import platform
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reuleauxcoder.domain.agent.agent import Agent

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.infrastructure.platform import get_platform_info
from reuleauxcoder.services.prompt.builder import system_prompt


class AgentLoop:
    """Manages the agent's conversation loop."""

    def __init__(self, agent: "Agent"):
        self.agent = agent
        self.last_response_streamed = False

    def _runtime_tail_message(self) -> dict:
        """Build ephemeral runtime context appended only at send time."""
        uname = platform.uname()
        shell = get_platform_info().get_preferred_shell()
        content = (
            "[Runtime Context]\n"
            "This is ephemeral runtime state for the current turn. "
            "Do not treat it as persisted conversation history or a new user request.\n"
            f"- Working directory: {os.getcwd()}\n"
            f"- OS: {uname.system} {uname.release} ({uname.machine})\n"
            f"- Python: {platform.python_version()}\n"
            f"- Shell: {shell.value}"
        )
        return {"role": "system", "content": content}

    def _full_messages(self) -> list[dict]:
        """Get full messages including system prompt and ephemeral runtime tail."""
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
            user_system_append=(
                getattr(getattr(self.agent, "runtime_config", None), "prompt", None).system_append
                if getattr(getattr(self.agent, "runtime_config", None), "prompt", None) is not None
                else ""
            ),
            blocked_tools=blocked_tools,
            mode_switch_hints=suggested_modes,
            available_modes=available_modes,
            skills_catalog=getattr(self.agent, "skills_catalog", ""),
        )
        return [
            {"role": "system", "content": system},
            *self.agent.state.messages,
            self._runtime_tail_message(),
        ]

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
            if self.agent.stop_requested():
                return "(stopped by cancellation request)"

            self.agent.state.current_round = round_num

            streamed_output = False

            def _on_token(token: str) -> None:
                nonlocal streamed_output
                streamed_output = True
                self.agent._emit_event(AgentEvent.stream_token(token))

            resp = self.agent.llm.chat(
                messages=self._full_messages(),
                tools=self._tool_schemas(),
                on_token=_on_token,
                hook_registry=self.agent.hook_registry,
                session_id=getattr(self.agent, "current_session_id", None),
                metadata={
                    "round_index": round_num,
                    "active_mode": self.agent.active_mode,
                    "pending_tool_calls": len(self.agent._collect_pending_tool_calls()),
                },
            )

            # Update token counts
            self.agent.state.total_prompt_tokens += resp.prompt_tokens
            self.agent.state.total_completion_tokens += resp.completion_tokens

            # No tool calls -> done
            if not resp.tool_calls:
                self.last_response_streamed = streamed_output
                self.agent.state.messages.append(resp.message)
                return resp.content

            # Tool calls -> execute
            self.agent.state.messages.append(resp.message)

            if self.agent.stop_requested():
                return "(stopped by cancellation request)"

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
                        if self.agent.stop_requested():
                            return "(stopped by cancellation request)"
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
                    if self.agent.stop_requested():
                        return "(stopped by cancellation request)"
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

        summary_prompt = (
            "Maximum tool-call rounds reached. Do not call any tools. "
            "Briefly summarize the current findings/status, list any blockers or incomplete work, "
            "and end the task."
        )
        self.agent.state.messages.append({"role": "user", "content": summary_prompt})
        summary_streamed = False

        def _on_summary_token(token: str) -> None:
            nonlocal summary_streamed
            summary_streamed = True
            self.agent._emit_event(AgentEvent.stream_token(token))

        summary_resp = self.agent.llm.chat(
            messages=self._full_messages(),
            tools=None,
            on_token=_on_summary_token,
            hook_registry=self.agent.hook_registry,
            session_id=getattr(self.agent, "current_session_id", None),
            metadata={
                "round_index": self.agent.state.current_round,
                "active_mode": self.agent.active_mode,
                "summary_phase": True,
                "pending_tool_calls": len(self.agent._collect_pending_tool_calls()),
            },
        )
        self.last_response_streamed = summary_streamed
        self.agent.state.total_prompt_tokens += summary_resp.prompt_tokens
        self.agent.state.total_completion_tokens += summary_resp.completion_tokens
        self.agent.state.messages.append(summary_resp.message)
        return summary_resp.content or "(reached maximum tool-call rounds)"
