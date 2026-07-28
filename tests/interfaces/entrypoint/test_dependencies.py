from typing import cast

from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.interfaces.entrypoint.dependencies import _default_create_agent
from reuleauxcoder.services.llm.client import LLM


class _LLMStub:
    model = "test"


def test_default_agent_factory_uses_the_injected_hook_registry() -> None:
    hook_registry = HookRegistry()

    agent = _default_create_agent(
        cast(LLM, _LLMStub()),
        [],
        Config(),
        hook_registry,
    )

    assert agent.hook_registry is hook_registry
