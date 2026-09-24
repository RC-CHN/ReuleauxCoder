"""Frontend runtime API; all operations cross the JSON-RPC codec."""

from __future__ import annotations

import threading
import time
import queue
import base64
from pathlib import Path
from concurrent.futures import Future

from reuleauxcoder.app.rpc.codec import encode, decode
from reuleauxcoder.app.rpc.models import RuntimeSnapshot
from reuleauxcoder.app.interaction_contracts import cancelled_response
from reuleauxcoder.infrastructure.rpc.peer import RpcError, RpcPeer


class RuntimeClient:
    def __init__(
        self, peer: RpcPeer, ui_bus, interactor, *, foreground_interactions=False
    ):
        self.peer, self.ui_bus, self.interactor = peer, ui_bus, interactor
        self.state = RuntimeSnapshot()
        self._condition = threading.Condition()
        self._foreground_interactions = foreground_interactions
        self._interaction_queue = queue.Queue()
        self._active_interactions = {}
        self._cancelled_interactions: dict[str, None] = {}
        self._failure = None
        self.on_state = lambda state: None
        self.on_completed = lambda result: None
        self.on_command = lambda text: None
        peer.on_close = self._disconnected
        peer.notifications.update(
            {
                "runtime.event": lambda event, session_generation=None: ui_bus.emit(
                    decode(event)
                ),
                "runtime.state": lambda state: self._state(decode(state)),
                "runtime.completed": lambda result: self.on_completed(decode(result)),
                "runtime.command": lambda text: self.on_command(text),
                "runtime.failed": self._failed,
                "interaction.cancel": self._cancel_interaction,
            }
        )
        peer.methods["interaction.request"] = self._interact

    def initialize(self, profile, *, activate=True):
        self.info = decode(
            self.peer.request(
                "initialize", {"profile": encode(profile), "version": 1}, timeout=30
            )
        )
        self.catalog = self.info["catalog"]
        self._state(self.info["state"])
        for event in self.info["runtime_events"]:
            self.ui_bus.emit_runtime(event)
        if activate:
            self.ready()
        return self.info

    def ready(self):
        if self.info.get("goals") and not self.info.get("host_mode"):
            self._state(decode(self.peer.request("runtime.ready")))

    def _state(self, state):
        with self._condition:
            if state.revision <= self.state.revision:
                return
            self.state = state
            self._condition.notify_all()
        self.on_state(state)

    def submit(self, value):
        self._failure = None
        admission = decode(
            self.peer.request("runtime.submit", {"value": encode(value)})
        )
        self._state(admission.state)
        return admission

    def attach_image(self, path: str):
        """Read a frontend-local path; the backend receives bounded chunks, never a path."""
        if not self.info.get("image_uploads"):
            raise ValueError("Backend does not support image attachments")
        path = Path(path).expanduser()
        state = self.state
        with path.open("rb") as stream:
            upload = self.peer.request(
                "images.begin",
                {
                    "session_id": state.session_id,
                    "session_generation": state.session_generation,
                    "name": path.name,
                    "size_bytes": path.stat().st_size,
                },
            )
            try:
                offset = 0
                while chunk := stream.read(upload["chunk_bytes"]):
                    offset = self.peer.request(
                        "images.append",
                        {
                            "upload_id": upload["upload_id"],
                            "offset": offset,
                            "data": base64.b64encode(chunk).decode("ascii"),
                        },
                    )
                image = decode(
                    self.peer.request(
                        "images.complete", {"upload_id": upload["upload_id"]}
                    )
                )
                if (state.session_id, state.session_generation) != (
                    self.state.session_id,
                    self.state.session_generation,
                ):
                    raise ValueError(
                        "Session changed during image upload; attach it again"
                    )
                return image
            finally:
                self.peer.request("images.cancel", {"upload_id": upload["upload_id"]})

    def build_panel(self, payload):
        return decode(self.peer.request("view.panel", {"payload": encode(payload)}))

    def interrupt(self):
        return self.peer.request("runtime.interrupt")

    def admit_steering(self, text):
        return self.peer.request("runtime.admit_steering", {"text": text})

    def stop(self):
        return self.peer.request("runtime.stop")

    def resize(self, rows, columns):
        self.peer.notify("runtime.resize", {"rows": rows, "columns": columns})

    def refresh(self):
        params = (
            {"known_revision": self.state.revision}
            if self.info.get("conditional_snapshots")
            else {}
        )
        result = self.peer.request("runtime.snapshot", params, timeout=5)
        if result is not None:
            self._state(decode(result))

    def report_runtime_issue(self, phase, error_type, ref, count=1, **route):
        return self.peer.request(
            "runtime.report_issue",
            {
                "phase": phase,
                "error_type": error_type,
                "ref": ref,
                "count": count,
                **route,
            },
        )

    def record_performance(
        self, category, name, elapsed_ms, *, status="ok", attributes=None
    ):
        self.peer.notify(
            "runtime.record_performance",
            {
                "category": category,
                "name": name,
                "elapsed_ms": elapsed_ms,
                "status": status,
                "attributes": attributes,
            },
        )

    @property
    def has_pending_interactions(self):
        return not self._interaction_queue.empty()

    def pump_interactions(self):
        while not self._interaction_queue.empty():
            kind, request, future = self._interaction_queue.get_nowait()
            if future.done():
                continue
            try:
                response = getattr(self.interactor, kind)(request)
                with self._condition:
                    if not future.done():
                        future.set_result(response)
            except BaseException as error:
                with self._condition:
                    if not future.done():
                        future.set_exception(error)

    def wait_idle(self, *, pump=lambda: None):
        while True:
            self.pump_interactions()
            pump()
            with self._condition:
                if self.peer.closed.is_set():
                    raise ConnectionError("Backend disconnected")
                idle = not self.state.running
                if not idle:
                    self._condition.wait(0.05)
            if idle:
                self.peer.wait_notifications()
                with self._condition:
                    if self.state.running:
                        continue
                    if self._failure is not None:
                        raise self._failure
                    return

    def _interact(self, kind, request, timeout_seconds=None):
        if kind not in ("confirm", "choose_one", "input_text", "review"):
            raise ValueError("Unknown interaction kind")
        request = decode(request)
        if timeout_seconds is not None:
            request.deadline = time.monotonic() + timeout_seconds
        future = Future()
        with self._condition:
            if self.peer.closed.is_set():
                raise ConnectionError("Backend disconnected")
            if request.request_id in self._cancelled_interactions:
                self._cancelled_interactions.pop(request.request_id)
                return encode(cancelled_response(request, "interaction cancelled"))
            self._active_interactions[request.request_id] = (request, future)
        try:
            if self._foreground_interactions:
                self._interaction_queue.put((kind, request, future))
                return encode(future.result())
            response = getattr(self.interactor, kind)(request)
            with self._condition:
                return encode(future.result() if future.done() else response)
        finally:
            with self._condition:
                self._active_interactions.pop(request.request_id, None)

    def _failed(self, error_type, message):
        self._failure = RpcError(-32000, f"{error_type}: {message}")

    def _cancel_interaction(self, request_id):
        with self._condition:
            active = self._active_interactions.get(request_id)
            if active is None:
                self._cancelled_interactions[request_id] = None
                if len(self._cancelled_interactions) > 1024:
                    self._cancelled_interactions.pop(next(iter(self._cancelled_interactions)))
                return
            request, future = active
            request.deadline = time.monotonic()
            if not future.done():
                future.set_result(cancelled_response(request, "interaction cancelled"))
        self.interactor.cancel(request_id)

    def _disconnected(self):
        with self._condition:
            active = tuple(self._active_interactions.items())
            for _, (request, future) in active:
                request.deadline = time.monotonic()
                if not future.done():
                    future.set_exception(ConnectionError("Backend disconnected"))
            self._condition.notify_all()
        for request_id, _ in active:
            self.interactor.cancel(request_id)

    def shutdown(self):
        # A slow durable save must finish before its process owner can tear down.
        result = self.peer.request("runtime.shutdown")
        self.refresh()
        return result

    def close(self):
        self.peer.close()
