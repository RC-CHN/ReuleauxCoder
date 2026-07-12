"""prompt_toolkit-native FORGE prompt fragments."""

from __future__ import annotations

from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.application.current import get_app
from prompt_toolkit.styles import Style


FORGE_USER_PROMPT_STYLE = Style.from_dict(
    {
        "": "bg:#191827 #f7f5ff",
        "prompt.label": "bold bg:#7c6cff #ffffff",
        "prompt.rail": "bold bg:#191827 #a99fff",
        "prompt.hint": "bg:#191827 #77728f",
    }
)


def forge_user_prompt(buffer_text: str = "") -> FormattedText:
    """Return a styled prompt without embedding ANSI width ambiguities."""
    label = "CMD" if buffer_text.lstrip().startswith("/") else "YOU"
    return FormattedText(
        [
            ("class:prompt.label", f" {label} "),
            ("class:prompt.rail", " // "),
            ("class:prompt.hint", "input  "),
        ]
    )


def forge_active_prompt() -> FormattedText:
    """Resolve the label from the live prompt buffer on each repaint."""
    try:
        buffer_text = get_app().current_buffer.text
    except (AttributeError, RuntimeError):
        buffer_text = ""
    return forge_user_prompt(buffer_text)
