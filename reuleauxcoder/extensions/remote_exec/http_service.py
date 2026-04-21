"""HTTP transport adapter for the remote relay host."""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from reuleauxcoder.extensions.remote_exec.bootstrap import generate_bootstrap_script
from reuleauxcoder.extensions.remote_exec.errors import RegisterRejectedError
from reuleauxcoder.extensions.remote_exec.protocol import (
    ApprovalReplyRequest,
    ApprovalReplyResponse,
    ChatRequest,
    ChatResponse,
    ChatStartRequest,
    ChatStartResponse,
    ChatStreamRequest,
    ChatStreamResponse,
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


@dataclass
class _RemoteChatSession:
    chat_id: str
    peer_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    running: bool = False
    seq_next: int = 1
    approval_waiters: dict[str, dict[str, Any]] = field(default_factory=dict)
    cond: threading.Condition = field(default_factory=threading.Condition)

    def append_event(self, event_type: str, payload: dict[str, Any] | None = None) -> int:
        with self.cond:
            seq = self.seq_next
            self.seq_next += 1
            self.events.append(
                {
                    "chat_id": self.chat_id,
                    "seq": seq,
                    "type": event_type,
                    "payload": payload or {},
                }
            )
            self.cond.notify_all()
            return seq

    def wait_events(self, cursor: int, timeout_sec: float) -> tuple[list[dict[str, Any]], bool, int]:
        deadline = time.time() + max(timeout_sec, 0.0)
        with self.cond:
            while cursor >= len(self.events) and not self.done:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self.cond.wait(timeout=remaining)
            out = self.events[cursor:]
            return out, self.done, len(self.events)

    def mark_running(self) -> None:
        with self.cond:
            self.running = True

    def mark_done(self) -> None:
        with self.cond:
            self.running = False
            self.done = True
            self.cond.notify_all()

    def register_approval(self, approval_id: str) -> None:
        with self.cond:
            self.approval_waiters[approval_id] = {}

    def resolve_approval(self, approval_id: str, decision: str, reason: str | None) -> bool:
        with self.cond:
            waiter = self.approval_waiters.get(approval_id)
            if waiter is None:
                return False
            waiter["done"] = True
            waiter["decision"] = decision
            waiter["reason"] = reason
            self.cond.notify_all()
            return True

    def wait_approval(self, approval_id: str, timeout_sec: float | None = None) -> tuple[str, str | None]:
        deadline = time.time() + timeout_sec if timeout_sec else None
        with self.cond:
            waiter = self.approval_waiters.setdefault(approval_id, {})
            while not waiter.get("done"):
                if deadline is None:
                    self.cond.wait(timeout=0.5)
                    continue
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self.cond.wait(timeout=remaining)
            decision = str(waiter.get("decision", "deny_once"))
            reason = waiter.get("reason")
            return decision, reason if isinstance(reason, str) else None


class RemoteRelayHTTPService:
    """Expose ``RelayServer`` over a minimal HTTP API for remote peers."""

    def __init__(
        self,
        relay_server: RelayServer,
        bind: str,
        *,
        ui_bus: UIEventBus | None = None,
        artifact_provider: callable | None = None,
        chat_handler: Callable[[str, str], ChatResponse] | None = None,
        stream_chat_handler: Callable[[str, str, _RemoteChatSession], None] | None = None,
    ) -> None:
        self.relay_server = relay_server
        self.bind = bind
        self.ui_bus = ui_bus
        self.artifact_provider = artifact_provider
        self.chat_handler = chat_handler
        self.stream_chat_handler = stream_chat_handler
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._queues: dict[str, queue.Queue[RelayEnvelope]] = {}
        self._queues_lock = threading.Lock()
        self._chat_lock = threading.Lock()
        self._chat_sessions: dict[str, _RemoteChatSession] = {}
        self._chat_sessions_lock = threading.Lock()
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

    def set_chat_handler(self, handler: Callable[[str, str], ChatResponse] | None) -> None:
        self.chat_handler = handler

    def set_stream_chat_handler(
        self,
        handler: Callable[[str, str, _RemoteChatSession], None] | None,
    ) -> None:
        self.stream_chat_handler = handler

    def _create_chat_session(self, peer_id: str) -> _RemoteChatSession:
        session = _RemoteChatSession(chat_id=str(uuid.uuid4()), peer_id=peer_id)
        with self._chat_sessions_lock:
            self._chat_sessions[session.chat_id] = session
        return session

    def _get_chat_session(self, chat_id: str) -> _RemoteChatSession | None:
        with self._chat_sessions_lock:
            return self._chat_sessions.get(chat_id)

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
                if parsed.path == "/remote/chat":
                    self._handle_chat()
                    return
                if parsed.path == "/remote/chat/start":
                    self._handle_chat_start()
                    return
                if parsed.path == "/remote/chat/stream":
                    self._handle_chat_stream()
                    return
                if parsed.path == "/remote/approval/reply":
                    self._handle_approval_reply()
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
                host_header = self.headers.get("Host")
                forwarded_proto = self.headers.get("X-Forwarded-Proto", "http")
                request_base_url = f"{forwarded_proto}://{host_header}" if host_header else service.base_url
                script = generate_bootstrap_script(request_base_url, token)
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
                elif result_type == "tool_stream":
                    env = RelayEnvelope(
                        type="tool_stream",
                        request_id=request_id,
                        peer_id=peer_id,
                        payload=result_payload,
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

            def _handle_chat(self) -> None:
                payload = self._read_json()
                try:
                    req = ChatRequest.from_dict(payload)
                except Exception:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_chat_request"})
                    return

                peer_id = service.relay_server.token_manager.verify_peer_token(req.peer_token)
                if peer_id is None:
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_peer_token"})
                    return

                if service.chat_handler is None:
                    self._send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        ChatResponse(response="", error="chat_unavailable").to_dict(),
                    )
                    return

                with service._chat_lock:
                    try:
                        response = service.chat_handler(peer_id, req.prompt)
                    except Exception as exc:
                        response = ChatResponse(response="", error=str(exc))

                self._send_json(HTTPStatus.OK, response.to_dict())

            def _handle_chat_start(self) -> None:
                payload = self._read_json()
                try:
                    req = ChatStartRequest.from_dict(payload)
                except Exception:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_chat_start_request"})
                    return

                peer_id = service.relay_server.token_manager.verify_peer_token(req.peer_token)
                if peer_id is None:
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_peer_token"})
                    return
                if service.stream_chat_handler is None:
                    self._send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        ChatStartResponse(chat_id="", error="chat_stream_unavailable").to_dict(),
                    )
                    return

                session = service._create_chat_session(peer_id)
                session.append_event("chat_start", {"prompt": req.prompt})
                session.mark_running()

                def _run_chat() -> None:
                    with service._chat_lock:
                        try:
                            service.stream_chat_handler(peer_id, req.prompt, session)
                        except Exception as exc:
                            session.append_event("error", {"message": str(exc)})
                        finally:
                            session.mark_done()

                threading.Thread(target=_run_chat, daemon=True).start()
                self._send_json(HTTPStatus.OK, ChatStartResponse(chat_id=session.chat_id).to_dict())

            def _handle_chat_stream(self) -> None:
                payload = self._read_json()
                try:
                    req = ChatStreamRequest.from_dict(payload)
                except Exception:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_chat_stream_request"})
                    return

                peer_id = service.relay_server.token_manager.verify_peer_token(req.peer_token)
                if peer_id is None:
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_peer_token"})
                    return
                session = service._get_chat_session(req.chat_id)
                if session is None or session.peer_id != peer_id:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "chat_not_found"})
                    return

                events, done, next_cursor = session.wait_events(req.cursor, req.timeout_sec)
                self._send_json(
                    HTTPStatus.OK,
                    ChatStreamResponse(events=events, done=done, next_cursor=next_cursor).to_dict(),
                )

            def _handle_approval_reply(self) -> None:
                payload = self._read_json()
                try:
                    req = ApprovalReplyRequest.from_dict(payload)
                except Exception:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_approval_reply_request"})
                    return

                peer_id = service.relay_server.token_manager.verify_peer_token(req.peer_token)
                if peer_id is None:
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid_peer_token"})
                    return
                session = service._get_chat_session(req.chat_id)
                if session is None or session.peer_id != peer_id:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        ApprovalReplyResponse(ok=False, error="chat_not_found").to_dict(),
                    )
                    return
                ok = session.resolve_approval(req.approval_id, req.decision, req.reason)
                if not ok:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        ApprovalReplyResponse(ok=False, error="approval_not_found").to_dict(),
                    )
                    return
                self._send_json(HTTPStatus.OK, ApprovalReplyResponse(ok=True).to_dict())

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
