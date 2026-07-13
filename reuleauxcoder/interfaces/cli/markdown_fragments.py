"""Retained Rich Markdown rendering for prompt_toolkit transcript cells."""

from __future__ import annotations

from collections import OrderedDict
from io import StringIO

from markdown_it import MarkdownIt
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from rich.console import Console
from rich.markdown import Markdown

from reuleauxcoder.interfaces.cli.streaming import find_committed_boundary

_PLAIN_MARKDOWN_PARSER = MarkdownIt().enable("strikethrough").enable("table")


class RetainedMarkdownRenderer:
    """Cache width-aware Markdown fragments without writing ANSI to stdout."""

    def __init__(self, *, max_entries: int = 1_000) -> None:
        self.max_entries = max(20, max_entries)
        self._cache: OrderedDict[
            tuple[str, int, int, int, bool], tuple[tuple[str, str], ...]
        ] = OrderedDict()

    def render(
        self,
        *,
        cell_id: str,
        revision: int,
        text: str,
        complete: bool,
        width: int,
        theme_revision: int = 0,
    ) -> list[tuple[str, str]]:
        width = max(20, width)
        key = (cell_id, revision, width, theme_revision, complete)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return list(cached)

        if complete:
            fragments = _markdown_fragments(text, width=width)
        else:
            boundary = find_committed_boundary(text)
            if boundary is None:
                fragments = [("class:assistant", text)] if text else []
            else:
                fragments = _markdown_fragments(text[:boundary], width=width)
                if tail := text[boundary:]:
                    fragments.append(("class:assistant", tail))
        if text and (not fragments or not fragments[-1][1].endswith("\n")):
            fragments.append(("", "\n"))
        if complete and text:
            fragments.append(("", "\n"))
        self._cache[key] = tuple(fragments)
        self._prune()
        return list(fragments)

    def _prune(self) -> None:
        while len(self._cache) > self.max_entries:
            self._cache.popitem(last=False)


def _markdown_fragments(text: str, *, width: int) -> list[tuple[str, str]]:
    plain = _plain_markdown_fragments(text)
    if plain is not None:
        return plain
    return _rich_markdown_fragments(text, width=width)


def _plain_markdown_fragments(text: str) -> list[tuple[str, str]] | None:
    """Return Rich-equivalent fragments for paragraphs without styled syntax."""
    tokens = _PLAIN_MARKDOWN_PARSER.parse(text)
    if not tokens:
        return [] if not text else None
    paragraphs: list[str] = []
    index = 0
    while index < len(tokens):
        if (
            index + 2 >= len(tokens)
            or tokens[index].type != "paragraph_open"
            or tokens[index + 1].type != "inline"
            or tokens[index + 2].type != "paragraph_close"
        ):
            return None
        inline = tokens[index + 1]
        content: list[str] = []
        for child in inline.children or ():
            if child.type == "text":
                content.append(child.content)
            elif child.type == "softbreak":
                content.append(" ")
            else:
                return None
        paragraphs.append("".join(content))
        index += 3

    fragments: list[tuple[str, str]] = []
    for paragraph_index, paragraph in enumerate(paragraphs):
        if paragraph:
            fragments.append(("class:assistant", paragraph))
        fragments.append(
            ("", "\n\n" if paragraph_index + 1 < len(paragraphs) else "\n")
        )
    return fragments


def _rich_markdown_fragments(text: str, *, width: int) -> list[tuple[str, str]]:
    if not text:
        return []
    stream = StringIO()
    console = Console(
        file=stream,
        force_terminal=True,
        color_system="truecolor",
        width=width,
        legacy_windows=False,
    )
    console.print(Markdown(text), end="", soft_wrap=True)
    converted = list(to_formatted_text(ANSI(stream.getvalue())))
    return _compact_fragments(_rstrip_visual_lines(converted))


def _rstrip_visual_lines(
    fragments: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    output: list[tuple[str, str]] = []
    line: list[tuple[str, str]] = []
    for style, text in fragments:
        for character in text:
            if character == "\n":
                while line and line[-1][1].isspace():
                    line.pop()
                output.extend(line)
                output.append(("", "\n"))
                line = []
            else:
                line.append((_assistant_style(style), character))
    while line and line[-1][1].isspace():
        line.pop()
    output.extend(line)
    return output


def _assistant_style(style: str) -> str:
    return f"class:assistant {style}".strip()


def _compact_fragments(
    fragments: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    compact: list[tuple[str, str]] = []
    for style, text in fragments:
        if compact and compact[-1][0] == style:
            previous_style, previous_text = compact[-1]
            compact[-1] = (previous_style, previous_text + text)
        else:
            compact.append((style, text))
    return compact
