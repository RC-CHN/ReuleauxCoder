from types import SimpleNamespace

import pytest

from reuleauxcoder.domain.agent.loop import AgentLoop
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.hooks.base import TransformHook
from reuleauxcoder.domain.hooks.types import BeforeLLMRequestContext, HookPoint
from reuleauxcoder.domain.context.replay import content_hash
from reuleauxcoder.domain.llm.models import LLMResponse, ToolCall
from reuleauxcoder.domain.plan import PlanState, ProgressState
from reuleauxcoder.services.llm.client import LLMRequestCancelled
from reuleauxcoder.services.prompt.builder import system_prompt


class _Tool:
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name}}


class _AgentStub:
    def __init__(self) -> None:
        self.active_mode = "coder"
        self.agent_id = "agent"
        self.available_modes = {
            "coder": SimpleNamespace(
                description="Default coding mode", prompt_append="Focus on code."
            )
        }
        self.state = SimpleNamespace(messages=[{"role": "user", "content": "hello"}])
        self.runtime_config = SimpleNamespace(
            prompt=SimpleNamespace(system_append="Always answer in Chinese.")
        )
        self.skills_catalog = "# Skills\n- skill-a"
        self.plan_controller = SimpleNamespace(
            state=PlanState(owner_agent_id="agent", session_generation=0),
            progress=ProgressState(),
        )
        self._subagent_manager = None

    def get_active_mode_config(self):
        return self.available_modes[self.active_mode]

    def get_active_tools(self):
        return [_Tool("read_file", "Read file")]

    def get_blocked_tools(self):
        return []

    def suggest_modes_for_tool(self, _tool_name: str):
        return []


def test_system_prompt_no_longer_contains_runtime_environment_block() -> None:
    prompt = system_prompt([_Tool("read_file", "Read file")])

    assert "# Environment" not in prompt
    assert "- Working directory: " not in prompt
    assert "- Shell: " not in prompt


def test_agent_loop_appends_ephemeral_runtime_context_at_tail() -> None:
    agent = _AgentStub()
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    messages = loop._full_messages()

    assert messages[0]["role"] == "system"
    assert "# Tools" in messages[0]["content"]
    assert "# Runtime Context Protocol" in messages[0]["content"]
    assert "# Environment" not in messages[0]["content"]

    assert messages[1:] == [
        {"role": "user", "content": "hello"},
        messages[-1],
    ]
    assert messages[-1]["role"] == "user"
    assert "<execution_state" in messages[-1]["content"]
    assert '"working_directory":' in messages[-1]["content"]
    assert '"shell":"bash"' in messages[-1]["content"]


def test_agent_loop_runtime_working_directory_override() -> None:
    agent = _AgentStub()
    agent.runtime_working_directory = "/tmp/remote-workspace"
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    messages = loop._full_messages()

    assert '"working_directory":"/tmp/remote-workspace"' in messages[-1]["content"]


def test_runtime_tail_uses_bound_two_scope_notes_store() -> None:
    agent = _AgentStub()
    agent.notes_store = SimpleNamespace(
        render=lambda *, max_chars: (
            "Workspace notes:\n  [wn_one] project\n\n"
            "Global notes:\n  [gn_one] preference"
        )[:max_chars]
    )
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    content = loop._full_messages()[-1]["content"]

    assert "Workspace notes" in content
    assert "Global notes" in content
    assert "wn_one" in content
    assert "gn_one" in content


def test_runtime_tail_honors_notes_inject_false() -> None:
    agent = _AgentStub()
    agent.runtime_config.notes_inject = False
    agent.notes_store = SimpleNamespace(render=lambda **_kwargs: "MUST NOT BE INJECTED")
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    content = loop._full_messages()[-1]["content"]

    assert "MUST NOT BE INJECTED" not in content


def test_runtime_tail_distinguishes_running_and_delivered_subagents() -> None:
    agent = _AgentStub()
    agent._subagent_manager = SimpleNamespace(
        list_jobs=lambda: [
            SimpleNamespace(
                id="sj_running",
                parent_agent_id="agent",
                status="running",
                mode="explore",
                task="inspect parser",
                injected_to_parent=False,
            ),
            SimpleNamespace(
                id="sj_done",
                parent_agent_id="agent",
                status="completed",
                mode="explore",
                task="inspect tests",
                injected_to_parent=True,
            ),
        ]
    )
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    content = loop._full_messages()[-1]["content"]

    assert (
        '"subagents":{"running":1,"blocked":0,"terminal":1,'
        '"delivered_terminal":1}' in content
    )
    assert '"job_id":"sj_running"' in content
    assert '"job_id":"sj_done"' not in content


class _BudgetLLM:
    model = "budget-model"
    max_tokens = 4096
    last_dispatched_request = None

    def __init__(self, response: LLMResponse | None = None) -> None:
        self.response = response or LLMResponse(content="done")
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class _HookAwareBudgetLLM(_BudgetLLM):
    def __init__(self, *responses: LLMResponse) -> None:
        super().__init__()
        self.responses = list(responses) or [self.response]

    def chat(self, **kwargs):
        request_params = {
            "messages": [dict(message) for message in kwargs["messages"]],
            "max_tokens": kwargs["max_output_tokens"] or self.max_tokens,
        }
        if kwargs.get("tools"):
            request_params["tools"] = list(kwargs["tools"])
        metadata = kwargs["metadata"]
        context = BeforeLLMRequestContext(
            hook_point=HookPoint.BEFORE_LLM_REQUEST,
            agent_id=metadata["agent_id"],
            session_generation=metadata["session_generation"],
            request_params=request_params,
            messages=list(request_params["messages"]),
            tools=list(kwargs.get("tools") or []),
            model=self.model,
            metadata=metadata,
        )
        registry = kwargs["hook_registry"]
        denied = next(
            (
                decision
                for decision in registry.run_guards(
                    HookPoint.BEFORE_LLM_REQUEST,
                    context,
                )
                if not decision.allowed
            ),
            None,
        )
        if denied is not None:
            raise RuntimeError(denied.reason or "request denied")
        context = registry.run_transforms(HookPoint.BEFORE_LLM_REQUEST, context)
        registry.run_observers(HookPoint.BEFORE_LLM_REQUEST, context)
        self.dispatch_callback_errors = context._commit_dispatch_callbacks()
        if context._consume_dispatch_payload_changed():
            refresh_budget = getattr(
                registry,
                "refresh_final_request_budget",
                None,
            )
            if callable(refresh_budget):
                refresh_budget(context)
        self.last_dispatched_request = {
            "model": self.model,
            **context.request_params,
            "messages": [dict(message) for message in context.messages],
        }
        self.calls.append(
            {
                **kwargs,
                "wire_messages": context.messages,
                "wire_max_output_tokens": context.request_params["max_tokens"],
                "wire_tools": context.request_params.get("tools"),
            }
        )
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


class _BudgetInjectionHook(TransformHook[BeforeLLMRequestContext]):
    def __init__(self) -> None:
        super().__init__(name="budget_injection")

    def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
        context.messages.insert(-1, {"role": "user", "content": "hook payload"})
        return context


class _BudgetCapHook(TransformHook[BeforeLLMRequestContext]):
    def __init__(self) -> None:
        super().__init__(name="budget_cap")

    def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
        context.messages.insert(-1, {"role": "user", "content": "smaller payload"})
        context.request_params["max_tokens"] = 170
        return context


class _BudgetShrinkHook(TransformHook[BeforeLLMRequestContext]):
    def __init__(self) -> None:
        super().__init__(name="budget_shrink")

    def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
        context.messages.insert(-1, {"role": "user", "content": "smaller payload"})
        return context


class _BudgetToolRewriteHook(TransformHook[BeforeLLMRequestContext]):
    def __init__(self) -> None:
        super().__init__(name="budget_tool_rewrite")

    def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
        context.request_params["tools"] = (
            {
                "type": "function",
                "function": {
                    "name": "first_hook_tool",
                    "description": "first",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "second_hook_tool",
                    "description": "second",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        )
        return context


class _DeferredBudgetInjectionHook(TransformHook[BeforeLLMRequestContext]):
    def __init__(self, committed: list[str]) -> None:
        super().__init__(name="deferred_budget_injection")
        self._committed = committed

    def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
        context.messages.insert(-1, {"role": "user", "content": "hook payload"})
        context.defer_until_dispatch(
            lambda _dispatched: self._committed.append("dispatched")
        )
        return context


class _DispatchShrinkHook(TransformHook[BeforeLLMRequestContext]):
    def __init__(self, *, max_tokens: int | None = None) -> None:
        super().__init__(name="dispatch_shrink")
        self._max_tokens = max_tokens

    def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
        marker = {"role": "user", "content": "dispatch removable"}
        context.messages.insert(-1, marker)
        if self._max_tokens is not None:
            context.request_params["max_tokens"] = self._max_tokens

        def remove_marker(dispatched: BeforeLLMRequestContext) -> None:
            dispatched.messages.remove(marker)
            dispatched.mark_dispatch_payload_changed()

        context.defer_until_dispatch(remove_marker)
        return context


class _DispatchGrowthHook(TransformHook[BeforeLLMRequestContext]):
    def __init__(self) -> None:
        super().__init__(name="dispatch_growth")

    def run(self, context: BeforeLLMRequestContext) -> BeforeLLMRequestContext:
        def grow_payload(dispatched: BeforeLLMRequestContext) -> None:
            dispatched.messages.insert(
                -1,
                {"role": "user", "content": "unexpected dispatch growth"},
            )
            dispatched.mark_dispatch_payload_changed()

        context.defer_until_dispatch(grow_payload)
        return context


def test_subagent_request_caps_output_by_remaining_total_budget() -> None:
    llm = _BudgetLLM()
    agent = Agent(llm=llm, tools=[], max_total_tokens=1_000)
    agent.subagent_depth = 1
    agent.state.total_prompt_tokens = 600
    agent.state.total_completion_tokens = 100
    agent.context.estimate_request_tokens = lambda *_args: 120

    assert agent._loop.run() == "done"
    assert llm.calls[0]["max_output_tokens"] == 180


def test_subagent_request_rebudgets_after_before_llm_message_injection() -> None:
    llm = _HookAwareBudgetLLM()
    agent = Agent(llm=llm, tools=[], max_total_tokens=1_000)
    agent.subagent_depth = 1
    agent.state.total_prompt_tokens = 600
    agent.state.total_completion_tokens = 100
    estimates = []

    def estimate(messages, _tools):
        injected = any(message.get("content") == "hook payload" for message in messages)
        value = 220 if injected else 120
        estimates.append(value)
        return value

    observed = []
    agent.context.estimate_request_tokens = estimate
    agent.context.observe_usage = lambda **usage: observed.append(usage)
    agent.hook_registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _BudgetInjectionHook(),
    )

    assert agent._loop.run() == "done"
    assert estimates == [120, 220]
    assert llm.calls[0]["max_output_tokens"] == 180
    assert llm.calls[0]["wire_max_output_tokens"] == 80
    assert (
        sum(
            message.get("content") == "hook payload"
            for message in llm.calls[0]["wire_messages"]
        )
        == 1
    )
    assert observed[-1]["local_request_estimate"] == 220
    assert agent.request_envelopes[-1].canonical_request_hash == content_hash(
        llm.last_dispatched_request
    )


def test_dispatch_callback_refreshes_exact_wire_token_estimate() -> None:
    llm = _HookAwareBudgetLLM()
    agent = Agent(llm=llm, tools=[], max_total_tokens=1_000)
    agent.subagent_depth = 1
    agent.state.total_prompt_tokens = 600
    agent.state.total_completion_tokens = 100
    estimates: list[int] = []

    def estimate(messages, _tools):
        injected = any(
            message.get("content") == "dispatch removable" for message in messages
        )
        value = 220 if injected else 120
        estimates.append(value)
        return value

    observed = []
    agent.context.estimate_request_tokens = estimate
    agent.context.observe_usage = lambda **usage: observed.append(usage)
    agent.hook_registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _DispatchShrinkHook(),
    )

    assert agent._loop.run() == "done"
    assert estimates == [120, 220, 120]
    assert not any(
        message.get("content") == "dispatch removable"
        for message in llm.calls[0]["wire_messages"]
    )
    assert llm.calls[0]["wire_max_output_tokens"] == 180
    assert observed[-1]["local_request_estimate"] == 120
    assert agent.request_envelopes[-1].canonical_request_hash == content_hash(
        llm.last_dispatched_request
    )


def test_dispatch_rebudget_preserves_explicit_hook_output_cap() -> None:
    llm = _HookAwareBudgetLLM()
    agent = Agent(llm=llm, tools=[], max_total_tokens=1_000)
    agent.subagent_depth = 1
    agent.state.total_prompt_tokens = 600
    agent.state.total_completion_tokens = 100

    def estimate(messages, _tools):
        return (
            220
            if any(
                message.get("content") == "dispatch removable" for message in messages
            )
            else 120
        )

    agent.context.estimate_request_tokens = estimate
    agent.hook_registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _DispatchShrinkHook(max_tokens=70),
    )

    assert agent._loop.run() == "done"
    assert llm.calls[0]["wire_max_output_tokens"] == 70


def test_dispatch_rebudget_surfaces_payload_growth_contract_violation() -> None:
    llm = _HookAwareBudgetLLM()
    agent = Agent(llm=llm, tools=[], max_total_tokens=1_000)
    agent.state.total_prompt_tokens = 600
    agent.state.total_completion_tokens = 100
    agent.context.estimate_request_tokens = lambda messages, _tools: (
        350
        if any(
            message.get("content") == "unexpected dispatch growth"
            for message in messages
        )
        else 120
    )
    agent.hook_registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _DispatchGrowthHook(),
    )

    with pytest.raises(
        RuntimeError,
        match="marked the request as reduced.*token estimate increased",
    ):
        agent._loop.run()
    assert llm.calls == []
    assert agent.state.total_model_calls == 0
    rejected = [
        event
        for event in agent.history_ledger.events
        if event.kind == "request_attempt_rejected"
    ][-1]
    assert rejected.payload["reason"] == "dispatch_payload_contract_violation"
    assert "token estimate increased" in rejected.payload["error"]


def test_subagent_request_rejects_hook_payload_that_exhausts_budget() -> None:
    llm = _HookAwareBudgetLLM()
    agent = Agent(llm=llm, tools=[], max_total_tokens=1_000)
    agent.state.total_prompt_tokens = 600
    agent.state.total_completion_tokens = 100
    estimates = []

    def estimate(messages, _tools):
        injected = any(message.get("content") == "hook payload" for message in messages)
        value = 350 if injected else 120
        estimates.append(value)
        return value

    agent.context.estimate_request_tokens = estimate
    agent.hook_registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _BudgetInjectionHook(),
    )

    assert agent._loop.run() == "(sub-agent token budget exhausted before request)"
    assert estimates == [120, 350]
    assert llm.calls == []
    assert agent.state.total_model_calls == 0
    attempt_events = [
        event
        for event in agent.history_ledger.events
        if event.kind.startswith("request_attempt_")
    ]
    assert [event.kind for event in attempt_events] == [
        "request_attempt_dispatched",
        "request_attempt_rejected",
    ]
    assert attempt_events[0].api_round_id == attempt_events[1].api_round_id
    assert attempt_events[1].payload == {
        "attempt_id": attempt_events[1].api_round_id,
        "round_index": 0,
        "reason": "token_budget_exhausted",
    }
    assert not any(
        event.kind in {"request_payload_observed", "request_committed"}
        and event.api_round_id == attempt_events[1].api_round_id
        for event in agent.history_ledger.events
    )


def test_rejected_final_budget_does_not_commit_deferred_hook_side_effect() -> None:
    llm = _HookAwareBudgetLLM()
    agent = Agent(llm=llm, tools=[], max_total_tokens=1_000)
    agent.state.total_prompt_tokens = 600
    agent.state.total_completion_tokens = 100
    committed: list[str] = []

    def estimate(messages, _tools):
        injected = any(message.get("content") == "hook payload" for message in messages)
        return 350 if injected else 120

    agent.context.estimate_request_tokens = estimate
    agent.hook_registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _DeferredBudgetInjectionHook(committed),
    )

    assert agent._loop.run() == "(sub-agent token budget exhausted before request)"
    assert committed == []
    assert llm.calls == []


def test_final_budget_rejects_unchanged_preliminary_over_budget_request() -> None:
    llm = _HookAwareBudgetLLM()
    agent = Agent(llm=llm, tools=[], max_total_tokens=1_000)
    agent.state.total_prompt_tokens = 800
    agent.state.total_completion_tokens = 100
    agent.context.estimate_request_tokens = lambda _messages, _tools: 120

    assert agent._loop.run() == "(sub-agent token budget exhausted before request)"
    assert llm.calls == []
    assert agent.state.total_model_calls == 0


def test_hook_max_output_cap_is_not_raised_when_final_payload_shrinks() -> None:
    llm = _HookAwareBudgetLLM()
    agent = Agent(llm=llm, tools=[], max_total_tokens=1_000)
    agent.subagent_depth = 1
    agent.state.total_prompt_tokens = 600
    agent.state.total_completion_tokens = 100

    def estimate(messages, _tools):
        shrunk = any(
            message.get("content") == "smaller payload" for message in messages
        )
        return 50 if shrunk else 120

    agent.context.estimate_request_tokens = estimate
    agent.hook_registry.register(HookPoint.BEFORE_LLM_REQUEST, _BudgetCapHook())

    assert agent._loop.run() == "done"
    assert llm.calls[0]["max_output_tokens"] == 180
    assert llm.calls[0]["wire_max_output_tokens"] == 170


def test_hook_can_rescue_preliminary_over_budget_request_by_shrinking_payload() -> None:
    llm = _HookAwareBudgetLLM()
    agent = Agent(llm=llm, tools=[], max_total_tokens=1_000)
    agent.subagent_depth = 1
    agent.state.total_prompt_tokens = 800
    agent.state.total_completion_tokens = 100

    def estimate(messages, _tools):
        shrunk = any(
            message.get("content") == "smaller payload" for message in messages
        )
        return 50 if shrunk else 120

    agent.context.estimate_request_tokens = estimate
    agent.hook_registry.register(HookPoint.BEFORE_LLM_REQUEST, _BudgetShrinkHook())

    assert agent._loop.run() == "done"
    assert llm.calls[0]["max_output_tokens"] == 1
    assert llm.calls[0]["wire_max_output_tokens"] == 50


def test_subagent_request_rebudgets_hook_rewritten_tool_tuple() -> None:
    llm = _HookAwareBudgetLLM()
    agent = Agent(llm=llm, tools=[], max_total_tokens=1_000)
    agent.subagent_depth = 1
    agent.state.total_prompt_tokens = 600
    agent.state.total_completion_tokens = 100
    estimated_tool_counts: list[int] = []

    def estimate(_messages, tools):
        tool_count = len(tools or [])
        estimated_tool_counts.append(tool_count)
        return 210 if tool_count == 2 else 100

    agent.context.estimate_request_tokens = estimate
    agent.hook_registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _BudgetToolRewriteHook(),
    )

    assert agent._loop.run() == "done"
    assert estimated_tool_counts == [0, 2]
    assert llm.calls[0]["max_output_tokens"] == 200
    assert llm.calls[0]["wire_max_output_tokens"] == 90
    assert len(llm.calls[0]["wire_tools"]) == 2
    assert agent.replay_envelope is not None
    assert [tool["function"]["name"] for tool in agent.replay_envelope.tools] == [
        "first_hook_tool",
        "second_hook_tool",
    ]
    assert agent.request_envelopes[-1].canonical_request_hash == content_hash(
        llm.last_dispatched_request
    )


def test_round_limit_summary_rebudgets_after_before_llm_message_injection() -> None:
    llm = _HookAwareBudgetLLM(
        LLMResponse(tool_calls=[ToolCall(id="missing", name="unknown", arguments={})]),
        LLMResponse(content="concise handoff"),
    )
    agent = Agent(llm=llm, tools=[], max_rounds=1, max_total_tokens=1_000)
    agent.subagent_depth = 1
    agent.state.total_prompt_tokens = 600
    agent.state.total_completion_tokens = 100

    def estimate(messages, _tools):
        summary = any(
            "working-round budget is exhausted" in str(message.get("content", ""))
            for message in messages
        )
        injected = any(message.get("content") == "hook payload" for message in messages)
        if summary:
            return 250 if injected else 100
        return 50

    observed = []
    agent.context.estimate_request_tokens = estimate
    agent.context.observe_usage = lambda **usage: observed.append(usage)
    agent.hook_registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _BudgetInjectionHook(),
    )

    assert agent._loop.run() == "concise handoff"
    assert len(llm.calls) == 2
    assert llm.calls[1]["max_output_tokens"] == 200
    assert llm.calls[1]["wire_max_output_tokens"] == 50
    assert observed[-1]["local_request_estimate"] == 250
    assert agent.replay_envelope is not None
    assert any(
        message.get("content") == "hook payload"
        for message in agent.replay_envelope.items
    )
    assert agent.replay_envelope.request_settings["dispatched"]["max_tokens"] == 50
    assert agent.request_envelopes[-1].canonical_request_hash == content_hash(
        llm.last_dispatched_request
    )


def test_round_limit_summary_restores_budget_after_dispatch_payload_shrinks() -> None:
    llm = _HookAwareBudgetLLM(
        LLMResponse(tool_calls=[ToolCall(id="missing", name="unknown", arguments={})]),
        LLMResponse(content="concise handoff"),
    )
    agent = Agent(llm=llm, tools=[], max_rounds=1, max_total_tokens=1_000)
    agent.subagent_depth = 1
    agent.state.total_prompt_tokens = 600
    agent.state.total_completion_tokens = 100

    def estimate(messages, _tools):
        summary = any(
            "working-round budget is exhausted" in str(message.get("content", ""))
            for message in messages
        )
        injected = any(
            message.get("content") == "dispatch removable" for message in messages
        )
        if summary:
            return 220 if injected else 100
        return 50

    agent.context.estimate_request_tokens = estimate
    agent.hook_registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _DispatchShrinkHook(),
    )

    assert agent._loop.run() == "concise handoff"
    assert len(llm.calls) == 2
    assert llm.calls[1]["wire_max_output_tokens"] == 200
    assert not any(
        message.get("content") == "dispatch removable"
        for message in llm.calls[1]["wire_messages"]
    )


def test_summary_hook_can_rescue_preliminary_over_budget_payload() -> None:
    llm = _HookAwareBudgetLLM(
        LLMResponse(tool_calls=[ToolCall(id="missing", name="unknown", arguments={})]),
        LLMResponse(content="rescued handoff"),
    )
    agent = Agent(llm=llm, tools=[], max_rounds=1, max_total_tokens=1_000)
    agent.subagent_depth = 1
    agent.state.total_prompt_tokens = 800
    agent.state.total_completion_tokens = 100

    def estimate(messages, _tools):
        summary = any(
            "working-round budget is exhausted" in str(message.get("content", ""))
            for message in messages
        )
        shrunk = any(
            message.get("content") == "smaller payload" for message in messages
        )
        if summary:
            return 50 if shrunk else 150
        return 50 if shrunk else 20

    agent.context.estimate_request_tokens = estimate
    agent.hook_registry.register(HookPoint.BEFORE_LLM_REQUEST, _BudgetShrinkHook())

    assert agent._loop.run() == "rescued handoff"
    assert llm.calls[1]["max_output_tokens"] == 1
    assert llm.calls[1]["wire_max_output_tokens"] == 50


def test_summary_final_budget_rejection_records_terminal_attempt() -> None:
    llm = _HookAwareBudgetLLM(
        LLMResponse(tool_calls=[ToolCall(id="missing", name="unknown", arguments={})]),
        LLMResponse(content="must not dispatch"),
    )
    agent = Agent(llm=llm, tools=[], max_rounds=1, max_total_tokens=1_000)
    agent.state.total_prompt_tokens = 600
    agent.state.total_completion_tokens = 100

    def estimate(messages, _tools):
        summary = any(
            "working-round budget is exhausted" in str(message.get("content", ""))
            for message in messages
        )
        injected = any(message.get("content") == "hook payload" for message in messages)
        if summary and injected:
            return 350
        return 50

    agent.context.estimate_request_tokens = estimate
    agent.hook_registry.register(
        HookPoint.BEFORE_LLM_REQUEST,
        _BudgetInjectionHook(),
    )

    assert (
        agent._loop.run() == "(sub-agent token budget exhausted before final handoff)"
    )
    assert len(llm.calls) == 1
    assert agent.state.total_model_calls == 1
    attempt_events = [
        event
        for event in agent.history_ledger.events
        if event.kind.startswith("request_attempt_")
    ]
    assert [event.kind for event in attempt_events[-2:]] == [
        "request_attempt_dispatched",
        "request_attempt_rejected",
    ]
    rejected = attempt_events[-1]
    assert rejected.payload == {
        "attempt_id": rejected.api_round_id,
        "round_index": agent.max_rounds,
        "summary_phase": True,
        "reason": "token_budget_exhausted",
    }
    assert attempt_events[-2].api_round_id == rejected.api_round_id
    assert not any(
        event.kind in {"request_payload_observed", "request_committed"}
        and event.api_round_id == rejected.api_round_id
        for event in agent.history_ledger.events
    )


def test_subagent_round_limit_returns_tool_free_partial_handoff() -> None:
    class _HandoffLLM(_BudgetLLM):
        def chat(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return LLMResponse(
                    tool_calls=[ToolCall(id="missing", name="unknown", arguments={})]
                )
            return LLMResponse(
                content="Found two relevant tests; one count remains incomplete."
            )

    llm = _HandoffLLM()
    agent = Agent(llm=llm, tools=[], max_rounds=1)
    agent.subagent_depth = 1

    result = agent._loop.run()

    assert result == "Found two relevant tests; one count remains incomplete."
    assert len(llm.calls) == 2
    assert llm.calls[1]["tools"] is None
    assert llm.calls[1]["metadata"]["summary_phase"] is True
    assert "Do not discard partial results" in llm.calls[1]["messages"][-2]["content"]
    assert agent._loop.round_limit_reached is True


def test_round_limit_summary_honours_immediate_steering_attempts() -> None:
    class _SummarySteeringLLM(_BudgetLLM):
        def __init__(self) -> None:
            super().__init__()
            self.agent = None

        def chat(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return LLMResponse(
                    tool_calls=[
                        ToolCall(id="missing", name="unknown", arguments={})
                    ]
                )
            if len(self.calls) == 2:
                kwargs["on_token"]("partial handoff")
                assert self.agent.submit_user_steering("include the migration risk")
                self.agent.request_interrupt_intent()
                raise LLMRequestCancelled("summary interrupted")
            return LLMResponse(content="Handoff including the migration risk.")

    llm = _SummarySteeringLLM()
    agent = Agent(llm=llm, tools=[], max_rounds=1)
    llm.agent = agent

    result = agent.chat("investigate")

    assert result == "Handoff including the migration risk."
    assert len(llm.calls) == 3
    assert all(call["metadata"]["summary_phase"] for call in llm.calls[1:])
    assert llm.calls[1]["metadata"]["attempt_id"].endswith(":1:2")
    assert llm.calls[2]["metadata"]["attempt_id"].endswith(":1:3")
    final_messages = llm.calls[2]["messages"]
    marker = next(
        index
        for index, message in enumerate(final_messages)
        if "<request_interrupted>" in str(message.get("content"))
    )
    steering = next(
        index
        for index, message in enumerate(final_messages)
        if message.get("content") == "include the migration risk"
    )
    assert marker < steering


def test_stop_racing_with_tool_response_still_pairs_every_tool_call() -> None:
    class _StopWithToolLLM(_BudgetLLM):
        def __init__(self) -> None:
            super().__init__()
            self.agent = None

        def chat(self, **kwargs):
            self.calls.append(kwargs)
            self.agent.request_stop()
            return LLMResponse(
                tool_calls=[
                    ToolCall(id="late-tool", name="probe", arguments={})
                ]
            )

    class _PairingExecutor:
        def __init__(self) -> None:
            self.calls = []

        def execute(self, tool_call, *, interrupt_baseline=None):
            self.calls.append((tool_call.id, interrupt_baseline))
            return "Tool execution interrupted (turn cancellation)."

    llm = _StopWithToolLLM()
    agent = Agent(llm=llm, tools=[])
    llm.agent = agent
    executor = _PairingExecutor()
    agent._executor = executor

    assert agent.chat("start") == "(stopped by cancellation request)"
    assert executor.calls and executor.calls[0][0] == "late-tool"
    assistant_index = next(
        index
        for index, message in enumerate(agent.messages)
        if any(
            call.get("id") == "late-tool"
            for call in (message.get("tool_calls") or [])
        )
    )
    assert agent.messages[assistant_index + 1] == {
        "role": "tool",
        "tool_call_id": "late-tool",
        "content": "Tool execution interrupted (turn cancellation).",
    }


def test_configured_approval_provider_does_not_serialize_unreviewed_tools() -> None:
    class _ParallelLLM(_BudgetLLM):
        def chat(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return LLMResponse(
                    tool_calls=[
                        ToolCall(id="first", name="one", arguments={}),
                        ToolCall(id="second", name="two", arguments={}),
                    ]
                )
            return LLMResponse(content="done")

    class _Executor:
        def __init__(self) -> None:
            self.parallel_calls = []

        def execute(self, _tool_call):
            raise AssertionError("multi-tool rounds must not use serial execution")

        def execute_parallel(self, tool_calls):
            self.parallel_calls.append(tuple(call.id for call in tool_calls))
            return ["one-result", "two-result"]

    llm = _ParallelLLM()
    executor = _Executor()
    agent = Agent(llm=llm, tools=[], executor=executor)
    agent.approval_provider = SimpleNamespace()

    assert agent._loop.run() == "done"
    assert executor.parallel_calls == [("first", "second")]
    assert [
        message["content"]
        for message in agent.messages
        if message.get("role") == "tool"
    ] == ["one-result", "two-result"]
