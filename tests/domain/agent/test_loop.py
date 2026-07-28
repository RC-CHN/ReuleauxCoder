from types import SimpleNamespace

from reuleauxcoder.domain.agent.loop import AgentLoop
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.llm.models import LLMResponse, ToolCall
from reuleauxcoder.domain.plan import PlanState, ProgressState
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


def test_subagent_request_caps_output_by_remaining_total_budget() -> None:
    llm = _BudgetLLM()
    agent = Agent(llm=llm, tools=[], max_total_tokens=1_000)
    agent.subagent_depth = 1
    agent.state.total_prompt_tokens = 600
    agent.state.total_completion_tokens = 100
    agent.context.estimate_request_tokens = lambda *_args: 120

    assert agent._loop.run() == "done"
    assert llm.calls[0]["max_output_tokens"] == 180


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
