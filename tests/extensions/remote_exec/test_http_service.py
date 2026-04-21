"""Tests for the HTTP transport adapter around the remote relay host."""

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib import request
from urllib.error import HTTPError

from reuleauxcoder.extensions.remote_exec.http_service import RemoteRelayHTTPService
from reuleauxcoder.extensions.remote_exec.protocol import ChatResponse, CleanupResult, ExecToolResult
from reuleauxcoder.extensions.remote_exec.server import RelayServer
from reuleauxcoder.extensions.tools.builtin.edit import EditFileTool
from reuleauxcoder.extensions.tools.builtin.glob import GlobTool
from reuleauxcoder.extensions.tools.builtin.grep import GrepTool
from reuleauxcoder.extensions.tools.builtin.read import ReadFileTool
from reuleauxcoder.extensions.tools.builtin.shell import ShellTool
from reuleauxcoder.extensions.tools.builtin.write import WriteFileTool
from reuleauxcoder.extensions.remote_exec.backend import RemoteRelayToolBackend
from reuleauxcoder.interfaces.entrypoint.runner import _default_create_remote_artifact_provider
from reuleauxcoder.interfaces.events import UIEventBus


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_request(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    with request.urlopen(req, timeout=5) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, json.loads(body) if body else {}


def _text_request(url: str) -> tuple[int, str]:
    with request.urlopen(url, timeout=5) as resp:
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
    def test_bootstrap_and_artifact_endpoints(self) -> None:
        relay = RelayServer()
        relay.start()
        port = _free_port()
        service = RemoteRelayHTTPService(
            relay_server=relay,
            bind=f"127.0.0.1:{port}",
            artifact_provider=lambda os_name, arch, name: (
                b"peer-binary",
                "application/octet-stream",
            )
            if (os_name, arch, name) == ("linux", "amd64", "rcoder-peer")
            else None,
        )
        service.start()
        try:
            status, script = _text_request(f"{service.base_url}/remote/bootstrap.sh")
            assert status == 200
            assert "rcoder-peer" in script
            assert service.base_url in script
            assert "/remote/artifacts/{os}/{arch}/rcoder-peer" in script

            with request.urlopen(
                f"{service.base_url}/remote/artifacts/linux/amd64/rcoder-peer", timeout=5
            ) as resp:
                assert resp.status == 200
                assert resp.read() == b"peer-binary"
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
                {"peer_token": peer_token, "ts": time.time()},
            )
            assert status == 200
            assert heartbeat_body["peer_id"] == peer_id

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
                    "payload": ExecToolResult(ok=True, result="hello from peer").to_dict(),
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
                    "payload": CleanupResult(ok=True, removed_items=["/tmp/rc-peer"]).to_dict(),
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
            cases = [
                (ShellTool(backend=backend), {"command": "echo hello"}, "shell", "shell-ok"),
                (ReadFileTool(backend=backend), {"file_path": "/tmp/demo.txt"}, "read_file", "read-ok"),
                (
                    WriteFileTool(backend=backend),
                    {"file_path": "/tmp/demo.txt", "content": "hello"},
                    "write_file",
                    "write-ok",
                ),
                (
                    EditFileTool(backend=backend),
                    {"file_path": "/tmp/demo.txt", "old_string": "a", "new_string": "b"},
                    "edit_file",
                    "edit-ok",
                ),
                (GlobTool(backend=backend), {"pattern": "*.py", "path": "/tmp"}, "glob", "glob-ok"),
                (GrepTool(backend=backend), {"pattern": "hello", "path": "/tmp"}, "grep", "grep-ok"),
            ]

            for tool, kwargs, expected_name, expected_result in cases:
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
                        "payload": ExecToolResult(ok=True, result=expected_result).to_dict(),
                    },
                )
                assert status == 200
                assert result_body["ok"] is True

                t.join(timeout=2)
                assert holder["result"] == expected_result
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
                data=json.dumps({"bootstrap_token": "bt_invalid", "cwd": "/tmp"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                request.urlopen(req, timeout=5)
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
            chat_handler=lambda peer_id, prompt: ChatResponse(response=f"{peer_id}:{prompt}"),
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
                {"bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60), "cwd": "/tmp/a"},
            )
            _, register_b = _json_request(
                "POST",
                f"{service.base_url}/remote/register",
                {"bootstrap_token": relay.issue_bootstrap_token(ttl_sec=60), "cwd": "/tmp/b"},
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

    def test_default_artifact_provider_builds_real_agent_binary(self) -> None:
        provider = _default_create_remote_artifact_provider(UIEventBus())
        try:
            content, content_type = provider("linux", "amd64", "rcoder-peer") or (None, None)
            assert content_type == "application/octet-stream"
            assert isinstance(content, bytes)
            assert len(content) > 0
        finally:
            _cleanup_provider_build_dir(provider)

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

            backend = RemoteRelayToolBackend(relay_server=relay)
            backend.context.peer_id = peer_id

            shell_result = ShellTool(backend=backend).execute(command="printf 'hi-from-agent'")
            assert "hi-from-agent" in shell_result

            read_result = ReadFileTool(backend=backend).execute(file_path=str(target_file))
            assert "1\thello world" in read_result

            write_result = WriteFileTool(backend=backend).execute(
                file_path=str(target_file),
                content="alpha\nbeta\n",
            )
            assert "Wrote" in write_result
            assert target_file.read_text() == "alpha\nbeta\n"

            edit_result = EditFileTool(backend=backend).execute(
                file_path=str(target_file),
                old_string="beta",
                new_string="gamma",
            )
            assert "Edited" in edit_result
            assert target_file.read_text() == "alpha\ngamma\n"

            glob_result = GlobTool(backend=backend).execute(pattern="*.txt", path=str(work_dir))
            assert str(target_file) in glob_result

            grep_result = GrepTool(backend=backend).execute(pattern="gamma", path=str(work_dir))
            assert str(target_file) in grep_result
            assert "gamma" in grep_result
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            service.stop()
            relay.stop()
