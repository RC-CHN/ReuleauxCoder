"""Agent domain - core agent behavior and orchestration."""

from reuleauxcoder.domain.agent.events import AgentEvent

__all__ = ["Agent", "AgentEvent"]


def __getattr__(name):
    # Importing event/result contracts must not load the execution runtime.
    if name == "Agent":
        from reuleauxcoder.domain.agent.agent import Agent

        return Agent
    raise AttributeError(name)
