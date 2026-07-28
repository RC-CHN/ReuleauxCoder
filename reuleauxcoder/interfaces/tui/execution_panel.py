"""Execution-status panel rendering for the production terminal UI."""

from __future__ import annotations

from reuleauxcoder.interfaces.tui.formatting import (
    fit_styled_row as _fit_styled_row,
)
from reuleauxcoder.presentation import ExecutionPanelView


def _execution_panel_rows(
    view: ExecutionPanelView,
    *,
    width: int,
    expanded: bool,
    details: tuple[str, ...] = (),
) -> tuple[tuple[tuple[str, str], ...], ...]:
    """Render a semantic panel snapshot without leaking layout into reducers."""
    width = max(20, width)
    plan_count = f"{view.plan_completed}/{view.plan_total}" if view.plan_total else "—"
    live = "LIVE" if view.is_live else "IDLE"

    if width < 60:
        summary = [
            ("class:panel.label", " RUN "),
            ("class:panel.phase", f" {view.phase} "),
            ("class:panel.label.secondary", " P "),
            ("class:panel.value", f" {plan_count} "),
            ("class:panel.label.secondary", " A "),
            ("class:panel.value", f" {len(view.subagents)}"),
        ]
        final = _compact_panel_tail(view)
        rows = [
            _fit_styled_row(summary, width),
            _fit_styled_row(
                _labeled_panel_row(
                    "PLAN", f"{'●' if view.plan_total else '○'} {view.active_plan}"
                ),
                width,
            ),
            _fit_styled_row(final, width),
        ]
    else:
        summary = [
            ("class:panel.label", " RUN "),
            ("class:panel.phase", f" {view.phase} "),
            ("class:panel.label.secondary", " PLAN "),
            ("class:panel.value", f" {plan_count} "),
            ("class:panel.label.secondary", " AGENTS "),
            ("class:panel.value", f" {len(view.subagents)} "),
            ("class:panel.live", f"● {live}"),
        ]
        if view.attention:
            summary.extend(
                [
                    ("class:panel.label.need", " NEED "),
                    ("class:warning", f" {len(view.attention)}"),
                ]
            )
        rows = [
            _fit_styled_row(summary, width),
            _fit_styled_row(
                _labeled_panel_row(
                    "PLAN", f"{'●' if view.plan_total else '○'} {view.active_plan}"
                ),
                width,
            ),
            _fit_styled_row(
                _labeled_panel_row(
                    "MAIN",
                    f"{view.main.marker} {view.main.activity or 'ready'}",
                ),
                width,
            ),
            _fit_styled_row(_compact_panel_tail(view), width),
        ]

    if not expanded:
        return tuple(rows)

    expanded_rows: list[tuple[tuple[str, str], ...]] = list(rows)
    for detail in details[:3]:
        label, _, value = detail.partition(" ")
        expanded_rows.append(
            _fit_styled_row(_labeled_panel_row(label, value, secondary=True), width)
        )
    for item in view.plan:
        marker = {
            "completed": "✓",
            "in_progress": "●",
            "pending": "○",
        }.get(item.status, "○")
        label = item.active_form if item.status == "in_progress" else item.step
        expanded_rows.append(
            _fit_styled_row(
                _labeled_panel_row("PLAN", f"{marker} {label}", secondary=True),
                width,
            )
        )
    for agent in view.subagents:
        expanded_rows.append(
            _fit_styled_row(
                _labeled_panel_row("SUB", _panel_agent_text(agent), secondary=True),
                width,
            )
        )
    for detail in details[3:]:
        expanded_rows.append(
            _fit_styled_row([("class:panel.detail", f"  {detail}")], width)
        )
    return tuple(expanded_rows[:12])


def _compact_panel_tail(view: ExecutionPanelView) -> list[tuple[str, str]]:
    if view.attention:
        row = _labeled_panel_row(
            "NEED",
            f"! {view.attention[0].title}",
            need=True,
        )
        if view.subagents:
            row.extend(
                [
                    ("class:panel.label.secondary", " SUB "),
                    ("class:panel.value", f" {_panel_agent_text(view.subagents[0])}"),
                ]
            )
        return row
    if view.subagents:
        return _labeled_panel_row("SUB", _panel_agent_text(view.subagents[0]))
    next_step = view.progress_next or view.progress_summary or "ready"
    return _labeled_panel_row("NEXT", next_step, secondary=True)


def _panel_agent_text(agent) -> str:
    task = agent.task or "working"
    activity = f" · {agent.activity}" if agent.activity else ""
    budget = f" · {agent.budget}" if agent.budget else ""
    return f"{agent.marker} {agent.label} · {task}{activity}{budget}"


def _labeled_panel_row(
    label: str,
    value: str,
    *,
    secondary: bool = False,
    need: bool = False,
) -> list[tuple[str, str]]:
    label_style = (
        "class:panel.label.need"
        if need
        else "class:panel.label.secondary"
        if secondary
        else "class:panel.label"
    )
    value_style = "class:warning" if need else "class:panel.value"
    return [(label_style, f" {label:<5} "), (value_style, f" {value}")]
