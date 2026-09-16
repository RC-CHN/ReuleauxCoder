from __future__ import annotations

import shlex
import sys
import time
from types import SimpleNamespace

import pytest

from reuleauxcoder.app.commands.capabilities import UICapability, UIProfile
from reuleauxcoder.app.commands.models import CommandEffect
from reuleauxcoder.app.commands.registry import ActionRegistry
from reuleauxcoder.app.commands.requests import ActionRequest
from reuleauxcoder.app.commands.service import CommandService
from reuleauxcoder.app.interaction_contracts import ConfirmResponse, InputTextResponse
from reuleauxcoder.app.ui_events import UIEventBus
from reuleauxcoder.domain.process import ProcessState
from reuleauxcoder.domain.process_manager import ProcessManager
from reuleauxcoder.extensions.command.builtin.processes import (
    ControlProcessCommand,
    ListProcessesCommand,
    SecureProcessInputCommand,
    _handle_control,
    _handle_list,
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


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


def _context(manager: ProcessManager, *, interactor=None, profile=CLI_PROFILE):
    return SimpleNamespace(
        agent=SimpleNamespace(
            process_manager=manager,
            agent_id="agent",
            current_session_id="session",
            session_generation=0,
        ),
        effect=CommandEffect(),
        ui_interactor=interactor,
        ui_profile=profile,
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
    assert registry.parse("/ps input proc_1", ui_profile=REMOTE_CLI_PROFILE) is None


def test_process_panel_poll_retains_output_without_notices_and_rejecting_stop_keeps_process(
    tmp_path,
):
    manager = ProcessManager()
    handle = manager.start(
        LocalProcessPort(),
        _python_command(
            "print('retained output', flush=True); import time; time.sleep(30)"
        ),
        cwd=str(tmp_path),
        runtime_timeout=60,
        tty=False,
        owner_agent_id="agent",
        owner_session_id="session",
        session_generation=0,
        origin_turn_id="turn",
    )
    manager.publish(handle.session_id)
    ctx = _context(
        manager,
        profile=UIProfile("tui", "TUI", frozenset({UICapability.MENUS})),
        interactor=SimpleNamespace(
            confirm=lambda request: ConfirmResponse(confirmed=False)
        ),
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            ctx.effect = CommandEffect()
            result = _handle_control(
                ControlProcessCommand("poll", handle.session_id), ctx
            )
            if "retained output" in result.views[-1].view_model.output:
                break
            assert time.monotonic() < deadline, "process did not produce output"
            time.sleep(0.01)
        assert not result.notifications
        ctx.effect = CommandEffect()
        again = _handle_control(ControlProcessCommand("poll", handle.session_id), ctx)
        assert again.views[-1].view_model.output == result.views[-1].view_model.output
        ctx.effect = CommandEffect()
        _handle_control(ControlProcessCommand("terminate", handle.session_id), ctx)
        assert (
            manager.get_view(
                handle.session_id,
                agent_id="agent",
                owner_session_id="session",
                session_generation=0,
            ).state
            is ProcessState.RUNNING
        )
        snapshot = manager.poll(
            handle.session_id,
            consumer="model",
            agent_id="agent",
            owner_session_id="session",
            session_generation=0,
        )
        assert "retained output" in snapshot.stdout
    finally:
        manager.shutdown(grace_seconds=0)


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
    confirmations = []

    def confirm(request):
        confirmations.append(request)
        return ConfirmResponse(confirmed=True)

    ctx = _context(manager, interactor=SimpleNamespace(confirm=confirm))

    result = _handle_list(ListProcessesCommand(), ctx)
    view = result.views[0].view_model
    definition = command_panel_spec().build_for(view, "Process Sessions")

    assert definition is not None
    child = definition.child_for(handle.session_id)
    assert child is not None
    assert (
        definition.items[0].label.startswith(sys.executable)
        or "python" in definition.items[0].label
    )
    assert child.keep_open_on_submit
    assert not child.show_auxiliary_actions
    assert child.on_open == ActionRequest(
        "processes.control", ControlProcessCommand("poll", handle.session_id)
    )
    assert tuple(item.action for item in child.items) == (
        ActionRequest(
            "processes.control", ControlProcessCommand("poll", handle.session_id)
        ),
        ActionRequest(
            "processes.control", ControlProcessCommand("interrupt", handle.session_id)
        ),
        ActionRequest(
            "processes.control", ControlProcessCommand("terminate", handle.session_id)
        ),
    )

    ctx.effect = CommandEffect()
    controlled = _handle_control(
        ControlProcessCommand("terminate", handle.session_id),
        ctx,
    )
    assert "latest state is" in controlled.notifications[0].message
    assert controlled.views[-1].action == "refresh"
    assert handle.session_id in confirmations[0].message
    assert "descendants" in confirmations[0].message
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

    bus = UIEventBus()
    registry = ActionRegistry()
    register_actions(registry)
    commands = CommandService(
        ctx.agent,
        SimpleNamespace(),
        bus,
        CLI_PROFILE,
        registry,
        interactions=ctx.ui_interactor,
    )
    commands.submit(
        ActionRequest(
            "processes.secure_input", SecureProcessInputCommand(handle.session_id)
        )
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
    assert all(secret not in event.message for event in bus.history_snapshot())
    assert secret not in output
    assert "[hidden input redacted]" in output
    definition = command_panel_spec().build_for(
        bus.history_snapshot()[-1].payload.view_model,
        "Process Sessions",
    )
    assert definition is not None
    child = definition.child_for(handle.session_id)
    assert child is not None
    assert ActionRequest(
        "processes.secure_input", SecureProcessInputCommand(handle.session_id)
    ) in tuple(item.action for item in child.items)
    manager.shutdown()
