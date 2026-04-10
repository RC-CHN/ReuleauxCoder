"""Help text generation from declarative action specs."""

from __future__ import annotations

from collections import defaultdict

from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.specs import TriggerKind
from reuleauxcoder.interfaces.ui_registry import UIProfile


def build_help_markdown(ui_profile: UIProfile, action_registry: ActionRegistry) -> str:
    """Build CLI-like help markdown from action registry metadata."""
    grouped: dict[str, list[str]] = defaultdict(list)

    for action in action_registry.iter_actions(ui_profile):
        slash_triggers = action.matching_triggers(ui_profile, kind=TriggerKind.SLASH)
        if not slash_triggers:
            continue
        usage = slash_triggers[0].value
        grouped[action.feature_id].append(f"- `{usage}` — {action.description}")

    lines: list[str] = ["**Commands:**"]
    for feature_id in sorted(grouped):
        lines.append("")
        lines.append(f"**{feature_id}:**")
        lines.extend(sorted(grouped[feature_id]))

    return "\n".join(lines)
