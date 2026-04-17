from types import SimpleNamespace

from reuleauxcoder.domain.config.models import ApprovalConfig, Config, ModeConfig
from reuleauxcoder.extensions.command.builtin.mode import SwitchModeCommand, _handle_switch_mode
from reuleauxcoder.interfaces.events import UIEventBus, UIEventLevel


class FakeAgent:
    def __init__(self) -> None:
        self.active_mode = "coder"

    def set_mode(self, mode_name: str) -> None:
        self.active_mode = mode_name


def _build_ctx() -> SimpleNamespace:
    config = Config(
        api_key="key",
        approval=ApprovalConfig(),
        modes={
            "coder": ModeConfig(name="coder", description="Default coding mode"),
            "debugger": ModeConfig(name="debugger", description="Debug mode"),
        },
        active_mode="coder",
    )
    agent = FakeAgent()
    ui_bus = UIEventBus()
    return SimpleNamespace(config=config, agent=agent, ui_bus=ui_bus)


def test_switch_mode_is_session_scoped() -> None:
    ctx = _build_ctx()

    result = _handle_switch_mode(SwitchModeCommand(mode_name="debugger"), ctx)

    assert ctx.agent.active_mode == "debugger"
    assert ctx.config.active_mode == "coder"
    assert result.payload["active_mode"] == "debugger"
    assert any(
        event.level == UIEventLevel.SUCCESS and event.message == "Switched session mode to 'debugger'"
        for event in ctx.ui_bus._history
    )


def test_switch_mode_rejects_unknown_mode() -> None:
    ctx = _build_ctx()

    result = _handle_switch_mode(SwitchModeCommand(mode_name="planner"), ctx)

    assert result.action == "continue"
    assert ctx.agent.active_mode == "coder"
    assert any(event.level == UIEventLevel.ERROR for event in ctx.ui_bus._history)
