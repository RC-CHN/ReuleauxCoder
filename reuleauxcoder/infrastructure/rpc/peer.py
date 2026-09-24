"""Bidirectional JSON-RPC 2.0; readers never wait for application handlers."""

from __future__ import annotations

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
import inspect
import itertools
import json
import logging
import queue
import threading
import time

from reuleauxcoder.domain.cancellation import CancellationSignal

from reuleauxcoder.infrastructure.rpc.transport import MessageTransport

log = logging.getLogger(__name__)


class RpcError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code, self.data = code, data


class RpcPeer:
    def __init__(
        self, transport: MessageTransport, *, methods=None, notifications=None
    ):
        self.transport = transport
        self.methods = dict(methods or {})
        self.notifications = dict(notifications or {})
        self.closed = threading.Event()
        self._close_complete = threading.Event()
        self.on_close = lambda: None
        self._ids = itertools.count(1)
        self._pending: dict[str, Future] = {}
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._events = queue.Queue()
        self._workers = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="rpc-request"
        )
        self._reader = threading.Thread(
            target=self._read, name="rpc-reader", daemon=True
        )
        self._notifier = threading.Thread(
            target=self._deliver, name="rpc-events", daemon=True
        )

    def start(self) -> None:
        self._notifier.start()
        self._reader.start()

    def _send(self, message) -> None:
        data = json.dumps(
            message, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        with self._send_lock:
            if self.closed.is_set():
                raise ConnectionError("RPC connection closed")
            self.transport.send(data)

    def request(
        self, method: str, params=None, *, timeout: float | None = None,
        cancellation_event: CancellationSignal | None = None,
    ):
        with self._lock:
            if self.closed.is_set():
                raise ConnectionError("RPC connection closed")
            request_id = str(next(self._ids))
            future = self._pending[request_id] = Future()
        try:
            if cancellation_event is not None and cancellation_event.is_set():
                raise CancelledError("RPC request cancelled")
            deadline = time.monotonic() + timeout if timeout is not None else None
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                }
            )
            if cancellation_event is None:
                return future.result(timeout=timeout)
            while True:
                if cancellation_event.is_set():
                    raise CancelledError("RPC request cancelled")
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("RPC request timed out")
                try:
                    return future.result(
                        timeout=0.05 if remaining is None else min(0.05, remaining)
                    )
                except TimeoutError:
                    if future.done():
                        raise
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params=None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _error(self, request_id, error: RpcError):
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": error.code, "message": str(error), "data": error.data},
        }

    def _invoke(self, message):
        request_id = message.get("id")
        has_id = "id" in message
        try:
            method = self.methods.get(message["method"])
            if method is None:
                raise RpcError(-32601, "Method not found")
            params = message.get("params", {})
            try:
                if isinstance(params, dict):
                    bound = inspect.signature(method).bind(**params)
                elif isinstance(params, list):
                    bound = inspect.signature(method).bind(*params)
                else:
                    raise TypeError("params must be an object or array")
            except TypeError as error:
                raise RpcError(-32602, "Invalid params") from error
            result = method(*bound.args, **bound.kwargs)
            # Fail in the handler boundary, so an invalid result still gets a reply.
            json.dumps(result, allow_nan=False)
            return (
                {"jsonrpc": "2.0", "id": request_id, "result": result}
                if has_id
                else None
            )
        except RpcError as error:
            return self._error(request_id, error) if has_id else None
        except BaseException as error:
            log.exception("RPC handler failed: %s", message["method"])
            return (
                self._error(
                    request_id,
                    RpcError(
                        -32603, "Internal error", {"error_type": type(error).__name__}
                    ),
                )
                if has_id
                else None
            )

    def _dispatch(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(None, RpcError(-32600, "Invalid Request"))
        if "method" not in message or not isinstance(message["method"], str):
            return self._error(None, RpcError(-32600, "Invalid Request"))
        if (
            "id" in message
            and message["id"] is not None
            and type(message["id"]) not in (str, int)
        ):
            return self._error(None, RpcError(-32600, "Invalid Request"))
        return self._invoke(message)

    def _respond(self, message):
        if isinstance(message, list) and message:
            response = [
                item for entry in message if (item := self._dispatch(entry)) is not None
            ]
            response = response or None
        else:
            response = self._dispatch(message)
        if response is not None and not self.closed.is_set():
            try:
                self._send(response)
            except Exception:
                if not self.closed.is_set():
                    log.exception("RPC response failed")
                self.close()

    def _read(self):
        try:
            while not self.closed.is_set():
                raw = self.transport.receive()
                if raw is None:
                    break
                try:
                    message = json.loads(raw)
                except ValueError:
                    self._send(self._error(None, RpcError(-32700, "Parse error")))
                    continue
                if (
                    isinstance(message, dict)
                    and "method" not in message
                    and "id" in message
                    and ("result" in message or "error" in message)
                ):
                    with self._lock:
                        future = self._pending.get(message["id"])
                        if future is not None and not future.done():
                            if "error" in message:
                                error = message["error"]
                                future.set_exception(
                                    RpcError(
                                        error["code"],
                                        error["message"],
                                        error.get("data"),
                                    )
                                )
                            else:
                                future.set_result(message["result"])
                elif (
                    isinstance(message, dict)
                    and message.get("jsonrpc") == "2.0"
                    and "id" not in message
                    and message.get("method") in self.notifications
                ):
                    self._events.put(message)
                else:
                    self._workers.submit(self._respond, message)
        except BaseException:
            if not self.closed.is_set():
                log.exception("RPC connection failed")
        finally:
            self.close()

    def _deliver(self):
        while (message := self._events.get()) is not None:
            if isinstance(message, threading.Event):
                message.set()
                continue
            try:
                self.notifications[message["method"]](**message.get("params", {}))
            except BaseException:
                log.exception("RPC notification failed: %s", message["method"])
                self.close()
                return

    def wait_notifications(self):
        """Wait for notifications already received before a request response.

        Responses resolve on the reader thread; their caller must not assume
        earlier notifications have finished on the separate delivery thread.
        """
        delivered = threading.Event()
        self._events.put(delivered)
        while not delivered.wait(0.05):
            if self.closed.is_set():
                raise ConnectionError("RPC connection closed")

    def close(self):
        with self._lock:
            if self.closed.is_set():
                return
            self.closed.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("RPC connection closed"))
        self._events.put(None)
        try:
            self.on_close()
        finally:
            try:
                # A sender may already be inside transport.send when closed is
                # set. Finish that write before closing its stream.
                with self._send_lock:
                    self.transport.close()
            finally:
                self._workers.shutdown(wait=False, cancel_futures=True)
                self._close_complete.set()

    def wait_closed(self, timeout: float | None = None) -> None:
        """Wait for transport cleanup, from the owning thread after close()."""
        if not self._close_complete.wait(timeout):
            raise TimeoutError("RPC transport cleanup did not finish")
