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
    assert "# Environment" not in messages[0]["content"]

    assert messages[1:] == [
        {"role": "user", "content": "hello"},
        messages[-1],
    ]
    assert messages[-1]["role"] == "system"
    assert "<execution_state" in messages[-1]["content"]
    assert '"working_directory":' in messages[-1]["content"]
    assert '"shell":"bash"' in messages[-1]["content"]


def test_agent_loop_runtime_working_directory_override() -> None:
    agent = _AgentStub()
    agent.runtime_working_directory = "/tmp/remote-workspace"
    loop = AgentLoop(agent, prompt_fn=system_prompt, shell_name="bash")

    messages = loop._full_messages()

    assert '"working_directory":"/tmp/remote-workspace"' in messages[-1]["content"]


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
