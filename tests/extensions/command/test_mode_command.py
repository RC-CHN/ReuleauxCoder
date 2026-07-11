from types import SimpleNamespace
from reuleauxcoder.app.commands.models import CommandEffect

from reuleauxcoder.domain.config.models import ApprovalConfig, Config, ModeConfig
from reuleauxcoder.extensions.command.builtin.mode import (
    SwitchModeCommand,
    _handle_switch_mode,
)


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
    effect = CommandEffect()
    return SimpleNamespace(config=config, agent=agent, effect=effect)


def test_switch_mode_is_session_scoped() -> None:
    ctx = _build_ctx()

    result = _handle_switch_mode(SwitchModeCommand(mode_name="debugger"), ctx)

    assert ctx.agent.active_mode == "debugger"
    assert ctx.config.active_mode == "coder"
    assert result.state["active_mode"] == "debugger"
    assert any(
        event.level == "success"
        and event.message == "Switched session mode to 'debugger'"
        for event in ctx.effect.notifications
    )


def test_switch_mode_rejects_unknown_mode() -> None:
    ctx = _build_ctx()

    result = _handle_switch_mode(SwitchModeCommand(mode_name="planner"), ctx)

    assert result.control == "continue"
    assert ctx.agent.active_mode == "coder"
    assert any(event.level == "error" for event in ctx.effect.notifications)
