"""Remote execution relay for ReuleauxCoder.

MVP: host-mode relay server with in-memory peer registry, short-lived tokens,
and forwarding of builtin tool execution to a connected peer.
"""

from reuleauxcoder.extensions.remote_exec.errors import (
    AuthError,
    PeerDisconnectedError,
    PeerNotFoundError,
    RegisterRejectedError,
    RemoteExecError,
    RemoteTimeoutError,
    RemoteToolError,
)
from reuleauxcoder.extensions.remote_exec.protocol import (
    ChatRequest,
    ChatResponse,
    ChatCancelRequest,
    ChatCancelResponse,
    ChatControlRequest,
    ChatControlResponse,
    CleanupRequest,
    CleanupResult,
    DisconnectNotice,
    DisconnectRequest,
    ErrorMessage,
    ExecToolRequest,
    ExecToolResult,
    Heartbeat,
    RegisterRejected,
    RegisterRequest,
    TerminalCapabilities,
    TokenRefreshRequest,
    TokenRefreshResponse,
    RegisterResponse,
    RelayEnvelope,
    ToolStreamChunk,
)

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
    "ChatRequest",
    "ChatResponse",
    "ChatCancelRequest",
    "ChatCancelResponse",
    "ChatControlRequest",
    "ChatControlResponse",
    "CleanupRequest",
    "CleanupResult",
    "DisconnectNotice",
    "DisconnectRequest",
    "ErrorMessage",
    "ExecToolRequest",
    "ExecToolResult",
    "Heartbeat",
    "RegisterRejected",
    "RegisterRequest",
    "TerminalCapabilities",
    "TokenRefreshRequest",
    "TokenRefreshResponse",
    "RegisterResponse",
    "RelayEnvelope",
    "ToolStreamChunk",
    "RelayServer",
]


def __getattr__(name):
    from importlib import import_module

    modules = {
        "RemoteRelayToolBackend": "backend",
        "RemoteRelayHTTPService": "http_service",
        "generate_bootstrap_script": "bootstrap",
        "PeerInfo": "peer_registry",
        "PeerRegistry": "peer_registry",
        "RelayServer": "server",
    }
    if name in modules:
        return getattr(import_module(f"{__name__}.{modules[name]}"), name)
    raise AttributeError(name)
