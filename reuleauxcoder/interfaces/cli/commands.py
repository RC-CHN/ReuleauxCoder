"""CLI command handlers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from reuleauxcoder.app.commands import CommandContext, dispatch_command, parse_command
from reuleauxcoder.app.commands.models import CommandEffect
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.infrastructure.persistence.session_store import SessionRestoreError
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind
from reuleauxcoder.interfaces.ui_registry import UIProfile

if TYPE_CHECKING:
    from reuleauxcoder.domain.agent.agent import Agent
    from reuleauxcoder.domain.config.models import Config
    from reuleauxcoder.extensions.skills.service import SkillsService


_RUNTIME_CONFIG_ACTIONS = frozenset(
    {
        "approval.set",
        "approval.set_global",
        "mcp.enable",
        "mcp.disable",
        "mode.switch",
        "model.use_main",
        "model.use_sub",
        "model.set_main",
        "model.set_sub",
        "model.switch",
        "skills.reload",
        "skills.enable",
        "skills.disable",
        "system.debug",
    }
)
_SESSION_ACTIONS = frozenset({"sessions.resume", "sessions.save", "sessions.new"})


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


def _invalid_command_usage(
    user_input: str,
    registry: ActionRegistry,
    ui_profile: UIProfile,
) -> str | None:
    """Explain malformed input for a known slash-command namespace."""
    if not user_input.startswith("/"):
        return None

    typed = user_input[1:].lstrip().split()[0] if user_input[1:].strip() else ""
    if not typed:
        return "Unknown command '/'. Use /help to list available commands."

    from reuleauxcoder.app.commands.specs import TriggerKind

    usages: list[str] = []
    for action in registry.iter_actions(ui_profile):
        for trigger in action.matching_triggers(ui_profile, kind=TriggerKind.SLASH):
            if _extract_base_name(trigger.value) == typed and trigger.value not in usages:
                usages.append(trigger.value)

    if not usages:
        return None
    rendered = "; ".join(usages)
    return f"Invalid '/{typed}' command. Usage: {rendered}"


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
            effect = CommandEffect()
            result = dispatch_command(
                parsed_action,
                CommandContext(
                    agent=agent,
                    config=config,
                    effect=effect,
                    ui_profile=ui_profile,
                    action_registry=parsed_action.registry,
                    ui_interactor=getattr(agent, "ui_interactor", None),
                    sessions_dir=sessions_dir,
                    skills_service=skills_service,
                ),
            )
            _record_command_control_event(agent, parsed_action.action.action_id, result)
        except SessionRestoreError as error:
            # The active session remains usable. This is a failed command, not
            # degraded state restored from disk, so publish it through the
            # generic runtime-incident channel.
            recorder_error_type = None
            record_runtime_issue = getattr(agent, "record_runtime_issue", None)
            if callable(record_runtime_issue):
                try:
                    record_runtime_issue(error.phase, error.error_type, error.ref)
                except KeyboardInterrupt:
                    raise
                except BaseException as recorder_error:
                    name = type(recorder_error).__name__
                    recorder_error_type = (
                        name
                        if name
                        and len(name) <= 64
                        and name.isascii()
                        and name.replace("_", "").isalnum()
                        else "Exception"
                    )
            result = CommandEffect()
            result.error(
                str(error),
                kind=UIEventKind.SESSION,
                phase=error.phase,
                error_type=error.error_type,
                ref=error.ref,
                runtime_issue_recorder_error_type=recorder_error_type,
            )
            result.finish(control="continue")
        except Exception as exc:
            result = CommandEffect()
            result.error(f"Command failed: {exc}", kind=UIEventKind.COMMAND)
            result.finish(control="continue")
        _apply_command_effect(result, ui_bus)
        return {
            "action": result.control,
            "action_id": parsed_action.action.action_id,
            "session_id": result.session_id
            if result.session_id is not None
            else current_session_id,
            "session_exit_time": result.session_exit_time,
        }

    # No command matched — check for fuzzy suggestions on /-prefixed input
    suggestion = _suggest_command(user_input, action_registry, ui_profile)
    if suggestion is None:
        suggestion = _invalid_command_usage(user_input, action_registry, ui_profile)
    if suggestion is not None:
        ui_bus.warning(suggestion, kind=UIEventKind.COMMAND)
        return {
            "action": "continue",
            "action_id": None,
            "session_id": current_session_id,
        }

    # Slash-prefixed input belongs to the local command namespace.  It must
    # never leak into model context, even when no registered parser accepts it.
    if user_input.startswith("/"):
        ui_bus.warning(
            "Unknown slash command. Use /help to list available commands.",
            kind=UIEventKind.COMMAND,
        )
        return {
            "action": "continue",
            "action_id": None,
            "session_id": current_session_id,
        }

    return {
        "action": "chat",
        "action_id": None,
        "session_id": current_session_id,
    }


def _record_command_control_event(agent, action_id: str, result: CommandEffect) -> None:
    ledger = getattr(agent, "history_ledger", None)
    if ledger is None:
        return
    if action_id in _RUNTIME_CONFIG_ACTIONS:
        kind = "runtime_config_changed"
    elif action_id in _SESSION_ACTIONS:
        kind = "session_lifecycle"
    else:
        return
    ledger.append(
        kind,
        {
            "action_id": action_id,
            "state_changes": result.state,
            "control": result.control,
            "session_id": result.session_id,
        },
        agent_id=getattr(agent, "agent_id", None),
        turn_id=getattr(agent, "_current_turn_id", None),
    )
    persist = getattr(agent, "persist_runtime_snapshot", None)
    if callable(persist):
        persist()


def _apply_command_effect(result: CommandEffect, ui_bus: UIEventBus) -> None:
    """Apply one command effect to the active interface event bus."""
    for notice in result.notifications:
        try:
            kind = UIEventKind(notice.kind)
        except ValueError:
            kind = UIEventKind.COMMAND
        emit = getattr(ui_bus, notice.level)
        emit(
            notice.message,
            kind=kind,
            payload=notice.payload,
            **notice.metadata,
        )

    for view in result.views:
        if view.action == "refresh":
            ui_bus.refresh_view(
                view.view_type,
                title=view.title,
                reuse_key=view.reuse_key,
                view_model=view.view_model,
            )
        else:
            ui_bus.open_view(
                view.view_type,
                title=view.title,
                focus=view.focus,
                reuse_key=view.reuse_key,
                view_model=view.view_model,
            )
