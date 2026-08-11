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
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
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
_UNEXPECTED_EXIT_STDERR_GRACE = 0.25
LSP_STDERR_TAIL_BYTES = 64 * 1024
_STDERR_READ_BYTES = 8 * 1024
MAX_LSP_MESSAGE_BYTES = 16 * 1024 * 1024
_MIN_PROTOCOL_ERROR_CODE = -(2**31)
_MAX_PROTOCOL_ERROR_CODE = 2**31 - 1
_KNOWN_LAUNCHER_NAMES = frozenset({"npx", "rust-analyzer", "gopls", "clangd", "node"})

# LSP protocol version
LSP_PROTOCOL_VERSION = "2.0"


def _safe_failure_value(value: str, fallback: str = "unknown") -> str:
    safe = "".join(
        character
        for character in value
        if character.isascii() and (character.isalnum() or character in {"_", "-", "."})
    )[:128]
    return safe or fallback


def _safe_protocol_error_code(value: object) -> int | None:
    if (
        type(value) is int
        and _MIN_PROTOCOL_ERROR_CODE <= value <= _MAX_PROTOCOL_ERROR_CODE
    ):
        return value
    return None


def _reject_nonfinite_json(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _safe_launcher_name(command: str) -> str:
    name = Path(command).name
    return name if name in _KNOWN_LAUNCHER_NAMES else "configured-launcher"


@dataclass(frozen=True, slots=True)
class LspFailureFacts:
    """Immutable, content-free facts captured at the causal failure boundary."""

    phase: str
    error_type: str
    language: str | None = None
    root_hash: str | None = None
    state: str | None = None
    generation: int | None = None
    launcher: str | None = None
    transport_error_phase: str | None = None
    transport_error_type: str | None = None
    transport_observation_error_type: str | None = None
    protocol_error_code: int | None = None
    return_code: int | None = None
    retry_scheduled: bool = False
    stderr_ref: str | None = None
    stderr_bytes: int | None = None
    stderr_truncated: bool = False
    stderr_finalized: bool | None = None
    stderr_read_error_type: str | None = None
    stderr_cleanup_operation: str | None = None
    stderr_cleanup_error_type: str | None = None
    stderr_metadata_error_type: str | None = None
    secondary_error_operation: str | None = None
    secondary_error_type: str | None = None
    failure_projection_error_type: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "protocol_error_code",
            _safe_protocol_error_code(self.protocol_error_code),
        )

    def render(self) -> str:
        fields = [
            f"phase={_safe_failure_value(self.phase)}",
            f"error_type={_safe_failure_value(self.error_type, 'Error')}",
        ]
        for name, value in (
            ("language", self.language),
            ("root_hash", self.root_hash),
            ("state", self.state),
            ("generation", self.generation),
            ("launcher", self.launcher),
            ("transport_phase", self.transport_error_phase),
            ("transport_error_type", self.transport_error_type),
            ("transport_observation_error_type", self.transport_observation_error_type),
            ("protocol_error_code", self.protocol_error_code),
            ("return_code", self.return_code),
            ("stderr_ref", self.stderr_ref),
            ("stderr_bytes", self.stderr_bytes),
            ("stderr_read_error_type", self.stderr_read_error_type),
            ("stderr_cleanup_operation", self.stderr_cleanup_operation),
            ("stderr_cleanup_error_type", self.stderr_cleanup_error_type),
            ("stderr_metadata_error_type", self.stderr_metadata_error_type),
            ("secondary_error_operation", self.secondary_error_operation),
            ("secondary_error_type", self.secondary_error_type),
            ("failure_projection_error_type", self.failure_projection_error_type),
        ):
            if value is not None:
                rendered = (
                    _safe_failure_value(value) if isinstance(value, str) else str(value)
                )
                fields.append(f"{name}={rendered}")
        if self.retry_scheduled:
            fields.append("retry_scheduled=true")
        if self.stderr_truncated:
            fields.append("stderr_truncated=true")
        if self.stderr_ref is not None:
            if self.stderr_finalized is False:
                fields.append("stderr_pending=true")
            elif self.stderr_finalized is None:
                fields.append("stderr_finalized=unknown")
        return "LSP request failed (" + ", ".join(fields) + ")"


def render_lsp_failure(
    facts: LspFailureFacts,
    *,
    fallback_phase: str,
    fallback_error_type: str,
) -> str:
    """Render failure facts without letting projection become a fatal fault."""
    try:
        return facts.render()
    except Exception as projection_error:
        return (
            "LSP request failed "
            f"(phase={_safe_failure_value(fallback_phase)}, "
            f"error_type={_safe_failure_value(fallback_error_type, 'Error')}, "
            "failure_projection_error_type="
            f"{_safe_failure_value(type(projection_error).__name__, 'Error')})"
        )


class LspClientError(Exception):
    """Raised when the LSP client encounters a fatal error."""

    failure_facts: LspFailureFacts | None = None


class LspServerError(LspClientError):
    """A server-declared JSON-RPC error without its untrusted message text."""

    def __init__(self, code: int | None) -> None:
        self.code = _safe_protocol_error_code(code)
        rendered = str(self.code) if self.code is not None else "unknown"
        super().__init__(f"LSP server returned error code {rendered}")


class LspRequestTimedOut(LspClientError):
    """Raised when an end-to-end LSP operation exhausts its deadline."""


class LspRequestCancelled(LspClientError):
    """Raised when the caller abandons an in-flight LSP operation."""


class LspServerUnavailable(LspClientError):
    """Raised when no usable transport can serve the requested file."""


class LspDocumentStatError(LspClientError):
    """Raised when document metadata cannot be read safely."""


class LspDocumentReadError(LspClientError):
    """Raised when document bytes cannot be read safely."""


class LspDocumentDecodeError(LspClientError):
    """Raised when document bytes are not valid UTF-8."""


class LspDocumentTooLarge(LspClientError):
    """Raised before syncing a document beyond the configured size bound."""


class LspDocumentChangedDuringRead(LspClientError):
    """Raised when a document cannot be read as one stable snapshot."""


class LspDocumentCloseError(LspClientError):
    """Raised when a successfully read document handle cannot be closed."""


class LspProtocolFramingError(LspClientError):
    """Raised when an incoming JSON-RPC frame is invalid or oversized."""


class LspProtocolDecodeError(LspClientError):
    """Raised when an incoming JSON-RPC body cannot be decoded."""


class LspProtocolMessageError(LspClientError):
    """Raised when a decoded JSON-RPC value has an invalid shape."""


@dataclass(frozen=True, slots=True)
class LspStderrSnapshot:
    """Raw in-memory stderr state; callers must sanitize before projection."""

    text: str = field(repr=False)
    total_bytes: int
    truncated: bool
    finalized: bool
    read_error: str | None = field(default=None, repr=False)
    cleanup_error: str | None = field(default=None, repr=False)
    read_error_type: str | None = None
    cleanup_operation: str | None = None
    cleanup_error_type: str | None = None


@dataclass(frozen=True, slots=True)
class LspStderrMetadata:
    """Content-free stderr counters safe for ordinary projections."""

    total_bytes: int
    truncated: bool
    tail_available: bool
    finalized: bool
    read_error_type: str | None = None
    cleanup_operation: str | None = None
    cleanup_error_type: str | None = None


class LspStderrCapture:
    """Raw live capture handle; never serialize this object or its snapshots."""

    def __init__(self, max_bytes: int = LSP_STDERR_TAIL_BYTES) -> None:
        self._max_bytes = max_bytes
        self._tail = bytearray()
        self._total_bytes = 0
        self._read_error: str | None = None
        self._cleanup_error: str | None = None
        self._read_error_type: str | None = None
        self._cleanup_operation: str | None = None
        self._cleanup_error_type: str | None = None
        self._finalized = False
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            self._total_bytes += len(chunk)
            self._tail.extend(chunk)
            excess = len(self._tail) - self._max_bytes
            if excess > 0:
                del self._tail[:excess]

    def record_error(self, error: BaseException | str) -> None:
        if isinstance(error, BaseException):
            detail = f"{type(error).__name__}: {error}"
            error_type = type(error).__name__
        else:
            detail = error
            error_type = "StderrDrainIncomplete"
        bounded = " ".join(detail.split())[:256] or "stderr reader failed"
        with self._lock:
            if self._read_error is None:
                self._read_error = bounded
                self._read_error_type = error_type

    def record_cleanup_error(self, operation: str, error: BaseException) -> None:
        safe_operation = (
            "".join(
                character
                for character in operation
                if character.isascii()
                and (character.isalnum() or character in {"_", "-"})
            )[:64]
            or "stderr_cleanup"
        )
        bounded = f"{safe_operation}: {type(error).__name__}"[:128]
        with self._lock:
            if self._cleanup_error is None:
                self._cleanup_error = bounded
                self._cleanup_operation = safe_operation
                self._cleanup_error_type = type(error).__name__

    def mark_finalized(self) -> None:
        """Mark that no further stderr bytes can be captured."""
        with self._lock:
            self._finalized = True

    def snapshot(self) -> LspStderrSnapshot:
        with self._lock:
            raw = bytes(self._tail)
            total_bytes = self._total_bytes
            read_error = self._read_error
            cleanup_error = self._cleanup_error
            read_error_type = self._read_error_type
            cleanup_operation = self._cleanup_operation
            cleanup_error_type = self._cleanup_error_type
            finalized = self._finalized
        return LspStderrSnapshot(
            text=raw.decode("utf-8", errors="replace"),
            total_bytes=total_bytes,
            truncated=total_bytes > len(raw),
            finalized=finalized,
            read_error=read_error,
            cleanup_error=cleanup_error,
            read_error_type=read_error_type,
            cleanup_operation=cleanup_operation,
            cleanup_error_type=cleanup_error_type,
        )

    def metadata(self) -> LspStderrMetadata:
        """Read counters and typed failures without copying or decoding raw text."""
        with self._lock:
            retained_bytes = len(self._tail)
            return LspStderrMetadata(
                total_bytes=self._total_bytes,
                truncated=self._total_bytes > retained_bytes,
                tail_available=retained_bytes > 0,
                finalized=self._finalized,
                read_error_type=self._read_error_type,
                cleanup_operation=self._cleanup_operation,
                cleanup_error_type=self._cleanup_error_type,
            )


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
        self._transport_failure_reason: str | None = None
        self._transport_failure_callback_error_type: str | None = None
        self._exit_reported = False
        self._reported_returncode: int | None = None
        self._request_id: int = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._initialized: bool = False
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_capture = LspStderrCapture()
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

    @property
    def transport_failure_reason(self) -> str | None:
        return self._transport_failure_reason

    @property
    def transport_failure_callback_error_type(self) -> str | None:
        return self._transport_failure_callback_error_type

    @property
    def transport_failure_returncode(self) -> int | None:
        return self._reported_returncode

    @property
    def stderr_snapshot(self) -> LspStderrSnapshot:
        """Return the bounded raw tail without placing it in model context."""
        return self._stderr_capture.snapshot()

    @property
    def stderr_capture(self) -> LspStderrCapture:
        """Return the live raw capture for manager-owned opaque references."""
        return self._stderr_capture

    # === Spawn & Initialize ===

    async def spawn(self, cmd: str, args: list[str]) -> None:
        """Start the LSP server subprocess."""
        self._closing = False
        self._transport_failed = False
        self._transport_failure_reason = None
        self._transport_failure_callback_error_type = None
        self._exit_reported = False
        self._reported_returncode = None
        stderr_capture = LspStderrCapture()
        self._stderr_capture = stderr_capture
        full_args = [cmd] + args
        logger.info(
            "Spawning LSP server: launcher=%s arg_count=%d lang=%s",
            Path(cmd).name or "configured-launcher",
            len(args),
            self._language_id_string,
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
            stderr_capture.mark_finalized()
            raise LspClientError(
                "LSP launcher was not found "
                f"(launcher={Path(cmd).name or 'configured-launcher'})"
            ) from None
        except OSError as e:
            stderr_capture.mark_finalized()
            raise LspClientError(
                f"LSP launcher failed to start (error_type={type(e).__name__})"
            ) from e

        # Drain stderr independently so a noisy server cannot block its own
        # protocol loop when the OS pipe buffer fills.
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(self._process, stderr_capture)
        )
        # Start reading responses/notifications from stdout.
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
            "LSP server initialized: lang=%s",
            self._language_id_string,
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

    async def refresh_diagnostics(self, file_path: Path) -> None:
        """Request pull diagnostics after document synchronization completes."""
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
        result = await self._send_request(method, params, timeout=timeout)
        try:
            self._validate_active_result(method, result)
        except LspProtocolMessageError as error:
            self._report_protocol_message_failure(error)
            raise
        return result

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
        stderr_task = self._stderr_task
        stderr_capture = self._stderr_capture
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

        await self._settle_stderr_task(
            stderr_task,
            stderr_capture,
            deadline_at=deadline_at,
        )

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

    async def _settle_stderr_task(
        self,
        task: asyncio.Task[None] | None,
        capture: LspStderrCapture,
        *,
        deadline_at: float,
    ) -> None:
        """Wait for stderr EOF after reaping, then cancel within the deadline."""
        if task is None:
            return
        if not task.done():
            remaining = max(0.0, deadline_at - time.monotonic())
            if remaining > 0:
                with suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
        if not task.done():
            capture.record_error(
                "stderr reader did not reach EOF before cleanup deadline"
            )
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
        if self._stderr_task is task:
            self._stderr_task = None

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
            try:
                process.stdin.write(frame)
                await process.stdin.drain()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._report_transport_failure(
                    f"protocol write failed: {type(error).__name__}",
                    process.returncode,
                )
                self._force_kill_failed_process(
                    process,
                    operation="protocol_write_force_kill_failed",
                )
                raise

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
            if not self._closing:
                await self._settle_stderr_task(
                    self._stderr_task,
                    self._stderr_capture,
                    deadline_at=time.monotonic() + _UNEXPECTED_EXIT_STDERR_GRACE,
                )

    async def _drain_stderr(
        self,
        process: asyncio.subprocess.Process,
        capture: LspStderrCapture,
    ) -> None:
        """Continuously drain stderr without parsing lines or blocking stdout."""
        stderr = process.stderr
        if stderr is None:
            capture.mark_finalized()
            return
        try:
            while chunk := await stderr.read(_STDERR_READ_BYTES):
                capture.append(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            capture.record_error(error)
            self._report_transport_failure(
                f"stderr reader failed: {type(error).__name__}",
                process.returncode,
            )
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                except Exception as cleanup_error:
                    capture.record_cleanup_error(
                        "stderr_force_kill_failed",
                        cleanup_error,
                    )
                    logger.warning(
                        "Failed to kill LSP after stderr reader failure: "
                        "lang=%s error_type=%s",
                        self._language_id_string,
                        type(cleanup_error).__name__,
                    )
            logger.warning(
                "LSP stderr reader failed: lang=%s error_type=%s",
                self._language_id_string,
                type(error).__name__,
            )
        finally:
            capture.mark_finalized()

    def _report_transport_failure(self, reason: str, returncode: int | None) -> None:
        """Notify once, plus one later refinement from unknown to known exit code."""
        bounded_reason = " ".join(reason.split())[:256] or "transport closed"
        self._transport_failed = True
        if self._transport_failure_reason is None:
            self._transport_failure_reason = bounded_reason
        self._fail_all_pending(bounded_reason)
        if self._closing:
            return
        is_returncode_refinement = (
            self._exit_reported
            and self._reported_returncode is None
            and returncode is not None
        )
        if self._exit_reported and not is_returncode_refinement:
            return
        self._exit_reported = True
        self._reported_returncode = returncode
        callback = self._on_unexpected_exit
        if callback is None:
            return
        try:
            callback(self, bounded_reason, returncode)
        except Exception as error:
            self._transport_failure_callback_error_type = type(error).__name__
            logger.warning(
                "LSP unexpected-exit callback failed: error_type=%s",
                type(error).__name__,
            )

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
                except asyncio.LimitOverrunError as error:
                    raise LspProtocolFramingError(
                        "LSP response header exceeds the stream bound"
                    ) from error
                except asyncio.IncompleteReadError:
                    failure_reason = "server stdout closed"
                    logger.debug(
                        "LSP server stdout closed (lang=%s)",
                        self._language_id_string,
                    )
                    return

                try:
                    header = header_bytes[:-4].decode("ascii")
                except UnicodeDecodeError as error:
                    raise LspProtocolFramingError(
                        "LSP response header is not ASCII"
                    ) from error
                content_lengths: list[int] = []
                for hdr_line in header.split("\r\n"):
                    if hdr_line.lower().startswith("content-length:"):
                        try:
                            content_lengths.append(
                                int(hdr_line.split(":", 1)[1].strip())
                            )
                        except ValueError as error:
                            raise LspProtocolFramingError(
                                "LSP response Content-Length is invalid"
                            ) from error

                if len(content_lengths) != 1:
                    raise LspProtocolFramingError(
                        "LSP response must contain one Content-Length"
                    )
                content_length = content_lengths[0]
                if not 0 < content_length <= MAX_LSP_MESSAGE_BYTES:
                    raise LspProtocolFramingError(
                        "LSP response Content-Length is outside the safe bound"
                    )

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
                    body = json.loads(
                        body_bytes.decode("utf-8"),
                        parse_constant=_reject_nonfinite_json,
                    )
                except (UnicodeDecodeError, ValueError) as error:
                    raise LspProtocolDecodeError(
                        "LSP response body is not valid UTF-8 JSON"
                    ) from error
                if not isinstance(body, dict):
                    raise LspProtocolMessageError(
                        "LSP response body must be a JSON object"
                    )

                self._dispatch_message(body)
        except Exception as error:
            failure_reason = f"response reader error: {type(error).__name__}"
            logger.debug(
                "LSP response reader stopped: error_type=%s",
                type(error).__name__,
            )
        finally:
            if failure_reason is not None:
                # Give asyncio's subprocess transport one event-loop turn to
                # publish a concrete return code before the EOF fallback.
                await asyncio.sleep(0)
                self._report_transport_failure(
                    failure_reason,
                    process.returncode,
                )
                self._force_kill_failed_process(
                    process,
                    operation="response_reader_force_kill_failed",
                )

    def _force_kill_failed_process(
        self,
        process: asyncio.subprocess.Process,
        *,
        operation: str,
    ) -> None:
        """Best-effort containment after a fatal transport/protocol failure."""
        if self._closing or process.returncode is not None:
            return
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except Exception as cleanup_error:
            self._stderr_capture.record_cleanup_error(operation, cleanup_error)
            logger.warning(
                "Failed to kill LSP after transport failure: "
                "lang=%s operation=%s error_type=%s",
                self._language_id_string,
                operation,
                type(cleanup_error).__name__,
            )

    def _report_protocol_message_failure(
        self,
        error: LspProtocolMessageError,
    ) -> None:
        process = self._process
        returncode = process.returncode if process is not None else None
        self._report_transport_failure(
            f"protocol message error: {type(error).__name__}",
            returncode,
        )
        if process is not None:
            self._force_kill_failed_process(
                process,
                operation="protocol_message_force_kill_failed",
            )

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        """Route an incoming JSON-RPC message."""
        if message.get("jsonrpc") != LSP_PROTOCOL_VERSION:
            raise LspProtocolMessageError("LSP message has an invalid jsonrpc version")

        has_id = "id" in message
        req_id = message.get("id")
        has_method = "method" in message
        method = message.get("method")

        if has_method:
            if not isinstance(method, str) or not method:
                raise LspProtocolMessageError("LSP method must be a non-empty string")
            if "result" in message or "error" in message:
                raise LspProtocolMessageError(
                    "LSP request or notification cannot contain a response payload"
                )
            if not has_id:
                if method == "textDocument/publishDiagnostics":
                    params = message.get("params", {})
                    if not isinstance(params, dict):
                        raise LspProtocolMessageError(
                            "LSP diagnostics params must be a JSON object"
                        )
                    self._handle_publish_diagnostics(params)
                return
            if not (type(req_id) is int or isinstance(req_id, str)):
                raise LspProtocolMessageError("LSP server request id is invalid")
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise LspProtocolMessageError(
                    "LSP server request params must be a JSON object"
                )
            self._handle_server_request(
                req_id,
                method,
                params,
            )
            return

        if not has_id or type(req_id) is not int:
            raise LspProtocolMessageError("LSP response id must be an integer")
        has_error = "error" in message
        has_result = "result" in message
        if has_error == has_result:
            raise LspProtocolMessageError(
                "LSP response must contain exactly one of result or error"
            )
        if has_error and not isinstance(message["error"], dict):
            raise LspProtocolMessageError("LSP response error must be a JSON object")

        future = self._pending.get(req_id)
        if future is None or future.done():
            return
        self._pending.pop(req_id, None)
        if has_error:
            code = _safe_protocol_error_code(message["error"].get("code"))
            future.set_exception(LspServerError(code))
        else:
            future.set_result(message["result"])

    def _handle_publish_diagnostics(self, params: dict[str, Any]) -> None:
        """Process a textDocument/publishDiagnostics notification."""
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise LspProtocolMessageError(
                "LSP diagnostics uri must be a non-empty string"
            )
        published_version = params.get("version")
        if published_version is not None and type(published_version) is not int:
            raise LspProtocolMessageError("LSP diagnostics version must be an integer")
        current_version = self._document_versions.get(uri, 0)
        if (
            type(published_version) is int
            and current_version
            and published_version < current_version
        ):
            logger.debug(
                "Ignoring stale diagnostics: lang=%s version=%s current=%s",
                self._language_id_string,
                published_version,
                current_version,
            )
            return
        diagnostics_raw = params.get("diagnostics")

        items = self._decode_diagnostics(diagnostics_raw)

        # publishDiagnostics is a full replacement for this document. Keeping
        # the key for an empty list is essential: it signals that stale errors
        # were explicitly cleared by the server.
        self._diagnostics_buffer[uri] = items
        self._diagnostics_snapshots[uri] = items
        self._diagnostic_generations[uri] = self._diagnostic_generations.get(uri, 0) + 1
        self._diagnostic_document_versions[uri] = (
            published_version if type(published_version) is int else current_version
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
        try:
            self._store_pull_diagnostics_result(uri, result)
        except LspProtocolMessageError as error:
            self._report_protocol_message_failure(error)
            raise

    def _store_pull_diagnostics_result(self, uri: str, result: object) -> None:
        """Validate and store one method-specific pull diagnostics payload."""
        if not isinstance(result, dict):
            raise LspProtocolMessageError(
                "LSP pull diagnostics result must be a JSON object"
            )
        kind = result.get("kind")
        if kind == "unchanged":
            items = list(self._diagnostics_snapshots.get(uri, []))
        elif kind == "full":
            raw_items = result.get("items")
            items = self._decode_diagnostics(raw_items)
        else:
            raise LspProtocolMessageError("LSP pull diagnostics kind is invalid")
        result_id = result.get("resultId")
        if result_id is not None and not isinstance(result_id, str):
            raise LspProtocolMessageError(
                "LSP pull diagnostics resultId must be a string"
            )
        if isinstance(result_id, str):
            self._diagnostic_result_ids[uri] = result_id
        self._diagnostics_buffer[uri] = items
        self._diagnostics_snapshots[uri] = items
        self._diagnostic_generations[uri] = self._diagnostic_generations.get(uri, 0) + 1
        self._diagnostic_document_versions[uri] = self._document_versions.get(uri, 0)

    @classmethod
    def _decode_diagnostics(
        cls,
        diagnostics_raw: object,
    ) -> list[Diagnostic]:
        if not isinstance(diagnostics_raw, list):
            raise LspProtocolMessageError("LSP diagnostics must be a JSON array")
        items: list[Diagnostic] = []
        for diagnostic in diagnostics_raw:
            if not isinstance(diagnostic, dict):
                raise LspProtocolMessageError(
                    "LSP diagnostic item must be a JSON object"
                )
            rng = diagnostic.get("range")
            if not cls._valid_range(rng):
                raise LspProtocolMessageError("LSP diagnostic range is invalid")
            assert isinstance(rng, dict)
            start = rng.get("start")
            assert isinstance(start, dict)
            line = start.get("line")
            character = start.get("character")
            message = diagnostic.get("message")
            severity = diagnostic.get("severity", SEVERITY_ERROR)
            code = diagnostic.get("code")
            if (
                type(line) is not int
                or line < 0
                or type(character) is not int
                or character < 0
                or not isinstance(message, str)
                or type(severity) is not int
                or severity not in {1, 2, 3, 4}
                or not (code is None or isinstance(code, str) or type(code) is int)
            ):
                raise LspProtocolMessageError(
                    "LSP diagnostic item contains an invalid field"
                )
            items.append(
                Diagnostic(
                    line=line + 1,
                    character=character + 1,
                    message=message,
                    severity=severity,
                    code=str(code) if code is not None else None,
                )
            )
        return items

    @classmethod
    def _validate_active_result(cls, method: str, result: object) -> None:
        if method == "textDocument/definition":
            if result is None:
                return
            locations = result if isinstance(result, list) else [result]
            if not all(cls._valid_location(item) for item in locations):
                raise LspProtocolMessageError(
                    "LSP definition result contains an invalid location"
                )
        elif method == "textDocument/references":
            if result is None:
                return
            if not isinstance(result, list) or not all(
                cls._valid_location(item, allow_link=False) for item in result
            ):
                raise LspProtocolMessageError(
                    "LSP references result contains an invalid location"
                )
        elif method == "textDocument/documentSymbol":
            if result is None:
                return
            if not isinstance(result, list) or not cls._valid_symbols(result):
                raise LspProtocolMessageError(
                    "LSP documentSymbol result contains an invalid symbol"
                )

    @classmethod
    def _valid_location(cls, value: object, *, allow_link: bool = True) -> bool:
        if not isinstance(value, dict):
            return False
        if isinstance(value.get("uri"), str) and bool(value["uri"]):
            return cls._valid_range(value.get("range"))
        if (
            allow_link
            and isinstance(value.get("targetUri"), str)
            and bool(value["targetUri"])
        ):
            return cls._valid_range(value.get("targetRange")) and cls._valid_range(
                value.get("targetSelectionRange")
            )
        return False

    @classmethod
    def _valid_range(cls, value: object) -> bool:
        if not isinstance(value, dict):
            return False
        return cls._valid_position(value.get("start")) and cls._valid_position(
            value.get("end")
        )

    @staticmethod
    def _valid_position(value: object) -> bool:
        return (
            isinstance(value, dict)
            and type(value.get("line")) is int
            and value["line"] >= 0
            and type(value.get("character")) is int
            and value["character"] >= 0
        )

    @classmethod
    def _valid_symbols(cls, values: list[object]) -> bool:
        for value in values:
            if not isinstance(value, dict):
                return False
            if (
                not isinstance(value.get("name"), str)
                or type(value.get("kind")) is not int
            ):
                return False
            location = value.get("location")
            if location is not None:
                if not cls._valid_location(location, allow_link=False):
                    return False
            elif not (
                cls._valid_range(value.get("range"))
                and cls._valid_range(value.get("selectionRange"))
            ):
                return False
            children = value.get("children")
            if children is not None and (
                not isinstance(children, list) or not cls._valid_symbols(children)
            ):
                return False
        return True

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
        self._stderr_task = None
        self._process_wait_task = None
        self._server_response_tasks.clear()
        self._transport_failed = False
        self._transport_failure_reason = None
        self._transport_failure_callback_error_type = None
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
