"""Explicit builtin command contributions."""

from __future__ import annotations

from collections.abc import Callable

from reuleauxcoder.app.commands.panels import (
    CommandPanelRegistry,
    CommandPanelSpec,
)
from reuleauxcoder.app.commands.registry import ActionRegistry
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
from reuleauxcoder.extensions.command.builtin.sessions import (
    command_panel_spec as sessions_panel_spec,
    register_actions as register_session_actions,
)
from reuleauxcoder.extensions.command.builtin.skills import (
    command_panel_spec as skills_panel_spec,
    register_actions as register_skill_actions,
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

_BUILTIN_COMMAND_REGISTRARS: tuple[CommandRegistrar, ...] = (
    register_approval_actions,
    register_mcp_actions,
    register_mode_actions,
    register_model_actions,
    register_session_actions,
    register_skill_actions,
    register_subagent_job_actions,
    register_system_actions,
    register_thinking_actions,
)

_BUILTIN_COMMAND_PANEL_SPECS: tuple[CommandPanelSpec, ...] = (
    approval_panel_spec(),
    mcp_panel_spec(),
    mode_panel_spec(),
    model_panel_spec(),
    sessions_panel_spec(),
    skills_panel_spec(),
    subagent_jobs_panel_spec(),
    thinking_panel_spec(),
)


def builtin_command_registrars() -> tuple[CommandRegistrar, ...]:
    """Return builtin registrars in stable schema/presentation order."""
    return _BUILTIN_COMMAND_REGISTRARS


def builtin_command_panel_specs() -> tuple[CommandPanelSpec, ...]:
    """Return command-owned panel contributions in stable feature order."""
    return _BUILTIN_COMMAND_PANEL_SPECS


def create_builtin_command_panel_registry() -> CommandPanelRegistry:
    """Compose the immutable builtin command panel registry."""
    return CommandPanelRegistry(builtin_command_panel_specs())


__all__ = [
    "CommandRegistrar",
    "builtin_command_panel_specs",
    "builtin_command_registrars",
    "create_builtin_command_panel_registry",
]
