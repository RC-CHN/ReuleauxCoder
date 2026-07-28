from __future__ import annotations

from collections import deque
import threading
import time

from reuleauxcoder.domain.process import ProcessCursor, ProcessState
from reuleauxcoder.extensions.remote_exec.backend import (
    RemoteProcessPort,
    RemoteRelayToolBackend,
)
from reuleauxcoder.extensions.remote_exec.errors import RemoteTimeoutError
from reuleauxcoder.extensions.remote_exec.peer_registry import PeerRegistry
from reuleauxcoder.extensions.remote_exec.protocol import WorkspaceResult
from reuleauxcoder.extensions.tools.backend import ExecutionContext


class _Relay:
    def __init__(self, responses) -> None:
        self.registry = PeerRegistry()
        self.peer_id = self.registry.register(
            {
                "protocol_version": 2,
                "capabilities": [
                    "process.start",
                    "process.poll",
                    "process.interrupt",
                    "process.terminate",
                    "process.release",
                ],
            }
        )
        self.responses = deque(responses)
        self.requests = []

    def send_workspace_request(self, peer_id, request, *, timeout_sec=30):
        assert peer_id == self.peer_id
        self.requests.append((request, timeout_sec))
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response


def _port(responses):
    relay = _Relay(responses)
    backend = RemoteRelayToolBackend(
        relay,  # type: ignore[arg-type]
        context=ExecutionContext(peer_id=relay.peer_id, cwd="/workspace"),
    )
    return RemoteProcessPort(backend), relay


def test_remote_process_preserves_command_and_retains_terminal_until_release() -> None:
    command = "first && second\nprintf '$HOME'"
    port, relay = _port(
        [
            WorkspaceResult(
                ok=True,
                data={"process_id": "remote-process", "reused": False},
            ),
            WorkspaceResult(
                ok=True,
                data={
                    "process_id": "remote-process",
                    "state": "exited",
                    "done": True,
                    "stdout": "actual output\n",
                    "stderr": "actual error\n",
                    "stdout_offset": 14,
                    "stderr_offset": 13,
                    "exit_code": 7,
                    "termination_reason": "exit",
                    "output_decode_replaced": True,
                },
            ),
            WorkspaceResult(
                ok=True,
                data={
                    "process_id": "remote-process",
                    "state": "exited",
                    "done": True,
                    "stdout": "actual output\n",
                    "stderr": "actual error\n",
                    "stdout_offset": 14,
                    "stderr_offset": 13,
                    "exit_code": 7,
                    "termination_reason": "exit",
                },
            ),
            WorkspaceResult(ok=True, data={"released": True}),
        ]
    )

    handle = port.start(command, cwd="/workspace", runtime_timeout=60)
    snapshot = port.poll(handle.session_id, cursor=ProcessCursor())

    start_request = relay.requests[0][0]
    assert start_request.operation == "process.start"
    assert relay.requests[0][1] == 2
    assert start_request.args["command"] == command
    assert start_request.args["runtime_timeout_ms"] == 60_000
    assert snapshot.state is ProcessState.EXITED
    assert snapshot.exit_code == 7
    assert snapshot.stdout == "actual output\n"
    assert snapshot.stderr == "actual error\n"
    assert snapshot.output_decode_replaced is True

    # A terminal result remains queryable until the owner explicitly releases it.
    assert port.poll(handle.session_id).state is ProcessState.EXITED
    port.release(handle.session_id)
    assert relay.requests[-1][0].operation == "process.release"


def test_ambiguous_remote_start_retries_same_intent_with_same_idempotency_key() -> None:
    port, relay = _port(
        [
            RemoteTimeoutError(30),
            WorkspaceResult(
                ok=True,
                data={"process_id": "accepted-process", "reused": True},
            ),
            WorkspaceResult(
                ok=True,
                data={
                    "process_id": "accepted-process",
                    "state": "running",
                    "done": False,
                    "stdout": "",
                    "stderr": "",
                    "stdout_offset": 0,
                    "stderr_offset": 0,
                },
            ),
        ]
    )

    handle = port.start("do-the-thing", cwd="/workspace", runtime_timeout=60)
    unknown = port._snapshot(port._lookup(handle.session_id), ProcessCursor())
    assert unknown.state is ProcessState.UNKNOWN

    reconciled = port.poll(handle.session_id)

    starts = [
        request
        for request, _timeout in relay.requests
        if request.operation == "process.start"
    ]
    assert len(starts) == 2
    assert starts[0].args["command"] == starts[1].args["command"]
    assert starts[0].args["idempotency_key"] == starts[1].args["idempotency_key"]
    start_timeouts = [
        timeout
        for request, timeout in relay.requests
        if request.operation == "process.start"
    ]
    assert start_timeouts == [2, 2]
    poll_timeout = next(
        timeout
        for request, timeout in relay.requests
        if request.operation == "process.poll"
    )
    assert poll_timeout == 1
    assert reconciled.state is ProcessState.RUNNING


def test_shutdown_attempts_to_terminate_unknown_remote_process() -> None:
    port, relay = _port(
        [
            RemoteTimeoutError(30),
            WorkspaceResult(
                ok=True,
                data={
                    "process_id": "ambiguous-process",
                    "done": True,
                    "exit_code": -1,
                    "termination_reason": "shutdown",
                },
            ),
        ]
    )
    port.start("possibly-started", cwd="/workspace", runtime_timeout=60)

    report = port.shutdown()

    assert report.unknown == 1
    assert report.terminated == 1
    assert relay.requests[-1][0].operation == "process.terminate"


def test_shutdown_terminates_independent_remote_processes_concurrently() -> None:
    class _SlowTerminationRelay(_Relay):
        def __init__(self) -> None:
            super().__init__([])
            self.active = 0
            self.max_active = 0
            self.activity_lock = threading.Lock()

        def send_workspace_request(self, peer_id, request, *, timeout_sec=30):
            del timeout_sec
            assert peer_id == self.peer_id
            self.requests.append((request, 30))
            if request.operation == "process.start":
                return WorkspaceResult(
                    ok=True,
                    data={"process_id": request.args["process_id"]},
                )
            assert request.operation == "process.terminate"
            with self.activity_lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.2)
            with self.activity_lock:
                self.active -= 1
            return WorkspaceResult(
                ok=True,
                data={
                    "done": True,
                    "exit_code": -1,
                    "termination_reason": "shutdown",
                },
            )

    relay = _SlowTerminationRelay()
    backend = RemoteRelayToolBackend(
        relay,  # type: ignore[arg-type]
        context=ExecutionContext(peer_id=relay.peer_id, cwd="/workspace"),
    )
    port = RemoteProcessPort(backend)
    for _ in range(4):
        port.start("long-running-command", cwd="/workspace", runtime_timeout=60)

    started = time.monotonic()
    report = port.shutdown()

    assert time.monotonic() - started < 0.6
    assert relay.max_active == 4
    assert report.terminated == 4


def test_out_of_order_remote_polls_cannot_regress_terminal_state_or_cursor() -> None:
    slow_poll_entered = threading.Event()
    terminal_sent = threading.Event()

    class _OutOfOrderRelay(_Relay):
        def __init__(self) -> None:
            super().__init__([])

        def send_workspace_request(self, peer_id, request, *, timeout_sec=30):
            del timeout_sec
            assert peer_id == self.peer_id
            self.requests.append((request, 30))
            if request.operation == "process.start":
                return WorkspaceResult(
                    ok=True,
                    data={"process_id": "out-of-order", "reused": False},
                )
            assert request.operation == "process.poll"
            if request.args["stdout_offset"] == 0:
                slow_poll_entered.set()
                assert terminal_sent.wait(2)
                return WorkspaceResult(
                    ok=True,
                    data={
                        "state": "running",
                        "done": False,
                        "stdout": "old",
                        "stdout_offset": 3,
                        "stderr_offset": 0,
                        "output_truncated": False,
                        "output_decode_replaced": False,
                    },
                )
            terminal_sent.set()
            return WorkspaceResult(
                ok=True,
                data={
                    "state": "exited",
                    "done": True,
                    "stdout": "terminal",
                    "stdout_offset": 20,
                    "stderr_offset": 0,
                    "exit_code": 0,
                    "termination_reason": "exit",
                    "output_truncated": True,
                    "output_decode_replaced": True,
                },
            )

    relay = _OutOfOrderRelay()
    backend = RemoteRelayToolBackend(
        relay,  # type: ignore[arg-type]
        context=ExecutionContext(peer_id=relay.peer_id, cwd="/workspace"),
    )
    port = RemoteProcessPort(backend)
    handle = port.start("command", cwd="/workspace", runtime_timeout=60)
    slow_results = []
    slow = threading.Thread(
        target=lambda: slow_results.append(
            port.poll(handle.session_id, cursor=ProcessCursor())
        )
    )
    slow.start()
    assert slow_poll_entered.wait(2)

    terminal = port.poll(
        handle.session_id,
        cursor=ProcessCursor(stdout_offset=10),
    )
    slow.join(timeout=2)
    entry = port._lookup(handle.session_id)
    latest = port._snapshot(entry, ProcessCursor(entry.stdout_offset))

    assert not slow.is_alive()
    assert terminal.state is ProcessState.EXITED
    assert terminal.cursor.stdout_offset == 20
    assert slow_results[0].state is ProcessState.EXITED
    assert slow_results[0].cursor.stdout_offset == 3
    assert latest.state is ProcessState.EXITED
    assert latest.cursor.stdout_offset == 20
    assert latest.output_truncated is True
    assert latest.output_decode_replaced is True
