"""Help text generation from declarative action specs."""

from __future__ import annotations

from collections import defaultdict

from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.specs import TriggerKind
from reuleauxcoder.app.commands.view_models import (
    HelpCommandViewModel,
    HelpSectionViewModel,
    HelpViewModel,
)
from reuleauxcoder.interfaces.ui_registry import UIProfile


def build_help_view(
    ui_profile: UIProfile, action_registry: ActionRegistry
) -> HelpViewModel:
    """Build a framework-neutral help model from action metadata."""
    grouped: dict[str, list[HelpCommandViewModel]] = defaultdict(list)

    for action in action_registry.iter_actions(ui_profile):
        slash_triggers = action.matching_triggers(ui_profile, kind=TriggerKind.SLASH)
        if not slash_triggers:
            continue
        usage = slash_triggers[0].value
        grouped[action.feature_id].append(
            HelpCommandViewModel(usage=usage, description=action.description)
        )

    return HelpViewModel(
        sections=tuple(
            HelpSectionViewModel(
                feature_id=feature_id,
                commands=tuple(sorted(commands, key=lambda item: item.usage)),
            )
            for feature_id, commands in sorted(grouped.items())
        )
    )


def build_help_markdown(ui_profile: UIProfile, action_registry: ActionRegistry) -> str:
    """Compatibility text projection; UI presenters should consume the model."""
    view = build_help_view(ui_profile, action_registry)

    lines: list[str] = ["**Commands:**"]
    for section in view.sections:
        lines.append("")
        lines.append(f"**{section.feature_id}:**")
        lines.extend(
            f"- `{command.usage}` — {command.description}"
            for command in section.commands
        )

    return "\n".join(lines)
