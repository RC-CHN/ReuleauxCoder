"""Shared helpers for declarative command actions."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.params import EnumParam, StrParam
from reuleauxcoder.app.commands.specs import TriggerKind, TriggerSpec
from reuleauxcoder.interfaces.ui_registry import UICapability

UI_TARGETS = frozenset({"cli", "tui", "vscode"})
TEXT_REQUIRED = frozenset({UICapability.TEXT_INPUT})


@dataclass(frozen=True, slots=True)
class EmptyCommand:
    """Marker command object for actions with no parse payload."""


def slash_trigger(value: str) -> TriggerSpec:
    """Build a CLI slash trigger declaration with text-input capability requirement."""
    return TriggerSpec(
        kind=TriggerKind.SLASH,
        value=value,
        ui_targets=frozenset({"cli"}),
        required_capabilities=TEXT_REQUIRED,
    )


def non_empty_text(*, lower: bool = False, reject: frozenset[str] = frozenset()) -> StrParam:
    """Common non-empty text parameter parser."""
    return StrParam(non_empty=True, lower=lower, reject=reject)


def enum_text(values: set[str] | frozenset[str], *, case_insensitive: bool = True) -> EnumParam:
    """Common enum text parameter parser."""
    return EnumParam(values=frozenset(values), case_insensitive=case_insensitive)
