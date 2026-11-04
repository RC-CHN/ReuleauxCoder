"""Plain scrollback views for restored context and runtime details."""

from dataclasses import asdict

from rich.markdown import Markdown
from rich.pretty import Pretty
from rich.text import Text

from reuleauxcoder.domain.runtime.events import (
    PlanUpdated,
    ProgressReported,
    RuntimeEvent,
)
from reuleauxcoder.presentation.execution import execution_panel_view
from reuleauxcoder.presentation.models import ToolCell


def restore_context(renderer, info, state):
    for entry in info["recent_conversation"]:
        renderer.console.rule("YOU" if entry["role"] == "user" else "AGENT")
        content = entry["content"]
        renderer.console.print(
            Text(content) if entry["role"] == "user" else Markdown(content)
        )
    if info["recent_conversation"] and info.get("conversation_preview"):
        renderer.console.print(
            Text(
                "Showing the latest 3 user turns; the model restored the full "
                "session. Browse history for older messages.",
                style="muted",
            )
        )
    plan, progress = info["plan"], info["progress"]
    if plan is not None:
        envelope = dict(
            agent_id=state.agent_id,
            session_id=state.session_id,
            session_generation=state.session_generation,
        )
        renderer.execution.apply(
            RuntimeEvent(
                payload=PlanUpdated(
                    revision=plan.revision,
                    items=tuple(asdict(item) for item in plan.items),
                    explanation=plan.explanation,
                ),
                **envelope,
            )
        )
        renderer.execution.apply(
            RuntimeEvent(
                payload=ProgressReported(
                    revision=progress.revision,
                    phase=progress.phase,
                    summary=progress.summary,
                    next=progress.next,
                ),
                **envelope,
            )
        )


def render_details(renderer, state, startup_events):
    console = renderer.console
    view = execution_panel_view(renderer.execution.state)
    console.rule("Session & execution")
    console.print(
        Text(
            f"Session: {state.session_id}\nModel: {state.model}\n"
            f"Workspace: {state.workspace}\n"
            f"Context: {state.context_tokens} / {state.context_limit} tokens\n"
            f"MCP: {state.mcp_enabled} servers, {state.mcp_tools} tools ({state.mcp_state})\n"
            f"State: {'stopping' if state.stopping else 'running' if state.running else 'ready'}\n"
            f"Activity: {view.main.activity}\n"
            f"Progress: {view.phase} · {view.progress_summary}"
        )
    )
    if view.progress_next:
        console.print(Text(f"Next: {view.progress_next}"))
    if state.goal:
        goal = state.goal
        budget = f"{goal.token_budget:,}" if goal.token_budget else "No limit"
        console.print(
            Text(
                f"Goal: {goal.objective}\n{goal.status} · Tokens {goal.tokens_used:,} / {budget} · {int(goal.time_used_seconds)}s"
            )
        )
    for item in view.plan:
        console.print(Text(f"[{item.status}] {item.step} · {item.active_form}"))
    for job in view.subagents:
        console.print(
            Text(
                f"{job.label}: {job.status} · {job.task} · {job.activity} · {job.budget}"
            )
        )
    console.print(
        Text(
            f"Processes: {view.process_running} running, {view.process_unknown} unknown"
        )
    )
    for item in view.attention:
        console.print(Text(f"Approval: {item.title}\n{item.preview or ''}"))
    for label, values in (
        ("Command", state.queued_commands),
        ("Prompt", state.queued_steering),
    ):
        for value in values:
            console.print(Text(f"Queued {label}: {value}"))
    for event in startup_events:
        console.print(Text(event.message), style="dim")


def render_tools(renderer):
    console = renderer.console
    console.rule("Retained tool arguments & full output")
    cells = [
        cell
        for cell in renderer.reducer.state.transcript.cells
        if isinstance(cell, ToolCell)
    ]
    if not cells:
        console.print("No retained tool calls.")
    for cell in cells:
        console.print(Text(f"{cell.name} · {cell.status.value}"), style="bold")
        console.print(Pretty(cell.arguments, expand_all=True))
        if cell.outcome is None:
            console.print(Text(cell.output))
        else:
            console.print(Text(cell.outcome.display_text))
            if cell.outcome.archive_reference:
                console.print(Text(f"Archive: {cell.outcome.archive_reference.path}"))
