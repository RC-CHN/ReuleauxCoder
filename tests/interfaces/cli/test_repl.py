"""Exercise line editing and approvals against a real JSON-RPC runtime."""

from io import StringIO
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.data_structures import Size
from prompt_toolkit.output.vt100 import Vt100_Output
from rich.console import Console

from reuleauxcoder.app.commands.loader import create_builtin_action_registry
from reuleauxcoder.app.interaction_contracts import ConfirmRequest
from reuleauxcoder.app.ui_events import UIEventBus
from reuleauxcoder.domain.agent.agent import Agent
from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.domain.runtime.events import (
    AssistantContentDelta,
    OperationPhaseChanged,
    ReasoningDelta,
    RuntimeEvent,
)
from reuleauxcoder.interfaces.cli.output import CLIOutputCoordinator
from reuleauxcoder.interfaces.cli.registration import create_cli_registration
from reuleauxcoder.interfaces.cli.render import CLIRenderer
from reuleauxcoder.interfaces.cli.repl import run_repl
from reuleauxcoder.interfaces.entrypoint.rpc import connect_local


def wait_for(predicate):
    deadline = monotonic() + 5
    while not predicate():
        assert monotonic() < deadline, "Timed out waiting for CLI/runtime"
        sleep(0.01)


def test_bracketed_image_path_paste_keeps_marker_in_user_message(
    cli_runtime, tmp_path, monkeypatch
):
    from reuleauxcoder.domain.images import display_content, image_parts
    from tests.domain.test_images import picture

    rt = cli_runtime
    rt.agent.llm.support_modal = ("text", "image")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    path = tmp_path / "screen shot.png"
    path.write_bytes(picture(size=(4, 3)))
    failures = []
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):

        def drive():
            try:
                pipe.send_text(f'look \x1b[200~"{path}"\x1b[201~')
                wait_for(lambda: "[Image #1]" in rt.transcript.getvalue())
                pipe.send_text("\n")
                wait_for(lambda: bool(image_parts(rt.agent.messages)))
                wait_for(lambda: not rt.client.state.running)
                pipe.send_text("/quit\n")
            except BaseException as error:
                failures.append(error)
                pipe.send_text("\x15\x04")

        driver = Thread(target=drive, daemon=True)
        driver.start()
        run_repl(rt.client, rt.bus, rt.output, rt.interactor)
        driver.join(6)
    assert not failures
    assert display_content(rt.agent.messages[0]["content"]) == "look [Image #1]"


@pytest.fixture
def cli_runtime(tmp_path):
    config = Config(
        api_key="test", session_auto_save=False, history_file=str(tmp_path / "history")
    )
    loop = SimpleNamespace(run=lambda: "done")
    agent = Agent(
        SimpleNamespace(model="test-model", debug_trace=False),
        tools=[],
        config=config,
        loop=loop,
    )
    agent.current_session_id = "cli-test"
    bus = UIEventBus()
    registration = create_cli_registration(bus)
    transcript = StringIO()
    renderer = CLIRenderer(
        console_override=Console(file=transcript, width=120),
        live_activity=False,
        root_agent_id=agent.agent_id,
    )
    output = CLIOutputCoordinator(renderer)
    bus.subscribe(output.on_ui_event)
    ctx = SimpleNamespace(
        agent=agent,
        config=config,
        ui_bus=UIEventBus(),
        action_registry=create_builtin_action_registry(),
        sessions_dir=tmp_path,
        session_exit_time=None,
        skills_service=None,
    )
    connection = connect_local(
        ctx,
        registration.profile,
        bus,
        registration.interactor,
        foreground_interactions=True,
    )
    try:
        yield SimpleNamespace(
            client=connection.client,
            server=connection.server,
            agent=agent,
            loop=loop,
            bus=bus,
            interactor=registration.interactor,
            output=output,
            transcript=transcript,
        )
    finally:
        connection.close()
        registration.interactor.shutdown()
        output.close()
        agent.unbind_session_persistence()


def test_redirected_cli_runs_commands_and_chat_through_rpc(cli_runtime, monkeypatch):
    rt = cli_runtime
    monkeypatch.setattr("sys.stdin", StringIO("/model\n/help\nhello\n/quit\n"))
    run_repl(rt.client, rt.bus, rt.output, rt.interactor)
    text = rt.transcript.getvalue()
    assert "/model" in text
    assert "Switch main: /model <profile>" in text
    assert "/model use-sub <profile>" in text
    assert "done" in text
    assert any(message.get("content") == "hello" for message in rt.agent.messages)
    assert not rt.client.state.running


def test_running_input_approval_draft_details_and_interrupt(cli_runtime, monkeypatch):
    rt = cli_runtime
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    ask = Event()
    approved = Event()
    stopped = Event()
    failures = []

    def run():
        for phase in ("connect", "await_first_chunk", "streaming"):
            rt.server.bus.emit_runtime(
                RuntimeEvent(
                    payload=OperationPhaseChanged(
                        operation_id="model-request",
                        operation="model",
                        phase=phase,
                    ),
                    agent_id=rt.agent.agent_id,
                    session_generation=rt.client.state.session_generation,
                )
            )
        assert ask.wait(5)
        response = rt.server.interactions.confirm(
            ConfirmRequest("Approve test?", "Continue?")
        )
        assert response.confirmed
        approved.set()
        assert stopped.wait(5)
        return "finished"

    rt.loop.run = run
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):

        def drive():
            try:
                pipe.send_text("first\n")
                wait_for(lambda: rt.client.state.running)
                wait_for(
                    lambda: rt.output.renderer.current_activity == "generating response"
                )
                assert "waiting for first response" not in rt.transcript.getvalue()
                assert "connecting to model" not in rt.transcript.getvalue()
                pipe.send_text("排队提示\n")
                wait_for(lambda: rt.client.state.queued_steering == ("排队提示",))
                sleep(0.1)
                pipe.send_text("/model test-profile\n")
                wait_for(lambda: bool(rt.client.state.queued_commands))
                assert rt.client.state.queued_steering == ("排队提示",)
                pipe.send_text("保留草稿")
                sleep(0.15)
                ask.set()
                wait_for(lambda: rt.interactor.active_request_id is not None)
                sleep(0.15)
                pipe.send_text("y\n")
                assert approved.wait(5)
                sleep(0.15)
                pipe.send_text("\x1bOS")  # F4 yields and restores the same draft.
                sleep(0.15)
                pipe.send_text("\x1bOQ")  # F2
                wait_for(lambda: "Queued Prompt: 排队提示" in rt.transcript.getvalue())
                pipe.send_text("\n")
                wait_for(lambda: "保留草稿" in rt.client.state.queued_steering)
                pipe.send_text("\x03")
                wait_for(lambda: rt.client.state.interrupt_pending)
                pipe.send_text("\x03")
                wait_for(rt.agent.stop_requested)
                stopped.set()
                wait_for(lambda: not rt.client.state.running)
                pipe.send_text("/quit\n")
            except BaseException as error:
                failures.append(error)
                ask.set()
                stopped.set()
                pipe.send_text("\x15\x04")

        driver = Thread(target=drive, daemon=True)
        driver.start()
        run_repl(rt.client, rt.bus, rt.output, rt.interactor)
        driver.join(6)
    assert not driver.is_alive()
    if failures:
        raise failures[0]
    assert "Retained tool arguments" in rt.transcript.getvalue()


def test_silent_stream_events_do_not_erase_the_prompt(cli_runtime, monkeypatch):
    rt = cli_runtime
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    release = Event()
    rt.loop.run = lambda: (release.wait(10), "done")[-1]
    terminal = StringIO()
    terminal_output = Vt100_Output(
        terminal,
        lambda: Size(rows=24, columns=100),
        enable_cpr=False,
    )
    failures = []

    def emit(payload):
        rt.server.bus.emit_runtime(
            RuntimeEvent(
                payload=payload,
                agent_id=rt.agent.agent_id,
                session_generation=rt.client.state.session_generation,
            )
        )

    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=terminal_output),
    ):

        def drive():
            try:
                pipe.send_text("first\n")
                wait_for(lambda: rt.client.state.running)
                emit(ReasoningDelta("first", display_mode="quiet"))
                wait_for(lambda: "THINK" in rt.transcript.getvalue())
                sleep(0.15)
                pipe.send_text("草稿")
                wait_for(lambda: "草稿" in terminal.getvalue())
                baseline = terminal.getvalue()
                for _ in range(20):
                    emit(ReasoningDelta("more", display_mode="quiet"))
                    sleep(0.03)
                emit(AssistantContentDelta("uncommitted paragraph"))
                wait_for(lambda: rt.output.renderer.stream.active_block is not None)
                assert terminal.getvalue() == baseline
                rt.server.bus.warning("VISIBLE WARNING")
                wait_for(lambda: "VISIBLE WARNING" in rt.transcript.getvalue())
                assert rt.transcript.getvalue().count("VISIBLE WARNING") == 1
                assert "草稿" in terminal.getvalue()[len(baseline):]
                pipe.send_text("\x03")
                release.set()
                wait_for(lambda: not rt.client.state.running)
                pipe.send_text("/quit\n")
            except BaseException as error:
                failures.append(error)
                release.set()
                pipe.send_text("\x15\x04")

        driver = Thread(target=drive, daemon=True)
        driver.start()
        run_repl(rt.client, rt.bus, rt.output, rt.interactor)
        driver.join(6)
    assert not driver.is_alive()
    if failures:
        raise failures[0]
