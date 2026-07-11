"""Backend markers and shared runtime context for tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading

from reuleauxcoder.domain.workspace import WorkspacePort
from reuleauxcoder.domain.process import ProcessPort
from reuleauxcoder.infrastructure.process import LocalProcessPort
from reuleauxcoder.infrastructure.workspace import LocalWorkspacePort


@dataclass(slots=True)
class ExecutionContext:
    """Runtime execution context for tool backends."""

    peer_id: str | None = None
    cwd: str | None = None
    workspace_root: str | None = None
    execution_target: str = "local"
    remote_stream_handler: object | None = None
    cancellation_event: threading.Event | None = None


class ToolBackend:
    """Base backend marker used by tool-local backend handlers."""

    backend_id = "base"

    def __init__(
        self,
        context: ExecutionContext | None = None,
        *,
        workspace: WorkspacePort | None = None,
        process: ProcessPort | None = None,
    ):
        self.context = context or ExecutionContext()
        self.workspace = workspace
        self.process = process


class LocalToolBackend(ToolBackend):
    """Default backend representing local in-process execution."""

    backend_id = "local"

    def __init__(
        self,
        context: ExecutionContext | None = None,
        *,
        workspace: WorkspacePort | None = None,
        process: ProcessPort | None = None,
    ):
        effective_context = context or ExecutionContext()
        root = effective_context.workspace_root or Path("/")
        cwd = effective_context.cwd or Path.cwd()
        super().__init__(
            effective_context,
            workspace=workspace or LocalWorkspacePort(root, cwd=cwd),
            process=process or LocalProcessPort(),
        )
