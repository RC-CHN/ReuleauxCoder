"""CLI command handlers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from reuleauxcoder.app.commands import CommandContext, dispatch_command, parse_command
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind
from reuleauxcoder.interfaces.ui_registry import UIProfile

if TYPE_CHECKING:
    from reuleauxcoder.domain.agent.agent import Agent
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.extensions.skills.service import SkillsService


# ---------------------------------------------------------------------------
# Fuzzy command matching
# ---------------------------------------------------------------------------


def _extract_base_name(trigger_value: str) -> str:
    """Extract the base command name from a slash trigger value.

    ``/thinking`` → ``thinking``, ``/thinking inline`` → ``thinking``.
    """
    if not trigger_value.startswith("/"):
        return ""
    return trigger_value[1:].split()[0] if trigger_value[1:].strip() else ""


def _levenshtein(s: str, t: str) -> int:
    """Compute edit distance between two strings."""
    if len(s) < len(t):
        return _levenshtein(t, s)
    if not t:
        return len(s)
    prev = list(range(len(t) + 1))
    for i, cs in enumerate(s, 1):
        curr = [i]
        for j, ct in enumerate(t, 1):
            curr.append(
                min(
                    curr[-1] + 1,
                    prev[j] + 1,
                    prev[j - 1] + (0 if cs == ct else 1),
                )
            )
        prev = curr
    return prev[-1]


def _suggest_command(
    user_input: str,
    registry: ActionRegistry,
    ui_profile: UIProfile,
) -> str | None:
    """Return a suggestion string for a mistyped slash command, or None."""
    if not user_input.startswith("/"):
        return None

    # Extract the typed command base name
    typed = user_input[1:].lstrip().split()[0] if user_input[1:].strip() else ""
    if not typed:
        return None

    from reuleauxcoder.app.commands.specs import TriggerKind

    # Collect all unique base command names from slash triggers
    candidates: set[str] = set()
    for action in registry.iter_actions(ui_profile):
        for trigger in action.matching_triggers(ui_profile, kind=TriggerKind.SLASH):
            base = _extract_base_name(trigger.value)
            if base:
                candidates.add(base)

    if not candidates:
        return None

    # Find closest match by edit distance
    best: str | None = None
    best_dist: int = 999
    for candidate in sorted(candidates):
        if candidate == typed:
            return None  # exact match but parser rejected → subcommand error, don't suggest
        dist = _levenshtein(typed, candidate)
        if dist < best_dist:
            best_dist = dist
            best = candidate

    # Threshold: max 2 edits, proportional to word length
    max_dist = max(1, min(2, len(typed) // 3 + 1))
    if best is not None and best_dist <= max_dist:
        return f"Unknown command '/{typed}'. Did you mean '/{best}'?"

    return f"Unknown command '/{typed}'."


def handle_command(
    user_input: str,
    agent: Agent,
    config: Config,
    current_session_id: str | None,
    ui_bus: UIEventBus,
    ui_profile: UIProfile,
    action_registry: ActionRegistry,
    sessions_dir: Path | None = None,
    skills_service: SkillsService | None = None,
):
    parsed_action = parse_command(
        user_input,
        ui_profile=ui_profile,
        action_registry=action_registry,
        current_session_id=current_session_id,
    )
    if parsed_action is not None:
        try:
            result = dispatch_command(
                parsed_action,
                CommandContext(
                    agent=agent,
                    config=config,
                    ui_bus=ui_bus,
                    ui_profile=ui_profile,
                    action_registry=parsed_action.registry,
                    ui_interactor=getattr(agent, "ui_interactor", None),
                    sessions_dir=sessions_dir,
                    skills_service=skills_service,
                ),
            )
        except Exception as exc:
            ui_bus.error(
                f"Command failed: {exc}",
                kind=UIEventKind.COMMAND,
            )
            return {"action": "continue", "session_id": current_session_id}
        return {
            "action": result.action,
            "session_id": result.session_id
            if result.session_id is not None
            else current_session_id,
            "session_exit_time": result.session_exit_time,
        }

    # No command matched — check for fuzzy suggestions on /-prefixed input
    suggestion = _suggest_command(user_input, action_registry, ui_profile)
    if suggestion is not None:
        ui_bus.warning(suggestion, kind=UIEventKind.COMMAND)
        return {"action": "continue", "session_id": current_session_id}

    return {"action": "chat", "session_id": current_session_id}
