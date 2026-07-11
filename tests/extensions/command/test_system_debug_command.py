from types import SimpleNamespace

from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.extensions.command.builtin.system import (
    _handle_config,
    _handle_debug,
    _parse_config,
    _parse_debug,
)
from reuleauxcoder.interfaces.events import UIEventBus


def test_parse_debug_on_off() -> None:
    assert _parse_debug("/debug on", None).enabled is True
    assert _parse_debug("/debug off", None).enabled is False
    assert _parse_debug("/debug maybe", None) is None


def test_handle_debug_toggles_runtime_flag() -> None:
    ui_bus = UIEventBus()
    llm = SimpleNamespace(debug_trace=False)
    config = SimpleNamespace(llm_debug_trace=False)
    ctx = SimpleNamespace(
        config=config,
        agent=SimpleNamespace(llm=llm),
        ui_bus=ui_bus,
    )

    result = _handle_debug(SimpleNamespace(enabled=True), ctx)
    assert ctx.config.llm_debug_trace is False
    assert ctx.agent.llm.debug_trace is True
    assert result.payload == {"llm_debug_trace": True}

    result = _handle_debug(SimpleNamespace(enabled=False), ctx)
    assert ctx.config.llm_debug_trace is False
    assert ctx.agent.llm.debug_trace is False
    assert result.payload == {"llm_debug_trace": False}


def test_config_command_emits_typed_effective_view() -> None:
    assert _parse_config("/config", None) is not None
    ui_bus = UIEventBus()
    seen = []
    ui_bus.subscribe(seen.append, replay_history=False)
    config = Config()
    agent = SimpleNamespace(
        active_main_model_profile=None,
        active_sub_model_profile=None,
        active_mode=None,
        llm=SimpleNamespace(model="demo"),
    )

    result = _handle_config(None, SimpleNamespace(config=config, agent=agent, ui_bus=ui_bus))

    event = seen[-1]
    assert event.data["view_type"] == "effective_config"
    assert event.data["view_model"].view_type == "effective_config"
    assert result.payload["rows"]
