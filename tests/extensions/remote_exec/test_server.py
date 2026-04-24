"""Tests for remote execution relay server."""

from __future__ import annotations

import time

import pytest

from reuleauxcoder.extensions.remote_exec.errors import (
    PeerNotFoundError,
    RegisterRejectedError,
    RemoteTimeoutError,
)
from reuleauxcoder.extensions.remote_exec.protocol import (
    ExecToolRequest,
    ExecToolResult,
    Heartbeat,
    RegisterRequest,
    RegisterResponse,
    RelayEnvelope,
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
            hb = Heartbeat(peer_token=resp.peer_token)
            env = RelayEnvelope(
                type="heartbeat",
                peer_id=resp.peer_id,
                payload=hb.to_dict(),
            )
            srv.handle_inbound(resp.peer_id, env)
            time.sleep(0.05)
            after = srv.registry.get(resp.peer_id).last_seen_at
            assert after > before
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


class TestCleanup:
    def test_cleanup_offline_peer(self) -> None:
        srv = RelayServer()
        srv.start()
        try:
            result = srv.request_cleanup("no-such-peer")
            assert result.ok is False
        finally:
            srv.stop()
