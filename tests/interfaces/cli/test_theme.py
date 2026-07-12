from rich.style import Style
from prompt_toolkit.formatted_text import fragment_list_to_text, to_formatted_text

from reuleauxcoder.interfaces.cli.prompt import (
    FORGE_USER_PROMPT_STYLE,
    forge_user_prompt,
)
from reuleauxcoder.interfaces.cli.theme import FORGE_THEME
from reuleauxcoder.presentation.semantics import DisplayTone


def test_forge_theme_uses_explicit_high_contrast_foregrounds() -> None:
    styles = [Style.parse(FORGE_THEME.style(tone)) for tone in DisplayTone]

    assert all(style.color is not None for style in styles)
    assert all("dim" not in FORGE_THEME.style(tone) for tone in DisplayTone)
    assert len({str(style.color) for style in styles}) == len(DisplayTone)


def test_forge_diff_roles_remain_visually_distinct() -> None:
    styles = {
        Style.parse(FORGE_THEME.diff_header),
        Style.parse(FORGE_THEME.diff_addition),
        Style.parse(FORGE_THEME.diff_deletion),
        Style.parse(FORGE_THEME.diff_context),
        Style.parse(FORGE_THEME.diff_fold),
    }

    assert len(styles) == 5
    assert Style.parse(FORGE_THEME.diff_addition).bold is True
    assert Style.parse(FORGE_THEME.diff_deletion).bold is True
    assert Style.parse(FORGE_THEME.diff_addition).bgcolor is not None
    assert Style.parse(FORGE_THEME.diff_deletion).bgcolor is not None


def test_forge_user_prompt_uses_native_formatted_fragments() -> None:
    fragments = to_formatted_text(forge_user_prompt())

    assert fragment_list_to_text(fragments) == " YOU   › "
    assert fragments[0][0] == "class:prompt.label"
    assert FORGE_USER_PROMPT_STYLE.style_rules[0] == (
        "",
        "bg:#14272d #f4f7fb",
    )
