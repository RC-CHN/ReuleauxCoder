"""Host-side relay server for remote tool execution."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any, Callable

from reuleauxcoder.extensions.remote_exec.auth import TokenManager
from reuleauxcoder.extensions.remote_exec.errors import (
    PeerDisconnectedError,
    PeerNotFoundError,
    RemoteTimeoutError,
    RegisterRejectedError,
)
from reuleauxcoder.extensions.remote_exec.peer_registry import PeerRegistry
from reuleauxcoder.extensions.remote_exec.protocol import (
    CleanupRequest,
    CleanupResult,
    ErrorMessage,
    ExecToolRequest,
    ExecToolResult,
    Heartbeat,
    RegisterRejected,
    RegisterRequest,
    RegisterResponse,
    RelayEnvelope,
)


SendFn = Callable[[str, RelayEnvelope], None]


class RelayServer:
    """Host relay server: manages peers, routes tool requests, handles heartbeats.

    Transport-agnostic: inject a ``send_fn(peer_id, envelope)`` that performs
    actual I/O (WebSocket, in-memory queue, etc.).
    """

    def __init__(
        self,
        send_fn: SendFn | None = None,
        heartbeat_interval_sec: int = 10,
        heartbeat_timeout_sec: int = 30,
        default_tool_timeout_sec: int = 30,
        shell_timeout_sec: int = 120,
    ):
        self._send_fn = send_fn
        self._token_manager = TokenManager()
        self._registry = PeerRegistry(heartbeat_timeout_sec=heartbeat_timeout_sec)
        self._heartbeat_interval_sec = heartbeat_interval_sec
        self._default_tool_timeout_sec = default_tool_timeout_sec
        self._shell_timeout_sec = shell_timeout_sec

        # asyncio plumbing
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._pending_peer_ids: dict[str, str] = {}
        self._lock = threading.Lock()
        self._prune_task: asyncio.Task | None = None
        self._shutdown_event = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the relay server in a background daemon thread."""
        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        # schedule periodic prune
        future = asyncio.run_coroutine_threadsafe(self._prune_loop(), self._loop)
        # wait for prune loop to start so we know loop is running
        try:
            future.result(timeout=2.0)
        except Exception:
            pass

    def stop(self) -> None:
        """Stop the relay server and cancel pending requests."""
        self._shutdown_event.set()
        if self._loop is not None:
            # cancel pending futures
            for fut in list(self._pending.values()):
                if not fut.done():
                    self._loop.call_soon_threadsafe(fut.cancel)
            if self._prune_task is not None:
                self._loop.call_soon_threadsafe(self._prune_task.cancel)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._loop = None

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    # ------------------------------------------------------------------
    # Public: inbound message handling (called by transport layer)
    # ------------------------------------------------------------------

    def handle_inbound(self, peer_id: str | None, envelope: RelayEnvelope) -> None:
        """Process a message received from a peer."""
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._handle_inbound_async(peer_id, envelope), self._loop
        )

    async def _handle_inbound_async(
        self, peer_id: str | None, envelope: RelayEnvelope
    ) -> None:
        msg_type = envelope.type
        payload = envelope.payload
        req_id = envelope.request_id

        if msg_type == "register":
            req = RegisterRequest.from_dict(payload)
            try:
                resp = self._on_register(req)
                if isinstance(resp, RegisterResponse):
                    self._send(resp.peer_id, RelayEnvelope(
                        type="register_ok",
                        request_id=req_id,
                        peer_id=resp.peer_id,
                        payload=resp.to_dict(),
                    ))
                else:
                    self._send("", RelayEnvelope(
                        type="register_rejected",
                        request_id=req_id,
                        payload=resp.to_dict(),
                    ))
            except RegisterRejectedError as e:
                self._send("", RelayEnvelope(
                    type="register_rejected",
                    request_id=req_id,
                    payload=RegisterRejected(reason=e.message).to_dict(),
                ))

        elif msg_type == "heartbeat":
            hb = Heartbeat.from_dict(payload)
            peer = self._token_manager.verify_peer_token(hb.peer_token)
            if peer:
                self._registry.update_heartbeat(peer)

        elif msg_type == "tool_result":
            result = ExecToolResult.from_dict(payload)
            if req_id:
                with self._lock:
                    fut = self._pending.pop(req_id, None)
                    self._pending_peer_ids.pop(req_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(result)

        elif msg_type == "cleanup_result":
            result = CleanupResult.from_dict(payload)
            if req_id:
                with self._lock:
                    fut = self._pending.pop(req_id, None)
                    self._pending_peer_ids.pop(req_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(result)

        elif msg_type == "disconnect":
            if peer_id:
                self._registry.mark_disconnected(peer_id, "peer_initiated")
                self._fail_pending_for_peer(peer_id)

        elif msg_type == "error":
            err = ErrorMessage.from_dict(payload)
            if req_id:
                with self._lock:
                    fut = self._pending.pop(req_id, None)
                    self._pending_peer_ids.pop(req_id, None)
                if fut is not None and not fut.done():
                    fut.set_exception(
                        PeerDisconnectedError(peer_id or "unknown")
                        if err.code == "PEER_DISCONNECTED"
                        else Exception(f"[{err.code}] {err.message}")
                    )

    # ------------------------------------------------------------------
    # Public: host-initiated actions (sync API for callers)
    # ------------------------------------------------------------------

    def send_exec_request(
        self,
        peer_id: str,
        request: ExecToolRequest,
        timeout_sec: int | None = None,
    ) -> ExecToolResult:
        """Send a tool execution request to a peer and wait for the result."""
        if self._loop is None:
            raise RuntimeError("RelayServer not started")

        peer = self._registry.get(peer_id)
        if peer is None:
            raise PeerNotFoundError(peer_id)

        req_id = str(uuid.uuid4())
        envelope = RelayEnvelope(
            type="exec_tool",
            request_id=req_id,
            peer_id=peer_id,
            payload=request.to_dict(),
        )

        # determine timeout
        effective_timeout = (
            timeout_sec
            if timeout_sec is not None
            else (
                self._shell_timeout_sec
                if request.tool_name == "shell"
                else self._default_tool_timeout_sec
            )
        )

        future = asyncio.run_coroutine_threadsafe(
            self._send_and_wait(req_id, peer_id, envelope, effective_timeout),
            self._loop,
        )
        return future.result()

    def request_cleanup(self, peer_id: str, timeout_sec: int = 10) -> CleanupResult:
        """Request cleanup on a peer. Returns best-effort result."""
        if self._loop is None:
            raise RuntimeError("RelayServer not started")

        peer = self._registry.get(peer_id)
        if peer is None:
            return CleanupResult(ok=False, error_message=f"Peer '{peer_id}' is offline")

        req_id = str(uuid.uuid4())
        envelope = RelayEnvelope(
            type="cleanup",
            request_id=req_id,
            peer_id=peer_id,
            payload=CleanupRequest().to_dict(),
        )

        future = asyncio.run_coroutine_threadsafe(
            self._send_and_wait(req_id, peer_id, envelope, timeout_sec),
            self._loop,
        )
        try:
            return future.result(timeout=timeout_sec + 2)
        except Exception as e:
            return CleanupResult(ok=False, error_message=str(e))

    # ------------------------------------------------------------------
    # Internal: request/response correlation
    # ------------------------------------------------------------------

    async def _send_and_wait(
        self,
        req_id: str,
        peer_id: str,
        envelope: RelayEnvelope,
        timeout_sec: float,
    ) -> Any:
        fut = self._loop.create_future()
        with self._lock:
            self._pending[req_id] = fut
            self._pending_peer_ids[req_id] = peer_id
        try:
            self._send(peer_id, envelope)
            return await asyncio.wait_for(fut, timeout=timeout_sec)
        except asyncio.TimeoutError:
            raise RemoteTimeoutError(int(timeout_sec))
        finally:
            with self._lock:
                self._pending.pop(req_id, None)
                self._pending_peer_ids.pop(req_id, None)

    def _send(self, peer_id: str, envelope: RelayEnvelope) -> None:
        if self._send_fn is not None:
            try:
                self._send_fn(peer_id, envelope)
            except Exception:
                pass

    def _fail_pending_for_peer(self, peer_id: str) -> None:
        """Fail all pending requests for a disconnected peer."""
        with self._lock:
            pending = [
                (req_id, fut)
                for req_id, fut in self._pending.items()
                if self._pending_peer_ids.get(req_id) == peer_id
            ]
            for req_id, _ in pending:
                self._pending.pop(req_id, None)
                self._pending_peer_ids.pop(req_id, None)
        for _, fut in pending:
            if fut.done():
                continue
            self._loop.call_soon_threadsafe(
                fut.set_exception,
                PeerDisconnectedError(peer_id),
            )

    # ------------------------------------------------------------------
    # Internal: registration / heartbeat
    # ------------------------------------------------------------------

    def _on_register(self, req: RegisterRequest) -> RegisterResponse | RegisterRejected:
        if not self._token_manager.consume_bootstrap_token(req.bootstrap_token):
            raise RegisterRejectedError("Invalid or expired bootstrap token")

        meta = {
            "cwd": req.cwd,
            "workspace_root": req.workspace_root,
            "capabilities": req.capabilities,
            "host_info_min": req.host_info_min,
        }
        peer_id = self._registry.register(meta=meta)
        peer_token = self._token_manager.issue_peer_token(
            peer_id, ttl_sec=3600
        )
        return RegisterResponse(
            peer_id=peer_id,
            peer_token=peer_token,
            heartbeat_interval_sec=self._heartbeat_interval_sec,
        )

    # ------------------------------------------------------------------
    # Token helpers
    # ------------------------------------------------------------------

    def issue_bootstrap_token(self, ttl_sec: int = 300) -> str:
        """Host API: issue a one-time bootstrap token for a new peer."""
        return self._token_manager.issue_bootstrap_token(ttl_sec=ttl_sec)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    async def _prune_loop(self) -> None:
        """Periodic cleanup of stale peers and expired tokens."""
        self._prune_task = asyncio.current_task()
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self._heartbeat_interval_sec)
            except asyncio.CancelledError:
                break
            stale = self._registry.prune_stale()
            for pid in stale:
                self._fail_pending_for_peer(pid)
            self._token_manager.prune_expired()

    @property
    def registry(self) -> PeerRegistry:
        return self._registry

    @property
    def token_manager(self) -> TokenManager:
        return self._token_manager
