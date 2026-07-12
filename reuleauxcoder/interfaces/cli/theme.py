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
    diff_header: str = "bold #67e8f9"
    diff_addition: str = "bold #a3ff5f"
    diff_deletion: str = "bold #ff6b81"
    diff_context: str = "#d8dee9"
    diff_fold: str = "bold #ffd75f"
    frame: str = "#8993a4"

    def style(self, tone: DisplayTone) -> str:
        return self.styles.get(tone, "")

    def label(self, value: str, tone: DisplayTone = DisplayTone.ACCENT) -> Text:
        text = Text(f" {value.upper()} ")
        text.stylize(Style.parse(self.label_styles.get(tone, "bold reverse")))
        return text


FORGE_THEME = CLITheme(
    name="forge",
    styles={
        DisplayTone.NEUTRAL: "#f4f7fb",
        DisplayTone.MUTED: "#aeb8c7",
        DisplayTone.ACCENT: "bold #67e8f9",
        DisplayTone.SUCCESS: "bold #a3ff5f",
        DisplayTone.WARNING: "bold #ffd75f",
        DisplayTone.ERROR: "bold #ff6b81",
    },
    label_styles={
        DisplayTone.NEUTRAL: "bold #101318 on #e5e9f0",
        DisplayTone.MUTED: "bold #f4f7fb on #596273",
        DisplayTone.ACCENT: "bold #071013 on #67e8f9",
        DisplayTone.SUCCESS: "bold #0b1207 on #a3ff5f",
        DisplayTone.WARNING: "bold #171105 on #ffd75f",
        DisplayTone.ERROR: "bold #fff7f8 on #d92f4c",
    },
)


DEFAULT_CLI_THEME = FORGE_THEME
