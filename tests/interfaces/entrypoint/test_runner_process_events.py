from __future__ import annotations

import time

from reuleauxcoder.domain.process import (
    ProcessSnapshot,
    ProcessState,
    ProcessStreamMode,
)
from reuleauxcoder.domain.process_manager import (
    ProcessEvent,
    ProcessEventKind,
)
from reuleauxcoder.domain.runtime.events import ProcessSessionChanged
from reuleauxcoder.interfaces.entrypoint.runner import AppRunner


def test_background_process_output_is_forwarded_as_safe_runtime_data() -> None:
    runtime_events = []

    class _Bus:
        def emit_runtime(self, event) -> None:
            runtime_events.append(event)

    snapshot = ProcessSnapshot(
        session_id="proc-output",
        state=ProcessState.RUNNING,
        stream_mode=ProcessStreamMode.PIPE,
        backend="local",
        stdout="ready\x1b]0;unsafe-title\x07\n",
        stderr="warning\n",
        started_at=time.time(),
        runtime_timeout_seconds=60,
    )
    event = ProcessEvent(
        kind=ProcessEventKind.OUTPUT,
        snapshot=snapshot,
        command="long-running",
        cwd="/workspace",
        owner_agent_id="agent",
        owner_session_id="session",
        session_generation=2,
        origin_turn_id="turn",
    )

    AppRunner._emit_process_event(  # type: ignore[arg-type]
        _Bus(),
        event,
    )

    payload = runtime_events[0].payload
    assert isinstance(payload, ProcessSessionChanged)
    assert payload.stdout == (
        "ready\\x1b]0;unsafe-title\\x07\n"
        "\n[terminal control bytes escaped for display]\n"
    )
    assert payload.stderr == "warning\n"
    assert runtime_events[0].correlation_id == "proc-output"

    AppRunner._emit_process_event(  # type: ignore[arg-type]
        _Bus(),
        ProcessEvent(
            kind=ProcessEventKind.COMPLETED,
            snapshot=snapshot,
            command=event.command,
            cwd=event.cwd,
            owner_agent_id=event.owner_agent_id,
            owner_session_id=event.owner_session_id,
            session_generation=event.session_generation,
            origin_turn_id=event.origin_turn_id,
        ),
    )
    completed = runtime_events[1].payload
    assert isinstance(completed, ProcessSessionChanged)
    assert completed.stdout == ""
    assert completed.stderr == ""
