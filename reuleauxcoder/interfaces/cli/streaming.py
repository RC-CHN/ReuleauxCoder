"""Incremental Markdown buffering for the CLI assistant stream."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from markdown_it import MarkdownIt

_SELF_CLOSING_BLOCKS = frozenset(("fence", "code_block", "hr", "html_block"))
_parser: "MarkdownIt | None" = None


def find_committed_boundary(text: str) -> int | None:
    """Return the safe prefix containing all complete Markdown blocks."""
    global _parser
    if _parser is None:
        from markdown_it import MarkdownIt

        _parser = MarkdownIt().enable("strikethrough").enable("table")
    tokens = _parser.parse(text)
    block_maps: list[list[int]] = []
    depth = 0
    for token in tokens:
        if token.nesting == 1:
            if depth == 0 and token.map is not None:
                block_maps.append(token.map)
            depth += 1
        elif token.nesting == -1:
            depth -= 1
        elif (
            depth == 0 and token.type in _SELF_CLOSING_BLOCKS and token.map is not None
        ):
            block_maps.append(token.map)
    if len(block_maps) < 2:
        return None
    target_line = block_maps[-2][1]
    offset = 0
    for _ in range(target_line):
        offset = text.index("\n", offset) + 1
    return offset


@dataclass(slots=True)
class ContentBlock:
    text_parts: list[str] = field(default_factory=list)
    rendered_length: int = 0

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    @property
    def pending_text(self) -> str:
        return self.text[self.rendered_length :]

    @property
    def is_empty(self) -> bool:
        return not self.text_parts


class CLIStreamPresenter:
    """Own stream buffering; terminal rendering is callback-injected."""

    def __init__(
        self,
        render_markdown: Callable[[str], None],
        render_plain: Callable[[str], None],
    ) -> None:
        self._render_markdown = render_markdown
        self._render_plain = render_plain
        self.active_block: ContentBlock | None = None

    def append(self, token: str) -> None:
        if self.active_block is None:
            self.active_block = ContentBlock()
        self.active_block.text_parts.append(token)
        self._flush_completed()

    def close(self) -> None:
        block = self.active_block
        if block is None:
            return
        self._flush_remaining()
        if not block.is_empty and not block.text.endswith("\n"):
            self._render_plain("\n")
        self.active_block = None

    def finalize(self, response: str, *, render_response: bool) -> None:
        if self.active_block is not None:
            self.close()
        elif response and render_response:
            self._render_markdown(response)

    def reset(self) -> None:
        self.active_block = None

    def flush_remaining(self) -> None:
        """Flush buffered text without closing the logical assistant cell."""
        self._flush_remaining()

    def _flush_completed(self) -> None:
        block = self.active_block
        if block is None or not block.pending_text:
            return
        boundary = find_committed_boundary(block.pending_text)
        if boundary is None:
            return
        text = block.pending_text[:boundary]
        if text:
            self._render_markdown(text)
            block.rendered_length += len(text)

    def _flush_remaining(self) -> None:
        block = self.active_block
        if block is not None and block.pending_text:
            self._render_markdown(block.pending_text)
            block.rendered_length = len(block.text)
