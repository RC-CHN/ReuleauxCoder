"""Remote execution error codes and exceptions."""

from __future__ import annotations


class RemoteExecError(Exception):
    """Base exception for remote execution failures."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class PeerDisconnectedError(RemoteExecError):
    """Peer disconnected while a task was in flight."""

    def __init__(self, peer_id: str):
        self.peer_id = peer_id
        super().__init__("PEER_DISCONNECTED", f"Peer '{peer_id}' disconnected during execution")


class RemoteTimeoutError(RemoteExecError):
    """Tool execution timed out on the remote peer."""

    def __init__(self, timeout_sec: int):
        self.timeout_sec = timeout_sec
        super().__init__("REMOTE_TIMEOUT", f"Remote execution timed out after {timeout_sec}s")


class PeerNotFoundError(RemoteExecError):
    """Requested peer is not registered or offline."""

    def __init__(self, peer_id: str):
        self.peer_id = peer_id
        super().__init__("PEER_NOT_FOUND", f"Peer '{peer_id}' is not online")


class AuthError(RemoteExecError):
    """Authentication or authorization failure."""

    def __init__(self, message: str = "Authentication failed"):
        super().__init__("AUTH_FAILED", message)


class RegisterRejectedError(RemoteExecError):
    """Peer registration was rejected by the host."""

    def __init__(self, message: str = "Registration rejected"):
        super().__init__("REGISTER_REJECTED", message)


class RemoteToolError(RemoteExecError):
    """Tool execution failed on the remote peer."""

    def __init__(self, tool_name: str, message: str):
        self.tool_name = tool_name
        super().__init__("REMOTE_TOOL_ERROR", f"Tool '{tool_name}' failed remotely: {message}")
