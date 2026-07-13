"""ShellTool product semantics over the shared ProcessPort."""

from __future__ import annotations

import os
from pathlib import Path
import threading
import time

from reuleauxcoder.domain.process import ProcessResult
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolOutcomeStatus,
    ToolRetentionStrategy,
)
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
    assert process.calls == []
