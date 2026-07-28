from reuleauxcoder.app.commands.loader import create_builtin_action_registry
from reuleauxcoder.extensions.command.builtin import builtin_command_registrars
from reuleauxcoder.interfaces.ui_registry import UICapability, UIProfile


CLI_PROFILE = UIProfile(
    ui_id="cli",
    display_name="CLI",
    capabilities=frozenset(UICapability),
)


def test_builtin_command_contributions_have_stable_explicit_order() -> None:
    assert tuple(
        registrar.__module__ for registrar in builtin_command_registrars()
    ) == (
        "reuleauxcoder.extensions.command.builtin.approval",
        "reuleauxcoder.extensions.command.builtin.mcp",
        "reuleauxcoder.extensions.command.builtin.mode",
        "reuleauxcoder.extensions.command.builtin.model",
        "reuleauxcoder.extensions.command.builtin.processes",
        "reuleauxcoder.extensions.command.builtin.sessions",
        "reuleauxcoder.extensions.command.builtin.skills",
        "reuleauxcoder.extensions.command.builtin.subagent_jobs",
        "reuleauxcoder.extensions.command.builtin.system",
        "reuleauxcoder.extensions.command.builtin.thinking",
    )


def test_builtin_action_registry_preserves_the_complete_action_order() -> None:
    registry = create_builtin_action_registry()

    assert tuple(action.action_id for action in registry.iter_actions(CLI_PROFILE)) == (
        "approval.show",
        "approval.set",
        "approval.set_global",
        "approval.unset",
        "approval.unset_global",
        "mcp.show",
        "mcp.enable",
        "mcp.disable",
        "mode.show",
        "mode.current",
        "mode.switch",
        "model.show",
        "model.use_main",
        "model.use_sub",
        "model.set_main",
        "model.set_sub",
        "model.switch",
        "processes.list",
        "processes.control",
        "sessions.list",
        "sessions.resume",
        "sessions.save",
        "sessions.new",
        "skills.show",
        "skills.reload",
        "skills.enable",
        "skills.disable",
        "subagent.jobs.list",
        "subagent.jobs.get",
        "subagent.jobs.wait",
        "subagent.jobs.control",
        "system.help",
        "system.exit",
        "system.reset",
        "system.compact",
        "system.tokens",
        "system.debug",
        "system.config",
        "thinking.show",
        "thinking.toggle_inline",
        "thinking.show_effort",
        "thinking.set_effort",
    )
