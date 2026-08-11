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
import time
from collections.abc import Callable
from contextlib import suppress
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
ABORT_TIMEOUT = 1.0
SHUTDOWN_TIMEOUT = 5.0
_FORCE_CLOSE_RESERVE = 0.25
_PROCESS_EXIT_POLL_INTERVAL = 0.05

# LSP protocol version
LSP_PROTOCOL_VERSION = "2.0"


class LspClientError(Exception):
    """Raised when the LSP client encounters a fatal error."""


class LspRequestTimedOut(LspClientError):
    """Raised when an end-to-end LSP operation exhausts its deadline."""


class LspRequestCancelled(LspClientError):
    """Raised when the caller abandons an in-flight LSP operation."""


class LspClient:
    """Minimal LSP client over stdio."""

    def __init__(
        self,
        language_id: LanguageId,
        workspace_root: Path,
        *,
        on_unexpected_exit: Callable[[LspClient, str, int | None], None] | None = None,
    ) -> None:
        self._language_id = language_id
        self._language_id_string = get_language_id_string(language_id)
        self._workspace_root = workspace_root
        self._process: asyncio.subprocess.Process | None = None
        self._on_unexpected_exit = on_unexpected_exit
        self._closing = False
        self._transport_failed = False
        self._exit_reported = False
        self._request_id: int = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._initialized: bool = False
        self._reader_task: asyncio.Task[None] | None = None
        self._process_wait_task: asyncio.Task[None] | None = None
        self._server_response_tasks: set[asyncio.Task[None]] = set()
        self._stdin_write_lock = asyncio.Lock()
        self._diagnostics_buffer: dict[str, list[Diagnostic]] = {}
        self._diagnostics_snapshots: dict[str, list[Diagnostic]] = {}
        self._diagnostic_generations: dict[str, int] = {}
        self._diagnostic_document_versions: dict[str, int] = {}
        self._diagnostic_result_ids: dict[str, str] = {}
        self._document_versions: dict[str, int] = {}
        self._supports_pull_diagnostics = False

    # === Properties ===

    @property
    def is_alive(self) -> bool:
        """Check whether the subprocess still needs to be reaped."""
        return self._process is not None and self._process.returncode is None

    @property
    def is_usable(self) -> bool:
        """Check whether the subprocess transport can serve requests."""
        return self.is_alive and not self._transport_failed

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    # === Spawn & Initialize ===

    async def spawn(self, cmd: str, args: list[str]) -> None:
        """Start the LSP server subprocess."""
        self._closing = False
        self._transport_failed = False
        self._exit_reported = False
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
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(self._workspace_root),
            )
        except FileNotFoundError:
            raise LspClientError(
                f"LSP server command not found: {cmd}. "
                f"Make sure the language toolchain is installed."
            )
        except OSError as e:
            raise LspClientError(f"Failed to spawn LSP server {cmd}: {e}")

        # Start reading responses/notifications from stdout
        self._reader_task = asyncio.create_task(self._read_responses())
        self._process_wait_task = asyncio.create_task(
            self._watch_process_exit(self._process)
        )

    async def initialize(
        self, init_opts: dict[str, Any] | None = None
    ) -> dict[str, Any]:
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
            "workspaceFolders": [{"uri": root_uri, "name": self._workspace_root.name}],
            "capabilities": {
                "textDocument": {
                    "publishDiagnostics": {},
                    "diagnostic": {
                        "dynamicRegistration": False,
                        "relatedDocumentSupport": False,
                    },
                    "definition": {"linkSupport": True},
                    "references": {},
                    "documentSymbol": {
                        "hierarchicalDocumentSymbolSupport": True,
                    },
                },
                "workspace": {"diagnostics": {"refreshSupport": True}},
            },
        }

        if init_opts:
            params["initializationOptions"] = init_opts

        capabilities = await self._send_request(
            "initialize", params, timeout=INITIALIZE_TIMEOUT
        )

        server_capabilities = capabilities.get("capabilities", {})
        self._supports_pull_diagnostics = bool(
            server_capabilities.get("diagnosticProvider")
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
        uri = self._file_uri(file_path)
        self._document_versions[uri] = 1
        await self._send_notification(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": self._language_id_string,
                    "version": 1,
                    "text": content,
                }
            },
        )
        await self._pull_document_diagnostics(file_path)

    async def did_change(
        self, file_path: Path, content: str, version: int | None = None
    ) -> None:
        """Notify the server that a file has changed."""
        uri = self._file_uri(file_path)
        current = self._document_versions.get(uri, 0)
        next_version = current + 1 if version is None else max(version, current + 1)
        self._document_versions[uri] = next_version
        await self._send_notification(
            "textDocument/didChange",
            {
                "textDocument": {
                    "uri": uri,
                    "version": next_version,
                },
                "contentChanges": [{"text": content}],
            },
        )
        await self._pull_document_diagnostics(file_path)

    async def did_save(self, file_path: Path) -> None:
        """Notify the server that a file has been saved."""
        await self._send_notification(
            "textDocument/didSave",
            {
                "textDocument": {
                    "uri": self._file_uri(file_path),
                }
            },
        )
        await self._pull_document_diagnostics(file_path)

    # === Diagnostics ===

    async def wait_for_diagnostics(
        self,
        file_path: Path,
        timeout: float = 5.0,
        *,
        after_generation: int | None = None,
    ) -> list[Diagnostic]:
        """Poll for publishDiagnostics for a specific file.

        Diagnostics arrive asynchronously via the _read_responses loop.
        This method waits for at least one publishDiagnostics notification
        for the given file, or returns whatever has accumulated after timeout.
        """
        file_uri = self._file_uri(file_path)
        current_generation = self._diagnostic_generations.get(file_uri, 0)
        if after_generation is None:
            baseline = (
                current_generation - 1
                if file_uri in self._diagnostics_buffer
                else current_generation
            )
        else:
            baseline = after_generation

        # Give the server a moment to publish
        for _ in range(int(timeout * 10)):
            await asyncio.sleep(0.1)
            if self._diagnostic_generations.get(file_uri, 0) > baseline:
                break

        if self._diagnostic_generations.get(file_uri, 0) <= baseline:
            return []
        return self._diagnostics_buffer.pop(file_uri, [])

    def diagnostics_generation(self, file_path: Path) -> int:
        """Return the latest publish generation observed for one document."""
        return self._diagnostic_generations.get(self._file_uri(file_path), 0)

    def document_version(self, file_path: Path) -> int:
        """Return the last monotonically increasing version sent for a document."""
        return self._document_versions.get(self._file_uri(file_path), 0)

    def diagnostic_document_version(self, file_path: Path) -> int:
        """Return the document version associated with the latest publish."""
        return self._diagnostic_document_versions.get(self._file_uri(file_path), 0)

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

    async def shutdown(self, *, deadline_at: float | None = None) -> None:
        """Gracefully shutdown, then force-close within one total deadline."""
        self._closing = True
        process = self._process
        if process is None:
            return
        if deadline_at is None:
            deadline_at = time.monotonic() + SHUTDOWN_TIMEOUT
        graceful_deadline = max(
            time.monotonic(),
            deadline_at - _FORCE_CLOSE_RESERVE,
        )

        logger.info(
            "Shutting down LSP server for %s",
            self._language_id_string,
        )

        try:
            remaining = graceful_deadline - time.monotonic()
            if remaining > 0:
                with suppress(Exception):
                    await asyncio.wait_for(
                        self._send_request("shutdown", {}, timeout=remaining),
                        timeout=remaining,
                    )

            remaining = graceful_deadline - time.monotonic()
            if remaining > 0:
                with suppress(Exception):
                    await asyncio.wait_for(
                        self._send_notification("exit", {}),
                        timeout=remaining,
                    )

            remaining = graceful_deadline - time.monotonic()
            if process.returncode is None and remaining > 0:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=remaining)
        finally:
            await self.abort(deadline_at=deadline_at)

    async def abort(self, *, deadline_at: float | None = None) -> None:
        """Force-close a failed or cancelled transport without an LSP handshake."""
        if deadline_at is None:
            deadline_at = time.monotonic() + ABORT_TIMEOUT
        cleanup = asyncio.create_task(self._abort_impl(deadline_at=deadline_at))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _abort_impl(self, *, deadline_at: float) -> None:
        """Finish force-close once started, even if the owner is cancelled."""
        self._closing = True
        process = self._process
        reader_task = self._reader_task
        process_wait_task = self._process_wait_task

        current_task = asyncio.current_task()
        if process_wait_task is not None and process_wait_task is not current_task:
            if not process_wait_task.done():
                process_wait_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await process_wait_task

        if reader_task is not None:
            reader_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await reader_task

        response_tasks = tuple(self._server_response_tasks)
        for task in response_tasks:
            if not task.done():
                task.cancel()
        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)
        self._server_response_tasks.clear()

        if process is not None and process.stdin is not None:
            with suppress(Exception):
                process.stdin.close()

        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            remaining = self._cleanup_remaining(deadline_at)
            graceful_remaining = max(0.0, remaining - _FORCE_CLOSE_RESERVE)
            if graceful_remaining > 0:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        process.wait(),
                        timeout=graceful_remaining,
                    )
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                remaining = self._cleanup_remaining(deadline_at)
                if remaining > 0:
                    with suppress(asyncio.TimeoutError, Exception):
                        await asyncio.wait_for(process.wait(), timeout=remaining)

        if process is not None and process.stdin is not None:
            remaining = self._cleanup_remaining(deadline_at)
            if remaining > 0:
                with suppress(asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(
                        process.stdin.wait_closed(),
                        timeout=remaining,
                    )

        self._fail_all_pending("LSP server aborted")
        if process is None or process.returncode is not None:
            self._reset_runtime_state()
        else:
            self._reader_task = None
            self._process_wait_task = None
            self._transport_failed = True
            self._initialized = False

    @staticmethod
    def _cleanup_remaining(deadline_at: float | None) -> float:
        if deadline_at is None:
            return ABORT_TIMEOUT
        return max(0.0, min(ABORT_TIMEOUT, deadline_at - time.monotonic()))

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
        except asyncio.TimeoutError as error:
            raise LspRequestTimedOut(
                f"LSP request '{method}' timed out after {timeout}s"
            ) from error
        finally:
            pending = self._pending.pop(req_id, None)
            if pending is future and not future.done():
                future.cancel()

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
        body = json.dumps(message, ensure_ascii=False)
        header = f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n"
        frame = (header + body).encode("utf-8")
        async with self._stdin_write_lock:
            process = self._process
            if process is None or process.stdin is None:
                raise LspClientError("LSP server not running")
            process.stdin.write(frame)
            await process.stdin.drain()

    # === Internal: Response Reader ===

    async def _watch_process_exit(self, process: asyncio.subprocess.Process) -> None:
        """Observe process exit even when a descendant keeps stdout open."""
        try:
            while process.returncode is None:
                await asyncio.sleep(_PROCESS_EXIT_POLL_INTERVAL)
        except asyncio.CancelledError:
            return
        if self._process is process:
            self._report_transport_failure("process exited", process.returncode)

    def _report_transport_failure(self, reason: str, returncode: int | None) -> None:
        """Fail pending work and notify the manager exactly once."""
        bounded_reason = " ".join(reason.split())[:256] or "transport closed"
        self._transport_failed = True
        self._fail_all_pending(bounded_reason)
        if self._closing or self._exit_reported:
            return
        self._exit_reported = True
        callback = self._on_unexpected_exit
        if callback is None:
            return
        try:
            callback(self, bounded_reason, returncode)
        except Exception:
            logger.exception("LSP unexpected-exit callback failed")

    async def _read_responses(self) -> None:
        """Continuously read JSON-RPC messages from the server's stdout.

        This runs as a background task for the lifetime of the client.
        Dispatches responses to pending futures and handles notifications
        (publishDiagnostics).
        """
        if self._process is None or self._process.stdout is None:
            return

        process = self._process
        stdout = self._process.stdout
        failure_reason: str | None = None
        try:
            while True:
                try:
                    header_bytes = await stdout.readuntil(b"\r\n\r\n")
                except asyncio.CancelledError:
                    return
                except asyncio.IncompleteReadError:
                    failure_reason = "server stdout closed"
                    logger.debug(
                        "LSP server stdout closed (lang=%s)",
                        self._language_id_string,
                    )
                    return

                header = header_bytes[:-4].decode("utf-8", errors="replace")
                content_length = 0
                for hdr_line in header.split("\r\n"):
                    if hdr_line.lower().startswith("content-length:"):
                        try:
                            content_length = int(hdr_line.split(":", 1)[1].strip())
                        except ValueError:
                            pass

                if content_length <= 0:
                    logger.debug("Skipping LSP message with missing Content-Length")
                    continue

                try:
                    body_bytes = await stdout.readexactly(content_length)
                except asyncio.CancelledError:
                    return
                except asyncio.IncompleteReadError:
                    failure_reason = "server stdout closed mid-message"
                    logger.debug(
                        "LSP server stdout closed mid-message (lang=%s)",
                        self._language_id_string,
                    )
                    return

                try:
                    body = json.loads(body_bytes.decode("utf-8"))
                except json.JSONDecodeError as e:
                    logger.warning("Failed to parse LSP message: %s", e)
                    continue

                self._dispatch_message(body)
        except Exception as error:
            failure_reason = f"response reader error: {type(error).__name__}"
            logger.debug("LSP response reader stopped: %s", error)
        finally:
            if failure_reason is not None:
                # Give asyncio's subprocess transport one event-loop turn to
                # publish a concrete return code before the EOF fallback.
                await asyncio.sleep(0)
                self._report_transport_failure(
                    failure_reason,
                    process.returncode,
                )

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        """Route an incoming JSON-RPC message."""
        req_id = message.get("id")
        method = message.get("method")

        if req_id is not None and isinstance(method, str):
            self._handle_server_request(
                req_id,
                method,
                message.get("params", {}),
            )
            return

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
        published_version = params.get("version")
        current_version = self._document_versions.get(uri, 0)
        if (
            isinstance(published_version, int)
            and current_version
            and published_version < current_version
        ):
            logger.debug(
                "Ignoring stale diagnostics for %s at version %s (current %s)",
                uri,
                published_version,
                current_version,
            )
            return
        diagnostics_raw = params.get("diagnostics", [])

        items = self._decode_diagnostics(diagnostics_raw)

        # publishDiagnostics is a full replacement for this document. Keeping
        # the key for an empty list is essential: it signals that stale errors
        # were explicitly cleared by the server.
        self._diagnostics_buffer[uri] = items
        self._diagnostics_snapshots[uri] = items
        self._diagnostic_generations[uri] = self._diagnostic_generations.get(uri, 0) + 1
        self._diagnostic_document_versions[uri] = (
            published_version if isinstance(published_version, int) else current_version
        )

    async def _pull_document_diagnostics(self, file_path: Path) -> None:
        if not self._supports_pull_diagnostics:
            return
        uri = self._file_uri(file_path)
        params: dict[str, Any] = {"textDocument": {"uri": uri}}
        if result_id := self._diagnostic_result_ids.get(uri):
            params["previousResultId"] = result_id
        result = await self._send_request(
            "textDocument/diagnostic", params, timeout=REQUEST_TIMEOUT
        )
        if not isinstance(result, dict):
            return
        kind = result.get("kind")
        if kind == "unchanged":
            items = list(self._diagnostics_snapshots.get(uri, []))
        elif kind == "full":
            raw_items = result.get("items", [])
            if not isinstance(raw_items, list):
                return
            items = self._decode_diagnostics(raw_items)
        else:
            return
        result_id = result.get("resultId")
        if isinstance(result_id, str):
            self._diagnostic_result_ids[uri] = result_id
        self._diagnostics_buffer[uri] = items
        self._diagnostics_snapshots[uri] = items
        self._diagnostic_generations[uri] = self._diagnostic_generations.get(uri, 0) + 1
        self._diagnostic_document_versions[uri] = self._document_versions.get(uri, 0)

    @staticmethod
    def _decode_diagnostics(diagnostics_raw: list[dict[str, Any]]) -> list[Diagnostic]:
        items: list[Diagnostic] = []
        for diagnostic in diagnostics_raw:
            rng = diagnostic.get("range", {})
            start = rng.get("start", {})
            items.append(
                Diagnostic(
                    line=start.get("line", 0) + 1,
                    character=start.get("character", 0) + 1,
                    message=diagnostic.get("message", ""),
                    severity=diagnostic.get("severity", SEVERITY_ERROR),
                    code=diagnostic.get("code"),
                )
            )
        return items

    def _handle_server_request(
        self, request_id: int | str, method: str, params: dict[str, Any]
    ) -> None:
        if method == "workspace/configuration":
            items = params.get("items", [])
            result: Any = [{} for _ in items] if isinstance(items, list) else []
        else:
            # Registration, progress and diagnostic refresh requests do not
            # require product-specific state in this minimal client.
            result = None
        task = asyncio.create_task(
            self._write_message(
                {
                    "jsonrpc": LSP_PROTOCOL_VERSION,
                    "id": request_id,
                    "result": result,
                }
            )
        )
        self._server_response_tasks.add(task)
        task.add_done_callback(self._server_response_done)

    def _server_response_done(self, task: asyncio.Task[None]) -> None:
        self._server_response_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._report_transport_failure(
                f"server response write failed: {type(error).__name__}",
                self._process.returncode if self._process is not None else None,
            )

    def _fail_all_pending(self, reason: str) -> None:
        """Fail all outstanding requests (used on server crash / shutdown)."""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(LspClientError(reason))
                # Retrieve the exception so asyncio does not warn about
                # "Future exception was never retrieved".
                future.exception()
        self._pending.clear()

    def _reset_runtime_state(self) -> None:
        self._process = None
        self._reader_task = None
        self._process_wait_task = None
        self._server_response_tasks.clear()
        self._transport_failed = False
        self._initialized = False
        self._diagnostics_buffer.clear()
        self._diagnostics_snapshots.clear()
        self._diagnostic_generations.clear()
        self._diagnostic_document_versions.clear()
        self._diagnostic_result_ids.clear()
        self._document_versions.clear()
        self._supports_pull_diagnostics = False

    # === Helpers ===

    def _file_uri(self, file_path: Path) -> str:
        """Convert a file path to a file:// URI."""
        return file_path.resolve().as_uri()
