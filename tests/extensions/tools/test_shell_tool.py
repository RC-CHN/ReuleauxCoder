"""ShellTool product semantics over the shared ProcessPort."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import threading
import time
from types import SimpleNamespace
from typing import Any, cast

from reuleauxcoder.domain.process_manager import ProcessManager
from reuleauxcoder.domain.process import (
    ProcessCursor,
    ProcessHandle,
    ProcessResult,
    ProcessShutdownReport,
    ProcessSnapshot,
    ProcessState,
    ProcessStreamMode,
)
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolOutcomeStatus,
    ToolRetentionStrategy,
)
from reuleauxcoder.extensions.tools.backend import ExecutionContext, LocalToolBackend
from reuleauxcoder.extensions.tools.builtin.shell import ShellSessionTool, ShellTool


class RecordingProcessPort:
    def __init__(self, result: ProcessResult | None = None):
        self.result = result or ProcessResult(stdout="ok\n", exit_code=0)
        self.calls = []

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return self.result


def _tool(process: RecordingProcessPort, *, cwd: str | None = None) -> ShellTool:
    return ShellTool(
        LocalToolBackend(
            ExecutionContext(cwd=cwd),
            process=process,
        )
    )


def _python_command(source: str) -> str:
    import sys

    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


def _bind(tool, manager: ProcessManager):
    tool.bind_agent(
        SimpleNamespace(
            process_manager=manager,
            agent_id="agent",
            current_session_id="session",
            session_generation=0,
            _current_turn_id="turn",
        )
    )
    tool.bind_execution(tool_call_id="call", session_generation=0)
    return tool


def test_explicit_cwd_overrides_and_can_be_persisted(tmp_path: Path) -> None:
    process = RecordingProcessPort()
    tool = _tool(process, cwd=str(tmp_path))
    alternate = str(tmp_path / "alternate")

    result = tool._execute_local("echo ok", cwd=alternate, persist_cwd=True)

    assert result.model_text == "ok"
    assert result.summary is not None
    assert result.summary.startswith("Command completed · ")
    assert result.duration_seconds is not None
    assert result.duration_seconds >= 0
    assert result.status is ToolOutcomeStatus.SUCCEEDED
    assert process.calls[0][1]["cwd"] == alternate
    assert tool._cwd == alternate


def test_cwd_without_persist_does_not_change_session(tmp_path: Path) -> None:
    process = RecordingProcessPort()
    tool = _tool(process, cwd=str(tmp_path))
    tool._cwd = str(tmp_path)

    tool._execute_local("echo ok", cwd=str(tmp_path / "one-off"))

    assert tool._cwd == str(tmp_path)


def test_shell_formats_stderr_exit_timeout_and_cancel(tmp_path: Path) -> None:
    process = RecordingProcessPort(
        ProcessResult(stdout="out", stderr="bad", exit_code=7)
    )
    tool = _tool(process, cwd=str(tmp_path))
    failed = tool._execute_local("demo")
    assert failed.model_text == "out\n[stderr]\nbad\n[exit code: 7]"
    assert failed.stdout == "out"
    assert failed.stderr == "bad"
    assert failed.exit_code == 7
    assert failed.status is ToolOutcomeStatus.FAILED

    process.result = ProcessResult(stdout="first\nlatest\n", timed_out=True)
    timed_out = tool._execute_local("demo", timeout=3)
    assert timed_out.model_text == (
        "first\nlatest\n"
        "[system] Command timed out after 3s; output captured until termination."
    )
    assert timed_out.status is ToolOutcomeStatus.TIMED_OUT
    assert timed_out.retention_hint.strategy is ToolRetentionStrategy.TAIL
    process.result = ProcessResult(stdout="partial\n", cancelled=True)
    cancelled = tool._execute_local("demo")
    assert cancelled.model_text == (
        "partial\n[system] Command was cancelled; output captured until termination."
    )
    assert cancelled.status is ToolOutcomeStatus.CANCELLED


def test_shell_passes_runtime_cancellation_event(tmp_path: Path) -> None:
    cancellation = threading.Event()
    process = RecordingProcessPort()
    backend = LocalToolBackend(
        ExecutionContext(cwd=str(tmp_path), cancellation_event=cancellation),
        process=process,
    )

    ShellTool(backend)._execute_local("echo ok")

    assert process.calls[0][1]["cancellation_event"] is cancellation


def test_local_process_port_executes_and_cancels_real_process() -> None:
    tool = ShellTool()
    assert "hello" in tool._execute_local("echo hello").model_text

    cancellation = threading.Event()
    backend = LocalToolBackend(ExecutionContext(cancellation_event=cancellation))
    cancellable = ShellTool(backend=backend)
    timer = threading.Timer(0.2, cancellation.set)
    started = time.monotonic()
    timer.start()
    try:
        result = cancellable._execute_local(
            "python -c 'import time; time.sleep(30)'", timeout=20
        )
    finally:
        timer.cancel()

    assert "cancelled" in result.model_text.lower()
    assert time.monotonic() - started < 3


def test_invalid_inputs_are_rejected_before_process_port() -> None:
    process = RecordingProcessPort()
    tool = _tool(process, cwd=os.getcwd())

    assert "non-empty" in tool.execute("").model_text
    assert "positive integer" in tool.execute("echo", timeout=0).model_text
    assert (
        "cwd must be"
        in tool.execute(  # type: ignore[arg-type]
            "echo", cwd=123
        ).model_text
    )
    assert "session manager" in tool.execute("echo", tty=True).model_text
    assert process.calls == []


def test_shell_session_schema_bounds_model_supplied_input() -> None:
    tool = ShellSessionTool()
    schema = tool.parameters

    assert schema["properties"]["chars"]["maxLength"] == 64 * 1024
    rejected = tool._preflight_validate(
        "session",
        "write",
        "密" * (64 * 1024),
    )
    assert rejected is not None
    assert "64 KiB" in rejected.model_text
    assert '"executed": false' in rejected.model_text


def test_rtk_configuration_never_rewrites_the_command(tmp_path: Path) -> None:
    process = RecordingProcessPort()
    tool = _tool(process, cwd=str(tmp_path))
    tool._agent_config = SimpleNamespace(shell_rtk="on")

    tool.execute("printf 'a && b'")

    assert process.calls[0][0] == "printf 'a && b'"


def test_managed_shell_yields_and_session_can_terminate(tmp_path: Path) -> None:
    manager = ProcessManager()
    backend = LocalToolBackend(
        ExecutionContext(cwd=str(tmp_path)),
    )
    shell = _bind(ShellTool(backend), manager)
    session = _bind(ShellSessionTool(backend), manager)
    started = time.monotonic()

    running = shell.execute(
        _python_command("import time; print('ready', flush=True); time.sleep(30)"),
        timeout=60,
        yield_ms=250,
    )
    facts = cast(dict[str, Any], running.metadata["process_snapshot"])

    assert facts["state"] == "running"
    assert facts["stream_mode"] == "pipe"
    assert "ready" in facts["stdout"]
    assert time.monotonic() - started < 2
    session_id = str(facts["session_id"])

    rejected_write = session.execute(session_id, "write", chars="hello\n")
    rejected_facts = cast(
        dict[str, Any],
        rejected_write.metadata["process_snapshot"],
    )
    assert rejected_facts["state"] == "running"
    assert rejected_facts["stream_mode"] == "pipe"
    assert '"executed": false' in rejected_write.model_text
    assert "stdin is closed" in rejected_write.model_text

    stopped = session.execute(session_id, "terminate")
    stopped_facts = cast(dict[str, Any], stopped.metadata["process_snapshot"])
    assert stopped_facts["state"] in {"running", "exited"}
    manager.shutdown(grace_seconds=0)


def test_managed_shell_reports_nonzero_exit_as_process_fact(tmp_path: Path) -> None:
    manager = ProcessManager()
    backend = LocalToolBackend(ExecutionContext(cwd=str(tmp_path)))
    shell = _bind(ShellTool(backend), manager)

    result = shell.execute(
        _python_command("import sys; print('bad command'); sys.exit(7)"),
        yield_ms=1_000,
    )
    facts = cast(dict[str, Any], result.metadata["process_snapshot"])

    assert result.status is ToolOutcomeStatus.FAILED
    assert facts["state"] == "exited"
    assert facts["exit_code"] == 7
    assert facts["stdout"] == "bad command\n"
    assert '"executed": false' not in result.model_text.lower()
    assert '"executed": true' in result.model_text.lower()
    manager.shutdown()


def test_ambiguous_session_operation_is_attempted_but_not_confirmed() -> None:
    class AmbiguousWritePort:
        backend_name = "remote"

        def __init__(self) -> None:
            self.state = ProcessState.RUNNING

        def start(self, command, **_kwargs):
            del command
            return ProcessHandle("ambiguous-write", ProcessStreamMode.PTY)

        def poll(self, session_id, *, cursor=None, wait_ms=0):
            del wait_ms
            return ProcessSnapshot(
                session_id=session_id,
                state=self.state,
                stream_mode=ProcessStreamMode.PTY,
                backend="remote",
                cursor=cursor or ProcessCursor(),
                started_at=time.time(),
                runtime_timeout_seconds=60,
            )

        def write_input(self, session_id, data):
            del session_id, data
            raise RuntimeError("write response was lost")

        def resize(self, session_id, *, rows, columns):
            del session_id, rows, columns

        def interrupt(self, session_id):
            return self.poll(session_id)

        def terminate(self, session_id, *, reason="terminated"):
            del reason
            self.state = ProcessState.EXITED
            return self.poll(session_id)

        def release(self, session_id):
            del session_id

        def shutdown(self, *, grace_seconds=0.5):
            del grace_seconds
            return ProcessShutdownReport()

    manager = ProcessManager()
    port = AmbiguousWritePort()
    handle = manager.start(
        port,  # type: ignore[arg-type]
        "interactive",
        cwd=".",
        runtime_timeout=60,
        tty=True,
        owner_agent_id="agent",
        owner_session_id="session",
        session_generation=0,
        origin_turn_id="turn",
    )
    manager.publish(handle.session_id)
    backend = LocalToolBackend(ExecutionContext())
    session = _bind(ShellSessionTool(backend), manager)

    result = session.execute(handle.session_id, "write", chars="maybe-once\n")

    assert '"executed": true' in result.model_text
    assert '"confirmed": false' in result.model_text
    assert "write response was lost" in result.model_text

    manager._entries[handle.session_id].input_bytes = 1024 * 1024
    rejected = session.execute(handle.session_id, "write", chars="bounded\n")
    assert '"executed": false' in rejected.model_text
    assert '"confirmed": true' in rejected.model_text
    assert "1 MiB session limit" in rejected.model_text
    manager.shutdown()
