"""Tests for remote execution relay server."""

from __future__ import annotations

import time

import pytest

from reuleauxcoder.extensions.remote_exec.errors import (
    PeerNotFoundError,
    RegisterRejectedError,
    RemoteTimeoutError,
    RemoteExecError,
)
from reuleauxcoder.extensions.remote_exec.protocol import (
    ExecToolRequest,
    ExecToolResult,
    Heartbeat,
    RegisterRequest,
    RegisterResponse,
    RelayEnvelope,
    TerminalCapabilities,
    WorkspaceRequest,
)
from reuleauxcoder.extensions.remote_exec.server import RelayServer


class TestRelayServerLifecycle:
    def test_start_stop(self) -> None:
        srv = RelayServer()
        srv.start()
        assert srv._loop is not None
        srv.stop()
        assert srv._loop is None

    def test_stop_idempotent(self) -> None:
        srv = RelayServer()
        srv.stop()
        srv.stop()


class TestRegistration:
    def test_register_success(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            bt = srv.issue_bootstrap_token(ttl_sec=60)
            req = RegisterRequest(bootstrap_token=bt, cwd="/tmp")
            resp = srv._on_register(req)
            assert isinstance(resp, RegisterResponse)
            assert resp.peer_id
            assert resp.peer_token.startswith("pt_")
        finally:
            srv.stop()

    def test_register_rejected_bad_token(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            req = RegisterRequest(bootstrap_token="bt_invalid", cwd="/tmp")
            with pytest.raises(RegisterRejectedError):
                srv._on_register(req)
        finally:
            srv.stop()

    def test_register_rejected_used_token(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            bt = srv.issue_bootstrap_token(ttl_sec=60)
            req = RegisterRequest(bootstrap_token=bt, cwd="/tmp")
            srv._on_register(req)
            with pytest.raises(RegisterRejectedError):
                srv._on_register(req)
        finally:
            srv.stop()

    def test_protocol_negotiation_rejects_unknown_version_without_using_token(
        self,
    ) -> None:
        srv = RelayServer()
        srv.start()
        try:
            token = srv.issue_bootstrap_token(ttl_sec=60)
            with pytest.raises(RegisterRejectedError, match="Unsupported protocol"):
                srv._on_register(
                    RegisterRequest(
                        bootstrap_token=token, cwd="/tmp", protocol_version=99
                    )
                )

            response = srv._on_register(
                RegisterRequest(bootstrap_token=token, cwd="/tmp", protocol_version=2)
            )
            assert response.protocol_version == 2
            assert "chat.control.steering.v1" in response.host_capabilities
        finally:
            srv.stop()

    def test_v2_capabilities_gate_workspace_dispatch(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            response = srv._on_register(
                RegisterRequest(
                    bootstrap_token=srv.issue_bootstrap_token(ttl_sec=60),
                    cwd="/tmp",
                    protocol_version=2,
                    capabilities=["shell"],
                )
            )

            with pytest.raises(RemoteExecError, match="REMOTE_CAPABILITY_UNAVAILABLE"):
                srv.send_workspace_request(
                    response.peer_id,
                    WorkspaceRequest(
                        operation="fs.read_text", args={"path": "README.md"}
                    ),
                )
        finally:
            srv.stop()

    def test_v2_capabilities_gate_process_dispatch_without_workspace_prefix(
        self,
    ) -> None:
        srv = RelayServer()
        srv.start()
        try:
            response = srv._on_register(
                RegisterRequest(
                    bootstrap_token=srv.issue_bootstrap_token(ttl_sec=60),
                    cwd="/tmp",
                    protocol_version=2,
                    capabilities=["workspace.process.start"],
                )
            )

            with pytest.raises(RemoteExecError, match="REMOTE_CAPABILITY_UNAVAILABLE"):
                srv.send_workspace_request(
                    response.peer_id,
                    WorkspaceRequest(
                        operation="process.start",
                        args={"command": "echo", "process_id": "p"},
                    ),
                )
        finally:
            srv.stop()

    def test_peer_token_uses_configured_ttl(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "reuleauxcoder.extensions.remote_exec.auth.time.time", lambda: 1000.0
        )
        srv = RelayServer(peer_token_ttl_sec=42)
        srv.start()
        try:
            response = srv._on_register(
                RegisterRequest(
                    bootstrap_token=srv.issue_bootstrap_token(ttl_sec=60),
                    cwd="/tmp",
                )
            )

            entry = srv.token_manager._peers[response.peer_token]
            assert entry.expires_at == 1042.0
        finally:
            srv.stop()

    def test_online_peer_token_refresh_uses_sliding_lease(self, monkeypatch) -> None:
        now = [1000.0]
        monkeypatch.setattr(
            "reuleauxcoder.extensions.remote_exec.auth.time.time", lambda: now[0]
        )
        srv = RelayServer(peer_token_ttl_sec=10, heartbeat_timeout_sec=5)
        srv.start()
        try:
            response = srv._on_register(
                RegisterRequest(
                    bootstrap_token=srv.issue_bootstrap_token(ttl_sec=60),
                    cwd="/tmp",
                )
            )
            now[0] = 1014.0

            assert srv.refresh_peer_token(response.peer_token) == response.peer_id
            now[0] = 1023.0
            assert (
                srv.token_manager.verify_peer_token(response.peer_token)
                == response.peer_id
            )
        finally:
            srv.stop()


class TestHeartbeat:
    def test_heartbeat_updates_peer(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            bt = srv.issue_bootstrap_token(ttl_sec=60)
            req = RegisterRequest(bootstrap_token=bt, cwd="/tmp")
            resp = srv._on_register(req)

            before = srv.registry.get(resp.peer_id).last_seen_at
            time.sleep(0.02)
            hb = Heartbeat(
                peer_token=resp.peer_token,
                terminal=TerminalCapabilities(width=123),
            )
            env = RelayEnvelope(
                type="heartbeat",
                peer_id=resp.peer_id,
                payload=hb.to_dict(),
            )
            srv.handle_inbound(resp.peer_id, env)
            time.sleep(0.05)
            after = srv.registry.get(resp.peer_id).last_seen_at
            assert after > before
            assert srv.registry.get(resp.peer_id).meta["terminal"]["width"] == 123
        finally:
            srv.stop()


class TestExecRequest:
    def test_exec_peer_not_found(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            with pytest.raises(PeerNotFoundError):
                srv.send_exec_request(
                    "no-such-peer", ExecToolRequest(tool_name="shell")
                )
        finally:
            srv.stop()

    def test_exec_timeout_when_no_response(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            bt = srv.issue_bootstrap_token(ttl_sec=60)
            req = RegisterRequest(bootstrap_token=bt, cwd="/tmp")
            resp = srv._on_register(req)

            with pytest.raises(RemoteTimeoutError):
                srv.send_exec_request(
                    resp.peer_id,
                    ExecToolRequest(tool_name="shell"),
                    timeout_sec=0,
                )
        finally:
            srv.stop()

    def test_exec_request_response_correlation(self) -> None:
        srv = RelayServer()
        received: list[tuple[str, RelayEnvelope]] = []

        def capture(peer_id: str, env: RelayEnvelope) -> None:
            received.append((peer_id, env))

        srv._send_fn = capture
        srv.start()
        try:
            bt = srv.issue_bootstrap_token(ttl_sec=60)
            req = RegisterRequest(bootstrap_token=bt, cwd="/tmp")
            resp = srv._on_register(req)

            # send exec request in background; we will manually inject response
            import threading

            result_holder = {}

            def send():
                try:
                    r = srv.send_exec_request(
                        resp.peer_id,
                        ExecToolRequest(tool_name="shell", args={"command": "ls"}),
                        timeout_sec=1,
                    )
                    result_holder["result"] = r
                except Exception as e:
                    result_holder["error"] = e

            t = threading.Thread(target=send)
            t.start()
            time.sleep(0.1)

            # inject response
            assert len(received) == 1
            req_id = received[0][1].request_id
            result_env = RelayEnvelope(
                type="tool_result",
                request_id=req_id,
                peer_id=resp.peer_id,
                payload=ExecToolResult(ok=True, result="hello").to_dict(),
            )
            srv.handle_inbound(resp.peer_id, result_env)
            t.join(timeout=2)

            assert "result" in result_holder
            assert result_holder["result"].ok is True
            assert result_holder["result"].result == "hello"
        finally:
            srv.stop()

    def test_response_from_wrong_peer_cannot_claim_request(self) -> None:
        srv = RelayServer()
        received: list[tuple[str, RelayEnvelope]] = []
        srv._send_fn = lambda peer_id, env: received.append((peer_id, env))
        srv.start()
        try:
            first = srv._on_register(
                RegisterRequest(
                    bootstrap_token=srv.issue_bootstrap_token(ttl_sec=60),
                    cwd="/first",
                )
            )
            second = srv._on_register(
                RegisterRequest(
                    bootstrap_token=srv.issue_bootstrap_token(ttl_sec=60),
                    cwd="/second",
                )
            )

            import threading

            holder: dict[str, object] = {}
            thread = threading.Thread(
                target=lambda: holder.setdefault(
                    "result",
                    srv.send_exec_request(
                        first.peer_id,
                        ExecToolRequest(tool_name="shell", args={"command": "pwd"}),
                        timeout_sec=2,
                    ),
                )
            )
            thread.start()
            deadline = time.time() + 1
            while not received and time.time() < deadline:
                time.sleep(0.01)
            request_id = received[0][1].request_id

            srv.handle_inbound(
                second.peer_id,
                RelayEnvelope(
                    type="tool_result",
                    request_id=request_id,
                    peer_id=second.peer_id,
                    payload=ExecToolResult(ok=True, result="spoofed").to_dict(),
                ),
            )
            time.sleep(0.05)
            assert thread.is_alive()

            srv.handle_inbound(
                first.peer_id,
                RelayEnvelope(
                    type="tool_result",
                    request_id=request_id,
                    peer_id=first.peer_id,
                    payload=ExecToolResult(ok=True, result="owned").to_dict(),
                ),
            )
            thread.join(timeout=2)

            assert holder["result"].result == "owned"
        finally:
            srv.stop()


class TestCleanup:
    def test_cleanup_offline_peer(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            result = srv.request_cleanup("no-such-peer")
            assert result.ok is False
        finally:
            srv.stop()
