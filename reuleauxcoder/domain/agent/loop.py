"""Agent loop - the main conversation loop."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, Callable, cast

if TYPE_CHECKING:
    from reuleauxcoder.domain.agent.agent import Agent

from reuleauxcoder.domain.agent.events import AgentEvent, AgentEventType
from reuleauxcoder.domain.agent.request_projection import RequestProjector, RequestProjectionState
from reuleauxcoder.domain.cancellation import CancellationView
from reuleauxcoder.domain.context.replay import (
    content_hash,
)
from reuleauxcoder.domain.hooks.types import BeforeLLMRequestContext, HookPoint
from reuleauxcoder.domain.llm.context_messages import (
    mark_synthetic_user_message,
    normalize_provider_message_roles,
    synthetic_user_message,
)
from reuleauxcoder.domain.llm.errors import LLMRequestCancelled


class _RequestTokenBudgetExhausted(RuntimeError):
    """Raised after request hooks make a payload exceed the remaining budget."""


class _DispatchPayloadContractViolation(RuntimeError):
    """Raised when a callback marked as shrinking a request instead grows it."""


class _FinalRequestBudget:
    """Apply the token budget at the final before-request hook boundary."""

    def __init__(
        self,
        agent: "Agent",
        *,
        preliminary_max_output_tokens: int | None,
    ) -> None:
        self._agent = agent
        self._preliminary_max_output_tokens = preliminary_max_output_tokens
        self._requested_output_ceiling: int | None = None
        self.local_request_estimate: int | None = None

    def apply(self, context: BeforeLLMRequestContext) -> None:
        estimate = self.refresh_estimate(context)

        if self._agent.max_total_tokens is None:
            return
        remaining = (
            self._agent.max_total_tokens
            - self._agent.state.total_prompt_tokens
            - self._agent.state.total_completion_tokens
            - estimate
        )
        if remaining <= 0:
            raise _RequestTokenBudgetExhausted

        transformed_limit = context.request_params.get("max_tokens")
        if transformed_limit == self._preliminary_max_output_tokens:
            transformed_limit = getattr(self._agent.llm, "max_tokens", remaining)
        requested = (
            remaining if transformed_limit is None else max(1, int(transformed_limit))
        )
        self._requested_output_ceiling = requested
        context.request_params["max_tokens"] = min(requested, remaining)

    def refresh_estimate(self, context: BeforeLLMRequestContext) -> int:
        """Refresh calibration after a dispatch callback only shrinks payload."""
        messages = normalize_provider_message_roles(context.messages)
        context.messages = messages
        request_tools = context.request_params.get("tools")
        tools = (
            list(request_tools) if isinstance(request_tools, (list, tuple)) else None
        )
        estimate = self._agent.context.estimate_request_tokens(messages, tools)
        self.local_request_estimate = estimate
        return estimate

    def refresh_after_dispatch(self, context: BeforeLLMRequestContext) -> None:
        """Rebudget after a deferred callback removes part of the payload."""
        previous_estimate = self.local_request_estimate
        estimate = self.refresh_estimate(context)
        if previous_estimate is not None and estimate > previous_estimate:
            raise _DispatchPayloadContractViolation(
                "dispatch callback marked the request as reduced, "
                "but its token estimate increased"
            )
        if (
            self._agent.max_total_tokens is None
            or self._requested_output_ceiling is None
        ):
            return
        remaining = (
            self._agent.max_total_tokens
            - self._agent.state.total_prompt_tokens
            - self._agent.state.total_completion_tokens
            - estimate
        )
        if remaining <= 0:
            raise _RequestTokenBudgetExhausted
        context.request_params["max_tokens"] = min(
            self._requested_output_ceiling,
            remaining,
        )


class _BudgetingHookRegistry:
    """Delegate hooks once, then budget their final before-request payload."""

    def __init__(self, registry: Any, budget: _FinalRequestBudget) -> None:
        self._registry = registry
        self._budget = budget

    def __getattr__(self, name: str) -> Any:
        return getattr(self._registry, name)

    def run_guards(self, hook_point: HookPoint, context: Any):
        return self._registry.run_guards(hook_point, context)

    def run_transforms(self, hook_point: HookPoint, context: Any):
        transformed = self._registry.run_transforms(hook_point, context)
        if hook_point is HookPoint.BEFORE_LLM_REQUEST:
            self._budget.apply(cast(BeforeLLMRequestContext, transformed))
        return transformed

    def run_observers(self, hook_point: HookPoint, context: Any):
        return self._registry.run_observers(hook_point, context)

    def refresh_final_request_budget(
        self,
        context: BeforeLLMRequestContext,
    ) -> None:
        self._budget.refresh_after_dispatch(context)


class AgentLoop:
    """Manages the agent's conversation loop."""

    def __init__(
        self, agent: "Agent", *, prompt_fn: Callable[..., str], shell_name: str
    ):
        self.agent = agent
        self._projection = RequestProjector(
            agent, prompt_fn=prompt_fn, shell_name=shell_name,
            state=getattr(agent, "request_projection_state", RequestProjectionState()),
        )
        self.last_response_streamed = False
        self.round_limit_reached = False

    def _flush_runtime_issues(self) -> bool:
        flush = getattr(
            self.agent._executor,
            "flush_pending_runtime_issues",
            None,
        )
        return not callable(flush) or flush() is not None


    def _wire_settings(self, *args, **kwargs):
        return self._projection._wire_settings(*args, **kwargs)


    def _full_messages(self, *args, **kwargs):
        return self._projection._full_messages(*args, **kwargs)


    def _record_request_envelopes(self, *args, **kwargs):
        return self._projection._record_request_envelopes(*args, **kwargs)

    def _record_dispatched_request_envelope(self, *args, **kwargs):
        return self._projection._record_dispatched_request_envelope(*args, **kwargs)


    def _tool_schemas(self, *args, **kwargs):
        return self._projection._tool_schemas(*args, **kwargs)

    def _record_image_traffic(self, attempt_id: str) -> None:
        attempts = getattr(self.agent.llm, "last_image_attempts", None)
        if not attempts or not any(
            item.get("image_count") or item.get("degradation_level")
            for item in attempts
        ):
            return
        self.agent.history_ledger.append(
            "image_payload_observed",
            {
                "attempt_id": attempt_id,
                "attempts": [dict(item) for item in attempts],
                "image_base64_bytes": sum(
                    item.get("image_base64_bytes", 0) for item in attempts
                ),
                "measurement": "base64 payload bytes across provider attempts; not network wire bytes",
            },
            agent_id=self.agent.agent_id,
            turn_id=self.agent._current_turn_id,
            api_round_id=attempt_id,
        )

    def _record_request_interrupt_marker(
        self, *, attempt_id: str, interrupt_epoch: int
    ) -> None:
        marker = mark_synthetic_user_message(
            "<request_interrupted>\n"
            "The preceding assistant response was interrupted before completion.\n"
            "Treat it as incomplete and follow the user's latest direction.\n"
            "</request_interrupted>",
            tag="request_interrupted",
            source="interrupt_marker",
        )
        self.agent._append_message(
            marker,
            source="interrupt_marker",
            history_metadata={
                "attempt_id": attempt_id,
                "interrupt_epoch": interrupt_epoch,
            },
        )

    def _dispatch_round_attempt(self, round_num: int):
        """Dispatch until one attempt settles without a round interrupt."""
        while True:
            attempt_id = self.agent.next_request_attempt_id(round_num)
            # A steering admission becomes model-visible only at this boundary,
            # and is correlated with the request that will consume it.
            self.agent._drain_user_steering(attempt_id=attempt_id)
            self.agent._inject_completed_subagent_jobs()

            streamed_output = False
            streamed_reasoning = False

            def _on_token(token: str) -> None:
                nonlocal streamed_output
                streamed_output = True
                self.agent._emit_event(AgentEvent.stream_token(token))

            def _on_reasoning(token: str) -> None:
                nonlocal streamed_reasoning
                streamed_reasoning = True
                self.agent._emit_event(
                    AgentEvent(
                        event_type=AgentEventType.STREAM_REASONING,
                        data={
                            "token": token,
                            "display_mode": self.agent.reasoning_display_mode,
                        },
                    )
                )

            if not self.agent.recover_control_plane_if_required():
                raise RuntimeError(
                    "Control state persistence is unavailable; refusing to issue "
                    "another model request until ledger recovery can be saved."
                )
            self.agent.report_operation_phase(
                "request_build",
                detail=f"round {round_num + 1}",
            )
            request_messages = self._full_messages()
            request_tools = self._tool_schemas()
            restored = getattr(self.agent, "_restored_replay_envelope", None)
            if (
                restored is not None
                and restored.validate()
                and content_hash(request_tools) == content_hash(restored.tools)
            ):
                request_tools = [dict(tool) for tool in restored.tools]
            local_request_estimate = self.agent.context.estimate_request_tokens(
                request_messages, request_tools
            )
            local_history_estimate = self.agent.context.get_context_tokens(
                self.agent.state.messages
            )
            max_output_tokens = None
            if self.agent.max_total_tokens is not None:
                remaining = (
                    self.agent.max_total_tokens
                    - self.agent.state.total_prompt_tokens
                    - self.agent.state.total_completion_tokens
                    - local_request_estimate
                )
                max_output_tokens = min(
                    int(getattr(self.agent.llm, "max_tokens", max(1, remaining))),
                    max(1, remaining),
                )
            final_budget = _FinalRequestBudget(
                self.agent,
                preliminary_max_output_tokens=max_output_tokens,
            )

            baseline_epoch = self.agent.round_interrupt_epoch()
            cancellation = CancellationView(
                self.agent._stop_event,
                self.agent.round_interrupt_epoch,
                baseline_epoch,
            )
            self.agent.history_ledger.append(
                "request_attempt_dispatched",
                {
                    "attempt_id": attempt_id,
                    "round_index": round_num,
                    "interrupt_epoch_baseline": baseline_epoch,
                },
                agent_id=self.agent.agent_id,
                turn_id=self.agent._current_turn_id,
                api_round_id=attempt_id,
            )
            try:
                self.agent.state.total_model_calls += 1
                resp = self.agent.llm.chat(
                    messages=request_messages,
                    tools=request_tools,
                    on_token=_on_token,
                    on_reasoning_token=_on_reasoning,
                    hook_registry=_BudgetingHookRegistry(
                        self.agent.hook_registry,
                        final_budget,
                    ),
                    session_id=getattr(self.agent, "current_session_id", None),
                    trace_id=attempt_id.replace(":", "_"),
                    metadata={
                        "agent_id": self.agent.agent_id,
                        "session_generation": self.agent.session_generation,
                        "turn_id": self.agent._current_turn_id,
                        "round_index": round_num,
                        "image_turn_id": self.agent.image_turn_id,
                        "image_retention": getattr(
                            getattr(self.agent.runtime_config, "context", None),
                            "image_retention",
                            "history",
                        ),
                        "attempt_id": attempt_id,
                        "active_mode": self.agent.active_mode,
                        "pending_tool_calls": len(
                            self.agent._collect_pending_tool_calls()
                        ),
                        "volatile_tail": True,
                    },
                    cancellation_event=cancellation,
                    max_output_tokens=max_output_tokens,
                )
            except _DispatchPayloadContractViolation as error:
                self.agent.state.total_model_calls -= 1
                self.agent.history_ledger.append(
                    "request_attempt_rejected",
                    {
                        "attempt_id": attempt_id,
                        "round_index": round_num,
                        "reason": "dispatch_payload_contract_violation",
                        "error": str(error),
                    },
                    agent_id=self.agent.agent_id,
                    turn_id=self.agent._current_turn_id,
                    api_round_id=attempt_id,
                )
                raise
            except _RequestTokenBudgetExhausted:
                self.agent.state.total_model_calls -= 1
                self.agent.history_ledger.append(
                    "request_attempt_rejected",
                    {
                        "attempt_id": attempt_id,
                        "round_index": round_num,
                        "reason": "token_budget_exhausted",
                    },
                    agent_id=self.agent.agent_id,
                    turn_id=self.agent._current_turn_id,
                    api_round_id=attempt_id,
                )
                return None
            except LLMRequestCancelled:
                interrupt_epoch = self.agent.round_interrupt_epoch()
                self.agent.history_ledger.append(
                    "request_attempt_cancelled",
                    {
                        "attempt_id": attempt_id,
                        "round_index": round_num,
                        "interrupt_epoch": interrupt_epoch,
                    },
                    agent_id=self.agent.agent_id,
                    turn_id=self.agent._current_turn_id,
                    api_round_id=attempt_id,
                )
                if self.agent.stop_requested() or interrupt_epoch <= baseline_epoch:
                    raise
                if streamed_output or streamed_reasoning:
                    self.agent._emit_event(
                        AgentEvent.assistant_stream_interrupted(
                            attempt_id=attempt_id,
                            interrupt_epoch=interrupt_epoch,
                        )
                    )
                if streamed_output or streamed_reasoning:
                    self._record_request_interrupt_marker(
                        attempt_id=attempt_id,
                        interrupt_epoch=interrupt_epoch,
                    )
                continue

            finally:
                self._record_image_traffic(attempt_id)

            if final_budget.local_request_estimate is not None:
                local_request_estimate = final_budget.local_request_estimate

            self._record_dispatched_request_envelope(
                request_messages,
                request_tools,
                attempt_id=attempt_id,
            )
            return (
                resp,
                streamed_output,
                local_request_estimate,
                local_history_estimate,
                attempt_id,
                baseline_epoch,
            )

    def run(self) -> str:
        """Run the conversation loop."""
        self.round_limit_reached = False
        self.agent.report_operation_phase("mcp_wait")
        self.agent.seal_startup_capabilities()
        if not self._flush_runtime_issues():
            return "(stopped: runtime failure facts could not be published)"
        if self.agent.stop_requested():
            return "(stopped by cancellation request)"
        # Compress if needed
        self.agent.report_operation_phase("context_prepare")
        self.agent.maybe_compress_context(
            self.agent.llm, reason="pre-request checkpoint"
        )

        for round_num in range(self.agent.max_rounds):
            if self.agent.stop_requested():
                return "(stopped by cancellation request)"
            if (
                self.agent.max_total_tokens is not None
                and self.agent.state.total_prompt_tokens
                + self.agent.state.total_completion_tokens
                >= self.agent.max_total_tokens
            ):
                return "(sub-agent token budget exhausted)"

            message_source = self.agent._external_message_source
            if callable(message_source):
                for external_message in message_source():
                    content = (
                        external_message.model_text()
                        if hasattr(external_message, "model_text")
                        else str(external_message)
                    )
                    self.agent._append_message(
                        synthetic_user_message(
                            "inter_agent_message",
                            content,
                            source="parent_to_child_mailbox",
                        ),
                        source="parent_to_child",
                    )

            self.agent.state.current_round = round_num
            dispatched_attempt = self._dispatch_round_attempt(round_num)
            if dispatched_attempt is None:
                return "(sub-agent token budget exhausted before request)"
            (
                resp,
                streamed_output,
                local_request_estimate,
                local_history_estimate,
                attempt_id,
                tool_interrupt_baseline,
            ) = dispatched_attempt

            # Store reasoning content for /thinking command
            if resp.reasoning_content:
                self.agent.last_reasoning_content = resp.reasoning_content
                self.agent.history_ledger.append(
                    "reasoning_metadata",
                    {
                        "present": True,
                        "characters": len(resp.reasoning_content),
                        "display_mode": self.agent.reasoning_display_mode,
                    },
                    agent_id=self.agent.agent_id,
                    turn_id=self.agent._current_turn_id,
                    api_round_id=attempt_id,
                )

            # Update token counts
            self.agent.state.total_prompt_tokens += resp.prompt_tokens
            self.agent.state.total_completion_tokens += resp.completion_tokens
            self.agent.context.observe_usage(
                actual_prompt_tokens=resp.prompt_tokens,
                cached_input_tokens=getattr(resp, "cached_input_tokens", None),
                local_request_estimate=local_request_estimate,
                local_history_estimate=local_history_estimate,
                request_boundary=attempt_id,
                model_profile=str(getattr(self.agent.llm, "model", "unknown")),
            )
            self.agent.history_ledger.append(
                "usage_observed",
                {
                    "actual_prompt_tokens": resp.prompt_tokens,
                    "cached_input_tokens": getattr(resp, "cached_input_tokens", None),
                    "local_request_estimate": local_request_estimate,
                    "local_history_estimate": local_history_estimate,
                    "request_boundary": attempt_id,
                    "attempt_id": attempt_id,
                    "model_profile": str(getattr(self.agent.llm, "model", "unknown")),
                },
                agent_id=self.agent.agent_id,
                turn_id=self.agent._current_turn_id,
                api_round_id=attempt_id,
            )

            # No tool calls -> done
            if not resp.tool_calls:
                self.agent._append_message(resp.message, source="assistant_response")
                if self.agent._has_user_steering():
                    continue
                if self.agent._has_subagent_activity():
                    self.agent._inject_completed_subagent_jobs()
                    continue
                self.last_response_streamed = streamed_output
                return resp.content

            # Tool calls -> execute
            self.agent._append_message(resp.message, source="assistant_tool_calls")

            if (
                self.agent.max_tool_calls is not None
                and self.agent.state.total_tool_calls + len(resp.tool_calls)
                > self.agent.max_tool_calls
            ):
                for tc in resp.tool_calls:
                    self.agent._append_message(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": "Sub-agent tool-call budget exhausted; summarize current findings.",
                        },
                        source="tool_budget_result",
                    )
                continue
            self.agent.state.total_tool_calls += len(resp.tool_calls)

            if len(resp.tool_calls) == 1:
                tc = resp.tool_calls[0]
                self.agent._emit_event(
                    AgentEvent.tool_call_start(
                        tc.name, tc.arguments, tool_call_id=tc.id
                    )
                )
                execute = self.agent._executor.execute
                if "interrupt_baseline" in inspect.signature(execute).parameters:
                    result = execute(
                        tc,
                        interrupt_baseline=tool_interrupt_baseline,
                    )
                else:
                    result = execute(tc)
                self.agent._append_message(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    },
                    source="tool_result",
                )
            else:
                # Tool execution stays concurrent. The root-scoped approval
                # coordinator serializes only calls that actually reach a
                # human review prompt.
                for tc in resp.tool_calls:
                    self.agent._emit_event(
                        AgentEvent.tool_call_start(
                            tc.name, tc.arguments, tool_call_id=tc.id
                        )
                    )
                execute_parallel = self.agent._executor.execute_parallel
                if (
                    "interrupt_baseline"
                    in inspect.signature(execute_parallel).parameters
                ):
                    results = execute_parallel(
                        resp.tool_calls,
                        interrupt_baseline=tool_interrupt_baseline,
                    )
                else:
                    results = execute_parallel(resp.tool_calls)
                for tc, result in zip(resp.tool_calls, results):
                    self.agent._append_message(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result,
                        },
                        source="tool_result",
                    )

            if not self._flush_runtime_issues():
                return "(stopped: runtime failure facts could not be published)"

            if self.agent._park_request is not None:
                request_id = self.agent._park_request.get("guidance_request_id", "-")
                return f"(sub-agent parked for guidance: {request_id})"

            # Compress if tool outputs are big
            self.agent.maybe_compress_context(
                self.agent.llm, reason="post-tool checkpoint"
            )

            # Flush any sub-agent injections buffered during tool execution.
            self.agent._flush_pending_subagent_injections()
            self.agent._inject_completed_subagent_jobs()

        self.round_limit_reached = True
        if self.agent.stop_requested():
            return "(stopped by cancellation request)"

        summary_prompt = (
            "Your working-round budget is exhausted. Stop working and do not call any tools. "
            "Return a concise handoff of the work already performed. Include: "
            "(1) completed findings or changes, (2) concrete evidence and relevant files, "
            "(3) incomplete items or blockers, and (4) the recommended next step. "
            "Do not discard partial results and do not claim unfinished work is complete."
        )
        self.agent._append_message(
            synthetic_user_message(
                "runtime_instruction",
                summary_prompt,
                source="max_round_handoff",
                attributes={"kind": "max_round_handoff"},
            ),
            source="max_round_summary_instruction",
        )
        while True:
            attempt_id = self.agent.next_request_attempt_id(self.agent.max_rounds)
            self.agent._drain_user_steering(attempt_id=attempt_id)
            summary_streamed = False
            summary_reasoning_streamed = False

            def _on_summary_token(token: str) -> None:
                nonlocal summary_streamed
                summary_streamed = True
                self.agent._emit_event(AgentEvent.stream_token(token))

            def _on_summary_reasoning(token: str) -> None:
                nonlocal summary_reasoning_streamed
                summary_reasoning_streamed = True
                self.agent._emit_event(
                    AgentEvent(
                        event_type=AgentEventType.STREAM_REASONING,
                        data={
                            "token": token,
                            "display_mode": self.agent.reasoning_display_mode,
                        },
                    )
                )

            summary_messages = normalize_provider_message_roles(self._full_messages())
            summary_local_request = self.agent.context.estimate_request_tokens(
                summary_messages, None
            )
            summary_local_history = self.agent.context.get_context_tokens(
                self.agent.state.messages
            )
            summary_max_output_tokens = None
            if self.agent.max_total_tokens is not None:
                remaining = (
                    self.agent.max_total_tokens
                    - self.agent.state.total_prompt_tokens
                    - self.agent.state.total_completion_tokens
                    - summary_local_request
                )
                summary_max_output_tokens = min(
                    int(getattr(self.agent.llm, "max_tokens", max(1, remaining))),
                    max(1, remaining),
                )
            final_budget = _FinalRequestBudget(
                self.agent,
                preliminary_max_output_tokens=summary_max_output_tokens,
            )
            baseline_epoch = self.agent.round_interrupt_epoch()
            cancellation = CancellationView(
                self.agent._stop_event,
                self.agent.round_interrupt_epoch,
                baseline_epoch,
            )
            self.agent.history_ledger.append(
                "request_attempt_dispatched",
                {
                    "attempt_id": attempt_id,
                    "round_index": self.agent.max_rounds,
                    "summary_phase": True,
                    "interrupt_epoch_baseline": baseline_epoch,
                },
                agent_id=self.agent.agent_id,
                turn_id=self.agent._current_turn_id,
                api_round_id=attempt_id,
            )
            try:
                self.agent.state.total_model_calls += 1
                summary_resp = self.agent.llm.chat(
                    messages=summary_messages,
                    tools=None,
                    cancellation_event=cancellation,
                    on_token=_on_summary_token,
                    on_reasoning_token=_on_summary_reasoning,
                    hook_registry=_BudgetingHookRegistry(
                        self.agent.hook_registry,
                        final_budget,
                    ),
                    session_id=getattr(self.agent, "current_session_id", None),
                    trace_id=attempt_id.replace(":", "_"),
                    metadata={
                        "agent_id": self.agent.agent_id,
                        "session_generation": self.agent.session_generation,
                        "turn_id": self.agent._current_turn_id,
                        "round_index": self.agent.state.current_round,
                        "image_turn_id": self.agent.image_turn_id,
                        "image_retention": getattr(
                            getattr(self.agent.runtime_config, "context", None),
                            "image_retention",
                            "history",
                        ),
                        "attempt_id": attempt_id,
                        "active_mode": self.agent.active_mode,
                        "summary_phase": True,
                        "pending_tool_calls": len(
                            self.agent._collect_pending_tool_calls()
                        ),
                    },
                    max_output_tokens=summary_max_output_tokens,
                )
            except _DispatchPayloadContractViolation as error:
                self.agent.state.total_model_calls -= 1
                self.agent.history_ledger.append(
                    "request_attempt_rejected",
                    {
                        "attempt_id": attempt_id,
                        "round_index": self.agent.max_rounds,
                        "summary_phase": True,
                        "reason": "dispatch_payload_contract_violation",
                        "error": str(error),
                    },
                    agent_id=self.agent.agent_id,
                    turn_id=self.agent._current_turn_id,
                    api_round_id=attempt_id,
                )
                raise
            except _RequestTokenBudgetExhausted:
                self.agent.state.total_model_calls -= 1
                self.agent.history_ledger.append(
                    "request_attempt_rejected",
                    {
                        "attempt_id": attempt_id,
                        "round_index": self.agent.max_rounds,
                        "summary_phase": True,
                        "reason": "token_budget_exhausted",
                    },
                    agent_id=self.agent.agent_id,
                    turn_id=self.agent._current_turn_id,
                    api_round_id=attempt_id,
                )
                return "(sub-agent token budget exhausted before final handoff)"
            except LLMRequestCancelled:
                interrupt_epoch = self.agent.round_interrupt_epoch()
                self.agent.history_ledger.append(
                    "request_attempt_cancelled",
                    {
                        "attempt_id": attempt_id,
                        "round_index": self.agent.max_rounds,
                        "summary_phase": True,
                        "interrupt_epoch": interrupt_epoch,
                    },
                    agent_id=self.agent.agent_id,
                    turn_id=self.agent._current_turn_id,
                    api_round_id=attempt_id,
                )
                if self.agent.stop_requested() or interrupt_epoch <= baseline_epoch:
                    raise
                if summary_streamed or summary_reasoning_streamed:
                    self.agent._emit_event(
                        AgentEvent.assistant_stream_interrupted(
                            attempt_id=attempt_id,
                            interrupt_epoch=interrupt_epoch,
                        )
                    )
                if summary_streamed or summary_reasoning_streamed:
                    self._record_request_interrupt_marker(
                        attempt_id=attempt_id,
                        interrupt_epoch=interrupt_epoch,
                    )
                continue
            finally:
                self._record_image_traffic(attempt_id)
            if final_budget.local_request_estimate is not None:
                summary_local_request = final_budget.local_request_estimate
            self._record_dispatched_request_envelope(
                summary_messages,
                [],
                attempt_id=attempt_id,
            )
            break

        self.last_response_streamed = summary_streamed
        self.agent.state.total_prompt_tokens += summary_resp.prompt_tokens
        self.agent.state.total_completion_tokens += summary_resp.completion_tokens
        self.agent.context.observe_usage(
            actual_prompt_tokens=summary_resp.prompt_tokens,
            cached_input_tokens=getattr(summary_resp, "cached_input_tokens", None),
            local_request_estimate=summary_local_request,
            local_history_estimate=summary_local_history,
            request_boundary=attempt_id,
            model_profile=str(getattr(self.agent.llm, "model", "unknown")),
        )
        self.agent.history_ledger.append(
            "usage_observed",
            {
                "actual_prompt_tokens": summary_resp.prompt_tokens,
                "cached_input_tokens": getattr(
                    summary_resp, "cached_input_tokens", None
                ),
                "local_request_estimate": summary_local_request,
                "local_history_estimate": summary_local_history,
                "request_boundary": attempt_id,
                "attempt_id": attempt_id,
                "model_profile": str(getattr(self.agent.llm, "model", "unknown")),
            },
            agent_id=self.agent.agent_id,
            turn_id=self.agent._current_turn_id,
            api_round_id=attempt_id,
        )
        self.agent._append_message(summary_resp.message, source="assistant_summary")
        return summary_resp.content or "(reached maximum tool-call rounds)"
