"""Remote relay tool backend implementation."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any
import uuid

from reuleauxcoder.domain.process import ProcessChunk, ProcessResult
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
    WorkspaceListResult,
    WorkspaceSearchResult,
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

    def exec_tool_outcome(
        self, tool_name: str, args: dict[str, Any]
    ) -> ToolOutcome:
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
            if tool_name == "shell" and self.ui_bus is not None:
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
        self.root = Path(
            backend.context.workspace_root or backend.context.cwd or "/"
        )

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
            raise WorkspaceError(
                WorkspaceErrorCode.IO_ERROR, str(error)
            ) from error
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
        entries = tuple(
            WorkspaceEntry(
                path=str(item["path"]),
                relative_path=str(item["relative_path"]),
                name=str(item["name"]),
                is_file=bool(item["is_file"]),
                is_dir=bool(item["is_dir"]),
                size=int(item["size"]),
                mtime=float(item["mtime"]),
                mode=int(item["mode"]),
            )
            for item in data.get("entries", [])
        )
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
        return search_text_via_primitives(
            self,
            pattern,
            path,
            include=include,
            exclude_dirs=exclude_dirs,
            max_files=max_files,
            max_matches=max_matches,
        )


class RemoteProcessPort:
    """ProcessPort using start/poll/cancel peer primitives."""

    def __init__(self, backend: RemoteRelayToolBackend):
        self.backend = backend

    def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout: int,
        cancellation_event=None,
        stream_handler=None,
    ) -> ProcessResult:
        process_id = uuid.uuid4().hex
        deadline_ms = int((time.time() + timeout) * 1000)
        data = self.backend.workspace._request(
            "process.start",
            process_id=process_id,
            idempotency_key=process_id,
            command=command,
            cwd=cwd,
            deadline_unix_ms=deadline_ms,
        )
        process_id = str(data.get("process_id", process_id))
        stdout_offset = 0
        stderr_offset = 0
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                self.cancel(process_id)
                return ProcessResult(cancelled=True)
            state = self.backend.workspace._request(
                "process.poll",
                process_id=process_id,
                stdout_offset=stdout_offset,
                stderr_offset=stderr_offset,
            )
            stdout = str(state.get("stdout", ""))
            stderr = str(state.get("stderr", ""))
            stdout_offset = int(state.get("stdout_offset", stdout_offset))
            stderr_offset = int(state.get("stderr_offset", stderr_offset))
            if stdout and stream_handler is not None:
                stream_handler(ProcessChunk("stdout", stdout))
            if stderr and stream_handler is not None:
                stream_handler(ProcessChunk("stderr", stderr))
            if state.get("done"):
                return ProcessResult(
                    stdout=str(state.get("stdout_all", "")),
                    stderr=str(state.get("stderr_all", "")),
                    exit_code=int(state.get("exit_code", 0)),
                    timed_out=bool(state.get("timed_out")),
                    cancelled=bool(state.get("cancelled")),
                )
            time.sleep(0.05)

    def write_input(
        self, process_id: str, data: str, *, close: bool = False
    ) -> int:
        result = self.backend.workspace._request(
            "process.input",
            process_id=process_id,
            data=data,
            close=close,
        )
        return int(result.get("bytes_written", 0))

    def cancel(self, process_id: str) -> None:
        self.backend.workspace._request("process.cancel", process_id=process_id)
