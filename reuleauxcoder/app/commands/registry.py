"""Action registry and parser/dispatcher helpers."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.models import CommandContext, CommandResult
from reuleauxcoder.app.commands.specs import (
    ActionSpec,
    CommandParseContext,
    TriggerKind,
)
from reuleauxcoder.interfaces.ui_registry import UIProfile


@dataclass(frozen=True, slots=True)
class ParsedAction:
    """A parsed command paired with the action spec that matched it."""

    command: object
    action: ActionSpec
    registry: "ActionRegistry"


class ActionRegistry:
    """Static action registry for explicit declarative specs."""

    def __init__(self, actions: list[ActionSpec] | None = None):
        self._actions = list(actions or [])

    def register(self, action: ActionSpec) -> None:
        """Register one action spec."""
        self._actions.append(action)

    def register_many(self, actions: list[ActionSpec] | tuple[ActionSpec, ...]) -> None:
        """Register multiple action specs."""
        self._actions.extend(actions)

    def iter_actions(self, ui_profile: UIProfile) -> list[ActionSpec]:
        """Return actions available for a UI profile."""
        return [
            action for action in self._actions if action.is_available_in(ui_profile)
        ]

    def parse(
        self,
        user_input: str,
        *,
        ui_profile: UIProfile,
        current_session_id: str | None = None,
    ) -> ParsedAction | None:
        """Try to parse user input using available action parsers."""
        parse_ctx = CommandParseContext(
            current_session_id=current_session_id, ui_profile=ui_profile
        )
        for action in self.iter_actions(ui_profile):
            if action.parser is None:
                continue
            if not action.matching_triggers(ui_profile, kind=TriggerKind.SLASH):
                continue
            parsed = action.parser(user_input, parse_ctx)
            if parsed is not None:
                return ParsedAction(command=parsed, action=action, registry=self)
        return None

    def dispatch(self, parsed: ParsedAction, ctx: CommandContext) -> CommandResult:
        """Dispatch a parsed action to its handler."""
        if parsed.action.handler is None:
            return CommandResult(action="continue")
        return parsed.action.handler(parsed.command, ctx)
