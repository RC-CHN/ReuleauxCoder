"""Rich-only visual tokens for the FORGE CLI theme."""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.style import Style
from rich.text import Text

from reuleauxcoder.presentation.semantics import DisplayTone


@dataclass(frozen=True, slots=True)
class CLITheme:
    """One replaceable set of CLI visual decisions.

    Presentation semantics deliberately live outside this class.  A future TUI
    may reuse those semantics without importing Rich or these style strings.
    """

    name: str
    styles: dict[DisplayTone, str] = field(default_factory=dict)
    label_styles: dict[DisplayTone, str] = field(default_factory=dict)
    diff_header: str = "bold bright_cyan"
    diff_addition: str = "bright_green"
    diff_deletion: str = "bright_red"
    diff_context: str = "dim"
    diff_fold: str = "bold yellow"
    frame: str = "bright_black"

    def style(self, tone: DisplayTone) -> str:
        return self.styles.get(tone, "")

    def label(self, value: str, tone: DisplayTone = DisplayTone.ACCENT) -> Text:
        text = Text(f" {value.upper()} ")
        text.stylize(Style.parse(self.label_styles.get(tone, "bold reverse")))
        return text


FORGE_THEME = CLITheme(
    name="forge",
    styles={
        DisplayTone.NEUTRAL: "",
        DisplayTone.MUTED: "dim",
        DisplayTone.ACCENT: "bright_cyan",
        DisplayTone.SUCCESS: "bright_green",
        DisplayTone.WARNING: "bright_yellow",
        DisplayTone.ERROR: "bold bright_red",
    },
    label_styles={
        DisplayTone.NEUTRAL: "bold reverse",
        DisplayTone.MUTED: "bold white on bright_black",
        DisplayTone.ACCENT: "bold black on bright_cyan",
        DisplayTone.SUCCESS: "bold black on bright_green",
        DisplayTone.WARNING: "bold black on bright_yellow",
        DisplayTone.ERROR: "bold white on red",
    },
)


DEFAULT_CLI_THEME = FORGE_THEME
