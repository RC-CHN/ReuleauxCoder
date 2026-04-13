"""Reusable text-template matchers for command parsing."""

from __future__ import annotations


def normalize_input(user_input: str) -> str:
    """Normalize whitespace for parser-friendly matching."""
    return " ".join(user_input.strip().split())


def match_template(user_input: str, template: str, *, case_insensitive: bool = False) -> dict[str, str] | None:
    """Match command input against a template with optional placeholders.

    Placeholder forms:
    - ``{name}``: capture one token
    - ``{name+}``: capture all remaining tokens (must be last)
    """
    input_tokens = normalize_input(user_input).split()
    template_tokens = normalize_input(template).split()

    captures: dict[str, str] = {}
    i = 0
    j = 0
    while i < len(template_tokens):
        template_token = template_tokens[i]

        if template_token.startswith("{") and template_token.endswith("}"):
            key = template_token[1:-1]
            if key.endswith("+"):
                key = key[:-1]
                if i != len(template_tokens) - 1:
                    return None
                if j >= len(input_tokens):
                    return None
                captures[key] = " ".join(input_tokens[j:])
                j = len(input_tokens)
                i += 1
                continue

            if j >= len(input_tokens):
                return None
            captures[key] = input_tokens[j]
            j += 1
            i += 1
            continue

        if j >= len(input_tokens):
            return None

        left = template_token.lower() if case_insensitive else template_token
        right = input_tokens[j].lower() if case_insensitive else input_tokens[j]
        if left != right:
            return None

        i += 1
        j += 1

    if j != len(input_tokens):
        return None

    return captures


def matches_any(user_input: str, templates: tuple[str, ...], *, case_insensitive: bool = False) -> bool:
    """Return True if input matches any provided template."""
    return any(match_template(user_input, template, case_insensitive=case_insensitive) is not None for template in templates)
