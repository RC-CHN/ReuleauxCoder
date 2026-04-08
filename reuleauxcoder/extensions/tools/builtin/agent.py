"""Sub-agent spawning tool."""

from reuleauxcoder.extensions.tools.base import Tool


class AgentTool(Tool):
    name = "agent"
    description = (
        "Spawn a sub-agent to handle a complex sub-task independently. "
        "The sub-agent has its own context and tool access. Use this for: "
        "researching a codebase, implementing a multi-step change in isolation, "
        "or any task that would benefit from a fresh context window."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "What the sub-agent should accomplish",
            },
        },
        "required": ["task"],
    }

    _parent_agent = None

    def execute(self, task: str) -> str:
        if self._parent_agent is None:
            return "Error: agent tool not initialized (no parent agent)"

        from reuleauxcoder.domain.agent.agent import Agent

        parent = self._parent_agent
        sub = Agent(
            llm=parent.llm,
            tools=[t for t in parent.tools if t.name != "agent"],
            max_context_tokens=parent.context.max_tokens,
            max_rounds=20,
            hook_registry=parent.hook_registry.clone(),
        )

        try:
            result = sub.chat(task)
            if len(result) > 5000:
                result = result[:4500] + "\n... (sub-agent output truncated)"
            return f"[Sub-agent completed]\n{result}"
        except Exception as e:
            return f"Sub-agent error: {e}"
