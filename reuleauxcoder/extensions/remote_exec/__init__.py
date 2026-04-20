"""Remote execution relay for ReuleauxCoder.

MVP: host-mode relay server with in-memory peer registry, short-lived tokens,
and forwarding of builtin tool execution to a connected peer.
"""

from reuleauxcoder.extensions.remote_exec.backend import RemoteRelayToolBackend
from reuleauxcoder.extensions.remote_exec.bootstrap import generate_bootstrap_script
from reuleauxcoder.extensions.remote_exec.http_service import RemoteRelayHTTPService
from reuleauxcoder.extensions.remote_exec.errors import (
    AuthError,
    PeerDisconnectedError,
    PeerNotFoundError,
    RegisterRejectedError,
    RemoteExecError,
    RemoteTimeoutError,
    RemoteToolError,
)
from reuleauxcoder.extensions.remote_exec.peer_registry import PeerInfo, PeerRegistry
from reuleauxcoder.extensions.remote_exec.protocol import (
    CleanupRequest,
    CleanupResult,
    DisconnectNotice,
    ErrorMessage,
    ExecToolRequest,
    ExecToolResult,
    Heartbeat,
    RegisterRejected,
    RegisterRequest,
    RegisterResponse,
    RelayEnvelope,
    ToolStreamChunk,
)
from reuleauxcoder.extensions.remote_exec.server import RelayServer

__all__ = [
    "RemoteRelayToolBackend",
    "RemoteRelayHTTPService",
    "generate_bootstrap_script",
    "AuthError",
    "PeerDisconnectedError",
    "PeerNotFoundError",
    "RegisterRejectedError",
    "RemoteExecError",
    "RemoteTimeoutError",
    "RemoteToolError",
    "PeerInfo",
    "PeerRegistry",
    "CleanupRequest",
    "CleanupResult",
    "DisconnectNotice",
    "ErrorMessage",
    "ExecToolRequest",
    "ExecToolResult",
    "Heartbeat",
    "RegisterRejected",
    "RegisterRequest",
    "RegisterResponse",
    "RelayEnvelope",
    "ToolStreamChunk",
    "RelayServer",
]
