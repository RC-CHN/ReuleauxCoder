"""Explicit builtin command contributions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from reuleauxcoder.app.commands.panels import (
    CommandPanelRegistry,
    CommandPanelSpec,
)
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.extensions.command.builtin.compact import (
    command_panel_spec as compact_panel_spec,
    register_actions as register_compact_actions,
)
from reuleauxcoder.extensions.command.builtin.goal import (
    command_panel_spec as goal_panel_spec,
    register_actions as register_goal_actions,
)
from reuleauxcoder.extensions.command.builtin.approval import (
    command_panel_spec as approval_panel_spec,
    register_actions as register_approval_actions,
)
from reuleauxcoder.extensions.command.builtin.mcp import (
    command_panel_spec as mcp_panel_spec,
    register_actions as register_mcp_actions,
)
from reuleauxcoder.extensions.command.builtin.mode import (
    command_panel_spec as mode_panel_spec,
    register_actions as register_mode_actions,
)
from reuleauxcoder.extensions.command.builtin.model import (
    command_panel_spec as model_panel_spec,
    register_actions as register_model_actions,
)
from reuleauxcoder.extensions.command.builtin.processes import (
    command_panel_spec as processes_panel_spec,
    register_actions as register_process_actions,
)
from reuleauxcoder.extensions.command.builtin.sessions import (
    command_panel_spec as sessions_panel_spec,
    register_actions as register_session_actions,
)
from reuleauxcoder.extensions.command.builtin.skills import (
    command_panel_spec as skills_panel_spec,
    register_actions as register_skill_actions,
)
from reuleauxcoder.extensions.command.builtin.shell import (
    command_panel_spec as shell_panel_spec,
    register_actions as register_shell_actions,
)
from reuleauxcoder.extensions.command.builtin.subagent_jobs import (
    command_panel_spec as subagent_jobs_panel_spec,
    register_actions as register_subagent_job_actions,
)
from reuleauxcoder.extensions.command.builtin.system import (
    register_actions as register_system_actions,
)
from reuleauxcoder.extensions.command.builtin.thinking import (
    command_panel_spec as thinking_panel_spec,
    register_actions as register_thinking_actions,
)

CommandRegistrar = Callable[[ActionRegistry], None]


@dataclass(frozen=True, slots=True)
class _CommandFeature:
    """Keep each feature's actions and optional interactive panel together."""

    register_actions: CommandRegistrar
    panel: CommandPanelSpec | None = None


_BUILTIN_COMMAND_FEATURES = (
    _CommandFeature(register_goal_actions, goal_panel_spec()),
    _CommandFeature(register_approval_actions, approval_panel_spec()),
    _CommandFeature(register_mcp_actions, mcp_panel_spec()),
    _CommandFeature(register_mode_actions, mode_panel_spec()),
    _CommandFeature(register_model_actions, model_panel_spec()),
    _CommandFeature(register_process_actions, processes_panel_spec()),
    _CommandFeature(register_session_actions, sessions_panel_spec()),
    _CommandFeature(register_skill_actions, skills_panel_spec()),
    _CommandFeature(register_shell_actions, shell_panel_spec()),
    _CommandFeature(register_subagent_job_actions, subagent_jobs_panel_spec()),
    _CommandFeature(register_system_actions),
    _CommandFeature(register_compact_actions, compact_panel_spec()),
    _CommandFeature(register_thinking_actions, thinking_panel_spec()),
)


def builtin_command_registrars() -> tuple[CommandRegistrar, ...]:
    """Return builtin registrars in stable schema/presentation order."""
    return tuple(feature.register_actions for feature in _BUILTIN_COMMAND_FEATURES)


def builtin_command_panel_specs() -> tuple[CommandPanelSpec, ...]:
    """Return command-owned panel contributions in stable feature order."""
    return tuple(
        feature.panel
        for feature in _BUILTIN_COMMAND_FEATURES
        if feature.panel is not None
    )


def create_builtin_command_panel_registry() -> CommandPanelRegistry:
    """Compose the immutable builtin command panel registry."""
    return CommandPanelRegistry(builtin_command_panel_specs())


__all__ = [
    "CommandRegistrar",
    "builtin_command_panel_specs",
    "builtin_command_registrars",
    "create_builtin_command_panel_registry",
]
