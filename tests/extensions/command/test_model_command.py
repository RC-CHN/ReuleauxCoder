from types import SimpleNamespace

from reuleauxcoder.domain.config.models import ApprovalConfig, Config, ModelProfileConfig
from reuleauxcoder.extensions.command.builtin.model import (
    SetMainModelCommand,
    SetSubModelCommand,
    SwitchModelCommand,
    UseMainModelCommand,
    UseSubModelCommand,
    _handle_set_main_model,
    _handle_set_sub_model,
    _handle_switch_model,
    _handle_use_main_model,
    _handle_use_sub_model,
)
from reuleauxcoder.interfaces.events import UIEventBus, UIEventLevel


class FakeLLM:
    def __init__(self) -> None:
        self.model = "base-model"
        self.api_key = "base-key"
        self.base_url = None
        self.temperature = 0.0
        self.max_tokens = 2048
        self.debug_trace = False

    def reconfigure(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def _build_ctx() -> SimpleNamespace:
    profile_a = ModelProfileConfig(
        name="alpha",
        model="model-alpha",
        api_key="key-alpha",
        base_url="https://alpha.example",
        max_tokens=4000,
        temperature=0.1,
        max_context_tokens=100000,
    )
    profile_b = ModelProfileConfig(
        name="beta",
        model="model-beta",
        api_key="key-beta",
        base_url="https://beta.example",
        max_tokens=8000,
        temperature=0.2,
        max_context_tokens=200000,
    )
    config = Config(
        model="base-model",
        api_key="base-key",
        approval=ApprovalConfig(),
        model_profiles={"alpha": profile_a, "beta": profile_b},
        active_main_model_profile="alpha",
        active_model_profile="alpha",
        active_sub_model_profile="alpha",
        max_context_tokens=64000,
    )
    llm = FakeLLM()
    agent = SimpleNamespace(
        llm=llm,
        context=SimpleNamespace(reconfigure=lambda max_tokens: setattr(agent.context, "max_tokens", max_tokens)),
        active_main_model_profile="alpha",
        active_sub_model_profile="alpha",
        active_mode="coder",
    )
    ui_bus = UIEventBus()
    return SimpleNamespace(config=config, agent=agent, ui_bus=ui_bus)


def test_switch_model_is_session_scoped() -> None:
    ctx = _build_ctx()

    result = _handle_switch_model(SwitchModelCommand(profile_name="beta"), ctx)

    assert ctx.agent.llm.model == "model-beta"
    assert ctx.agent.active_main_model_profile == "beta"
    assert ctx.config.active_main_model_profile == "alpha"
    assert ctx.config.active_model_profile == "alpha"
    assert result.payload["active_main_profile"] == "beta"
    assert result.payload["active_sub_profile"] == "alpha"


def test_use_main_model_alias_switches_session_main_model() -> None:
    ctx = _build_ctx()

    result = _handle_use_main_model(UseMainModelCommand(profile_name="beta"), ctx)

    assert ctx.agent.active_main_model_profile == "beta"
    assert ctx.config.active_main_model_profile == "alpha"
    assert result.payload["current_model"] == "model-beta"


def test_set_main_model_updates_global_and_runtime(monkeypatch) -> None:
    ctx = _build_ctx()
    saved = {}

    def fake_save(self, profile_name: str):
        saved["profile_name"] = profile_name
        return "/tmp/config.yaml"

    monkeypatch.setattr(
        "reuleauxcoder.extensions.command.builtin.model.WorkspaceConfigStore.save_active_model_profile",
        fake_save,
    )

    result = _handle_set_main_model(SetMainModelCommand(profile_name="beta"), ctx)

    assert saved["profile_name"] == "beta"
    assert ctx.config.active_main_model_profile == "beta"
    assert ctx.config.active_model_profile == "beta"
    assert ctx.config.model == "model-beta"
    assert ctx.agent.active_main_model_profile == "beta"
    assert result.payload["active_main_profile"] == "beta"


def test_use_sub_model_alias_switches_session_sub_model() -> None:
    ctx = _build_ctx()

    result = _handle_use_sub_model(UseSubModelCommand(profile_name="beta"), ctx)

    assert ctx.agent.active_sub_model_profile == "beta"
    assert ctx.config.active_sub_model_profile == "alpha"
    assert result.payload["active_sub_profile"] == "beta"
    assert any(
        event.level == UIEventLevel.SUCCESS and "session sub-agent model profile" in event.message
        for event in ctx.ui_bus._history
    )


def test_set_sub_model_updates_global_sub_profile(monkeypatch) -> None:
    ctx = _build_ctx()
    saved = {}

    def fake_save(self, profile_name: str):
        saved["profile_name"] = profile_name
        return "/tmp/config.yaml"

    monkeypatch.setattr(
        "reuleauxcoder.extensions.command.builtin.model.WorkspaceConfigStore.save_active_sub_model_profile",
        fake_save,
    )

    result = _handle_set_sub_model(SetSubModelCommand(profile_name="beta"), ctx)

    assert saved["profile_name"] == "beta"
    assert ctx.config.active_sub_model_profile == "beta"
    assert ctx.agent.active_sub_model_profile == "alpha"
    assert result.payload["active_sub_profile"] == "alpha"
    assert any(
        event.level == UIEventLevel.SUCCESS and "global sub-agent model profile" in event.message
        for event in ctx.ui_bus._history
    )
