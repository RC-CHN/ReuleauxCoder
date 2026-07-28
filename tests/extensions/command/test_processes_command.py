from __future__ import annotations

import shlex
import sys
import time
from types import SimpleNamespace

import pytest

from reuleauxcoder.app.commands.models import CommandEffect
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.domain.process import ProcessState
from reuleauxcoder.domain.process_manager import ProcessManager
from reuleauxcoder.extensions.command.builtin.processes import (
    ControlProcessCommand,
    ListProcessesCommand,
    SecureProcessInputCommand,
    _handle_control,
    _handle_list,
    _handle_secure_input,
    _parse_control,
    _parse_list,
    _parse_secure_input,
    command_panel_spec,
    register_actions,
)
from reuleauxcoder.infrastructure.process.local import LocalProcessPort
from reuleauxcoder.interfaces.cli.registration import (
    CLI_PROFILE,
    REMOTE_CLI_PROFILE,
)
from reuleauxcoder.interfaces.interactions import InputTextResponse


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


def _context(manager: ProcessManager, *, interactor=None):
    return SimpleNamespace(
        agent=SimpleNamespace(
            process_manager=manager,
            agent_id="agent",
            current_session_id="session",
            session_generation=0,
        ),
        effect=CommandEffect(),
        ui_interactor=interactor,
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
    assert _parse_secure_input("/ps input proc_1", None) == SecureProcessInputCommand(
        session_id="proc_1",
    )


def test_hidden_input_command_is_not_advertised_to_unmasked_remote_cli() -> None:
    registry = ActionRegistry()
    register_actions(registry)

    assert registry.parse("/ps input proc_1", ui_profile=CLI_PROFILE) is not None
    assert (
        registry.parse("/ps input proc_1", ui_profile=REMOTE_CLI_PROFILE)
        is None
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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY integration")
def test_hidden_process_input_is_direct_and_never_returned(tmp_path) -> None:
    secret = "direct-user-secret"
    requests = []

    class _Interactor:
        def input_text(self, request):
            requests.append(request)
            return InputTextResponse(secret)

    manager = ProcessManager()
    port = LocalProcessPort()
    handle = manager.start(
        port,
        _python_command(
            "import time; "
            "print('ready', flush=True); "
            "value=input(); "
            "print('received:'+value, flush=True); "
            "time.sleep(1)"
        ),
        cwd=str(tmp_path),
        runtime_timeout=5,
        tty=True,
        owner_agent_id="agent",
        owner_session_id="session",
        session_generation=0,
        origin_turn_id="turn",
    )
    manager.publish(handle.session_id)
    ctx = _context(manager, interactor=_Interactor())

    result = _handle_secure_input(
        SecureProcessInputCommand(handle.session_id),
        ctx,
    )
    deadline = time.monotonic() + 5
    output = ""
    state = ProcessState.RUNNING
    while state is ProcessState.RUNNING and time.monotonic() < deadline:
        snapshot = manager.poll(
            handle.session_id,
            consumer="test",
            agent_id="agent",
            owner_session_id="session",
            session_generation=0,
            wait_ms=100,
        )
        output += snapshot.stdout + snapshot.stderr
        state = snapshot.state

    assert requests[0].secret is True
    assert secret not in result.notifications[0].message
    assert secret not in output
    assert "[hidden input redacted]" in output
    definition = command_panel_spec().build_for(
        result.views[-1].view_model,
        "Process Sessions",
    )
    assert definition is not None
    child = definition.child_for(handle.session_id)
    assert child is not None
    assert f"/ps input {handle.session_id}" in tuple(
        item.command for item in child.items
    )
    manager.shutdown()
