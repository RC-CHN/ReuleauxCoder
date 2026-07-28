"""Builtin process-session browsing and control commands."""

from __future__ import annotations

from dataclasses import dataclass

from reuleauxcoder.app.commands.matchers import match_template
from reuleauxcoder.app.commands.models import CommandEffect
from reuleauxcoder.app.commands.panels import (
    CommandPanelSpec,
    PanelDefinition,
    PanelItem,
)
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.shared import (
    TEXT_REQUIRED,
    UI_TARGETS,
    slash_trigger,
)
from reuleauxcoder.app.commands.specs import ActionSpec, DuringTurnPolicy
from reuleauxcoder.domain.process import (
    ProcessSessionNotFound,
    ProcessSnapshot,
    ProcessState,
)
from reuleauxcoder.domain.process_manager import ManagedProcessView, ProcessManager
from reuleauxcoder.interfaces.events import UIEventKind
from reuleauxcoder.interfaces.interactions import InputTextRequest
from reuleauxcoder.interfaces.ui_registry import UICapability


_MAX_UI_OUTPUT_CHARS = 8_000


@dataclass(frozen=True, slots=True)
class ListProcessesCommand:
    stop_picker: bool = False


@dataclass(frozen=True, slots=True)
class ControlProcessCommand:
    action: str
    session_id: str


@dataclass(frozen=True, slots=True)
class SecureProcessInputCommand:
    session_id: str


@dataclass(frozen=True, slots=True)
class ProcessRowViewModel:
    session_id: str
    command: str
    cwd: str
    state: str
    stream_mode: str
    backend: str
    elapsed_seconds: float
    exit_code: int | None
    termination_reason: str | None
    output_truncated: bool
    output_decode_replaced: bool


@dataclass(frozen=True, slots=True)
class ProcessSessionsViewModel:
    sessions: tuple[ProcessRowViewModel, ...]
    view_type: str = "process_sessions"

    def to_payload(self) -> dict[str, object]:
        return {
            "sessions": [
                {
                    "session_id": session.session_id,
                    "command": session.command,
                    "cwd": session.cwd,
                    "state": session.state,
                    "stream_mode": session.stream_mode,
                    "backend": session.backend,
                    "elapsed_seconds": session.elapsed_seconds,
                    "exit_code": session.exit_code,
                    "termination_reason": session.termination_reason,
                    "output_truncated": session.output_truncated,
                    "output_decode_replaced": session.output_decode_replaced,
                }
                for session in self.sessions
            ]
        }


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
    ctx.effect.open_view(
        view.view_type,
        title=("Stop a Process" if command.stop_picker else "Process Sessions"),
        view_model=view,
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
        )
    except ProcessSessionNotFound as error:
        ctx.effect.error(str(error), kind=UIEventKind.COMMAND)
        return ctx.effect.finish(control="continue")
    except Exception as error:
        ctx.effect.error(
            f"Process operation was not confirmed: {error}",
            kind=UIEventKind.COMMAND,
        )
        return ctx.effect.finish(control="continue")

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
        manager.write_sensitive_line(
            command.session_id,
            response.value,
            consumer=f"human:{agent_id}",
            agent_id=agent_id,
            owner_session_id=owner_session_id,
            session_generation=generation,
        )
    except Exception as error:
        ctx.effect.error(
            f"Hidden input was not confirmed for {command.session_id}: {error}",
            kind=UIEventKind.COMMAND,
        )
        return ctx.effect.finish(control="continue")

    ctx.effect.info(
        f"Hidden input was sent to {command.session_id}; its value was not recorded.",
        kind=UIEventKind.COMMAND,
        process_session_id=command.session_id,
    )
    return _refresh(manager, ctx, agent_id, owner_session_id, generation)


def _run_control(
    manager: ProcessManager,
    command: ControlProcessCommand,
    *,
    agent_id: str,
    owner_session_id: str | None,
    generation: int,
) -> ProcessSnapshot:
    common = {
        "consumer": f"human:{agent_id}",
        "agent_id": agent_id,
        "owner_session_id": owner_session_id,
        "session_generation": generation,
    }
    if command.action == "poll":
        return manager.poll(command.session_id, wait_ms=0, **common)
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
) -> CommandEffect:
    view = _build_view(
        manager,
        agent_id=agent_id,
        owner_session_id=owner_session_id,
        session_generation=generation,
    )
    ctx.effect.refresh_view(
        view.view_type,
        title="Process Sessions",
        view_model=view,
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
    if snapshot.stdout:
        lines.append("stdout:\n" + _safe_output(snapshot.stdout))
    if snapshot.stderr:
        lines.append("stderr:\n" + _safe_output(snapshot.stderr))
    if not snapshot.stdout and not snapshot.stderr:
        lines.append("(no new output)")
    return "\n".join(lines)


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
    return safe[:_MAX_UI_OUTPUT_CHARS] + f"\n… ({omitted} UI preview chars omitted)"


def _single_line(value: str, limit: int = 80) -> str:
    safe = " ".join(_safe_output(value).split())
    return safe if len(safe) <= limit else safe[: limit - 1] + "…"


def command_panel_spec() -> CommandPanelSpec:
    """Contribute one process browser with per-session control actions."""

    def build(model: object, title: str) -> PanelDefinition:
        assert isinstance(model, ProcessSessionsViewModel)
        items = tuple(
            PanelItem(
                label=session.session_id,
                description=(
                    f"{session.state} · {session.backend}/{session.stream_mode} · "
                    f"{session.elapsed_seconds:.1f}s · {_single_line(session.command)}"
                ),
                command="",
                current=session.state != "exited",
            )
            for session in model.sessions
        ) or (
            PanelItem(
                label="(no process sessions)",
                description="long-running shell calls will appear here",
                command="",
            ),
        )
        children: list[tuple[str, PanelDefinition]] = []
        for session in model.sessions:
            actions = [
                PanelItem(
                    label="poll output",
                    description="read new output and latest process facts",
                    command=f"/ps poll {session.session_id}",
                )
            ]
            if session.state != "exited":
                if session.stream_mode == "pty":
                    actions.append(
                        PanelItem(
                            label="send hidden input",
                            description="write one masked line directly to the PTY",
                            command=f"/ps input {session.session_id}",
                        )
                    )
                actions.extend(
                    (
                        PanelItem(
                            label="interrupt",
                            description="send a soft interrupt",
                            command=f"/ps interrupt {session.session_id}",
                        ),
                        PanelItem(
                            label="terminate",
                            description="stop the process tree",
                            command=f"/ps terminate {session.session_id}",
                        ),
                    )
                )
            children.append(
                (
                    session.session_id,
                    PanelDefinition(
                        view_type="process_session_actions",
                        title=f"{title} · {session.session_id}",
                        items=tuple(actions),
                        return_to_parent_on_submit=True,
                    ),
                )
            )
        return PanelDefinition(
            view_type=model.view_type,
            title=title,
            items=items,
            children=tuple(children),
            filterable=True,
        )

    return CommandPanelSpec(
        "process_sessions",
        ProcessSessionsViewModel,
        build,
    )


def register_actions(registry: ActionRegistry) -> None:
    registry.register_many(
        [
            ActionSpec(
                action_id="processes.list",
                feature_id="processes",
                description="[session] Browse running and retained shell process sessions",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
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
                feature_id="processes",
                description="[session] Poll, interrupt, or terminate a shell process session",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED,
                triggers=(
                    slash_trigger("/ps poll <id>"),
                    slash_trigger("/ps interrupt <id>"),
                    slash_trigger("/ps terminate <id>"),
                    slash_trigger("/stop <id|all>"),
                ),
                parser=_parse_control,
                handler=_handle_control,
                during_turn=DuringTurnPolicy.IMMEDIATE,
            ),
            ActionSpec(
                action_id="processes.secure_input",
                feature_id="processes",
                description="[session] Send one masked line directly to a PTY session",
                ui_targets=UI_TARGETS,
                required_capabilities=TEXT_REQUIRED
                | {UICapability.SECURE_TEXT_INPUT},
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
