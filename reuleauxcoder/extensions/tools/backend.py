"""Backend markers and shared runtime context for tools."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
import threading

from reuleauxcoder.domain.workspace import WorkspacePort
from reuleauxcoder.domain.workspace import WorkspaceRevision
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
        self._stream_handler_local = threading.local()
        self._workspace_revision_local = threading.local()

    @contextmanager
    def stream_handler_scope(self, handler: object | None) -> Iterator[None]:
        """Bind one tool-call stream sink without mutating shared context."""
        missing = object()
        previous = getattr(self._stream_handler_local, "handler", missing)
        self._stream_handler_local.handler = handler
        try:
            yield
        finally:
            if previous is missing:
                del self._stream_handler_local.handler
            else:
                self._stream_handler_local.handler = previous

    def current_stream_handler(self) -> object | None:
        return getattr(self._stream_handler_local, "handler", None)

    @contextmanager
    def workspace_revision_scope(
        self, revision: WorkspaceRevision | None
    ) -> Iterator[None]:
        """Bind the prepared/approved file revision to one executing call."""
        missing = object()
        previous = getattr(self._workspace_revision_local, "revision", missing)
        self._workspace_revision_local.revision = revision
        try:
            yield
        finally:
            if previous is missing:
                del self._workspace_revision_local.revision
            else:
                self._workspace_revision_local.revision = previous

    def current_workspace_revision(self) -> WorkspaceRevision | None:
        return getattr(self._workspace_revision_local, "revision", None)

    def clone_for_scope(self, scope: str) -> "ToolBackend":
        """Materialize a backend adapter for an independent Agent scope."""
        del scope
        context = replace(self.context, cancellation_event=None)
        return type(self)(context=context)


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
        cwd = effective_context.cwd or Path.cwd()
        root = effective_context.workspace_root or Path(cwd).anchor or Path("/")
        super().__init__(
            effective_context,
            workspace=workspace or LocalWorkspacePort(root, cwd=cwd),
            process=process or LocalProcessPort(),
        )
