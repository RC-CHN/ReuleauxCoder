"""Bounded model projection; original diagnostic events remain untouched."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from html import escape

from reuleauxcoder.extensions.lsp.config import LspConfig
from reuleauxcoder.extensions.lsp.diagnostic_outcomes import (
    DiagnosticOutcome,
    render_diagnostic_outcomes,
)
from reuleauxcoder.extensions.lsp.diagnostics import (
    DiagnosticBatch,
    render_diagnostic_line,
    select_diagnostics,
)

_PREFIX = '[LSP DIAGNOSTICS]\n<lsp_diagnostics trust="untrusted_data">\n'
_SUFFIX = "\n</lsp_diagnostics>\n"
_OUTCOMES_OPEN = "<lsp_diagnostic_outcomes>\n"
_OUTCOMES_CLOSE = "\n</lsp_diagnostic_outcomes>"


@dataclass(frozen=True, slots=True)
class DiagnosticProjection:
    text: str | None
    summary: str


def _limit_notice(omitted: int, outcomes: int, shortened: int) -> str:
    return (
        f"[truncated: {omitted} diagnostics and {outcomes} outcomes omitted; "
        f"{shortened} messages shortened. "
        "Use lsp_diagnostics to inspect current diagnostics.]"
    )


def _shorten(message: str, max_chars: int) -> tuple[str, bool]:
    first = message[:max_chars].partition("\n")[0]
    shortened = first != message
    return (first[: max_chars - 1] + "…" if shortened else first), shortened


def project_diagnostic_results(
    batches: tuple[DiagnosticBatch, ...],
    outcomes: tuple[DiagnosticOutcome, ...],
    config: LspConfig,
) -> DiagnosticProjection:
    total = sum(
        config.include_warnings or item.is_error
        for batch in batches
        for item in batch.block.items
    )
    candidates = [
        (batch.block.file_path, item)
        for batch in batches
        for item in select_diagnostics(
            batch.block,
            max_diagnostics=config.max_diagnostics,
            include_warnings=config.include_warnings,
        )
    ]
    # Prioritize errors across files as well as within each file.
    candidates.sort(
        key=lambda entry: (
            entry[1].severity,
            entry[0],
            entry[1].line,
            entry[1].character,
        )
    )
    groups: dict[str, list[str]] = {}
    counts: Counter[str] = Counter()
    shortened_count = 0
    # Reserve enough room to report every omission, including XML framing.
    used = (
        len(_PREFIX)
        + len(_SUFFIX)
        + 2
        + len(_limit_notice(total, len(outcomes), total + len(outcomes)))
    )
    for path, item in candidates:
        message, shortened = _shorten(item.message, config.max_message_chars)
        line = render_diagnostic_line(replace(item, message=message))
        heading = f'<diagnostics file="{escape(path, quote=True)}">'
        cost = len(line) + (
            1
            if path in groups
            else len(heading) + len("\n\n</diagnostics>") + (2 if groups else 0)
        )
        if used + cost > config.max_injection_chars:
            continue
        used += cost
        if path not in groups:
            groups[path] = [heading]
        groups[path].append(line)
        counts[item.severity_label.lower()] += 1
        shortened_count += shortened

    parts = ["\n".join([*lines, "</diagnostics>"]) for lines in groups.values()]
    failure_lines: list[str] = []
    for outcome in outcomes:
        rendered = render_diagnostic_outcomes((outcome,))
        if rendered is None:
            continue
        message, shortened = _shorten(rendered, config.max_message_chars)
        line = escape(message, quote=False)
        cost = len(line) + (
            1
            if failure_lines
            else len(_OUTCOMES_OPEN) + len(_OUTCOMES_CLOSE) + (2 if parts else 0)
        )
        if used + cost > config.max_injection_chars:
            continue
        used += cost
        failure_lines.append(line)
        shortened_count += shortened
    if failure_lines:
        parts.append(_OUTCOMES_OPEN + "\n".join(failure_lines) + _OUTCOMES_CLOSE)

    omitted = total - counts.total()
    omitted_outcomes = len(outcomes) - len(failure_lines)
    if omitted or omitted_outcomes or shortened_count:
        parts.append(_limit_notice(omitted, omitted_outcomes, shortened_count))
    summary = [
        f"{count} {severity}{'s' if count != 1 and severity != 'info' else ''}"
        for severity, count in counts.items()
    ]
    if failure_lines:
        summary.append(f"{len(failure_lines)} diagnostic outcome(s)")
    if omitted or omitted_outcomes:
        summary.append(f"{omitted + omitted_outcomes} omitted")
    if shortened_count:
        summary.append(f"{shortened_count} shortened")
    return DiagnosticProjection(
        text=_PREFIX + "\n\n".join(parts) + _SUFFIX if parts else None,
        summary=", ".join(summary),
    )
