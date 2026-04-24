from types import SimpleNamespace

from reuleauxcoder.domain.config.models import Config, ModelProfileConfig
from reuleauxcoder.extensions.subagent.manager import _create_subagent_llm


class _FakeParentLLM:
    def __init__(self) -> None:
        self.model = "parent-model"
        self.debug_trace = True


def test_create_subagent_llm_uses_full_profile_runtime_settings() -> None:
    sub_profile = ModelProfileConfig(
        name="sub-profile",
        model="deepseek-v4-pro",
        api_key="sub-key",
        base_url="https://api.deepseek.com",
        max_tokens=8192,
        temperature=0.0,
        max_context_tokens=128000,
        preserve_reasoning_content=True,
        backfill_reasoning_content_for_tool_calls=False,
        reasoning_effort="high",
        thinking_enabled=True,
        reasoning_replay_mode="tool_calls",
        reasoning_replay_placeholder="[PLACE_HOLDER]",
    )
    config = Config(
        model_profiles={"sub-profile": sub_profile},
        active_main_model_profile="sub-profile",
        active_model_profile="sub-profile",
        active_sub_model_profile="sub-profile",
    )
    parent_agent = SimpleNamespace(
        runtime_config=config,
        llm=_FakeParentLLM(),
    )

    llm, profile_name = _create_subagent_llm(parent_agent, None)

    assert profile_name == "sub-profile"
    assert llm.model == "deepseek-v4-pro"
    assert llm.api_key == "sub-key"
    assert llm.base_url == "https://api.deepseek.com"
    assert llm.max_tokens == 8192
    assert llm.temperature == 0.0
    assert llm.preserve_reasoning_content is True
    assert llm.backfill_reasoning_content_for_tool_calls is False
    assert llm.reasoning_effort == "high"
    assert llm.thinking_enabled is True
    assert llm.reasoning_replay_mode == "tool_calls"
    assert llm.reasoning_replay_placeholder == "[PLACE_HOLDER]"
    assert llm.debug_trace is True
