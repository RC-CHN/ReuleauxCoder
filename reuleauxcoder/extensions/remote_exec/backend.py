"""Remote relay tool backend implementation."""

from __future__ import annotations

from typing import Any

from reuleauxcoder.extensions.remote_exec.errors import PeerNotFoundError, RemoteExecError
from reuleauxcoder.extensions.remote_exec.protocol import ExecToolRequest, ToolStreamChunk
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
        if tool_name != "shell" or self.ui_bus is None:
            return None

        def _handle(chunk: ToolStreamChunk) -> None:
            if not chunk.data:
                return
            self.ui_bus.info(
                "",
                kind=UIEventKind.REMOTE,
                remote_stream=True,
                tool_name=tool_name,
                stream=chunk.chunk_type,
                chunk=chunk.data,
            )

        return _handle
