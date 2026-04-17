from types import SimpleNamespace

from reuleauxcoder.extensions.command.builtin.system import _handle_debug, _parse_debug
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
