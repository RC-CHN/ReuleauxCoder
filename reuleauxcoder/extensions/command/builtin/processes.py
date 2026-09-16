"""Builtin process-session browsing and control commands."""

from __future__ import annotations

from dataclasses import dataclass, replace

from reuleauxcoder.app.commands.capabilities import UICapability
from reuleauxcoder.app.commands.matchers import match_template
from reuleauxcoder.app.commands.models import CommandEffect
from reuleauxcoder.app.commands.panels import (
    CommandPanelSpec,
    PanelDefinition,
    PanelItem,
)
from reuleauxcoder.app.commands.process_views import (
    ProcessRowViewModel,
    ProcessSessionsViewModel,
)
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.requests import ActionRequest
from reuleauxcoder.app.commands.shared import (
    UI_TARGETS,
    slash_trigger,
)
from reuleauxcoder.app.commands.specs import ActionSpec, DuringTurnPolicy
from reuleauxcoder.app.interaction_contracts import ConfirmRequest, InputTextRequest
from reuleauxcoder.app.ui_events import UIEventKind
from reuleauxcoder.domain.process import (
    ProcessSessionError,
    ProcessSessionNotFound,
    ProcessSnapshot,
    ProcessState,
)
from reuleauxcoder.domain.process_manager import ManagedProcessView, ProcessManager

_MAX_UI_OUTPUT_CHARS = 8_000


@dataclass(frozen=True, slots=True)
class ListProcessesCommand:
    stop_picker: bool = False
    refresh: bool = False


@dataclass(frozen=True, slots=True)
class ControlProcessCommand:
    action: str
    session_id: str


@dataclass(frozen=True, slots=True)
class SecureProcessInputCommand:
    session_id: str


def _parse_list(user_input: str, parse_ctx):
    del parse_ctx
    if any(
        match_template(user_input, command) is not None
        for command in ("/ps", "/processes")
    ):
        return ListProcessesCommand()
    if match_template(user_input, "/stop") is not None:
        return ListProcessesCommand(stop_picker=True)
    return None


def _parse_control(user_input: str, parse_ctx):
    del parse_ctx
    for action in ("poll", "interrupt", "terminate"):
        captures = match_template(user_input, f"/ps {action} {{session_id+}}")
        if captures is not None:
            return ControlProcessCommand(
                action=action,
                session_id=captures["session_id"].strip(),
            )
    captures = match_template(user_input, "/stop {session_id+}")
    if captures is not None:
        return ControlProcessCommand(
            action="terminate",
            session_id=captures["session_id"].strip(),
        )
    return None


def _parse_secure_input(user_input: str, parse_ctx):
    del parse_ctx
    captures = match_template(user_input, "/ps input {session_id+}")
    if captures is None:
        return None
    return SecureProcessInputCommand(
        session_id=captures["session_id"].strip(),
    )


def _identity(ctx) -> tuple[ProcessManager | None, str, str | None, int]:
    agent = ctx.agent
    manager = getattr(agent, "process_manager", None)
    return (
        manager if isinstance(manager, ProcessManager) else None,
        str(agent.agent_id),
        agent.current_session_id,
        int(agent.session_generation),
    )


def _build_view(
    manager: ProcessManager,
    *,
    agent_id: str,
    owner_session_id: str | None,
    session_generation: int,
) -> ProcessSessionsViewModel:
    sessions = manager.list(
        agent_id=agent_id,
        owner_session_id=owner_session_id,
        session_generation=session_generation,
        include_observed=True,
    )
    return ProcessSessionsViewModel(
        sessions=tuple(_row(session) for session in sessions)
    )


def _row(session: ManagedProcessView) -> ProcessRowViewModel:
    return ProcessRowViewModel(
        session_id=session.session_id,
        command=session.command,
        cwd=session.cwd,
        state=session.state.value,
        stream_mode=session.stream_mode,
        backend=session.backend,
        elapsed_seconds=session.elapsed_seconds,
        exit_code=session.exit_code,
        termination_reason=session.termination_reason,
        output_truncated=session.output_truncated,
        output_decode_replaced=session.output_decode_replaced,
    )


def _handle_list(command, ctx) -> CommandEffect:
    manager, agent_id, owner_session_id, generation = _identity(ctx)
    if manager is None:
        ctx.effect.error(
            "Process session manager is unavailable.",
            kind=UIEventKind.COMMAND,
        )
        return ctx.effect.finish(control="continue")
    view = _build_view(
        manager,
        agent_id=agent_id,
        owner_session_id=owner_session_id,
        session_generation=generation,
    )
    present = ctx.effect.refresh_view if command.refresh else ctx.effect.open_view
    present(
        view,
        title=("Stop a Process" if command.stop_picker else "Process Sessions"),
        reuse_key=view.view_type,
    )
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _handle_control(command, ctx) -> CommandEffect:
    manager, agent_id, owner_session_id, generation = _identity(ctx)
    if manager is None:
        ctx.effect.error(
            "Process session manager is unavailable.",
            kind=UIEventKind.COMMAND,
        )
        return ctx.effect.finish(control="continue")

    if command.action not in {"poll", "interrupt", "terminate"}:
        ctx.effect.error(f"Unknown process action: {command.action}")
        return ctx.effect.finish(control="continue")

    if command.action == "terminate":
        if command.session_id == "all":
            subject = "all unresolved processes owned by this session"
        else:
            try:
                process = manager.get_view(
                    command.session_id,
                    agent_id=agent_id,
                    owner_session_id=owner_session_id,
                    session_generation=generation,
                )
            except ProcessSessionNotFound as error:
                ctx.effect.error(str(error), kind=UIEventKind.COMMAND)
                return ctx.effect.finish(control="continue")
            subject = f"{process.command}\n{process.backend} · {process.cwd}\n{process.session_id}"
        response = ctx.ui_interactor.confirm(
            ConfirmRequest(
                "Terminate process tree",
                f"Stop this process and its descendants?\n\n{subject}",
                severity="warning",
            )
        )
        if response.cancelled or not response.confirmed:
            return ctx.effect.finish(control="continue")

    if command.session_id == "all":
        if command.action != "terminate":
            ctx.effect.error(
                f"Action {command.action!r} requires one process session ID.",
                kind=UIEventKind.COMMAND,
            )
            return ctx.effect.finish(control="continue")
        count = manager.stop_all(
            agent_id=agent_id,
            owner_session_id=owner_session_id,
            session_generation=generation,
            reason="user_terminated",
        )
        ctx.effect.info(
            f"Termination requested for {count} unresolved process session(s).",
            kind=UIEventKind.COMMAND,
        )
        return _refresh(manager, ctx, agent_id, owner_session_id, generation)

    try:
        snapshot = _run_control(
            manager,
            command,
            agent_id=agent_id,
            owner_session_id=owner_session_id,
            generation=generation,
            retain_output=UICapability.MENUS in ctx.ui_profile.capabilities,
        )
    except ProcessSessionError as error:
        ctx.effect.error(
            f"Process operation was not confirmed: {error}",
            kind=UIEventKind.COMMAND,
        )
        return ctx.effect.finish(control="continue")

    if UICapability.MENUS in ctx.ui_profile.capabilities:
        return _refresh(manager, ctx, agent_id, owner_session_id, generation, snapshot)
    if command.action == "poll":
        ctx.effect.info(
            _snapshot_text(snapshot),
            kind=UIEventKind.COMMAND,
            process_session_id=snapshot.session_id,
        )
    else:
        ctx.effect.info(
            f"{command.action.capitalize()} sent to {snapshot.session_id}; "
            f"latest state is {snapshot.state.value}.",
            kind=UIEventKind.COMMAND,
            process_session_id=snapshot.session_id,
        )
    return _refresh(manager, ctx, agent_id, owner_session_id, generation)


def _handle_secure_input(command, ctx) -> CommandEffect:
    manager, agent_id, owner_session_id, generation = _identity(ctx)
    if manager is None:
        ctx.effect.error(
            "Process session manager is unavailable.",
            kind=UIEventKind.COMMAND,
        )
        return ctx.effect.finish(control="continue")
    if ctx.ui_interactor is None:
        ctx.effect.error(
            "This interface cannot collect hidden input; no input was sent.",
            kind=UIEventKind.COMMAND,
        )
        return ctx.effect.finish(control="continue")
    try:
        view = manager.get_view(
            command.session_id,
            agent_id=agent_id,
            owner_session_id=owner_session_id,
            session_generation=generation,
        )
    except ProcessSessionNotFound as error:
        ctx.effect.error(str(error), kind=UIEventKind.COMMAND)
        return ctx.effect.finish(control="continue")
    if view.stream_mode != "pty":
        ctx.effect.error(
            f"Process {command.session_id} uses pipe mode; no input was sent.",
            kind=UIEventKind.COMMAND,
        )
        return ctx.effect.finish(control="continue")
    if view.state is not ProcessState.RUNNING:
        ctx.effect.error(
            f"Process {command.session_id} is {view.state.value}; no input was sent.",
            kind=UIEventKind.COMMAND,
        )
        return ctx.effect.finish(control="continue")

    response = ctx.ui_interactor.input_text(
        InputTextRequest(
            title=f"Hidden input · {command.session_id}",
            prompt=(
                "Enter one hidden line. It will be sent directly to the PTY "
                "followed by Enter and will not be added to model context or history"
            ),
            placeholder="blank cancels",
            secret=True,
        )
    )
    if response.cancelled or response.value is None:
        ctx.effect.info(
            f"Hidden input to {command.session_id} was cancelled; no input was sent.",
            kind=UIEventKind.COMMAND,
        )
        return ctx.effect.finish(control="continue")
    try:
        snapshot = manager.write_sensitive_line(
            command.session_id,
            response.value,
            consumer=f"human:{agent_id}",
            agent_id=agent_id,
            owner_session_id=owner_session_id,
            session_generation=generation,
        )
    except (ProcessSessionError, ValueError) as error:
        ctx.effect.error(
            f"Hidden input was not confirmed for {command.session_id}: {error}",
            kind=UIEventKind.COMMAND,
        )
        return ctx.effect.finish(control="continue")

    if UICapability.MENUS not in ctx.ui_profile.capabilities:
        ctx.effect.info(
            f"Hidden input was sent to {command.session_id}; its value was not recorded.",
            kind=UIEventKind.COMMAND,
            process_session_id=command.session_id,
        )
    return _refresh(manager, ctx, agent_id, owner_session_id, generation, snapshot)


def _run_control(
    manager: ProcessManager,
    command: ControlProcessCommand,
    *,
    agent_id: str,
    owner_session_id: str | None,
    generation: int,
    retain_output: bool = False,
) -> ProcessSnapshot:
    common = {
        "consumer": f"human:{agent_id}",
        "agent_id": agent_id,
        "owner_session_id": owner_session_id,
        "session_generation": generation,
    }
    if command.action == "poll":
        return manager.poll(
            command.session_id,
            wait_ms=0,
            retain_output_chars=_MAX_UI_OUTPUT_CHARS if retain_output else None,
            **common,
        )
    if command.action == "interrupt":
        return manager.interrupt(command.session_id, **common)
    return manager.terminate(
        command.session_id,
        reason="user_terminated",
        **common,
    )


def _refresh(
    manager: ProcessManager,
    ctx,
    agent_id: str,
    owner_session_id: str | None,
    generation: int,
    snapshot: ProcessSnapshot | None = None,
) -> CommandEffect:
    view = _build_view(
        manager,
        agent_id=agent_id,
        owner_session_id=owner_session_id,
        session_generation=generation,
    )
    if snapshot is not None:
        view = replace(
            view, output_session_id=snapshot.session_id, output=_output_text(snapshot) or "(no output)"
        )
    ctx.effect.refresh_view(
        view,
        title="Process Sessions",
        reuse_key=view.view_type,
    )
    return ctx.effect.finish(control="continue", state_changes=view.to_payload())


def _snapshot_text(snapshot: ProcessSnapshot) -> str:
    lines = [
        f"Process {snapshot.session_id}: {snapshot.state.value}",
        (
            f"exit_code={snapshot.exit_code!r} "
            f"termination_reason={snapshot.termination_reason!r} "
            f"output_truncated={snapshot.output_truncated} "
            f"output_decode_replaced={snapshot.output_decode_replaced}"
        ),
    ]
    lines.append(_output_text(snapshot) or "(no new output)")
    return "\n".join(lines)


def _output_text(snapshot: ProcessSnapshot) -> str:
    return "\n".join(
        f"{stream}:\n{_safe_output(value)}"
        for stream, value in (("stdout", snapshot.stdout), ("stderr", snapshot.stderr))
        if value
    )


def _safe_output(value: str) -> str:
    safe = "".join(
        character
        if character in {"\n", "\t"} or ord(character) >= 32 and ord(character) != 127
        else f"\\x{ord(character):02x}"
        for character in value
    )
    if len(safe) <= _MAX_UI_OUTPUT_CHARS:
        return safe
    omitted = len(safe) - _MAX_UI_OUTPUT_CHARS
    return f"… ({omitted} UI preview chars omitted)\n" + safe[-_MAX_UI_OUTPUT_CHARS:]


def _single_line(value: str, limit: int = 80) -> str:
    safe = " ".join(_safe_output(value).split())
    return safe if len(safe) <= limit else safe[: limit - 1] + "…"


def command_panel_spec() -> CommandPanelSpec:
    """Contribute one process browser with per-session control actions."""

    def build(model: object, title: str) -> PanelDefinition:
        assert isinstance(model, ProcessSessionsViewModel)

        def process_item(session):
            return PanelItem(
                label=_single_line(session.command),
                description=(
                    f"{session.state} · {session.elapsed_seconds:.1f}s · "
                    f"{session.backend}/{session.stream_mode}"
                ),
                id=session.session_id,
            )

        active = tuple(
            session for session in model.sessions if session.state != "exited"
        )
        ended = tuple(
            session for session in model.sessions if session.state == "exited"
        )
        active_ids = {session.session_id for session in active}
        ended_ids = {session.session_id for session in ended}
        items = [process_item(session) for session in active]
        if ended:
            items.append(
                PanelItem(
                    f"Ended processes · {len(ended)}",
                    "Inspect retained output and exit facts",
                    id="ended",
                )
            )
        items.append(
            PanelItem(
                "Refresh processes",
                "Reload process states and elapsed time",
                ActionRequest("processes.list", ListProcessesCommand(refresh=True)),
                id="refresh",
            )
        )
        children: list[tuple[str, PanelDefinition]] = []
        for session in model.sessions:
            actions = [
                PanelItem(
                    label="Refresh output",
                    description="Read output and update process facts",
                    action=ActionRequest(
                        "processes.control",
                        ControlProcessCommand("poll", session.session_id),
                    ),
                )
            ]
            if session.state != "exited":
                if session.stream_mode == "pty" and session.state == "running":
                    actions.append(
                        PanelItem(
                            label="Send hidden input…",
                            description="write one masked line directly to the PTY",
                            action=ActionRequest(
                                "processes.secure_input",
                                SecureProcessInputCommand(session.session_id),
                            ),
                        )
                    )
                actions.extend(
                    (
                        PanelItem(
                            label="Interrupt",
                            description="Send Ctrl+C; the process may handle or ignore it",
                            action=ActionRequest(
                                "processes.control",
                                ControlProcessCommand("interrupt", session.session_id),
                            ),
                        ),
                        PanelItem(
                            label="Terminate process tree…",
                            description="Stop this process and its descendants; requires confirmation",
                            action=ActionRequest(
                                "processes.control",
                                ControlProcessCommand("terminate", session.session_id),
                            ),
                        ),
                    )
                )
            children.append(
                (
                    session.session_id,
                    PanelDefinition(
                        view_type=f"process_session:{session.session_id}",
                        title=f"{session.state} · {session.backend} · {_single_line(session.command)}",
                        items=tuple(actions),
                        keep_open_on_submit=True,
                        show_auxiliary_actions=False,
                        body=_process_facts(session),
                        output=model.output
                        if model.output_session_id == session.session_id
                        else None,
                        on_open=ActionRequest(
                            "processes.control",
                            ControlProcessCommand("poll", session.session_id),
                        ),
                    ),
                )
            )
        children.append(
            (
                "ended",
                PanelDefinition(
                    view_type="process_sessions_ended",
                    title="Ended processes",
                    items=tuple(process_item(session) for session in ended),
                    children=tuple(
                        child
                        for child in children
                        if child[0] in ended_ids
                    ),
                    filterable=True,
                    show_auxiliary_actions=False,
                ),
            )
        )
        return PanelDefinition(
            view_type=model.view_type,
            title=title,
            items=tuple(items),
            children=tuple(
                child
                for child in children
                if child[0] == "ended"
                or child[0] in active_ids
            ),
            filterable=True,
            keep_open_on_submit=True,
            show_auxiliary_actions=False,
        )

    return CommandPanelSpec(
        "process_sessions",
        ProcessSessionsViewModel,
        build,
    )


def _process_facts(session: ProcessRowViewModel) -> str:
    facts = [
        session.command,
        f"{session.state} · {session.elapsed_seconds:.1f}s · {session.backend}/{session.stream_mode}",
        f"Directory: {session.cwd}",
        f"Session: {session.session_id}",
    ]
    if session.exit_code is not None:
        facts.append(f"Exit code: {session.exit_code}")
    if session.termination_reason:
        facts.append(f"Termination: {session.termination_reason}")
    if session.output_truncated:
        facts.append("Some process output was truncated.")
    if session.output_decode_replaced:
        facts.append("Invalid output bytes were replaced while decoding.")
    facts.append(
        f"Output preview: latest {_MAX_UI_OUTPUT_CHARS:,} characters per stream read by this interface"
    )
    return "\n".join(facts)


def register_actions(registry: ActionRegistry) -> None:
    registry.register_many(
        [
            ActionSpec(
                action_id="processes.list",
                preview=True,
                command_type=ListProcessesCommand,
                feature_id="processes",
                description="[session] Browse running and retained shell process sessions",
                ui_targets=UI_TARGETS,
                triggers=(
                    slash_trigger("/ps"),
                    slash_trigger("/processes"),
                    slash_trigger("/stop"),
                ),
                parser=_parse_list,
                handler=_handle_list,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
            ActionSpec(
                action_id="processes.control",
                command_type=ControlProcessCommand,
                feature_id="processes",
                description="[session] Poll, interrupt, or terminate a shell process session",
                ui_targets=UI_TARGETS,
                triggers=(
                    slash_trigger("/ps poll <id>"),
                    slash_trigger("/ps interrupt <id>"),
                    slash_trigger("/ps terminate <id>"),
                    slash_trigger("/stop <id|all>"),
                ),
                parser=_parse_control,
                handler=_handle_control,
                interactive=True,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
            ActionSpec(
                action_id="processes.secure_input",
                command_type=SecureProcessInputCommand,
                feature_id="processes",
                description="[session] Send one masked line directly to a PTY session",
                ui_targets=UI_TARGETS,
                required_capabilities=frozenset({UICapability.SECURE_TEXT_INPUT}),
                triggers=(slash_trigger("/ps input <id>"),),
                parser=_parse_secure_input,
                handler=_handle_secure_input,
                interactive=True,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
        ]
    )


__all__ = [
    "ProcessRowViewModel",
    "ProcessSessionsViewModel",
    "command_panel_spec",
    "register_actions",
]
