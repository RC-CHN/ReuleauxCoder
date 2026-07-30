"""Width-model integrity for TUI formatting helpers.

prompt_toolkit renders control characters using two-cell caret notation
while ``get_cwidth`` counts them as zero width. Fragment text carrying raw
control characters therefore overflows its measured width, wraps physically
and corrupts the whole layout (scrollbar drifts sideways, the execution
panel is pushed out). The helpers must sanitize before measuring.
"""

from prompt_toolkit.utils import get_cwidth

from reuleauxcoder.interfaces.tui.formatting import (
    fit_display,
    fit_styled_row,
    sanitize_display_text,
    wrap_fragments,
    wrapped_row_count,
)


def test_sanitize_strips_ansi_csi_sequences() -> None:
    assert sanitize_display_text("A\x1b[31mB\x1b[0mC") == "ABC"
    assert sanitize_display_text("\x1b[38;5;123mtruecolor\x1b[39m") == "truecolor"


def test_sanitize_strips_osc_sequences() -> None:
    assert sanitize_display_text("x\x1b]8;;http://e\x07link\x1b]8;;\x07y") == "xlinky"
    assert sanitize_display_text("x\x1b]0;title\x1b\\y") == "xy"


def test_sanitize_removes_zero_width_controls() -> None:
    assert sanitize_display_text("ab\rcd\x07e\x01f") == "abcdef"


def test_sanitize_expands_tabs() -> None:
    assert sanitize_display_text("a\tb") == "a    b"


def test_sanitize_preserves_newlines_and_clean_text() -> None:
    clean = "plain 文本 ✓ ok\nnext"
    assert sanitize_display_text(clean) == clean


def test_sanitized_text_never_contains_caret_rendered_controls() -> None:
    sanitized = sanitize_display_text("A\x1b[31mB\rc\x07\x7f\x9b")
    assert all(get_cwidth(char) > 0 or char == "\n" for char in sanitized)


def test_wrap_fragments_sanitizes_control_characters() -> None:
    wrapped = wrap_fragments([("", "A\x1b[31mBC")], width=10)
    text = "".join(part for _, part in wrapped)
    assert text == "ABC"


def test_fit_styled_row_sanitizes_before_clipping() -> None:
    # Five visible cells plus zero-width controls: without sanitizing, the
    # row physically occupies 17 cells and overflows an 8-cell panel row.
    row = fit_styled_row([("class:x", "\x1b[31mAB\rCD\x07E")], 8)
    text = "".join(part for _, part in row)
    assert text == "ABCDE"


def test_fit_display_strips_ansi_before_measuring() -> None:
    assert fit_display("\x1b[32mgreen\x1b[0m", 20) == "green"


def test_wrapped_row_count_ignores_escape_sequences() -> None:
    assert wrapped_row_count("\x1b[31mshort\x1b[0m", 80) == 1
