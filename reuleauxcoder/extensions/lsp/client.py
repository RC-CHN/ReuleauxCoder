"""LSP Client — JSON-RPC over stdio subprocess.

Implements a minimal LSP client (~400 lines self-contained, no external
LSP library).  Communication is async via asyncio subprocess pipes.

Key lifecycle:
  1. spawn(cmd, args, language_id, workspace_root) → start child process
  2. initialize request → server capabilities response → send initialized
  3. didOpen (first file) or didChange (subsequent files)
  4. Wait for publishDiagnostics notification (background)
  5. Active tool requests (textDocument/definition, references, etc.)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic, SEVERITY_ERROR
from reuleauxcoder.extensions.lsp.registry import LanguageId, get_language_id_string

logger = logging.getLogger(__name__)

# === Constants ===

# Max file size for LSP analysis (matches zenfun-code limit)
MAX_LSP_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

# Default timeouts
INITIALIZE_TIMEOUT = 30.0  # seconds — initial indexing can be slow
REQUEST_TIMEOUT = 10.0  # seconds — per-request timeout for active tools

# LSP protocol version
LSP_PROTOCOL_VERSION = "2.0"


class LspClientError(Exception):
    """Raised when the LSP client encounters a fatal error."""


class LspClient:
    """Minimal LSP client over stdio."""

    def __init__(
        self,
        language_id: LanguageId,
        workspace_root: Path,
    ) -> None:
        self._language_id = language_id
        self._language_id_string = get_language_id_string(language_id)
        self._workspace_root = workspace_root
        self._process: asyncio.subprocess.Process | None = None
        self._request_id: int = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._initialized: bool = False
        self._reader_task: asyncio.Task[None] | None = None
        self._diagnostics_buffer: dict[str, list[Diagnostic]] = {}

    # === Properties ===

    @property
    def is_alive(self) -> bool:
        """Check if the subprocess is still running."""
        return self._process is not None and self._process.returncode is None

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    # === Spawn & Initialize ===

    async def spawn(self, cmd: str, args: list[str]) -> None:
        """Start the LSP server subprocess."""
        full_args = [cmd] + args
        logger.info(
            "Spawning LSP server: %s (lang=%s, root=%s)",
            " ".join(full_args),
            self._language_id_string,
            self._workspace_root,
        )

        try:
            self._process = await asyncio.create_subprocess_exec(
                *full_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._workspace_root),
            )
        except FileNotFoundError:
            raise LspClientError(
                f"LSP server command not found: {cmd}. "
                f"Make sure the language toolchain is installed."
            )
        except OSError as e:
            raise LspClientError(
                f"Failed to spawn LSP server {cmd}: {e}"
            )

        # Start reading responses/notifications from stdout
        self._reader_task = asyncio.create_task(self._read_responses())

    async def initialize(self, init_opts: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform the LSP initialize handshake.

        Returns the server capabilities dict.
        """
        if self._process is None:
            raise LspClientError("Cannot initialize: server not spawned")

        root_uri = self._workspace_root.resolve().as_uri()

        params: dict[str, Any] = {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "rootPath": str(self._workspace_root),
            "workspaceFolders": [
                {"uri": root_uri, "name": self._workspace_root.name}
            ],
            "capabilities": {
                "textDocument": {
                    "publishDiagnostics": {},
                    "definition": {"linkSupport": True},
                    "references": {},
                    "documentSymbol": {
                        "hierarchicalDocumentSymbolSupport": True,
                    },
                },
            },
        }

        if init_opts:
            params["initializationOptions"] = init_opts

        capabilities = await self._send_request(
            "initialize", params, timeout=INITIALIZE_TIMEOUT
        )

        # Send initialized notification
        await self._send_notification("initialized", {})

        self._initialized = True
        logger.info(
            "LSP server initialized: lang=%s, server=%s",
            self._language_id_string,
            capabilities.get("serverInfo", {}).get("name", "unknown"),
        )
        return capabilities

    # === Document Sync ===

    async def did_open(self, file_path: Path, content: str) -> None:
        """Notify the server that a file has been opened."""
        await self._send_notification("textDocument/didOpen", {
            "textDocument": {
                "uri": self._file_uri(file_path),
                "languageId": self._language_id_string,
                "version": 1,
                "text": content,
            }
        })

    async def did_change(self, file_path: Path, content: str, version: int = 2) -> None:
        """Notify the server that a file has changed."""
        await self._send_notification("textDocument/didChange", {
            "textDocument": {
                "uri": self._file_uri(file_path),
                "version": version,
            },
            "contentChanges": [
                {"text": content}
            ],
        })

    async def did_save(self, file_path: Path) -> None:
        """Notify the server that a file has been saved."""
        await self._send_notification("textDocument/didSave", {
            "textDocument": {
                "uri": self._file_uri(file_path),
            }
        })

    # === Diagnostics ===

    async def wait_for_diagnostics(
        self,
        file_path: Path,
        timeout: float = 5.0,
    ) -> list[Diagnostic]:
        """Poll for publishDiagnostics for a specific file.

        Diagnostics arrive asynchronously via the _read_responses loop.
        This method waits for at least one publishDiagnostics notification
        for the given file, or returns whatever has accumulated after timeout.
        """
        file_uri = self._file_uri(file_path)

        # Give the server a moment to publish
        for _ in range(int(timeout * 10)):
            await asyncio.sleep(0.1)
            if file_uri in self._diagnostics_buffer:
                break

        return self._diagnostics_buffer.pop(file_uri, [])

    # === Active Tool Requests ===

    async def send_request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = REQUEST_TIMEOUT,
    ) -> Any:
        """Send a synchronous LSP request and wait for the response."""
        return await self._send_request(method, params, timeout=timeout)

    async def send_notification(
        self,
        method: str,
        params: dict[str, Any],
    ) -> None:
        """Send a fire-and-forget LSP notification."""
        await self._send_notification(method, params)

    # === Shutdown ===

    async def shutdown(self) -> None:
        """Gracefully shutdown the LSP server."""
        if self._process is None:
            return

        logger.info(
            "Shutting down LSP server for %s",
            self._language_id_string,
        )

        try:
            await asyncio.wait_for(
                self._send_request("shutdown", {}, timeout=5.0),
                timeout=5.0,
            )
        except Exception:
            pass

        try:
            await self._send_notification("exit", {})
        except Exception:
            pass

        if self._reader_task:
            self._reader_task.cancel()

        try:
            self._process.stdin.close()  # type: ignore[union-attr]
        except Exception:
            pass

        try:
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            self._process.kill()
            await self._process.wait()

    # === Internal: Request/Response ===

    async def _send_request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = REQUEST_TIMEOUT,
    ) -> Any:
        """Send a JSON-RPC request and wait for the matching response."""
        if self._process is None or self._process.stdin is None:
            raise LspClientError("LSP server not running")

        self._request_id += 1
        req_id = self._request_id

        message = {
            "jsonrpc": LSP_PROTOCOL_VERSION,
            "id": req_id,
            "method": method,
            "params": params,
        }

        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        try:
            await self._write_message(message)
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise LspClientError(
                f"LSP request '{method}' timed out after {timeout}s"
            )

    async def _send_notification(
        self,
        method: str,
        params: dict[str, Any],
    ) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        message = {
            "jsonrpc": LSP_PROTOCOL_VERSION,
            "method": method,
            "params": params,
        }
        await self._write_message(message)

    async def _write_message(self, message: dict[str, Any]) -> None:
        """Write a JSON-RPC message to the server's stdin."""
        if self._process is None or self._process.stdin is None:
            raise LspClientError("LSP server not running")

        body = json.dumps(message, ensure_ascii=False)
        header = f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n"
        self._process.stdin.write((header + body).encode("utf-8"))
        await self._process.stdin.drain()

    # === Internal: Response Reader ===

    async def _read_responses(self) -> None:
        """Continuously read JSON-RPC messages from the server's stdout.

        This runs as a background task for the lifetime of the client.
        Dispatches responses to pending futures and handles notifications
        (publishDiagnostics).
        """
        if self._process is None or self._process.stdout is None:
            return

        buffer = b""

        while True:
            try:
                line = await self._process.stdout.readline()
            except (asyncio.CancelledError, Exception):
                break

            if not line:
                # EOF — server exited
                logger.warning(
                    "LSP server stdout closed (lang=%s)",
                    self._language_id_string,
                )
                self._fail_all_pending("LSP server exited unexpectedly")
                break

            buffer += line

            # Look for Content-Length header
            while b"\r\n\r\n" in buffer:
                header_end = buffer.index(b"\r\n\r\n")
                header = buffer[:header_end].decode("utf-8", errors="replace")
                buffer = buffer[header_end + 4:]

                content_length = 0
                for hdr_line in header.split("\r\n"):
                    if hdr_line.lower().startswith("content-length:"):
                        try:
                            content_length = int(hdr_line.split(":", 1)[1].strip())
                        except ValueError:
                            pass

                if content_length <= 0:
                    continue

                # Wait for full body
                if len(buffer) < content_length:
                    # Need more — put header back and wait
                    buffer = (
                        header.encode("utf-8") + b"\r\n\r\n" + buffer
                    )
                    break

                body_bytes = buffer[:content_length]
                buffer = buffer[content_length:]

                try:
                    body = json.loads(body_bytes.decode("utf-8"))
                except json.JSONDecodeError as e:
                    logger.warning("Failed to parse LSP message: %s", e)
                    continue

                self._dispatch_message(body)

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        """Route an incoming JSON-RPC message."""
        req_id = message.get("id")

        if req_id is not None:
            # Response to a request
            future = self._pending.pop(req_id, None)
            if future is not None and not future.done():
                if "error" in message:
                    err = message["error"]
                    future.set_exception(
                        LspClientError(
                            f"LSP error {err.get('code')}: {err.get('message')}"
                        )
                    )
                else:
                    future.set_result(message.get("result"))
        else:
            # Notification
            method = message.get("method", "")
            if method == "textDocument/publishDiagnostics":
                self._handle_publish_diagnostics(message.get("params", {}))

    def _handle_publish_diagnostics(self, params: dict[str, Any]) -> None:
        """Process a textDocument/publishDiagnostics notification."""
        uri = params.get("uri", "")
        diagnostics_raw = params.get("diagnostics", [])

        items: list[Diagnostic] = []
        for d in diagnostics_raw:
            rng = d.get("range", {})
            start = rng.get("start", {})
            items.append(Diagnostic(
                line=start.get("line", 0) + 1,      # 0-based → 1-based
                character=start.get("character", 0) + 1,
                message=d.get("message", ""),
                severity=d.get("severity", SEVERITY_ERROR),
                code=d.get("code"),
            ))

        # Accumulate — caller drains via wait_for_diagnostics()
        existing = self._diagnostics_buffer.get(uri, [])
        self._diagnostics_buffer[uri] = existing + items

    def _fail_all_pending(self, reason: str) -> None:
        """Fail all outstanding requests (used on server crash)."""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(LspClientError(reason))
        self._pending.clear()

    # === Helpers ===

    def _file_uri(self, file_path: Path) -> str:
        """Convert a file path to a file:// URI."""
        return file_path.resolve().as_uri()
