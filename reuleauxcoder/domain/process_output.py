"""Safe display projection for untrusted process text."""

from __future__ import annotations


def escape_terminal_controls(text: str) -> tuple[str, bool]:
    """Make terminal control bytes visible without changing retained output."""
    pieces: list[str] = []
    filtered = False
    for character in text:
        codepoint = ord(character)
        if character in {"\n", "\t"}:
            pieces.append(character)
            continue
        if codepoint < 32 or codepoint == 127 or 0x80 <= codepoint <= 0x9F:
            filtered = True
            if character == "\r":
                pieces.append("\\r")
            elif codepoint <= 0xFF:
                pieces.append(f"\\x{codepoint:02x}")
            else:
                pieces.append(f"\\u{codepoint:04x}")
            continue
        pieces.append(character)
    return "".join(pieces), filtered


def terminal_safe_display(text: str) -> str:
    """Return text safe for a terminal and disclose any display escaping."""
    safe, filtered = escape_terminal_controls(text)
    if not filtered:
        return safe
    return safe + "\n[terminal control bytes escaped for display]\n"


__all__ = ["escape_terminal_controls", "terminal_safe_display"]
