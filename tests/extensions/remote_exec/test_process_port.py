from __future__ import annotations

from collections import deque

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
    assert start_request.args["command"] == command
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
