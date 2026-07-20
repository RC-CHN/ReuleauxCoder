"""Registry-driven slash command popup entries (UI-neutral, testable)."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.specs import TriggerKind
from reuleauxcoder.interfaces.ui_registry import UIProfile


@dataclass(frozen=True, slots=True)
class PopupEntry:
    """One completable slash command candidate."""

    completion: str
    description: str
    has_arg: bool
    interactive: bool


def _strip_placeholders(template: str) -> tuple[str, bool]:
    """Remove <...> and {...} placeholders; report whether any existed."""
    output: list[str] = []
    has_arg = False
    depth = 0
    for character in template:
        if character in "<{":
            depth += 1
            has_arg = True
            continue
        if character in ">}" and depth:
            depth -= 1
            continue
        if depth == 0:
            output.append(character)
    return "".join(output).rstrip(), has_arg


def build_popup_entries(
    registry: ActionRegistry, ui_profile: UIProfile
) -> tuple[PopupEntry, ...]:
    """Collect deduplicated slash candidates for the popup.

    Aliases collapse to the first-declared root of the same action (e.g.
    ``/sessions`` and ``/jobs`` hide behind ``/session`` and ``/agents``);
    distinct sub-commands are kept. Families without a bare trigger still
    gain a synthetic root row so users can discover their sub-commands.
    """
    entries: dict[str, PopupEntry] = {}
    for action in registry.iter_actions(ui_profile):
        candidates: list[PopupEntry] = []
        for trigger in action.matching_triggers(ui_profile, kind=TriggerKind.SLASH):
            completion, has_arg = _strip_placeholders(trigger.value)
            if not completion:
                continue
            candidates.append(
                PopupEntry(
                    completion=completion,
                    description=action.description,
                    has_arg=has_arg,
                    interactive=action.interactive,
                )
            )
        if not candidates:
            continue
        canonical_root = candidates[0].completion.split(" ", 1)[0]
        for entry in candidates:
            if entry.completion.split(" ", 1)[0] != canonical_root:
                continue  # alias trigger of the same action
            existing = entries.get(entry.completion)
            if existing is None or (existing.has_arg and not entry.has_arg):
                entries[entry.completion] = entry

    roots = {entry.completion.split(" ", 1)[0] for entry in entries.values()}
    for root in sorted(roots):
        if root in entries:
            continue
        subcommands = sorted(
            (
                entry
                for entry in entries.values()
                if entry.completion.startswith(root + " ")
            ),
            key=lambda entry: entry.completion,
        )
        if not subcommands:
            continue
        first = subcommands[0]
        entries[root] = PopupEntry(
            completion=root,
            description=first.description,
            has_arg=False,
            interactive=first.interactive,
        )

    return tuple(sorted(entries.values(), key=lambda entry: entry.completion))


def filter_entries(
    entries: tuple[PopupEntry, ...], buffer_text: str
) -> tuple[PopupEntry, ...]:
    """Return candidates for the current composer text.

    ``/mo`` filters root commands by prefix; ``/agents c`` filters
    sub-commands; a bare ``/agents `` (trailing space) lists every
    sub-command under that root.
    """
    text = buffer_text.lstrip()
    if not text.startswith("/"):
        return ()
    lowered = text.lower()
    if " " not in lowered:
        return tuple(
            entry
            for entry in entries
            if " " not in entry.completion
            and entry.completion.lower().startswith(lowered)
        )
    root, rest = lowered.split(" ", 1)
    prefix = root + " "
    if not rest.strip():
        return tuple(
            entry
            for entry in entries
            if " " in entry.completion and entry.completion.lower().startswith(prefix)
        )
    return tuple(
        entry
        for entry in entries
        if " " in entry.completion and entry.completion.lower().startswith(lowered)
    )
