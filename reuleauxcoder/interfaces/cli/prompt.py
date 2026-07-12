"""prompt_toolkit-native FORGE prompt fragments."""

from __future__ import annotations

from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.styles import Style


FORGE_USER_PROMPT_STYLE = Style.from_dict(
    {
        "": "bg:#14272d #f4f7fb",
        "prompt.label": "bold bg:#67e8f9 #071013",
        "prompt.rail": "bold bg:#14272d #67e8f9",
    }
)


def forge_user_prompt() -> FormattedText:
    """Return a styled prompt without embedding ANSI width ambiguities."""
    return FormattedText(
        [
            ("class:prompt.label", " YOU "),
            ("class:prompt.rail", "  › "),
        ]
    )
