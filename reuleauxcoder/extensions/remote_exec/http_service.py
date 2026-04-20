"""HTTP transport adapter for the remote relay host."""

from __future__ import annotations

import json
import queue
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from reuleauxcoder.extensions.remote_exec.bootstrap import generate_bootstrap_script
from reuleauxcoder.extensions.remote_exec.errors import RegisterRejectedError
from reuleauxcoder.extensions.remote_exec.protocol import (
    CleanupResult,
    DisconnectNotice,
    ExecToolResult,
    Heartbeat,
    RegisterRejected,
    RegisterRequest,
    RelayEnvelope,
)
from reuleauxcoder.extensions.remote_exec.server import RelayServer
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind


class RemoteRelayHTTPService:
    """Expose ``RelayServer`` over a minimal HTTP API for remote peers."""

    def __init__(
        self,
        relay_server: RelayServer,
        bind: str,
        *,
        ui_bus: UIEventBus | None = None,
        artifact_provider: callable | None = None,
    ) -> None:
        self.relay_server = relay_server
        self.bind = bind
        self.ui_bus = ui_bus
        self.artifact_provider = artifact_provider
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._queues: dict[str, queue.Queue[RelayEnvelope]] = {}
        self._queues_lock = threading.Lock()
        self.relay_server._send_fn = self._enqueue_outbound

    @property
    def base_url(self) -> str:
        host, port = _parse_bind(self.bind)
        if host == "0.0.0.0":
            host = "127.0.0.1"
        return f"http://{host}:{port}"

    def start(self) -> None:
        if self._server is not None:
            return
        host, port = _parse_bind(self.bind)
        handler_cls = self._build_handler()
        self._server = ThreadingHTTPServer((host, port), handler_cls)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        if self.ui_bus is not None:
            self.ui_bus.info(
                f"Remote relay HTTP service listening on {self.base_url}",
                kind=UIEventKind.REMOTE,
            )

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._server = None

    def issue_bootstrap_token(self, ttl_sec: int = 300) -> str:
        return self.relay_server.issue_bootstrap_token(ttl_sec=ttl_sec)

    def _enqueue_outbound(self, peer_id: str, envelope: RelayEnvelope) -> None:
        with self._queues_lock:
            peer_queue = self._queues.setdefault(peer_id, queue.Queue())
        peer_queue.put(envelope)

    def _next_envelope(self, peer_id: str) -> RelayEnvelope | None:
        with self._queues_lock:
            peer_queue = self._queues.setdefault(peer_id, queue.Queue())
        try:
            return peer_queue.get_nowait()
        except queue.Empty:
            return None

    def _build_handler(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                return

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/remote/bootstrap.sh":
                    self._handle_bootstrap(parsed)
                    return
                if parsed.path.startswith("/remote/artifacts/"):
                    self._handle_artifact(parsed.path)
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/remote/register":
                    self._handle_register()
                    return
                if parsed.path == "/remote/heartbeat":
                    self._handle_heartbeat()
                    return
                if parsed.path == "/remote/poll":
                    self._handle_poll()
                    return
                if parsed.path == "/remote/result":
                    self._handle_result()
                    return
                if parsed.path == "/remote/disconnect":
                    self._handle_disconnect()
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

            def _read_json(self) -> dict[str, Any]:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length <= 0:
                    return {}
                raw = self.rfile.read(content_length)
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_text(self, status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle_bootstrap(self, parsed) -> None:
                qs = parse_qs(parsed.query)
                ttl_sec = int(qs.get("ttl_sec", ["300"])[0])
                token = qs.get("token", [None])[0] or service.issue_bootstrap_token(ttl_sec=ttl_sec)
                script = generate_bootstrap_script(service.base_url, token)
                self._send_text(HTTPStatus.OK, script, "text/x-shellscript; charset=utf-8")

            def _handle_artifact(self, path: str) -> None:
                if service.artifact_provider is None:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "artifact_unavailable", "message": "peer artifact not uploaded yet"},
                    )
                    return
                suffix = path.removeprefix("/remote/artifacts/")
                parts = suffix.split("/")
                if len(parts) != 3:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                os_name, arch, artifact_name = parts
                artifact = service.artifact_provider(os_name, arch, artifact_name)
                if artifact is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "artifact_unavailable"})
                    return
                content, content_type = artifact
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def _handle_register(self) -> None:
                payload = self._read_json()
                try:
                    resp = service.relay_server._on_register(RegisterRequest.from_dict(payload))
                except RegisterRejectedError as exc:
                    self._send_json(
                        HTTPStatus.FORBIDDEN,
                        {"type": "register_rejected", "payload": RegisterRejected(reason=exc.message).to_dict()},
                    )
                    return
                service.ui_bus and service.ui_bus.success(
                    f"Remote peer registered: {resp.peer_id}",
                    kind=UIEventKind.REMOTE,
                    peer_id=resp.peer_id,
                )
                self._send_json(HTTPStatus.OK, {"type": "register_ok", "payload": resp.to_dict()})

            def _handle_heartbeat(self) -> None:
                payload = self._read_json()
                hb = Heartbeat.from_dict(payload)
                peer_id = service.relay_server.token_manager.verify_peer_token(hb.peer_token)
                if peer_id is None:
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_peer_token"})
                    return
                service.relay_server.registry.update_heartbeat(peer_id)
                self._send_json(HTTPStatus.OK, {"ok": True, "peer_id": peer_id})

            def _handle_poll(self) -> None:
                payload = self._read_json()
                peer_token = payload.get("peer_token")
                if not isinstance(peer_token, str) or not peer_token:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "peer_token_required"})
                    return
                peer_id = service.relay_server.token_manager.verify_peer_token(peer_token)
                if peer_id is None:
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_peer_token"})
                    return
                service.relay_server.registry.update_heartbeat(peer_id)
                env = service._next_envelope(peer_id)
                if env is None:
                    self._send_json(HTTPStatus.OK, {"type": "noop", "payload": {}})
                    return
                self._send_json(HTTPStatus.OK, env.to_dict())

            def _handle_result(self) -> None:
                payload = self._read_json()
                peer_token = payload.get("peer_token")
                request_id = payload.get("request_id")
                result_type = payload.get("type", "tool_result")
                result_payload = payload.get("payload", {})
                peer_id = service.relay_server.token_manager.verify_peer_token(peer_token)
                if peer_id is None:
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_peer_token"})
                    return
                if not isinstance(request_id, str) or not request_id:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "request_id_required"})
                    return
                if result_type == "cleanup_result":
                    result = CleanupResult.from_dict(result_payload)
                    env = RelayEnvelope(
                        type="cleanup_result",
                        request_id=request_id,
                        peer_id=peer_id,
                        payload=result.to_dict(),
                    )
                else:
                    result = ExecToolResult.from_dict(result_payload)
                    env = RelayEnvelope(
                        type="tool_result",
                        request_id=request_id,
                        peer_id=peer_id,
                        payload=result.to_dict(),
                    )
                service.relay_server.handle_inbound(peer_id, env)
                self._send_json(HTTPStatus.OK, {"ok": True})

            def _handle_disconnect(self) -> None:
                payload = self._read_json()
                peer_token = payload.get("peer_token")
                peer_id = service.relay_server.token_manager.verify_peer_token(peer_token)
                if peer_id is None:
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_peer_token"})
                    return
                notice = DisconnectNotice(reason=payload.get("reason", "peer_initiated"))
                service.relay_server.handle_inbound(
                    peer_id,
                    RelayEnvelope(type="disconnect", peer_id=peer_id, payload=notice.to_dict()),
                )
                service.ui_bus and service.ui_bus.warning(
                    f"Remote peer disconnected: {peer_id}",
                    kind=UIEventKind.REMOTE,
                    peer_id=peer_id,
                    reason=notice.reason,
                )
                self._send_json(HTTPStatus.OK, {"ok": True})

        return Handler


def _parse_bind(bind: str) -> tuple[str, int]:
    host, sep, port = bind.rpartition(":")
    if not sep or not host:
        raise ValueError(f"Invalid relay bind address: {bind!r}")
    return host, int(port)
