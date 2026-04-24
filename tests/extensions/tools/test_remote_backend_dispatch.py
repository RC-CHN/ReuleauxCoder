"""Tests that builtin tools dispatch to the remote backend correctly."""

from __future__ import annotations

import pytest

from reuleauxcoder.extensions.remote_exec.backend import RemoteRelayToolBackend
from reuleauxcoder.extensions.remote_exec.errors import (
    PeerDisconnectedError,
    PeerNotFoundError,
)
from reuleauxcoder.extensions.remote_exec.protocol import (
    ExecToolRequest,
    ExecToolResult,
)
from reuleauxcoder.extensions.remote_exec.server import RelayServer
from reuleauxcoder.extensions.tools.builtin.edit import EditFileTool
from reuleauxcoder.extensions.tools.builtin.glob import GlobTool
from reuleauxcoder.extensions.tools.builtin.grep import GrepTool
from reuleauxcoder.extensions.tools.builtin.read import ReadFileTool
from reuleauxcoder.extensions.tools.builtin.shell import ShellTool
from reuleauxcoder.extensions.tools.builtin.write import WriteFileTool


class TestRemoteBackendDispatch:
    def test_shell_no_peer(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            backend = RemoteRelayToolBackend(relay_server=srv)
            tool = ShellTool(backend=backend)
            result = tool.execute(command="ls")
            assert "no remote peer" in result.lower()
        finally:
            srv.stop()

    def test_read_file_no_peer(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            backend = RemoteRelayToolBackend(relay_server=srv)
            tool = ReadFileTool(backend=backend)
            result = tool.execute(file_path="/tmp/foo")
            assert "no remote peer" in result.lower()

            result = tool.execute(file_path="/tmp/foo", offset=0)
            assert "positive integer" in result.lower()

            result = tool.execute(file_path="/tmp/foo", limit=0)
            assert "positive integer" in result.lower()

            result = tool.execute(file_path="/tmp/foo", override="yes")
            assert "boolean" in result.lower()
        finally:
            srv.stop()

    def test_write_file_no_peer(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            backend = RemoteRelayToolBackend(relay_server=srv)
            tool = WriteFileTool(backend=backend)
            result = tool.execute(file_path="/tmp/foo", content="bar")
            assert "no remote peer" in result.lower()
        finally:
            srv.stop()

    def test_edit_file_no_peer(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            backend = RemoteRelayToolBackend(relay_server=srv)
            tool = EditFileTool(backend=backend)
            result = tool.execute(
                file_path="/tmp/foo",
                old_string="a",
                new_string="b",
            )
            assert "no remote peer" in result.lower()

            result = tool.execute(
                file_path="/tmp/foo",
                old_string="same",
                new_string="same",
            )
            assert "must differ" in result.lower()
        finally:
            srv.stop()

    def test_glob_no_peer(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            backend = RemoteRelayToolBackend(relay_server=srv)
            tool = GlobTool(backend=backend)
            result = tool.execute(pattern="*.py")
            assert "no remote peer" in result.lower()

            result = tool.execute(pattern="*.py", path="")
            assert "non-empty string" in result.lower()
        finally:
            srv.stop()

    def test_grep_no_peer(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            backend = RemoteRelayToolBackend(relay_server=srv)
            tool = GrepTool(backend=backend)
            result = tool.execute(pattern="foo")
            assert "no remote peer" in result.lower()

            result = tool.execute(pattern="foo", path="")
            assert "non-empty string" in result.lower()

            result = tool.execute(pattern="foo", include=123)
            assert "must be a string" in result.lower()
        finally:
            srv.stop()

    def test_shell_invalid_args(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            backend = RemoteRelayToolBackend(relay_server=srv)
            tool = ShellTool(backend=backend)
            result = tool.execute(command="")
            assert "non-empty string" in result.lower()

            result = tool.execute(command="echo ok", timeout=0)
            assert "positive integer" in result.lower()
        finally:
            srv.stop()

    def test_remote_backend_exec_forwards_to_server(self) -> None:
        """Simulate a full round-trip: register peer, inject response, verify result."""
        srv = RelayServer()
        received: list[tuple[str, object]] = []

        def mock_send(peer_id: str, envelope: object) -> None:
            received.append((peer_id, envelope))

        srv._send_fn = mock_send
        srv.start()
        try:
            # register peer
            bt = srv.issue_bootstrap_token(ttl_sec=60)
            from reuleauxcoder.extensions.remote_exec.protocol import RegisterRequest

            resp = srv._on_register(RegisterRequest(bootstrap_token=bt, cwd="/tmp"))

            backend = RemoteRelayToolBackend(relay_server=srv)
            backend.context.peer_id = resp.peer_id
            tool = ShellTool(backend=backend)

            import threading

            result_holder = {}

            def run_tool():
                result_holder["result"] = tool.execute(command="echo hello")

            t = threading.Thread(target=run_tool)
            t.start()
            import time

            time.sleep(0.1)

            # inject tool result
            assert len(received) == 1
            req_id = received[0][1].request_id
            from reuleauxcoder.extensions.remote_exec.protocol import RelayEnvelope

            env = RelayEnvelope(
                type="tool_result",
                request_id=req_id,
                peer_id=resp.peer_id,
                payload=ExecToolResult(ok=True, result="hello").to_dict(),
            )
            srv.handle_inbound(resp.peer_id, env)
            t.join(timeout=2)

            assert result_holder["result"] == "hello"
        finally:
            srv.stop()

    def test_relay_server_fails_inflight_requests_on_disconnect(self) -> None:
        srv = RelayServer()
        received: list[tuple[str, object]] = []

        def mock_send(peer_id: str, envelope: object) -> None:
            received.append((peer_id, envelope))

        srv._send_fn = mock_send
        srv.start()
        try:
            bt = srv.issue_bootstrap_token(ttl_sec=60)
            from reuleauxcoder.extensions.remote_exec.protocol import RegisterRequest

            resp = srv._on_register(RegisterRequest(bootstrap_token=bt, cwd="/tmp"))

            import threading
            import time
            from reuleauxcoder.extensions.remote_exec.protocol import RelayEnvelope

            result_holder: dict[str, object] = {}

            def run_request() -> None:
                try:
                    srv.send_exec_request(
                        resp.peer_id,
                        ExecToolRequest(
                            tool_name="shell", args={"command": "echo hello"}
                        ),
                        timeout_sec=5,
                    )
                except Exception as exc:
                    result_holder["error"] = exc

            t = threading.Thread(target=run_request)
            t.start()
            time.sleep(0.1)

            assert len(received) == 1
            srv.handle_inbound(
                resp.peer_id,
                RelayEnvelope(
                    type="disconnect",
                    peer_id=resp.peer_id,
                    payload={"reason": "peer_initiated"},
                ),
            )
            t.join(timeout=2)

            assert isinstance(result_holder.get("error"), PeerDisconnectedError)
        finally:
            srv.stop()
