from reuleauxcoder.domain.process_output import (
    escape_terminal_controls,
    terminal_safe_display,
)


def test_terminal_controls_are_made_visible_without_hiding_payload() -> None:
    raw = "before\x1b]52;c;secret\x07after\rnext"

    safe, filtered = escape_terminal_controls(raw)

    assert filtered is True
    assert "\x1b" not in safe
    assert "\x07" not in safe
    assert "\r" not in safe
    assert safe == "before\\x1b]52;c;secret\\x07after\\rnext"
    assert "terminal control bytes escaped" in terminal_safe_display(raw)


def test_normal_unicode_and_line_structure_are_unchanged() -> None:
    raw = "你好\nplain\ttext\ufffd"

    assert escape_terminal_controls(raw) == (raw, False)
    assert terminal_safe_display(raw) == raw
