"""Cell-width-aware formatting primitives shared by TUI components."""

from __future__ import annotations

from prompt_toolkit.utils import get_cwidth


def fit_styled_row(
    fragments: list[tuple[str, str]], width: int
) -> tuple[tuple[str, str], ...]:
    """Clip styled fragments by terminal cell width and preserve their tones."""
    output: list[tuple[str, str]] = []
    used = 0
    clipped = False
    for style, text in fragments:
        chunk = ""
        for character in text:
            char_width = max(0, get_cwidth(character))
            if used + char_width > width:
                clipped = True
                break
            chunk += character
            used += char_width
        if chunk:
            output.append((style, chunk))
        if clipped:
            break
    if clipped and width > 0:
        while output and used >= width:
            style, text = output[-1]
            if not text:
                output.pop()
                continue
            removed = text[-1]
            used -= max(0, get_cwidth(removed))
            text = text[:-1]
            if text:
                output[-1] = (style, text)
            else:
                output.pop()
        output.append(("class:panel.detail", "…"))
    return tuple(output)


def wrap_fragments(
    fragments: list[tuple[str, str]], *, width: int
) -> list[tuple[str, str]]:
    """Pre-wrap styled fragments so Window scroll units equal visual rows."""
    width = max(1, width)
    output: list[tuple[str, str]] = []
    column = 0
    for style, text in fragments:
        chunk = ""
        for character in text:
            if character == "\n":
                if chunk:
                    output.append((style, chunk))
                    chunk = ""
                output.append(("", "\n"))
                column = 0
                continue
            character_width = max(0, get_cwidth(character))
            if column and column + character_width > width:
                if chunk:
                    output.append((style, chunk))
                    chunk = ""
                output.append(("", "\n"))
                column = 0
            chunk += character
            column += character_width
        if chunk:
            output.append((style, chunk))
    return output


def wrapped_row_count(text: str, width: int, *, cap: int = 8) -> int:
    """Count visual rows for single-line text wrapped at a cell width."""
    width = max(1, width)
    used = 0
    rows = 1
    for character in text:
        char_width = max(0, get_cwidth(character))
        if used and used + char_width > width:
            rows += 1
            used = 0
        used += char_width
    return min(cap, rows)


def fragments_to_visual_lines(
    fragments: list[tuple[str, str]],
) -> tuple[tuple[tuple[str, str], ...], ...]:
    """Group newline-delimited fragments into immutable visual rows."""
    lines: list[list[tuple[str, str]]] = [[]]
    for style, text in fragments:
        parts = text.split("\n")
        for index, part in enumerate(parts):
            if part:
                if lines[-1] and lines[-1][-1][0] == style:
                    previous_style, previous_text = lines[-1][-1]
                    lines[-1][-1] = (previous_style, previous_text + part)
                else:
                    lines[-1].append((style, part))
            if index + 1 < len(parts):
                lines.append([])
    return tuple(tuple(line) for line in lines)


def fit_display(text: str, width: int) -> str:
    """Clip text by terminal cell width, including CJK and emoji."""
    if get_cwidth(text) <= width:
        return text
    target = max(1, width - 1)
    result: list[str] = []
    used = 0
    for char in text:
        char_width = max(0, get_cwidth(char))
        if used + char_width > target:
            break
        result.append(char)
        used += char_width
    return "".join(result) + "…"


def first_meaningful_line(text: str) -> str:
    """Return the first non-empty line without surrounding whitespace."""
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def clip(text: str, width: int) -> str:
    """Clip plain text by codepoint count for compact descriptive labels."""
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


__all__ = [
    "clip",
    "first_meaningful_line",
    "fit_display",
    "fit_styled_row",
    "fragments_to_visual_lines",
    "wrap_fragments",
    "wrapped_row_count",
]
