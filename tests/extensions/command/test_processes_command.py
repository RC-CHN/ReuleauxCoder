from __future__ import annotations

import shlex
import sys
from types import SimpleNamespace

from reuleauxcoder.app.commands.models import CommandEffect
from reuleauxcoder.domain.process_manager import ProcessManager
from reuleauxcoder.extensions.command.builtin.processes import (
    ControlProcessCommand,
    ListProcessesCommand,
    _handle_control,
    _handle_list,
    _parse_control,
    _parse_list,
    command_panel_spec,
)
from reuleauxcoder.infrastructure.process.local import LocalProcessPort


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


def _context(manager: ProcessManager):
    return SimpleNamespace(
        agent=SimpleNamespace(
            process_manager=manager,
            agent_id="agent",
            current_session_id="session",
            session_generation=0,
        ),
        effect=CommandEffect(),
    )


def test_process_command_parsing_keeps_control_explicit() -> None:
    assert _parse_list("/ps", None) == ListProcessesCommand()
    assert _parse_list("/stop", None) == ListProcessesCommand(stop_picker=True)
    assert _parse_control("/ps interrupt proc_1", None) == ControlProcessCommand(
        action="interrupt",
        session_id="proc_1",
    )
    assert _parse_control("/stop all", None) == ControlProcessCommand(
        action="terminate",
        session_id="all",
    )


def test_process_panel_is_owned_by_process_command_and_exposes_factual_actions(
    tmp_path,
) -> None:
    manager = ProcessManager()
    port = LocalProcessPort()
    handle = manager.start(
        port,
        _python_command("import time; time.sleep(30)"),
        cwd=str(tmp_path),
        runtime_timeout=60,
        tty=False,
        owner_agent_id="agent",
        owner_session_id="session",
        session_generation=0,
        origin_turn_id="turn",
    )
    manager.publish(handle.session_id)
    ctx = _context(manager)

    result = _handle_list(ListProcessesCommand(), ctx)
    view = result.views[0].view_model
    definition = command_panel_spec().build_for(view, "Process Sessions")

    assert definition is not None
    child = definition.child_for(handle.session_id)
    assert child is not None
    assert tuple(item.command for item in child.items) == (
        f"/ps poll {handle.session_id}",
        f"/ps interrupt {handle.session_id}",
        f"/ps terminate {handle.session_id}",
    )

    ctx.effect = CommandEffect()
    controlled = _handle_control(
        ControlProcessCommand("terminate", handle.session_id),
        ctx,
    )
    assert "latest state is" in controlled.notifications[0].message
    assert controlled.views[-1].action == "refresh"
    manager.shutdown(grace_seconds=0)
