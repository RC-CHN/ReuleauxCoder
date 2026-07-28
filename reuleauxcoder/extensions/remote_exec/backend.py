"""Remote relay tool backend implementation."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Mapping
from dataclasses import dataclass, field
import threading
import time
from typing import Any
import uuid

from reuleauxcoder.domain.process import (
    MAX_PROCESS_INPUT_BYTES,
    ProcessCapacityError,
    ProcessChunk,
    ProcessCursor,
    ProcessHandle,
    ProcessOperationUnsupported,
    ProcessResult,
    ProcessSessionNotFound,
    ProcessShutdownReport,
    ProcessSnapshot,
    ProcessState,
    ProcessStreamHandler,
    ProcessStreamMode,
)
from reuleauxcoder.domain.agent.tool_outcome import (
    ToolErrorKind,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolRetentionHint,
    ToolRetentionStrategy,
)
from reuleauxcoder.domain.workspace import (
    WorkspaceEntry,
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceGlobResult,
    WorkspaceListResult,
    WorkspaceSearchMatch,
    WorkspaceSearchResult,
    glob_paths_via_primitives,
    search_text_via_primitives,
)
from reuleauxcoder.extensions.remote_exec.errors import (
    PeerNotFoundError,
    RemoteExecError,
)
from reuleauxcoder.extensions.remote_exec.protocol import (
    ExecToolRequest,
    ToolStreamChunk,
    WorkspaceRequest,
)
from reuleauxcoder.extensions.remote_exec.server import RelayServer
from reuleauxcoder.extensions.tools.backend import ExecutionContext, ToolBackend
from reuleauxcoder.interfaces.events import UIEventBus


class RemoteRelayToolBackend(ToolBackend):
    """Backend that forwards tool execution to a remote peer via the relay server."""

    backend_id = "remote_relay"

    def __init__(
        self,
        relay_server: RelayServer,
        context: ExecutionContext | None = None,
        ui_bus: UIEventBus | None = None,
    ):
        super().__init__(context or ExecutionContext(execution_target="remote"))
        self.relay_server = relay_server
        self.ui_bus = ui_bus
        self.workspace = RemoteWorkspacePort(self)
        self.process = RemoteProcessPort(self)

    def clone_for_scope(self, scope: str) -> "RemoteRelayToolBackend":
        """Rebuild remote adapters while sharing only the relay transport."""
        del scope
        context = ExecutionContext(
            peer_id=self.context.peer_id,
            cwd=self.context.cwd,
            workspace_root=self.context.workspace_root,
            execution_target=self.context.execution_target,
            remote_stream_handler=self.context.remote_stream_handler,
            cancellation_event=None,
        )
        return RemoteRelayToolBackend(
            relay_server=self.relay_server,
            context=context,
            ui_bus=self.ui_bus,
        )

    def resolve_peer_id(self) -> str:
        peer_id = self.context.peer_id
        if peer_id is None:
            peer = self.relay_server.registry.pick_default_peer()
            if peer is None:
                raise WorkspaceError(
                    WorkspaceErrorCode.IO_ERROR,
                    "no remote peer is currently connected",
                )
            peer_id = peer.peer_id
        return peer_id

    def supports_capability(self, capability: str) -> bool:
        try:
            peer = self.relay_server.registry.get(self.resolve_peer_id())
        except WorkspaceError:
            return False
        return bool(
            peer is not None
            and int(peer.meta.get("protocol_version", 1)) >= 2
            and capability in peer.capabilities
        )

    def exec_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        """Execute a tool on the remote peer and return the text result.

        If no peer is explicitly selected, picks the single online peer (MVP).
        """
        return self.exec_tool_outcome(tool_name, args).model_text

    def exec_tool_outcome(self, tool_name: str, args: dict[str, Any]) -> ToolOutcome:
        """Adapt protocol-v1 execution into the canonical Host outcome."""
        peer_id = self.context.peer_id
        if peer_id is None:
            peer = self.relay_server.registry.pick_default_peer()
            if peer is None:
                return _remote_failure("Error: no remote peer is currently connected")
            peer_id = peer.peer_id

        timeout = None
        if tool_name == "shell":
            timeout = args.get("timeout", 120)
        else:
            timeout = 30

        request = ExecToolRequest(
            tool_name=tool_name,
            args=args,
            cwd=self.context.cwd,
            timeout_sec=timeout,
        )

        downstream_stream_handler = self._build_stream_handler(tool_name)
        captured = {"stdout": [], "stderr": []}

        def stream_handler(chunk: ToolStreamChunk) -> None:
            captured.setdefault(chunk.chunk_type, []).append(chunk.data)
            if downstream_stream_handler is not None:
                downstream_stream_handler(chunk)

        try:
            result = self.relay_server.send_exec_request(
                peer_id=peer_id,
                request=request,
                timeout_sec=timeout,
                stream_handler=stream_handler,
            )
        except PeerNotFoundError:
            return _remote_failure(f"Error: peer '{peer_id}' is not online")
        except RemoteExecError as e:
            if e.code == "REMOTE_TIMEOUT" and tool_name == "shell":
                return ToolOutcome(
                    status=ToolOutcomeStatus.TIMED_OUT,
                    content=(
                        f"[system] Remote command timed out after {timeout}s; "
                        "output captured until transport timeout."
                    ),
                    stdout="".join(captured["stdout"]).strip(),
                    stderr="".join(captured["stderr"]).strip(),
                    error_kind=ToolErrorKind.INTERRUPTED,
                    metadata={"remote_error_code": e.code},
                    retention_hint=ToolRetentionHint(
                        strategy=ToolRetentionStrategy.TAIL
                    ),
                )
            return _remote_failure(
                f"Error [{e.code}]: {e.message}", metadata={"remote_error_code": e.code}
            )
        except Exception as e:
            return _remote_failure(f"Error executing {tool_name} remotely: {e}")

        if result.ok:
            return ToolOutcome(content=result.result, metadata=dict(result.meta))
        error_msg = result.error_message or "unknown remote error"
        return _remote_failure(
            f"Error [{result.error_code or 'REMOTE_TOOL_ERROR'}]: {error_msg}",
            metadata={
                **dict(result.meta),
                "remote_error_code": result.error_code or "REMOTE_TOOL_ERROR",
            },
        )

    def _build_stream_handler(self, tool_name: str):
        remote_stream_handler = getattr(self.context, "remote_stream_handler", None)
        if tool_name != "shell" and remote_stream_handler is None:
            return None
        if (
            tool_name == "shell"
            and self.ui_bus is None
            and not callable(remote_stream_handler)
        ):
            return None

        def _handle(chunk: ToolStreamChunk) -> None:
            if not chunk.data:
                return
            if callable(remote_stream_handler):
                try:
                    remote_stream_handler(tool_name, chunk)
                except Exception:
                    pass
            elif tool_name == "shell" and self.ui_bus is not None:
                self.ui_bus.emit_remote_stream(
                    tool_name=tool_name,
                    stream=chunk.chunk_type,
                    chunk=chunk.data,
                )

        return _handle


def _remote_failure(
    message: str, *, metadata: dict[str, object] | None = None
) -> ToolOutcome:
    return ToolOutcome(
        status=ToolOutcomeStatus.FAILED,
        content=message,
        error_kind=ToolErrorKind.EXECUTION,
        metadata=metadata or {},
    )


class RemoteWorkspacePort:
    """WorkspacePort adapter backed by generic peer primitives."""

    def __init__(self, backend: RemoteRelayToolBackend):
        self.backend = backend
        self.root = Path(backend.context.workspace_root or backend.context.cwd or "/")

    def resolve(self, path: str | Path) -> Path:
        value = Path(path)
        return value if value.is_absolute() else self.root / value

    def _request(self, operation: str, **args: Any) -> dict[str, Any]:
        try:
            result = self.backend.relay_server.send_workspace_request(
                self.backend.resolve_peer_id(),
                WorkspaceRequest(
                    operation=operation,
                    args=args,
                    cwd=self.backend.context.cwd,
                ),
            )
        except WorkspaceError:
            raise
        except Exception as error:
            raise WorkspaceError(WorkspaceErrorCode.IO_ERROR, str(error)) from error
        if not result.ok:
            try:
                code = WorkspaceErrorCode(result.error_code or "io_error")
            except ValueError:
                code = WorkspaceErrorCode.IO_ERROR
            raise WorkspaceError(
                code, result.error_message or "remote workspace operation failed"
            )
        return result.data

    def read_text(self, path: str | Path) -> str:
        return str(self._request("fs.read_text", path=str(path)).get("content", ""))

    def stat_entry(self, path: str | Path) -> WorkspaceEntry:
        item = self._request("fs.stat", path=str(path))["entry"]
        return _workspace_entry(item)

    def write_text_atomic(self, path: str | Path, content: str) -> str:
        return str(
            self._request("fs.write_text_atomic", path=str(path), content=content).get(
                "old_content", ""
            )
        )

    def replace_exact_atomic(
        self, path: str | Path, old: str, new: str
    ) -> tuple[str, str]:
        data = self._request(
            "fs.replace_exact_atomic", path=str(path), old=old, new=new
        )
        return str(data.get("old_content", "")), str(data.get("new_content", ""))

    def list_entries(
        self,
        path: str | Path,
        *,
        recursive: bool = False,
        include_hidden: bool = True,
        max_entries: int = 10_000,
    ) -> WorkspaceListResult:
        data = self._request(
            "fs.list",
            path=str(path),
            recursive=recursive,
            include_hidden=include_hidden,
            max_entries=max_entries,
        )
        entries = tuple(_workspace_entry(item) for item in data.get("entries", []))
        return WorkspaceListResult(entries, truncated=bool(data.get("truncated")))

    def search_text(
        self,
        pattern: str,
        path: str | Path,
        *,
        include: str | None = None,
        exclude_dirs: tuple[str, ...] = (),
        max_files: int = 5_000,
        max_matches: int = 200,
    ) -> WorkspaceSearchResult:
        if self.backend.supports_capability(
            "workspace.fs.search_text"
        ) and _peer_literal_search_safe(pattern, include):
            data = self._request(
                "fs.search_text",
                path=str(path),
                pattern=pattern,
                literal=True,
                include=include,
                exclude_dirs=list(exclude_dirs),
                max_files=max_files,
                max_matches=max_matches,
            )
            return WorkspaceSearchResult(
                matches=tuple(
                    WorkspaceSearchMatch(
                        path=str(item["path"]),
                        line_number=int(item["line_number"]),
                        line=str(item["line"]),
                    )
                    for item in data.get("matches", [])
                ),
                truncated=bool(data.get("truncated")),
            )
        return search_text_via_primitives(
            self,
            pattern,
            path,
            include=include,
            exclude_dirs=exclude_dirs,
            max_files=max_files,
            max_matches=max_matches,
        )

    def glob_paths(
        self,
        pattern: str,
        path: str | Path,
        *,
        max_entries: int = 20_000,
        max_matches: int = 100,
    ) -> WorkspaceGlobResult:
        if self.backend.supports_capability("workspace.fs.glob") and _peer_glob_safe(
            pattern
        ):
            data = self._request(
                "fs.glob",
                path=str(path),
                pattern=pattern,
                max_entries=max_entries,
                max_matches=max_matches,
            )
            return WorkspaceGlobResult(
                entries=tuple(
                    _workspace_entry(item) for item in data.get("entries", [])
                ),
                match_count=int(data.get("match_count", 0)),
                listing_truncated=bool(data.get("listing_truncated")),
            )
        return glob_paths_via_primitives(
            self,
            pattern,
            path,
            max_entries=max_entries,
            max_matches=max_matches,
        )


def _workspace_entry(item: dict[str, Any]) -> WorkspaceEntry:
    return WorkspaceEntry(
        path=str(item["path"]),
        relative_path=str(item["relative_path"]),
        name=str(item["name"]),
        is_file=bool(item["is_file"]),
        is_dir=bool(item["is_dir"]),
        size=int(item["size"]),
        mtime=float(item["mtime"]),
        mode=int(item["mode"]),
    )


def _peer_literal_search_safe(pattern: str, include: str | None) -> bool:
    regex_metacharacters = frozenset(r".\^$*+?{}[]|()")
    if any(character in regex_metacharacters for character in pattern):
        return False
    if include is None:
        return True
    return (
        "/" not in include
        and "\\" not in include
        and "[" not in include
        and "]" not in include
    )


def _peer_glob_safe(pattern: str) -> bool:
    return "\\" not in pattern and "[" not in pattern and "]" not in pattern


class RemoteProcessPort:
    """ProcessPort over one exact peer's resumable process primitives."""

    backend_name = "remote"

    def __init__(self, backend: RemoteRelayToolBackend):
        self.backend = backend
        self._entries: dict[str, _RemoteProcessEntry] = {}
        self._lock = threading.RLock()
        self._closing = False

    def start(
        self,
        command: str,
        *,
        cwd: str,
        runtime_timeout: int,
        tty: bool = False,
        env: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        stream_handler: ProcessStreamHandler | None = None,
    ) -> ProcessHandle:
        if env:
            raise ProcessOperationUnsupported(
                "the connected remote peer does not accept process environment overrides"
            )
        if tty:
            raise ProcessOperationUnsupported(
                "the connected remote peer does not advertise PTY support"
            )
        with self._lock:
            if self._closing:
                raise RuntimeError("remote process port is shutting down")
        peer_id = self.backend.resolve_peer_id()
        process_id = f"proc_{uuid.uuid4().hex}"
        entry = _RemoteProcessEntry(
            session_id=process_id,
            process_id=process_id,
            peer_id=peer_id,
            stream_mode=ProcessStreamMode.PIPE,
            runtime_timeout=runtime_timeout,
            started_at=time.time(),
            stream_handler=stream_handler,
            start_args={
                "process_id": process_id,
                "idempotency_key": idempotency_key or process_id,
                "command": command,
                "cwd": cwd,
                "tty": False,
                "deadline_unix_ms": int(
                    (time.time() + runtime_timeout) * 1000
                ),
            },
        )
        try:
            data = self._request(
                entry,
                "process.start",
                entry.start_args,
                timeout_sec=30,
            )
            entry.process_id = str(data.get("process_id", process_id))
            entry.start_confirmed = True
        except RemoteExecError as error:
            if not _ambiguous_remote_start_error(error):
                if error.code in {
                    "path_outside_workspace",
                    "invalid_path",
                    "not_found",
                }:
                    raise FileNotFoundError(error.message) from error
                if error.code == "REMOTE_CAPABILITY_UNAVAILABLE":
                    raise ProcessOperationUnsupported(error.message) from error
                raise RuntimeError(str(error)) from error
            # Delivery may have happened before the transport failed. Keep the
            # reservation and expose unknown rather than losing a real process.
            entry.state = ProcessState.UNKNOWN
            entry.termination_reason = "transport_unknown"
        with self._lock:
            if self._closing:
                try:
                    self._terminate_entry(entry, reason="shutdown")
                finally:
                    raise RuntimeError("remote process port is shutting down")
            self._entries[entry.session_id] = entry
        return ProcessHandle(entry.session_id, entry.stream_mode)

    def poll(
        self,
        session_id: str,
        *,
        cursor: ProcessCursor | None = None,
        wait_ms: int = 0,
    ) -> ProcessSnapshot:
        entry = self._lookup(session_id)
        current = cursor or ProcessCursor()
        if not entry.start_confirmed:
            try:
                data = self._request(
                    entry,
                    "process.start",
                    entry.start_args,
                    timeout_sec=30,
                )
                confirmed_id = str(data.get("process_id", entry.process_id))
                with entry.lock:
                    entry.process_id = confirmed_id
                    entry.start_confirmed = True
                    if entry.state is not ProcessState.EXITED:
                        entry.state = ProcessState.RUNNING
                        entry.termination_reason = None
            except (PeerNotFoundError, RemoteExecError):
                return self._snapshot(entry, current)
        try:
            data = self._request(
                entry,
                "process.poll",
                {
                    "process_id": entry.process_id,
                    "stdout_offset": current.stdout_offset,
                    "stderr_offset": current.stderr_offset,
                    "wait_ms": wait_ms,
                },
                timeout_sec=max(5, int(wait_ms / 1000) + 2),
            )
        except (PeerNotFoundError, RemoteExecError):
            with entry.lock:
                if entry.state is not ProcessState.EXITED:
                    entry.state = ProcessState.UNKNOWN
                    entry.termination_reason = "transport_unknown"
            return self._snapshot(entry, current)

        stdout = str(data.get("stdout", ""))
        stderr = str(data.get("stderr", ""))
        response_stdout_offset = int(
            data.get("stdout_offset", current.stdout_offset + len(stdout))
        )
        response_stderr_offset = int(
            data.get("stderr_offset", current.stderr_offset + len(stderr))
        )
        with entry.lock:
            entry.stdout_offset = max(entry.stdout_offset, response_stdout_offset)
            entry.stderr_offset = max(entry.stderr_offset, response_stderr_offset)
            entry.total_stdout_bytes = max(
                entry.total_stdout_bytes,
                int(data.get("total_stdout_bytes", response_stdout_offset)),
            )
            entry.total_stderr_bytes = max(
                entry.total_stderr_bytes,
                int(data.get("total_stderr_bytes", response_stderr_offset)),
            )
            entry.output_truncated = entry.output_truncated or bool(
                data.get("output_truncated", False)
            )
            entry.output_decode_replaced = entry.output_decode_replaced or bool(
                data.get("output_decode_replaced", False)
            )
            state_value = data.get("state")
            done = bool(data.get("done"))
            if entry.state is not ProcessState.EXITED:
                if state_value == "unknown":
                    entry.state = ProcessState.UNKNOWN
                elif state_value == "exited" or done:
                    entry.state = ProcessState.EXITED
                    entry.exit_code = _optional_int(data.get("exit_code"))
                    entry.termination_reason = _remote_termination_reason(data)
                    entry.finished_at = _milliseconds_to_seconds(
                        data.get("finished_unix_ms")
                    ) or time.time()
                else:
                    entry.state = ProcessState.RUNNING
            if entry.state is not ProcessState.EXITED:
                entry.exit_code = None
                entry.finished_at = None
                if entry.state is ProcessState.RUNNING:
                    entry.termination_reason = None
            started_at = _milliseconds_to_seconds(data.get("started_unix_ms"))
            if started_at is not None:
                entry.started_at = started_at
        if stdout and entry.stream_handler is not None:
            entry.stream_handler(ProcessChunk("stdout", stdout))
        if stderr and entry.stream_handler is not None:
            entry.stream_handler(ProcessChunk("stderr", stderr))
        return self._snapshot(
            entry,
            ProcessCursor(response_stdout_offset, response_stderr_offset),
            stdout=stdout,
            stderr=stderr,
        )

    def write_input(self, session_id: str, data: str) -> int:
        if len(data.encode("utf-8")) > MAX_PROCESS_INPUT_BYTES:
            raise ProcessCapacityError(
                "process input exceeds the 64 KiB per-write limit"
            )
        entry = self._lookup(session_id)
        if entry.stream_mode is not ProcessStreamMode.PTY:
            raise ProcessOperationUnsupported(
                f"session '{session_id}' uses pipe mode; stdin is closed"
            )
        result = self._request(
            entry,
            "process.input",
            {"process_id": entry.process_id, "data": data, "close": False},
        )
        return int(result.get("bytes_written", 0))

    def interrupt(self, session_id: str) -> ProcessSnapshot:
        entry = self._lookup(session_id)
        if entry.state is ProcessState.EXITED:
            return self._snapshot(entry, ProcessCursor())
        if not self._peer_supports(entry.peer_id, "process.interrupt"):
            raise ProcessOperationUnsupported(
                "the connected remote peer does not support soft process interrupts"
            )
        try:
            self._request(
                entry,
                "process.interrupt",
                {"process_id": entry.process_id},
            )
        except (PeerNotFoundError, RemoteExecError):
            with entry.lock:
                if entry.state is not ProcessState.EXITED:
                    entry.state = ProcessState.UNKNOWN
                    entry.termination_reason = "transport_unknown"
        return self._snapshot(entry, ProcessCursor())

    def terminate(
        self,
        session_id: str,
        *,
        reason: str = "terminated",
    ) -> ProcessSnapshot:
        entry = self._lookup(session_id)
        self._terminate_entry(entry, reason=reason)
        return self._snapshot(entry, ProcessCursor())

    def _terminate_entry(self, entry: "_RemoteProcessEntry", *, reason: str) -> None:
        if entry.state is ProcessState.EXITED:
            return
        operation = (
            "process.terminate"
            if self._peer_supports(entry.peer_id, "process.terminate")
            else "process.cancel"
        )
        try:
            data = self._request(
                entry,
                operation,
                {"process_id": entry.process_id, "reason": reason},
            )
            if bool(data.get("done", operation == "process.cancel")):
                with entry.lock:
                    entry.state = ProcessState.EXITED
                    entry.exit_code = _optional_int(data.get("exit_code"))
                    entry.termination_reason = str(
                        data.get("termination_reason") or reason
                    )
                    entry.finished_at = time.time()
        except (PeerNotFoundError, RemoteExecError):
            with entry.lock:
                if entry.state is not ProcessState.EXITED:
                    entry.state = ProcessState.UNKNOWN
                    entry.termination_reason = "transport_unknown"

    def release(self, session_id: str) -> None:
        entry = self._lookup(session_id)
        if entry.state is not ProcessState.EXITED:
            raise RuntimeError(
                f"cannot release unresolved process session '{session_id}'"
            )
        if self._peer_supports(entry.peer_id, "process.release"):
            try:
                self._request(
                    entry,
                    "process.release",
                    {"process_id": entry.process_id},
                )
            except (PeerNotFoundError, RemoteExecError):
                pass
        with self._lock:
            self._entries.pop(session_id, None)

    def shutdown(self, *, grace_seconds: float = 0.5) -> ProcessShutdownReport:
        del grace_seconds
        with self._lock:
            self._closing = True
            entries = tuple(self._entries.values())
        live = [entry for entry in entries if entry.state is ProcessState.RUNNING]
        unknown = [entry for entry in entries if entry.state is ProcessState.UNKNOWN]
        terminal = [entry for entry in entries if entry.state is ProcessState.EXITED]
        unresolved = [*live, *unknown]
        for entry in unresolved:
            self._terminate_entry(entry, reason="shutdown")
        with self._lock:
            self._entries.clear()
        return ProcessShutdownReport(
            total=len(entries),
            already_exited=len(terminal),
            terminated=len(unresolved),
            unknown=len(unknown),
        )

    def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout: int,
        cancellation_event=None,
        stream_handler=None,
    ) -> ProcessResult:
        handle = self.start(
            command,
            cwd=cwd,
            runtime_timeout=timeout,
            tty=False,
            stream_handler=stream_handler,
        )
        cursor = ProcessCursor()
        stdout: list[str] = []
        stderr: list[str] = []
        unknown_deadline = time.monotonic() + timeout + 5
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                terminated = self.terminate(
                    handle.session_id,
                    reason="cancelled",
                )
                return ProcessResult(
                    stdout="".join(stdout),
                    stderr="".join(stderr),
                    cancelled=True,
                    output_decode_replaced=terminated.output_decode_replaced,
                )
            snapshot = self.poll(handle.session_id, cursor=cursor, wait_ms=50)
            cursor = snapshot.cursor
            stdout.append(snapshot.stdout)
            stderr.append(snapshot.stderr)
            if snapshot.state is ProcessState.RUNNING:
                continue
            if snapshot.state is ProcessState.UNKNOWN:
                if time.monotonic() < unknown_deadline:
                    time.sleep(0.05)
                    continue
                self.terminate(handle.session_id, reason="transport_unknown")
                return ProcessResult(
                    stdout="".join(stdout),
                    stderr="".join(stderr),
                    output_truncated=snapshot.output_truncated,
                    output_decode_replaced=snapshot.output_decode_replaced,
                    state_unknown=True,
                )
            result = ProcessResult(
                stdout="".join(stdout),
                stderr="".join(stderr),
                exit_code=snapshot.exit_code,
                timed_out=snapshot.termination_reason == "timeout",
                cancelled=snapshot.termination_reason == "cancelled",
                output_truncated=snapshot.output_truncated,
                output_decode_replaced=snapshot.output_decode_replaced,
            )
            self.release(handle.session_id)
            return result

    def _request(
        self,
        entry: "_RemoteProcessEntry",
        operation: str,
        args: dict[str, Any],
        *,
        timeout_sec: int = 30,
    ) -> dict[str, Any]:
        result = self.backend.relay_server.send_workspace_request(
            entry.peer_id,
            WorkspaceRequest(
                operation=operation,
                args=args,
                cwd=self.backend.context.cwd,
                timeout_sec=timeout_sec,
            ),
            timeout_sec=timeout_sec,
        )
        if not result.ok:
            raise RemoteExecError(
                result.error_code or "REMOTE_PROCESS_ERROR",
                result.error_message or "remote process operation failed",
            )
        return result.data

    def _peer_supports(self, peer_id: str, capability: str) -> bool:
        peer = self.backend.relay_server.registry.get(peer_id)
        if peer is None:
            return False
        version = int(peer.meta.get("protocol_version", 1))
        return version >= 2 and capability in peer.capabilities

    def _lookup(self, session_id: str) -> "_RemoteProcessEntry":
        with self._lock:
            entry = self._entries.get(session_id)
        if entry is None:
            raise ProcessSessionNotFound(
                f"process session '{session_id}' was not found"
            )
        return entry

    def _snapshot(
        self,
        entry: "_RemoteProcessEntry",
        cursor: ProcessCursor,
        *,
        stdout: str = "",
        stderr: str = "",
    ) -> ProcessSnapshot:
        with entry.lock:
            return ProcessSnapshot(
                session_id=entry.session_id,
                state=entry.state,
                stream_mode=entry.stream_mode,
                backend=self.backend_name,
                stdout=stdout,
                stderr=stderr,
                cursor=cursor,
                exit_code=entry.exit_code,
                termination_reason=entry.termination_reason,
                started_at=entry.started_at,
                finished_at=entry.finished_at,
                runtime_timeout_seconds=entry.runtime_timeout,
                output_truncated=entry.output_truncated,
                output_decode_replaced=entry.output_decode_replaced,
                total_stdout_bytes=entry.total_stdout_bytes,
                total_stderr_bytes=entry.total_stderr_bytes,
            )


@dataclass(slots=True)
class _RemoteProcessEntry:
    session_id: str
    process_id: str
    peer_id: str
    stream_mode: ProcessStreamMode
    runtime_timeout: int
    started_at: float
    stream_handler: ProcessStreamHandler | None = None
    start_args: dict[str, Any] = field(default_factory=dict)
    start_confirmed: bool = False
    state: ProcessState = ProcessState.RUNNING
    stdout_offset: int = 0
    stderr_offset: int = 0
    total_stdout_bytes: int = 0
    total_stderr_bytes: int = 0
    exit_code: int | None = None
    termination_reason: str | None = None
    finished_at: float | None = None
    output_truncated: bool = False
    output_decode_replaced: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _milliseconds_to_seconds(value: object) -> float | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        return None
    return milliseconds / 1000


def _remote_termination_reason(data: dict[str, Any]) -> str:
    reason = data.get("termination_reason")
    if isinstance(reason, str) and reason:
        return reason
    if bool(data.get("timed_out")):
        return "timeout"
    if bool(data.get("cancelled")):
        return "cancelled"
    return "exit"


def _ambiguous_remote_start_error(error: RemoteExecError) -> bool:
    return error.code in {
        "PEER_DISCONNECTED",
        "PEER_NOT_FOUND",
        "REMOTE_TIMEOUT",
    }
