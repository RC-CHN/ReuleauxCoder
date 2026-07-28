"""Explicit builtin command contributions."""

from __future__ import annotations

from collections.abc import Callable

from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.extensions.command.builtin.approval import (
    register_actions as register_approval_actions,
)
from reuleauxcoder.extensions.command.builtin.mcp import (
    register_actions as register_mcp_actions,
)
from reuleauxcoder.extensions.command.builtin.mode import (
    register_actions as register_mode_actions,
)
from reuleauxcoder.extensions.command.builtin.model import (
    register_actions as register_model_actions,
)
from reuleauxcoder.extensions.command.builtin.sessions import (
    register_actions as register_session_actions,
)
from reuleauxcoder.extensions.command.builtin.skills import (
    register_actions as register_skill_actions,
)
from reuleauxcoder.extensions.command.builtin.subagent_jobs import (
    register_actions as register_subagent_job_actions,
)
from reuleauxcoder.extensions.command.builtin.system import (
    register_actions as register_system_actions,
)
from reuleauxcoder.extensions.command.builtin.thinking import (
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


def builtin_command_registrars() -> tuple[CommandRegistrar, ...]:
    """Return builtin registrars in stable schema/presentation order."""
    return _BUILTIN_COMMAND_REGISTRARS


__all__ = ["CommandRegistrar", "builtin_command_registrars"]
