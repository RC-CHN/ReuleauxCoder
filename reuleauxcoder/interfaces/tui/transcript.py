"""Transcript cell rendering for the production terminal UI."""

from __future__ import annotations

import json

from prompt_toolkit.utils import get_cwidth

from reuleauxcoder.domain.approval import ApprovalSectionKind
from reuleauxcoder.interfaces.tui.markdown_fragments import RetainedMarkdownRenderer
from reuleauxcoder.interfaces.tui.formatting import (
    clip,
    first_meaningful_line,
    fit_display,
)
from reuleauxcoder.presentation import (
    ApprovalCell,
    AssistantCell,
    DiagnosticCell,
    DiffCell,
    NoticeCell,
    SubagentCell,
    ToolCell,
    TranscriptPlacement,
    UserCell,
)


def cell_fragments(
    cell,
    *,
    width: int = 100,
    markdown_renderer: RetainedMarkdownRenderer | None = None,
) -> list[tuple[str, str]]:
    """Render one semantic transcript cell into styled fragments."""
    if isinstance(cell, UserCell):
        return [
            ("class:user.label", " YOU "),
            ("class:user", f" {cell.text} "),
        ]
    if isinstance(cell, AssistantCell):
        renderer = markdown_renderer or RetainedMarkdownRenderer()
        fragments = renderer.render(
            cell_id=cell.id,
            revision=cell.revision,
            text=cell.text,
            complete=cell.complete,
            width=width,
        )
        if cell.interrupted:
            fragments.extend(
                [
                    ("class:warning", " [response interrupted]\n"),
                    ("", "\n"),
                ]
            )
        return fragments
    if isinstance(cell, ToolCell):
        status = cell.status.value.upper()
        style = "class:error" if cell.status.value == "failed" else "class:tool"
        text = f" {cell.name}"
        if cell.outcome is not None:
            summary = cell.outcome.summary or first_meaningful_line(
                cell.outcome.ui_text(include_details=True)
            )
            if summary:
                text += f" · {clip(summary, 160)}"
        status_text = f" {status} "
        text = fit_display(text, max(10, width - get_cwidth(status_text) - 2))
        padding = " " * max(2, width - get_cwidth(text) - get_cwidth(status_text))
        fragments = [
            (style, text),
            (style, padding),
            (f"{style} bold", status_text),
            ("", "\n"),
        ]
        for line in cell.output.splitlines()[-5:]:
            fragments.append(("class:muted", f" └ {line}\n"))
        if cell.status.value != "running":
            fragments.append(("", "\n"))
        return fragments
    if isinstance(cell, DiffCell):
        fragments: list[tuple[str, str]] = []
        for line in cell.diff.splitlines():
            style = "class:diff.header"
            if line.startswith("+") and not line.startswith("+++"):
                style = "class:diff.add"
            elif line.startswith("-") and not line.startswith("---"):
                style = "class:diff.del"
            fragments.append((style, line + "\n"))
        fragments.append(("", "\n"))
        return fragments
    if isinstance(cell, NoticeCell):
        if cell.category == "user" or cell.level == "user":
            return [("class:user", f" YOU  {cell.message}\n")]
        style = {
            "error": "class:error",
            "warning": "class:warning",
            "success": "class:success",
        }.get(cell.level, "class:muted")
        return [(style, cell.message + "\n")]
    if isinstance(cell, ApprovalCell):
        return approval_fragments(cell, width=width)
    if isinstance(cell, SubagentCell):
        return [
            ("class:tool", f" AGENT  {cell.job_id} · {cell.status} · {cell.task}\n")
        ]
    if isinstance(cell, DiagnosticCell):
        return [
            (
                "class:warning",
                f" LSP  {cell.path} · {len(cell.diagnostics)} diagnostic(s)\n",
            )
        ]
    return [("class:muted", str(cell) + "\n")]


def decorate_transcript_fragments(
    placement: TranscriptPlacement,
    fragments: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Apply turn chrome and one centralized outer-spacing policy."""
    output: list[tuple[str, str]] = []
    if placement.begins_turn:
        output.append(("class:turn.separator", " ╶────────────────\n"))
    if placement.show_assistant_label:
        output.append(("class:assistant.label", " FORGE "))
        output.append(("", "\n"))
    output.extend(rstrip_fragment_newlines(fragments))
    if placement.blank_lines_after:
        output.append(("", "\n" * placement.blank_lines_after))
    return output


def rstrip_fragment_newlines(
    fragments: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Remove outer trailing newlines while preserving fragment styles."""
    trimmed = list(fragments)
    while trimmed:
        style, text = trimmed[-1]
        stripped = text.rstrip("\n")
        if stripped:
            trimmed[-1] = (style, stripped)
            break
        trimmed.pop()
    return trimmed


def approval_fragments(cell: ApprovalCell, *, width: int) -> list[tuple[str, str]]:
    """Render the v0.4-style review card in the scrollable transcript."""
    frame_width = max(24, min(100, width - 1))
    inner = frame_width - 4
    status = cell.status.upper()
    title = f" {cell.title.upper()} · {status} "
    title = fit_display(title, frame_width - 4)
    rule = "━" * max(1, frame_width - get_cwidth(title) - 3)
    state_style = {
        "approved": "class:review.approved",
        "denied": "class:review.denied",
    }.get(cell.status, "class:review.border")
    title_style = {
        "approved": "class:review.title.approved",
        "denied": "class:review.title.denied",
    }.get(cell.status, "class:review.title.pending")
    fragments: list[tuple[str, str]] = [
        (state_style, "┏━"),
        (title_style, title),
        (state_style, f"{rule}┓\n"),
    ]

    def add_line(style: str, text: str = "") -> None:
        fitted = fit_display(text, inner)
        padding = " " * max(0, inner - get_cwidth(fitted))
        fragments.append((state_style, "┃ "))
        fragments.append((style, fitted))
        fragments.append(("class:review.body", padding))
        fragments.append((state_style, " ┃\n"))

    if cell.summary:
        for line in cell.summary.splitlines():
            add_line("class:review.body", line)
    for section in cell.sections:
        add_line("class:panel.header", f" {section.title.upper()} ")
        content = section.content
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, indent=2)
        lines = content.splitlines()
        visible = lines[:20]
        for line in visible:
            style = "class:review.body"
            if section.kind is ApprovalSectionKind.DIFF:
                if line.startswith("+") and not line.startswith("+++"):
                    style = "class:diff.add"
                elif line.startswith("-") and not line.startswith("---"):
                    style = "class:diff.del"
                elif line.startswith(("@@", "+++", "---")):
                    style = "class:diff.header"
            add_line(style, line)
        if len(lines) > len(visible):
            add_line("class:muted", f"… {len(lines) - len(visible)} more lines")
    if not cell.sections and cell.preview:
        add_line("class:muted", cell.preview)
    if cell.reason:
        reason_style = "class:success" if cell.status == "approved" else "class:error"
        add_line(reason_style, cell.reason)
    resolution_source = cell.resolution_source
    if (
        resolution_source is not None
        and resolution_source != "user"
        and cell.status == "approved"
    ):
        resolver = resolution_source.replace("_", " ")
        add_line("class:success", f"Decision  Auto-approved by {resolver}")
    elif (
        resolution_source is not None
        and resolution_source != "user"
        and cell.status == "denied"
    ):
        resolver = resolution_source.replace("_", " ")
        add_line("class:error", f"Decision  Automatically denied by {resolver}")
    elif cell.mode == "allow_session":
        scope = f" · {cell.grant_label}" if cell.grant_label else ""
        add_line("class:success", f"Decision  Approved for this session{scope}")
    elif cell.mode == "allow_once":
        add_line("class:success", "Decision  Allowed once")
    elif cell.mode == "deny_once":
        add_line("class:error", "Decision  Denied")
    if cell.released_count:
        add_line(
            "class:success",
            f"Also released {cell.released_count} matching queued call"
            f"{'s' if cell.released_count != 1 else ''}.",
        )
    fragments.extend(
        [
            (state_style, f"┗{'━' * (frame_width - 2)}┛\n"),
            ("", "\n"),
        ]
    )
    return fragments


__all__ = [
    "approval_fragments",
    "cell_fragments",
    "decorate_transcript_fragments",
    "rstrip_fragment_newlines",
]
