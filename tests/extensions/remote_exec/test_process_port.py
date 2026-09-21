from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
import queue
import threading
import time

import pytest

from reuleauxcoder.domain.process import (
    ProcessCursor,
    ProcessOperationUnconfirmed,
    ProcessState,
)
from reuleauxcoder.extensions.remote_exec.backend import (
    RemoteProcessPort,
    RemoteRelayToolBackend,
)
from reuleauxcoder.extensions.remote_exec.errors import RemoteTimeoutError
from reuleauxcoder.extensions.remote_exec.peer_registry import PeerRegistry
from reuleauxcoder.extensions.remote_exec.protocol import RelayEnvelope, WorkspaceResult
from reuleauxcoder.extensions.remote_exec.server import RelayServer
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


def test_cancel_remote_long_poll_detaches_wait_without_losing_output():
    relay = RelayServer()
    requests = queue.Queue()
    relay._send_fn = lambda _peer, envelope: requests.put(envelope)
    relay.start()
    peer_id = relay.registry.register(
        {
            "protocol_version": 2,
            "capabilities": [
                "process.start",
                "process.poll",
                "process.poll.concurrent",
            ],
        }
    )
    port = RemoteProcessPort(
        RemoteRelayToolBackend(
            relay, context=ExecutionContext(peer_id=peer_id, cwd="/workspace")
        )
    )
    cancellation = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)

    def respond(request, data):
        relay.handle_inbound(
            peer_id,
            RelayEnvelope(
                type="workspace_result",
                request_id=request.request_id,
                peer_id=peer_id,
                payload=WorkspaceResult(ok=True, data=data).to_dict(),
            ),
        )

    try:
        started = executor.submit(
            port.start, "server", cwd="/workspace", runtime_timeout=0
        )
        respond(requests.get(timeout=2), {"process_id": "process"})
        handle = started.result(timeout=2)
        waiting = executor.submit(
            port.poll, handle.session_id, wait_ms=30_000, cancellation=cancellation
        )
        abandoned = requests.get(timeout=2)
        assert abandoned.payload["args"]["wait_ms"] == 30_000
        with pytest.raises(queue.Empty):
            requests.get(timeout=0.15)
        cancellation.set()
        snapshot = waiting.result(timeout=2)
        assert snapshot.state is ProcessState.RUNNING
        assert snapshot.termination_reason is None
        assert snapshot.cursor == ProcessCursor()
        deadline = time.monotonic() + 2
        while relay._pending:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        data = {"stdout": "later", "stdout_offset": 5, "state": "running"}
        respond(abandoned, data)
        cancellation.clear()
        waiting = executor.submit(
            port.poll,
            handle.session_id,
            cursor=snapshot.cursor,
            wait_ms=500,
            cancellation=cancellation,
        )
        request = requests.get(timeout=2)
        assert request.payload["args"]["stdout_offset"] == 0
        respond(request, data)
        snapshot = waiting.result(timeout=2)
        assert snapshot.stdout == "later"
        waiting = executor.submit(port.poll, handle.session_id, cursor=snapshot.cursor)
        request = requests.get(timeout=2)
        assert request.payload["args"]["stdout_offset"] == 5
        respond(request, {"stdout_offset": 5, "state": "running"})
        assert waiting.result(timeout=2).stdout == ""
    finally:
        cancellation.set()
        relay.stop()
        executor.shutdown(wait=True)


def test_remote_background_start_does_not_send_an_expired_deadline():
    port, relay = _port([WorkspaceResult(ok=True, data={"process_id": "background"})])
    port.start("server", cwd="/workspace", runtime_timeout=0)
    args = relay.requests[0][0].args
    assert args["runtime_timeout_ms"] == 0
    assert args["deadline_unix_ms"] == 0


def test_old_peer_keeps_short_requests_during_a_long_wait():
    port, relay = _port(
        [
            WorkspaceResult(ok=True, data={"process_id": "legacy"}),
            WorkspaceResult(ok=True, data={"state": "running"}),
            WorkspaceResult(ok=True, data={"state": "running"}),
            WorkspaceResult(
                ok=True,
                data={"state": "running", "stdout": "ready", "stdout_offset": 5},
            ),
        ]
    )
    handle = port.start("server", cwd="/workspace", runtime_timeout=0)
    result = port.poll(handle.session_id, wait_ms=1_000)
    assert result.stdout == "ready"
    assert [req.args["wait_ms"] for req, _ in relay.requests[1:]] == [50, 50, 50]


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

    assert report.unknown == 0
    assert report.terminated == 1
    assert relay.requests[-1][0].operation == "process.terminate"


def test_shutdown_reports_remote_termination_that_remains_unconfirmed() -> None:
    port, _relay = _port(
        [
            WorkspaceResult(
                ok=True,
                data={"process_id": "unconfirmed-shutdown", "reused": False},
            ),
            RemoteTimeoutError(2),
        ]
    )
    port.start("long-running", cwd="/workspace", runtime_timeout=60)

    report = port.shutdown()

    assert report.terminated == 1
    assert report.unknown == 1


def test_remote_control_transport_failures_remain_unconfirmed() -> None:
    port, _relay = _port(
        [
            WorkspaceResult(
                ok=True,
                data={"process_id": "ambiguous-control", "reused": False},
            ),
            RemoteTimeoutError(2),
            RemoteTimeoutError(2),
        ]
    )
    handle = port.start("long-running", cwd="/workspace", runtime_timeout=60)

    with pytest.raises(
        ProcessOperationUnconfirmed,
        match="interrupt",
    ) as interrupted:
        port.interrupt(handle.session_id)
    assert interrupted.value.snapshot is not None
    assert interrupted.value.snapshot.state is ProcessState.UNKNOWN
    assert port._lookup(handle.session_id).state is ProcessState.UNKNOWN

    with pytest.raises(
        ProcessOperationUnconfirmed,
        match="termination",
    ) as terminated:
        port.terminate(handle.session_id)
    assert terminated.value.snapshot is not None
    assert terminated.value.snapshot.state is ProcessState.UNKNOWN
    assert port._lookup(handle.session_id).state is ProcessState.UNKNOWN


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
