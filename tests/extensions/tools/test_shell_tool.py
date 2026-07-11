"""ShellTool product semantics over the shared ProcessPort."""

from __future__ import annotations

import os
from pathlib import Path
import threading
import time

from reuleauxcoder.domain.process import ProcessResult
from reuleauxcoder.extensions.tools.backend import ExecutionContext, LocalToolBackend
from reuleauxcoder.extensions.tools.builtin.shell import ShellTool


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


def test_explicit_cwd_overrides_and_can_be_persisted(tmp_path: Path) -> None:
    process = RecordingProcessPort()
    tool = _tool(process, cwd=str(tmp_path))
    alternate = str(tmp_path / "alternate")

    result = tool._execute_local(
        "echo ok", cwd=alternate, persist_cwd=True
    )

    assert result == "ok"
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
    assert tool._execute_local("demo") == "out\n[stderr]\nbad\n[exit code: 7]"

    process.result = ProcessResult(timed_out=True)
    assert tool._execute_local("demo", timeout=3) == "Error: timed out after 3s"
    process.result = ProcessResult(cancelled=True)
    assert tool._execute_local("demo") == "Error: shell command cancelled"


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
    assert "hello" in tool._execute_local("echo hello")

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

    assert "cancelled" in result.lower()
    assert time.monotonic() - started < 3


def test_invalid_inputs_are_rejected_before_process_port() -> None:
    process = RecordingProcessPort()
    tool = _tool(process, cwd=os.getcwd())

    assert "non-empty" in tool.execute("")
    assert "positive integer" in tool.execute("echo", timeout=0)
    assert "cwd must be" in tool.execute("echo", cwd=123)  # type: ignore[arg-type]
    assert process.calls == []
