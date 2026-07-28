"""Tests for the HTTP transport adapter around the remote relay host."""

from __future__ import annotations

import json
import os
from hashlib import sha256
from http import HTTPStatus
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from urllib import request
from urllib.error import HTTPError

import pytest
from reuleauxcoder.domain.process import ProcessCursor, ProcessState
from reuleauxcoder.domain.process_manager import ProcessManager
from reuleauxcoder.extensions.remote_exec.http_service import RemoteRelayHTTPService
from reuleauxcoder.extensions.remote_exec.protocol import (
    ChatResponse,
    CleanupResult,
    ExecToolResult,
    RelayEnvelope,
    WorkspaceResult,
)
from reuleauxcoder.extensions.remote_exec.server import RelayServer
from reuleauxcoder.extensions.tools.builtin.edit import EditFileTool
from reuleauxcoder.extensions.tools.builtin.glob import GlobTool
from reuleauxcoder.extensions.tools.builtin.grep import GrepTool
from reuleauxcoder.extensions.tools.builtin.list_file import ListFileTool
from reuleauxcoder.extensions.tools.builtin.read import ReadFileTool
from reuleauxcoder.extensions.tools.builtin.shell import ShellTool
from reuleauxcoder.extensions.tools.builtin.write import WriteFileTool
from reuleauxcoder.extensions.remote_exec.backend import RemoteRelayToolBackend
from reuleauxcoder.extensions.tools.backend import ExecutionContext, LocalToolBackend
from reuleauxcoder.interfaces.entrypoint.runner import (
    _default_create_remote_artifact_provider,
)
from reuleauxcoder.interfaces.events import UIEventBus


_URLOPEN = request.build_opener(request.ProxyHandler({})).open

_GO_AVAILABLE = shutil.which("go") is not None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_request(
    method: str, url: str, payload: dict | None = None
) -> tuple[int, dict]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    with _URLOPEN(req, timeout=5) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, json.loads(body) if body else {}


def _text_request(url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    req = request.Request(url, headers=headers or {}, method="GET")
    with _URLOPEN(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _build_go_agent_binary() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    agent_dir = repo_root / "reuleauxcoder-agent"
    target_dir = Path(tempfile.mkdtemp(prefix="rc-go-agent-bin-"))
    binary_path = target_dir / "reuleauxcoder-agent"
    subprocess.run(
        ["go", "build", "-o", str(binary_path), "./cmd/reuleauxcoder-agent"],
        cwd=agent_dir,
        check=True,
        timeout=120,
    )
    return binary_path


def _cleanup_provider_build_dir(provider: object) -> None:
    build_dir = getattr(provider, "_build_dir", None)
    if isinstance(build_dir, Path):
        subprocess.run(["rm", "-rf", str(build_dir)], check=False, timeout=30)


class TestRemoteRelayHTTPService:
    def test_peer_poll_waits_for_server_side_envelope(self) -> None:
        relay = RelayServer()
        service = RemoteRelayHTTPService(relay_server=relay, bind="127.0.0.1:0")
        holder: dict[str, object] = {}

        thread = threading.Thread(
            target=lambda: holder.setdefault(
                "envelope", service._next_envelope("peer-1", timeout_sec=1)
            )
        )
        thread.start()
        time.sleep(0.05)
        assert thread.is_alive()

        expected = RelayEnvelope(type="cleanup", request_id="request-1")
        service._enqueue_outbound("peer-1", expected)
        thread.join(timeout=1)

        assert holder["envelope"] is expected

    def test_bootstrap_and_artifact_endpoints(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            artifact_provider=lambda os_name, arch, name: (
                (
                    b"peer-binary",
                    "application/octet-stream",
                )
                if (os_name, arch, name) == ("linux", "amd64", "rcoder-peer")
                else None
            ),
            bootstrap_access_secret="top-secret",
            bootstrap_token_ttl_sec=60,
        )
        service.start()
        try:
            try:
                _text_request(f"{service.base_url}/remote/bootstrap.sh")
                raise AssertionError("bootstrap should require secret")
            except HTTPError as exc:
                assert exc.code == 403
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "invalid_bootstrap_secret"

            status, script = _text_request(
                f"{service.base_url}/remote/bootstrap.sh",
                headers={"X-RC-Bootstrap-Secret": "top-secret"},
            )
            assert status == 200
            assert "rcoder-peer" in script
            assert service.base_url in script
            assert "/remote/artifacts/{os}/{arch}/rcoder-peer" in script
            assert "sha256sum" in script
            assert "shasum -a 256" in script
            assert "MAX_ARTIFACT_BYTES" in script
            assert "did not include a SHA-256 checksum" in script

            with _URLOPEN(
                f"{service.base_url}/remote/artifacts/linux/amd64/rcoder-peer",
                timeout=5,
            ) as resp:
                assert resp.status == 200
                assert (
                    resp.headers["X-ReuleauxCoder-SHA256"]
                    == sha256(b"peer-binary").hexdigest()
                )
                assert resp.read() == b"peer-binary"
        finally:
            service.stop()
            relay.stop()

    def test_artifact_endpoint_rejects_binary_over_size_budget(self) -> None:
        relay = RelayServer()
        relay.start()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{_free_port()}",
            artifact_provider=lambda _os, _arch, _name: (
                b"oversized",
                "application/octet-stream",
            ),
        )
        service.start()
        try:
            with patch(
                "reuleauxcoder.extensions.remote_exec.artifacts.MAX_PEER_ARTIFACT_BYTES",
                4,
            ):
                with pytest.raises(HTTPError) as raised:
                    _URLOPEN(
                        f"{service.base_url}/remote/artifacts/linux/amd64/rcoder-peer",
                        timeout=5,
                    )
            assert raised.value.code == 413
            body = json.loads(raised.value.read().decode("utf-8"))
            assert body["error"] == "artifact_too_large"
        finally:
            service.stop()
            relay.stop()

    @pytest.mark.skipif(
        os.name == "nt"
        or shutil.which("sh") is None
        or shutil.which("curl") is None
        or (shutil.which("sha256sum") is None and shutil.which("shasum") is None),
        reason="requires POSIX shell, curl and a SHA-256 utility",
    )
    def test_bootstrap_script_downloads_verifies_and_executes_peer(self) -> None:
        peer_script = b"#!/bin/sh\nprintf 'verified-peer\\n'\n"
        relay = RelayServer()
        relay.start()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{_free_port()}",
            artifact_provider=lambda _os, _arch, _name: (
                peer_script,
                "application/octet-stream",
            ),
            bootstrap_access_secret="top-secret",
        )
        service.start()
        try:
            _, script = _text_request(
                f"{service.base_url}/remote/bootstrap.sh",
                headers={"X-RC-Bootstrap-Secret": "top-secret"},
            )
            env = {
                key: value
                for key, value in os.environ.items()
                if key.lower() not in {"all_proxy", "http_proxy", "https_proxy"}
            }
            env["NO_PROXY"] = "127.0.0.1,localhost"

            completed = subprocess.run(
                ["sh"],
                input=script,
                text=True,
                capture_output=True,
                env=env,
                timeout=15,
                check=False,
            )

            assert completed.returncode == 0, completed.stderr
            assert "verified-peer" in completed.stdout

            with patch(
                "reuleauxcoder.extensions.remote_exec.http_service.peer_artifact_sha256",
                return_value="0" * 64,
            ):
                rejected = subprocess.run(
                    ["sh"],
                    input=script,
                    text=True,
                    capture_output=True,
                    env=env,
                    timeout=15,
                    check=False,
                )
            assert rejected.returncode != 0
            assert "SHA-256 verification failed" in rejected.stderr
        finally:
            service.stop()
            relay.stop()

    def test_register_poll_result_disconnect_and_cleanup(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(relay_server=relay, bind=f"127.0.0.1:{port}")
        service.start()
        try:
            bootstrap_token = relay.issue_bootstrap_token(ttl_sec=60)
            status, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": bootstrap_token,
                    "cwd": "/tmp/peer",
                    "workspace_root": "/tmp",
                    "capabilities": ["shell", "read_file"],
                },
            )
            assert status == 200
            assert register_body["type"] == "register_ok"
            payload = register_body["payload"]
            peer_id = payload["peer_id"]
            peer_token = payload["peer_token"]

            status, heartbeat_body = _json_request(
                "POST",
                f"{service.base_url}/remote/heartbeat",
                {
                    "peer_token": peer_token,
                    "ts": time.time(),
                    "terminal": {"width": 111, "color_level": "none"},
                },
            )
            assert status == 200
            assert heartbeat_body["peer_id"] == peer_id
            updated_peer = service.relay_server.registry.get(peer_id)
            assert updated_peer is not None
            assert updated_peer.meta["terminal"]["width"] == 111

            status, poll_body = _json_request(
                "POST",
                f"{service.base_url}/remote/poll",
                {"peer_token": peer_token},
            )
            assert status == 200
            assert poll_body["type"] == "noop"

            result_holder: dict[str, object] = {}

            def run_exec() -> None:
                result_holder["result"] = relay.send_exec_request(
                    peer_id,
                    request=__import__(
                        "reuleauxcoder.extensions.remote_exec.protocol",
                        fromlist=["ExecToolRequest"],
                    ).ExecToolRequest(tool_name="shell", args={"command": "echo hi"}),
                    timeout_sec=2,
                )

            exec_thread = threading.Thread(target=run_exec)
            exec_thread.start()
            time.sleep(0.1)

            status, poll_body = _json_request(
                "POST",
                f"{service.base_url}/remote/poll",
                {"peer_token": peer_token},
            )
            assert status == 200
            assert poll_body["type"] == "exec_tool"
            assert poll_body["payload"]["tool_name"] == "shell"
            req_id = poll_body["request_id"]

            status, result_body = _json_request(
                "POST",
                f"{service.base_url}/remote/result",
                {
                    "peer_token": peer_token,
                    "request_id": req_id,
                    "type": "tool_result",
                    "payload": ExecToolResult(
                        ok=True, result="hello from peer"
                    ).to_dict(),
                },
            )
            assert status == 200
            assert result_body["ok"] is True
            exec_thread.join(timeout=2)
            assert result_holder["result"].result == "hello from peer"

            cleanup_holder: dict[str, object] = {}

            def run_cleanup() -> None:
                cleanup_holder["result"] = relay.request_cleanup(peer_id, timeout_sec=2)

            cleanup_thread = threading.Thread(target=run_cleanup)
            cleanup_thread.start()
            time.sleep(0.1)

            status, poll_body = _json_request(
                "POST",
                f"{service.base_url}/remote/poll",
                {"peer_token": peer_token},
            )
            assert status == 200
            assert poll_body["type"] == "cleanup"
            cleanup_req_id = poll_body["request_id"]

            status, cleanup_body = _json_request(
                "POST",
                f"{service.base_url}/remote/result",
                {
                    "peer_token": peer_token,
                    "request_id": cleanup_req_id,
                    "type": "cleanup_result",
                    "payload": CleanupResult(
                        ok=True, removed_items=["/tmp/rc-peer"]
                    ).to_dict(),
                },
            )
            assert status == 200
            assert cleanup_body["ok"] is True
            cleanup_thread.join(timeout=2)
            assert cleanup_holder["result"].ok is True
            assert cleanup_holder["result"].removed_items == ["/tmp/rc-peer"]

            status, disconnect_body = _json_request(
                "POST",
                f"{service.base_url}/remote/disconnect",
                {"peer_token": peer_token, "reason": "peer_initiated"},
            )
            assert status == 200
            assert disconnect_body["ok"] is True
            assert relay.registry.get(peer_id) is None
        finally:
            service.stop()
            relay.stop()

    def test_all_remote_builtin_tools_dispatch_over_http_contract(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(relay_server=relay, bind=f"127.0.0.1:{port}")
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_id = register_body["payload"]["peer_id"]
            peer_token = register_body["payload"]["peer_token"]

            backend = RemoteRelayToolBackend(relay_server=relay)
            backend.context.peer_id = peer_id
            legacy_forwarded_cases = [
                (
                    ShellTool(backend=backend),
                    {"command": "echo hello"},
                    "shell",
                    "shell-ok",
                ),
            ]

            # Protocol-v1 shell remains as a compatibility fixture. Workspace
            # tools intentionally use Host-owned primitives below and must not
            # be forwarded as Peer product tools.
            for tool, kwargs, expected_name, expected_result in legacy_forwarded_cases:
                holder: dict[str, object] = {}

                def run_tool(current_tool=tool, current_kwargs=kwargs) -> None:
                    holder["result"] = current_tool.execute(**current_kwargs)

                t = threading.Thread(target=run_tool)
                t.start()
                time.sleep(0.1)

                status, poll_body = _json_request(
                    "POST",
                    f"{service.base_url}/remote/poll",
                    {"peer_token": peer_token},
                )
                assert status == 200
                assert poll_body["type"] == "exec_tool"
                assert poll_body["payload"]["tool_name"] == expected_name
                for key, value in kwargs.items():
                    assert poll_body["payload"]["args"][key] == value

                status, result_body = _json_request(
                    "POST",
                    f"{service.base_url}/remote/result",
                    {
                        "peer_token": peer_token,
                        "request_id": poll_body["request_id"],
                        "type": "tool_result",
                        "payload": ExecToolResult(
                            ok=True, result=expected_result
                        ).to_dict(),
                    },
                )
                assert status == 200
                assert result_body["ok"] is True

                t.join(timeout=2)
                assert holder["result"].model_text == expected_result

            def poll_workspace() -> dict:
                time.sleep(0.05)
                status, body = _json_request(
                    "POST",
                    f"{service.base_url}/remote/poll",
                    {"peer_token": peer_token},
                )
                assert status == 200
                assert body["type"] == "workspace_request"
                return body

            def reply_workspace(body: dict, data: dict) -> None:
                status, result_body = _json_request(
                    "POST",
                    f"{service.base_url}/remote/result",
                    {
                        "peer_token": peer_token,
                        "request_id": body["request_id"],
                        "type": "workspace_result",
                        "payload": WorkspaceResult(ok=True, data=data).to_dict(),
                    },
                )
                assert status == 200
                assert result_body["ok"] is True

            holder = {}
            thread = threading.Thread(
                target=lambda: holder.setdefault(
                    "result",
                    ReadFileTool(backend=backend).execute("/tmp/demo.txt"),
                )
            )
            thread.start()
            body = poll_workspace()
            assert body["payload"]["operation"] == "fs.read_text"
            reply_workspace(body, {"content": "read-ok\n"})
            thread.join(timeout=2)
            assert holder["result"].model_text == "1\tread-ok"

            holder = {}
            thread = threading.Thread(
                target=lambda: holder.setdefault(
                    "result",
                    WriteFileTool(backend=backend).execute("/tmp/demo.txt", "hello"),
                )
            )
            thread.start()
            body = poll_workspace()
            assert body["payload"]["operation"] == "fs.write_text_atomic"
            reply_workspace(body, {"old_content": ""})
            thread.join(timeout=2)
            assert holder["result"].model_text.startswith("Wrote 1 lines")

            holder = {}
            thread = threading.Thread(
                target=lambda: holder.setdefault(
                    "result",
                    EditFileTool(backend=backend).execute("/tmp/demo.txt", "a", "b"),
                )
            )
            thread.start()
            body = poll_workspace()
            assert body["payload"]["operation"] == "fs.read_text"
            reply_workspace(body, {"content": "a"})
            body = poll_workspace()
            assert body["payload"]["operation"] == "fs.replace_exact_atomic"
            reply_workspace(body, {"old_content": "a", "new_content": "b"})
            thread.join(timeout=2)
            assert holder["result"].model_text.startswith("Edited /tmp/demo.txt")
        finally:
            service.stop()
            relay.stop()

    def test_register_rejected_over_http(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(relay_server=relay, bind=f"127.0.0.1:{port}")
        service.start()
        try:
            req = request.Request(
                f"{service.base_url}/remote/register",
                data=json.dumps(
                    {"bootstrap_token": "bt_invalid", "cwd": "/tmp"}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                _URLOPEN(req, timeout=5)
                assert False, "expected HTTPError"
            except HTTPError as exc:
                assert exc.code == 403
                body = json.loads(exc.read().decode("utf-8"))
                assert body["type"] == "register_rejected"
        finally:
            service.stop()
            relay.stop()

    def test_chat_endpoint_routes_to_host_chat_handler(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            chat_handler=lambda peer_id, prompt: ChatResponse(
                response=f"{peer_id}:{prompt}"
            ),
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_id = register_body["payload"]["peer_id"]
            peer_token = register_body["payload"]["peer_token"]

            status, chat_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat",
                {
                    "peer_token": peer_token,
                    "prompt": "hello",
                },
            )
            assert status == 200
            assert chat_body["response"] == f"{peer_id}:hello"
            assert chat_body.get("error") in (None, "")
        finally:
            service.stop()
            relay.stop()

    def test_chat_endpoint_allows_concurrent_requests_across_peers(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def chat_handler(peer_id: str, prompt: str) -> ChatResponse:
            time.sleep(0.3)
            return ChatResponse(response=f"{peer_id}:{prompt}")

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            chat_handler=chat_handler,
        )
        service.start()
        try:
            _, register_a = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/a",
                },
            )
            _, register_b = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/b",
                },
            )

            token_a = register_a["payload"]["peer_token"]
            token_b = register_b["payload"]["peer_token"]
            results: dict[str, dict] = {}

            def run_chat(label: str, token: str) -> None:
                _, body = _json_request(
                    "POST",
                    f"{service.base_url}/remote/chat",
                    {"peer_token": token, "prompt": label},
                )
                results[label] = body

            started = time.time()
            t1 = threading.Thread(target=run_chat, args=("p1", token_a))
            t2 = threading.Thread(target=run_chat, args=("p2", token_b))
            t1.start()
            t2.start()
            t1.join(timeout=3)
            t2.join(timeout=3)
            elapsed = time.time() - started

            assert "p1" in results and "p2" in results
            assert elapsed < 0.55
        finally:
            service.stop()
            relay.stop()

    def test_expired_peer_lease_refreshes_within_bounded_grace(
        self, monkeypatch
    ) -> None:
        now = [1000.0]
        monkeypatch.setattr(
            "reuleauxcoder.extensions.remote_exec.auth.time.time",
            lambda: now[0],
        )
        relay = RelayServer(
            peer_token_ttl_sec=1,
            heartbeat_timeout_sec=5,
        )
        relay.start()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{_free_port()}",
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            now[0] = 1002.0

            with pytest.raises(HTTPError) as expired:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/poll",
                    {"peer_token": peer_token},
                )
            assert expired.value.code == HTTPStatus.UNAUTHORIZED

            status, refreshed = _json_request(
                "POST",
                f"{service.base_url}/remote/token/refresh",
                {"peer_token": peer_token},
            )
            assert status == HTTPStatus.OK
            assert refreshed == {
                "ok": True,
                "peer_token": peer_token,
                "expires_in_sec": 1,
                "error": None,
            }

            status, _ = _json_request(
                "POST",
                f"{service.base_url}/remote/poll",
                {"peer_token": peer_token},
            )
            assert status == HTTPStatus.OK
            now[0] = 1010.0
            with pytest.raises(HTTPError) as rejected:
                _json_request(
                    "POST",
                    f"{service.base_url}/remote/token/refresh",
                    {"peer_token": peer_token},
                )
            assert rejected.value.code == HTTPStatus.UNAUTHORIZED
        finally:
            service.stop()
            relay.stop()

    def test_disconnect_aborts_active_stream_chat_session(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def stream_chat_handler(_peer_id: str, _prompt: str, session) -> None:
            # Wait long enough so test can force disconnect first.
            session.wait_approval("hold", timeout_sec=2)

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            stream_chat_handler=stream_chat_handler,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/start",
                {
                    "peer_token": peer_token,
                    "prompt": "long-run",
                },
            )
            chat_id = start_body["chat_id"]

            status, _ = _json_request(
                "POST",
                f"{service.base_url}/remote/disconnect",
                {"peer_token": peer_token, "reason": "test_disconnect"},
            )
            assert status == 200

            _, stream_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/stream",
                {
                    "peer_token": peer_token,
                    "chat_id": chat_id,
                    "cursor": 0,
                    "timeout_sec": 1,
                },
            )
            assert stream_body["done"] is True
            event_types = [event["type"] for event in stream_body["events"]]
            assert "chat_start" in event_types
            assert "error" in event_types
        finally:
            service.stop()
            relay.stop()

    def test_chat_cancel_invokes_runtime_callback_and_resolves_waiters(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        cancelled = threading.Event()

        def stream_chat_handler(_peer_id: str, _prompt: str, session) -> None:
            session.cancel_callback = cancelled.set
            session.register_interaction("hold")
            session.wait_interaction("hold", timeout_sec=3)

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            stream_chat_handler=stream_chat_handler,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "cancel me"},
            )
            chat_id = start_body["chat_id"]
            deadline = time.time() + 2
            while time.time() < deadline:
                session = service._get_chat_session(chat_id)
                if session is not None and session.cancel_callback is not None:
                    break
                time.sleep(0.01)

            status, cancel_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/cancel",
                {
                    "peer_token": peer_token,
                    "chat_id": chat_id,
                    "reason": "test_interrupt",
                },
            )

            assert status == 200
            assert cancel_body["ok"] is True
            assert cancelled.wait(timeout=1)
            cursor = 0
            stream_body = {"done": False, "next_cursor": cursor}
            deadline = time.time() + 2
            while not stream_body["done"] and time.time() < deadline:
                _, stream_body = _json_request(
                    "POST",
                    f"{service.base_url}/remote/chat/stream",
                    {
                        "peer_token": peer_token,
                        "chat_id": chat_id,
                        "cursor": cursor,
                        "timeout_sec": 2,
                    },
                )
                cursor = stream_body["next_cursor"]
            assert stream_body["done"] is True
        finally:
            service.stop()
            relay.stop()

    def test_approval_reply_routes_to_matching_chat_session_only(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def stream_chat_handler(_peer_id: str, _prompt: str, session) -> None:
            approval_id = "approval-1"
            session.register_approval(approval_id)
            session.append_event(
                "approval_request",
                {
                    "approval_id": approval_id,
                    "tool_name": "shell",
                    "tool_source": "builtin",
                    "reason": "need approval",
                },
            )
            decision, reason = session.wait_approval(approval_id, timeout_sec=2)
            session.append_event(
                "approval_resolved",
                {"approval_id": approval_id, "decision": decision, "reason": reason},
            )

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            stream_chat_handler=stream_chat_handler,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]

            _, start_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "approve me"},
            )
            chat_id = start_body["chat_id"]

            _, stream_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/stream",
                {
                    "peer_token": peer_token,
                    "chat_id": chat_id,
                    "cursor": 0,
                    "timeout_sec": 1,
                },
            )
            approval_events = [
                event
                for event in stream_body["events"]
                if event["type"] == "approval_request"
            ]
            assert approval_events
            approval_id = approval_events[0]["payload"]["approval_id"]

            status, reply_body = _json_request(
                "POST",
                f"{service.base_url}/remote/approval/reply",
                {
                    "peer_token": peer_token,
                    "chat_id": chat_id,
                    "approval_id": approval_id,
                    "decision": "allow_once",
                    "reason": "ok",
                },
            )
            assert status == 200
            assert reply_body["ok"] is True

            _, resolved_body = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/stream",
                {
                    "peer_token": peer_token,
                    "chat_id": chat_id,
                    "cursor": stream_body["next_cursor"],
                    "timeout_sec": 1,
                },
            )
            resolved_events = [
                event
                for event in resolved_body["events"]
                if event["type"] == "approval_resolved"
            ]
            assert resolved_events
            assert resolved_events[0]["payload"]["decision"] == "allow_once"
            assert resolved_body["done"] is True

            bad_chat_req = request.Request(
                f"{service.base_url}/remote/approval/reply",
                data=json.dumps(
                    {
                        "peer_token": peer_token,
                        "chat_id": "missing-chat",
                        "approval_id": approval_id,
                        "decision": "allow_once",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                _URLOPEN(bad_chat_req, timeout=5)
                assert False, "expected HTTPError"
            except HTTPError as exc:
                assert exc.code == 404
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "chat_not_found"

            bad_approval_req = request.Request(
                f"{service.base_url}/remote/approval/reply",
                data=json.dumps(
                    {
                        "peer_token": peer_token,
                        "chat_id": chat_id,
                        "approval_id": "missing-approval",
                        "decision": "allow_once",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                _URLOPEN(bad_approval_req, timeout=5)
                assert False, "expected HTTPError"
            except HTTPError as exc:
                assert exc.code == 404
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "approval_not_found"
        finally:
            service.stop()
            relay.stop()

    def test_generic_interaction_reply_routes_opaque_value(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()

        def stream_chat_handler(_peer_id: str, _prompt: str, session) -> None:
            session.register_interaction("request-1")
            session.append_event(
                "interaction_request",
                {
                    "request_id": "request-1",
                    "kind": "choose_one",
                    "rendered_frame": "Pick one\n",
                    "input_constraints": {"options": ["a", "b"]},
                },
            )
            value, cancelled, reason = session.wait_interaction(
                "request-1", timeout_sec=2
            )
            session.append_event(
                "interaction_resolved",
                {"value": value, "cancelled": cancelled, "reason": reason},
            )

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            stream_chat_handler=stream_chat_handler,
        )
        service.start()
        try:
            _, register_body = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {
                    "bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60),
                    "cwd": "/tmp/peer",
                },
            )
            peer_token = register_body["payload"]["peer_token"]
            _, start = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/start",
                {"peer_token": peer_token, "prompt": "choose"},
            )
            _, first = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/stream",
                {
                    "peer_token": peer_token,
                    "chat_id": start["chat_id"],
                    "cursor": 0,
                    "timeout_sec": 1,
                },
            )
            interaction = next(
                event
                for event in first["events"]
                if event["type"] == "interaction_request"
            )

            status, reply = _json_request(
                "POST",
                f"{service.base_url}/remote/interaction/reply",
                {
                    "peer_token": peer_token,
                    "chat_id": start["chat_id"],
                    "request_id": interaction["payload"]["request_id"],
                    "value": "b",
                    "cancelled": False,
                },
            )

            assert status == 200
            assert reply["ok"] is True
            _, resolved = _json_request(
                "POST",
                f"{service.base_url}/remote/chat/stream",
                {
                    "peer_token": peer_token,
                    "chat_id": start["chat_id"],
                    "cursor": first["next_cursor"],
                    "timeout_sec": 1,
                },
            )
            event = next(
                event
                for event in resolved["events"]
                if event["type"] == "interaction_resolved"
            )
            assert event["payload"]["value"] == "b"
        finally:
            service.stop()
            relay.stop()

    def test_default_artifact_provider_prefers_prebuilt_binary(
        self, tmp_path: Path
    ) -> None:
        provider = _default_create_remote_artifact_provider(UIEventBus())
        artifact_root = getattr(provider, "_artifact_root")
        prebuilt_path = artifact_root / "linux" / "amd64" / "rcoder-peer"
        prebuilt_path.parent.mkdir(parents=True, exist_ok=True)
        prebuilt_path.write_bytes(b"prebuilt-peer")
        try:
            with patch(
                "reuleauxcoder.interfaces.entrypoint.dependencies.subprocess.run"
            ) as mock_run:
                content, content_type = provider("linux", "amd64", "rcoder-peer") or (
                    None,
                    None,
                )
            assert content == b"prebuilt-peer"
            assert content_type == "application/octet-stream"
            mock_run.assert_not_called()
        finally:
            _cleanup_provider_build_dir(provider)
            prebuilt_path.unlink(missing_ok=True)
            for parent in [
                prebuilt_path.parent,
                prebuilt_path.parent.parent,
                artifact_root,
            ]:
                try:
                    parent.rmdir()
                except OSError:
                    pass

    def test_default_artifact_provider_rejects_oversized_prebuilt(
        self, tmp_path: Path
    ) -> None:
        del tmp_path
        provider = _default_create_remote_artifact_provider(UIEventBus())
        artifact_root = getattr(provider, "_artifact_root")
        prebuilt_path = artifact_root / "linux" / "amd64" / "rcoder-peer"
        prebuilt_path.parent.mkdir(parents=True, exist_ok=True)
        prebuilt_path.write_bytes(b"oversized")
        try:
            with patch(
                "reuleauxcoder.interfaces.entrypoint.dependencies.MAX_PEER_ARTIFACT_BYTES",
                4,
            ):
                with pytest.raises(RuntimeError, match="exceeds size budget"):
                    provider("linux", "amd64", "rcoder-peer")
        finally:
            _cleanup_provider_build_dir(provider)
            prebuilt_path.unlink(missing_ok=True)
            for parent in [
                prebuilt_path.parent,
                prebuilt_path.parent.parent,
                artifact_root,
            ]:
                try:
                    parent.rmdir()
                except OSError:
                    pass

    def test_default_artifact_provider_raises_without_prebuilt_or_go(self) -> None:
        provider = _default_create_remote_artifact_provider(UIEventBus())
        try:
            with patch(
                "reuleauxcoder.interfaces.entrypoint.dependencies.shutil.which",
                return_value=None,
            ):
                with pytest.raises(RuntimeError, match="no prebuilt binary found"):
                    provider("linux", "amd64", "rcoder-peer")
        finally:
            _cleanup_provider_build_dir(provider)

    @pytest.mark.skipif(not _GO_AVAILABLE, reason="go toolchain is not installed")
    def test_default_artifact_provider_builds_real_agent_binary(self) -> None:
        provider = _default_create_remote_artifact_provider(UIEventBus())
        try:
            content, content_type = provider("linux", "amd64", "rcoder-peer") or (
                None,
                None,
            )
            assert content_type == "application/octet-stream"
            assert isinstance(content, bytes)
            assert len(content) > 0
        finally:
            _cleanup_provider_build_dir(provider)

    def test_artifact_endpoint_returns_clear_error_when_unavailable(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            artifact_provider=lambda _os_name, _arch, _name: (_ for _ in ()).throw(
                RuntimeError(
                    "peer artifact unavailable: no prebuilt binary found and local 'go' toolchain is not installed"
                )
            ),
        )
        service.start()
        try:
            try:
                _URLOPEN(
                    f"{service.base_url}/remote/artifacts/linux/amd64/rcoder-peer",
                    timeout=5,
                )
                assert False, "expected HTTPError"
            except HTTPError as exc:
                assert exc.code == 404
                body = json.loads(exc.read().decode("utf-8"))
                assert body["error"] == "artifact_unavailable"
                assert "no prebuilt binary found" in body["message"]
        finally:
            service.stop()
            relay.stop()

    @pytest.mark.skipif(not _GO_AVAILABLE, reason="go toolchain is not installed")
    def test_go_agent_end_to_end_with_http_host(self, tmp_path: Path) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(relay_server=relay, bind=f"127.0.0.1:{port}")
        service.start()
        agent_binary = _build_go_agent_binary()
        work_dir = tmp_path / "peer-work"
        work_dir.mkdir()
        target_file = work_dir / "demo.txt"
        target_file.write_text("hello world\n")
        proc = subprocess.Popen(
            [
                str(agent_binary),
                "--host",
                service.base_url,
                "--bootstrap-token",
                relay.issue_bootstrap_token(ttl_sec=60),
                "--cwd",
                str(work_dir),
                "--workspace-root",
                str(work_dir),
                "--poll-interval",
                "100ms",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "COLUMNS": "97",
                "COLORTERM": "truecolor",
                "TERM": "xterm-256color",
                "LANG": "en_US.UTF-8",
                "NO_COLOR": "",
            },
        )
        try:
            deadline = time.time() + 10
            peer_id = None
            while time.time() < deadline:
                online = relay.registry.list_online()
                if online:
                    peer_id = online[0].peer_id
                    break
                time.sleep(0.1)
            assert peer_id is not None
            peer = relay.registry.get(peer_id)
            assert peer is not None
            assert peer.meta["terminal"] == {
                "width": 97,
                "color_level": "truecolor",
                "unicode": True,
                "interactive": False,
            }
            assert {
                "process.interrupt",
                "process.terminate",
                "process.release",
            }.issubset(peer.capabilities)

            backend = RemoteRelayToolBackend(relay_server=relay)
            backend.context.peer_id = peer_id
            process_manager = ProcessManager()
            shell = ShellTool(backend=backend)
            shell.bind_agent(
                SimpleNamespace(
                    process_manager=process_manager,
                    agent_id="agent",
                    current_session_id="session",
                    session_generation=0,
                    _current_turn_id="turn",
                )
            )
            shell.bind_execution(tool_call_id="call", session_generation=0)

            process = backend.process
            process_handle = process.start(
                "printf '%s' 'left && right'",
                cwd=str(work_dir),
                runtime_timeout=10,
            )
            cursor = ProcessCursor()
            output = []
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                snapshot = process.poll(
                    process_handle.session_id,
                    cursor=cursor,
                    wait_ms=100,
                )
                cursor = snapshot.cursor
                output.append(snapshot.stdout)
                if snapshot.state is ProcessState.EXITED:
                    break
            else:
                raise AssertionError("resumable remote process did not exit")
            assert "".join(output) == "left && right"
            assert snapshot.exit_code == 0
            process.release(process_handle.session_id)

            running_handle = process.start(
                "sleep 30",
                cwd=str(work_dir),
                runtime_timeout=60,
            )
            running = process.poll(running_handle.session_id)
            assert running.state is ProcessState.RUNNING
            process.terminate(
                running_handle.session_id,
                reason="test_terminated",
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                stopped = process.poll(
                    running_handle.session_id,
                    wait_ms=100,
                )
                if stopped.state is ProcessState.EXITED:
                    break
            else:
                raise AssertionError("remote terminate did not stop the process")
            assert stopped.termination_reason == "test_terminated"
            process.release(running_handle.session_id)

            shell_result = shell.execute(command="printf 'hi-from-agent'")
            assert "hi-from-agent" in shell_result.model_text

            timeout_started = time.monotonic()
            timeout_result = shell.execute(command="sleep 10", timeout=1)
            timeout_snapshot = cast(
                dict[str, Any],
                timeout_result.metadata["process_snapshot"],
            )
            assert (
                timeout_snapshot["termination_reason"] == "timeout"
            )
            assert time.monotonic() - timeout_started < 3

            cancellation = threading.Event()
            backend.context.cancellation_event = cancellation
            timer = threading.Timer(0.2, cancellation.set)
            cancel_started = time.monotonic()
            timer.start()
            try:
                cancel_result = shell.execute(command="sleep 30", timeout=20)
            finally:
                timer.cancel()
                cancellation.clear()
            assert "cancelled" in cancel_result.model_text.lower()
            assert time.monotonic() - cancel_started < 3
            process_manager.shutdown()
            assert (
                "still-alive"
                in ShellTool(backend=backend)
                .execute(command="printf 'still-alive'")
                .model_text
            )

            read_result = ReadFileTool(backend=backend).execute(
                file_path=str(target_file)
            )
            assert "1\thello world" in read_result.model_text

            local_backend = LocalToolBackend(
                ExecutionContext(cwd=str(work_dir), workspace_root=str(work_dir))
            )
            write_result = WriteFileTool(backend=backend).execute(
                file_path=str(target_file),
                content="alpha\nbeta\n",
            )
            assert "Wrote" in write_result.model_text
            assert target_file.read_text() == "alpha\nbeta\n"
            target_file.write_text("hello world\n")
            local_write_result = WriteFileTool(backend=local_backend).execute(
                file_path=str(target_file),
                content="alpha\nbeta\n",
            )
            assert write_result == local_write_result

            edit_result = EditFileTool(backend=backend).execute(
                file_path=str(target_file),
                old_string="beta",
                new_string="gamma",
            )
            assert edit_result.diff is not None
            assert "--- a/" in edit_result.diff.unified
            assert "+++ b/" in edit_result.diff.unified
            assert "-beta" in edit_result.diff.unified
            assert "+gamma" in edit_result.diff.unified
            assert target_file.read_text() == "alpha\ngamma\n"
            target_file.write_text("alpha\nbeta\n")
            local_edit_result = EditFileTool(backend=local_backend).execute(
                file_path=str(target_file),
                old_string="beta",
                new_string="gamma",
            )
            assert edit_result == local_edit_result

            glob_result = GlobTool(backend=backend).execute(
                pattern="*.txt", path=str(work_dir)
            )
            assert str(target_file) in glob_result.model_text

            grep_result = GrepTool(backend=backend).execute(
                pattern="gamma", path=str(work_dir)
            )
            assert str(target_file) in grep_result.model_text
            assert "gamma" in grep_result.model_text

            assert glob_result == GlobTool(backend=local_backend).execute(
                pattern="*.txt", path=str(work_dir)
            )
            assert grep_result == GrepTool(backend=local_backend).execute(
                pattern="gamma", path=str(work_dir)
            )
            remote_list = ListFileTool(backend=backend).execute(
                path=str(work_dir), long=False, recursive=True
            )
            local_list = ListFileTool(backend=local_backend).execute(
                path=str(work_dir), long=False, recursive=True
            )
            assert remote_list == local_list
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            service.stop()
            relay.stop()

    @pytest.mark.skipif(not _GO_AVAILABLE, reason="go toolchain is not installed")
    def test_go_peer_refreshes_expired_lease_and_retries_result(
        self, tmp_path: Path
    ) -> None:
        relay = RelayServer(
            peer_token_ttl_sec=1,
            heartbeat_interval_sec=30,
            heartbeat_timeout_sec=5,
        )
        relay.start()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{_free_port()}",
        )
        service.start()
        binary = _build_go_agent_binary()
        work_dir = tmp_path / "lease-peer"
        work_dir.mkdir()
        target = work_dir / "lease.txt"
        target.write_text("lease survived\n")
        proc = subprocess.Popen(
            [
                str(binary),
                "--host",
                service.base_url,
                "--bootstrap-token",
                relay.issue_bootstrap_token(ttl_sec=60),
                "--cwd",
                str(work_dir),
                "--workspace-root",
                str(work_dir),
                "--poll-interval",
                "50ms",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.time() + 10
            peer_id = None
            while time.time() < deadline:
                peers = relay.registry.list_online()
                if peers:
                    peer_id = peers[0].peer_id
                    break
                time.sleep(0.05)
            assert peer_id is not None
            time.sleep(2)

            backend = RemoteRelayToolBackend(relay_server=relay)
            backend.context.peer_id = peer_id
            result = ReadFileTool(backend=backend).execute(file_path=str(target))

            assert "lease survived" in result.model_text
            assert proc.poll() is None
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            service.stop()
            relay.stop()

    @pytest.mark.skipif(
        not _GO_AVAILABLE or sys.platform == "win32",
        reason="requires Go and POSIX process signals",
    )
    def test_interactive_go_peer_ctrl_c_cancels_host_chat(self, tmp_path: Path) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        callback_ready = threading.Event()
        cancelled = threading.Event()

        def stream_chat_handler(_peer_id: str, _prompt: str, session) -> None:
            session.cancel_callback = cancelled.set
            session.register_interaction("hold")
            callback_ready.set()
            session.wait_interaction("hold", timeout_sec=10)

        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            stream_chat_handler=stream_chat_handler,
        )
        service.start()
        binary = _build_go_agent_binary()
        work_dir = tmp_path / "interactive-peer"
        work_dir.mkdir()
        proc = subprocess.Popen(
            [
                str(binary),
                "--host",
                service.base_url,
                "--bootstrap-token",
                relay.issue_bootstrap_token(ttl_sec=60),
                "--cwd",
                str(work_dir),
                "--workspace-root",
                str(work_dir),
                "--interactive",
                "--poll-interval",
                "100ms",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.time() + 10
            while time.time() < deadline and not relay.registry.list_online():
                time.sleep(0.05)
            assert relay.registry.list_online()
            assert proc.stdin is not None
            proc.stdin.write("long running chat\n")
            proc.stdin.flush()
            assert callback_ready.wait(timeout=5)

            proc.send_signal(signal.SIGINT)

            assert cancelled.wait(timeout=3)
            time.sleep(0.2)
            assert proc.poll() is None
            proc.stdin.write("/exit\n")
            proc.stdin.flush()
            assert proc.wait(timeout=5) == 0
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            service.stop()
            relay.stop()
