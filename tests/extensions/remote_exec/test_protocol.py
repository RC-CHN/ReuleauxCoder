"""Tests for remote execution protocol message models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reuleauxcoder.extensions.remote_exec.protocol import (
    ChatControlRequest,
    ChatControlResponse,
    CleanupRequest,
    CleanupResult,
    DisconnectNotice,
    DisconnectRequest,
    ErrorMessage,
    ExecToolRequest,
    ExecToolResult,
    Heartbeat,
    InteractionReplyRequest,
    InteractionReplyResponse,
    RegisterRejected,
    RegisterRequest,
    RegisterResponse,
    TerminalCapabilities,
    TokenRefreshRequest,
    TokenRefreshResponse,
    RelayEnvelope,
    ToolStreamChunk,
    WorkspaceRequest,
    WorkspaceResult,
    REMOTE_PROTOCOL_VERSION,
)


_CONTRACT_FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "protocol"
    / "fixtures"
    / "remote_contract.json"
)

_FIXTURE_MODELS = {
    model.__name__: model
    for model in (
        RegisterRequest,
        TokenRefreshRequest,
        DisconnectRequest,
        InteractionReplyRequest,
        RelayEnvelope,
        WorkspaceRequest,
        WorkspaceResult,
        ToolStreamChunk,
        ExecToolRequest,
    )
}


@pytest.mark.parametrize(
    "case", json.loads(_CONTRACT_FIXTURES.read_text(encoding="utf-8"))
)
def test_shared_contract_fixture_roundtrips_through_python(case: dict) -> None:
    model = _FIXTURE_MODELS[case["model"]]

    restored = model.from_dict(case["payload"])

    assert restored.to_dict() == case["payload"]


class TestRelayEnvelope:
    def test_roundtrip(self) -> None:
        env = RelayEnvelope(
            type="exec_tool",
            request_id="req-123",
            peer_id="peer-456",
            payload={"tool_name": "shell", "args": {"command": "ls"}},
        )
        d = env.to_dict()
        restored = RelayEnvelope.from_dict(d)
        assert restored.type == "exec_tool"
        assert restored.request_id == "req-123"
        assert restored.peer_id == "peer-456"
        assert restored.payload["tool_name"] == "shell"


class TestRegisterRequest:
    def test_roundtrip(self) -> None:
        req = RegisterRequest(
            bootstrap_token="bt_abc",
            cwd="/tmp",
            workspace_root="/workspace",
            capabilities=["shell", "read_file"],
            protocol_version=REMOTE_PROTOCOL_VERSION,
            terminal=TerminalCapabilities(
                width=132,
                color_level="256",
                unicode=False,
                interactive=True,
            ),
        )
        d = req.to_dict()
        restored = RegisterRequest.from_dict(d)
        assert restored.bootstrap_token == "bt_abc"
        assert restored.cwd == "/tmp"
        assert restored.workspace_root == "/workspace"
        assert restored.capabilities == ["shell", "read_file"]
        assert restored.protocol_version == REMOTE_PROTOCOL_VERSION
        assert restored.terminal == req.terminal

    def test_terminal_capabilities_are_safely_normalized(self) -> None:
        terminal = TerminalCapabilities.from_dict(
            {"width": 9999, "color_level": "invented"}
        )

        assert terminal.width == 500
        assert terminal.color_level == "none"


class TestRegisterResponse:
    def test_roundtrip(self) -> None:
        resp = RegisterResponse(
            peer_id="p1",
            peer_token="pt_xyz",
            heartbeat_interval_sec=15,
            protocol_version=REMOTE_PROTOCOL_VERSION,
        )
        d = resp.to_dict()
        restored = RegisterResponse.from_dict(d)
        assert restored.peer_id == "p1"
        assert restored.peer_token == "pt_xyz"
        assert restored.heartbeat_interval_sec == 15
        assert restored.protocol_version == REMOTE_PROTOCOL_VERSION
        assert restored.host_capabilities == []

    def test_host_capabilities_are_optional_and_roundtrip(self) -> None:
        response = RegisterResponse(
            peer_id="p1",
            peer_token="pt_xyz",
            host_capabilities=["chat.control.steering.v1"],
        )

        assert RegisterResponse.from_dict(response.to_dict()) == response
        legacy = response.to_dict()
        legacy.pop("host_capabilities")
        assert RegisterResponse.from_dict(legacy).host_capabilities == []


def test_chat_control_models_roundtrip_and_reject_empty_identity() -> None:
    request = ChatControlRequest(
        peer_token="pt_xyz",
        chat_id="chat-1",
        control_id="control-1",
        action="admit_steering",
        content="change direction",
    )
    response = ChatControlResponse(
        ok=True,
        control_id="control-1",
        outcome="admitted",
        steering_id="steer-1",
    )

    assert ChatControlRequest.from_dict(request.to_dict()) == request
    assert ChatControlResponse.from_dict(response.to_dict()) == response
    with pytest.raises(ValueError, match="control_id"):
        ChatControlRequest.from_dict(
            {
                "peer_token": "pt_xyz",
                "chat_id": "chat-1",
                "control_id": "",
                "action": "interrupt_intent",
            }
        )


class TestRegisterRejected:
    def test_roundtrip(self) -> None:
        rej = RegisterRejected(reason="bad token")
        d = rej.to_dict()
        restored = RegisterRejected.from_dict(d)
        assert restored.reason == "bad token"


class TestHeartbeat:
    def test_roundtrip(self) -> None:
        hb = Heartbeat(
            peer_token="pt_tok",
            ts=1234.5,
            terminal=TerminalCapabilities(width=101, color_level="256"),
        )
        d = hb.to_dict()
        restored = Heartbeat.from_dict(d)
        assert restored.peer_token == "pt_tok"
        assert restored.ts == 1234.5
        assert restored.terminal == hb.terminal


def test_token_refresh_models_roundtrip() -> None:
    request = TokenRefreshRequest(peer_token="pt_demo")
    response = TokenRefreshResponse(ok=True, peer_token="pt_demo", expires_in_sec=60)

    assert TokenRefreshRequest.from_dict(request.to_dict()) == request
    assert TokenRefreshResponse.from_dict(response.to_dict()) == response


class TestExecToolRequest:
    def test_roundtrip(self) -> None:
        req = ExecToolRequest(
            tool_name="shell",
            args={"command": "ls"},
            cwd="/tmp",
            timeout_sec=60,
        )
        d = req.to_dict()
        restored = ExecToolRequest.from_dict(d)
        assert restored.tool_name == "shell"
        assert restored.args == {"command": "ls"}
        assert restored.cwd == "/tmp"
        assert restored.timeout_sec == 60

    def test_defaults(self) -> None:
        req = ExecToolRequest(tool_name="read_file")
        assert req.args == {}
        assert req.cwd is None
        assert req.timeout_sec == 30


class TestExecToolResult:
    def test_roundtrip(self) -> None:
        res = ExecToolResult(
            ok=False,
            result="",
            error_code="PEER_DISCONNECTED",
            error_message="peer gone",
            meta={"exit_code": 1},
        )
        d = res.to_dict()
        restored = ExecToolResult.from_dict(d)
        assert restored.ok is False
        assert restored.error_code == "PEER_DISCONNECTED"
        assert restored.meta["exit_code"] == 1


class TestWorkspaceProtocol:
    def test_request_roundtrip(self) -> None:
        request = WorkspaceRequest(
            operation="fs.replace_exact_atomic",
            args={"path": "src/app.py", "old": "a", "new": "b"},
            cwd="/workspace",
            timeout_sec=12,
        )

        restored = WorkspaceRequest.from_dict(request.to_dict())

        assert restored == request

    def test_result_roundtrip(self) -> None:
        result = WorkspaceResult(
            ok=False,
            error_code="not_unique",
            error_message="old text occurs twice",
        )

        restored = WorkspaceResult.from_dict(result.to_dict())

        assert restored == result


class TestToolStreamChunk:
    def test_roundtrip(self) -> None:
        chunk = ToolStreamChunk(chunk_type="stdout", data="hello", meta={"seq": 1})
        d = chunk.to_dict()
        restored = ToolStreamChunk.from_dict(d)
        assert restored.chunk_type == "stdout"
        assert restored.data == "hello"


class TestInteractionReply:
    def test_roundtrip(self) -> None:
        request = InteractionReplyRequest(
            peer_token="pt_1",
            chat_id="chat_1",
            request_id="interaction_1",
            value=True,
        )

        assert InteractionReplyRequest.from_dict(request.to_dict()) == request
        response = InteractionReplyResponse(ok=True)
        assert InteractionReplyResponse.from_dict(response.to_dict()) == response


class TestDisconnectNotice:
    def test_roundtrip(self) -> None:
        n = DisconnectNotice(reason="shutdown")
        d = n.to_dict()
        restored = DisconnectNotice.from_dict(d)
        assert restored.reason == "shutdown"

    def test_default_reason(self) -> None:
        n = DisconnectNotice.from_dict({})
        assert n.reason == "peer_initiated"


class TestCleanupRequest:
    def test_roundtrip(self) -> None:
        req = CleanupRequest()
        d = req.to_dict()
        restored = CleanupRequest.from_dict(d)
        assert isinstance(restored, CleanupRequest)


class TestCleanupResult:
    def test_roundtrip(self) -> None:
        res = CleanupResult(ok=True, removed_items=["/tmp/a"], error_message=None)
        d = res.to_dict()
        restored = CleanupResult.from_dict(d)
        assert restored.ok is True
        assert restored.removed_items == ["/tmp/a"]


class TestErrorMessage:
    def test_roundtrip(self) -> None:
        err = ErrorMessage(code="AUTH_FAILED", message="bad token")
        d = err.to_dict()
        restored = ErrorMessage.from_dict(d)
        assert restored.code == "AUTH_FAILED"
        assert restored.message == "bad token"
