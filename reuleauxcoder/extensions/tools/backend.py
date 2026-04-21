"""Backend markers and shared runtime context for tools."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ExecutionContext:
    """Runtime execution context for tool backends."""

    peer_id: str | None = None
    cwd: str | None = None
    workspace_root: str | None = None
    execution_target: str = "local"
    remote_stream_handler: object | None = None


class ToolBackend:
    """Base backend marker used by tool-local backend handlers."""

    backend_id = "base"

    def __init__(self, context: ExecutionContext | None = None):
        self.context = context or ExecutionContext()


class LocalToolBackend(ToolBackend):
    """Default backend representing local in-process execution."""

    backend_id = "local"
