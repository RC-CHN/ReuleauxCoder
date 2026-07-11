"""Remote relay tool backend implementation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
from reuleauxcoder.interfaces.events import UIEventBus, UIEventKind


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

    def exec_tool(self, tool_name: str, args: dict[str, Any]) -> str:
        """Execute a tool on the remote peer and return the text result.

        If no peer is explicitly selected, picks the single online peer (MVP).
        """
        peer_id = self.context.peer_id
        if peer_id is None:
            peer = self.relay_server.registry.pick_default_peer()
            if peer is None:
                return "Error: no remote peer is currently connected"
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

        stream_handler = self._build_stream_handler(tool_name)

        try:
            result = self.relay_server.send_exec_request(
                peer_id=peer_id,
                request=request,
                timeout_sec=timeout,
                stream_handler=stream_handler,
            )
        except PeerNotFoundError:
            return f"Error: peer '{peer_id}' is not online"
        except RemoteExecError as e:
            return f"Error [{e.code}]: {e.message}"
        except Exception as e:
            return f"Error executing {tool_name} remotely: {e}"

        if result.ok:
            return result.result
        error_msg = result.error_message or "unknown remote error"
        return f"Error [{result.error_code or 'REMOTE_TOOL_ERROR'}]: {error_msg}"

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
                self.ui_bus.info(
                    "",
                    kind=UIEventKind.REMOTE,
                    remote_stream=True,
                    tool_name=tool_name,
                    stream=chunk.chunk_type,
                    chunk=chunk.data,
                )

        return _handle


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
