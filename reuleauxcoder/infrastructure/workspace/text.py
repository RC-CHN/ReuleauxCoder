"""Bounded text paging with Python splitlines semantics, including CRLF."""

from typing import TextIO

from reuleauxcoder.domain.workspace import WorkspaceTextPage


_LINE_ENDINGS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"


def read_text_page(
    stream: TextIO,
    *,
    offset: int,
    limit: int,
    max_chars: int,
) -> WorkspaceTextPage:
    lines: list[str] = []
    pieces: list[str] = []
    line_number = 1
    line_has_text = False
    skip_lf = False
    kept = 0
    while chunk := stream.read(64 * 1024):
        for piece in chunk.splitlines(keepends=True):
            if skip_lf and piece.startswith("\n"):
                piece = piece[1:]
            skip_lf = False
            if not piece:
                continue
            if line_number >= offset + limit:
                return WorkspaceTextPage(tuple(lines), None, True)
            ended = piece[-1] in _LINE_ENDINGS
            skip_lf = piece.endswith("\r")
            body = (
                piece[:-2] if piece.endswith("\r\n") else piece[:-1] if ended else piece
            )
            line_has_text = line_has_text or bool(body)
            if line_number >= offset:
                if not pieces and lines:
                    kept += 1  # The separator in the returned page.
                    if kept > max_chars:
                        return WorkspaceTextPage(
                            tuple(lines), None, True, truncated=True
                        )
                remaining = max_chars - kept
                pieces.append(body[:remaining])
                kept += min(remaining, len(body))
                if len(body) > remaining:
                    lines.append("".join(pieces))
                    return WorkspaceTextPage(tuple(lines), None, True, truncated=True)
            if ended:
                if line_number >= offset:
                    lines.append("".join(pieces))
                    pieces.clear()
                line_number += 1
                line_has_text = False
    if line_has_text and line_number >= offset:
        lines.append("".join(pieces))
    total = line_number - 1 + int(line_has_text)
    return WorkspaceTextPage(tuple(lines), total, False)
