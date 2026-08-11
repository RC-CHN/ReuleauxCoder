"""LSP Manager — workspace-scoped coordinator for LSP server lifecycle.

Ownership:
- All LSP subprocess communication (sole writer to stdin, via worker thread)
- Lazy per-language LspClient map
- Dual-queue worker thread (diagnostics fire-and-forget + active tool sync bridge)
- Crash detection with re-spawn limit
- Lazy command availability with a short negative cache
- Session-scoped document sync tracking
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import logging
import os
import shutil
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any

from reuleauxcoder.domain.cancellation import CancellationSignal
from reuleauxcoder.domain.runtime.events import (
    DiagnosticsCleared,
    DiagnosticsPublished,
    RuntimeDiagnostic,
    RuntimeEvent,
)
from reuleauxcoder.domain.runtime.performance import (
    PerformanceValue,
    RuntimePerformanceMonitor,
)

from reuleauxcoder.extensions.lsp.client import (
    LspClient,
    LspClientError,
    LspDocumentChangedDuringRead,
    LspDocumentCloseError,
    LspDocumentDecodeError,
    LspDocumentReadError,
    LspDocumentStatError,
    LspDocumentTooLarge,
    LspFailureFacts,
    LspRequestCancelled,
    LspRequestTimedOut,
    LspServerError,
    LspServerUnavailable,
    LspStderrCapture,
    MAX_LSP_FILE_SIZE_BYTES,
    render_lsp_failure,
)
from reuleauxcoder.extensions.lsp.config import LspConfig
from reuleauxcoder.extensions.lsp.diagnostics import (
    DiagnosticBatch,
    DiagnosticBlock,
    DiagnosticRoute,
    DiagnosticRouteFilter,
)
from reuleauxcoder.extensions.lsp.diagnostic_outcomes import (
    DiagnosticOutcome,
    DiagnosticOutcomeStatus,
)
from reuleauxcoder.extensions.lsp.registry import (
    LanguageId,
    LspServerLaunch,
    detect_language,
    get_language_id_string,
    resolve_server_launch,
    get_server_command,
    resolve_workspace_root,
)

logger = logging.getLogger(__name__)

# === Constants ===

MAX_RESPWANS = 3
WORKER_SHUTDOWN_TIMEOUT = (
    5.0  # one total manager deadline, including active-work cancellation
)
_TOOL_REQUEST_POLL_INTERVAL = 0.05
_WORKER_WAKEUP_FALLBACK_INTERVAL = 0.01
SPAWN_TIMEOUT = 30.0
MISSING_COMMAND_TTL_SECONDS = 30.0
DIAGNOSTIC_BATCH_TTL_SECONDS = 300.0
MAX_PENDING_DIAGNOSTIC_BATCHES_PER_OWNER = 32
MAX_TRANSPORT_STATE_HISTORY = 256
MAX_LSP_STDERR_RECORDS = 16
_LSP_PERFORMANCE_ATTRIBUTE_KEYS = frozenset(
    {
        "language",
        "root_hash",
        "launcher",
        "transport_generation",
        "work_kind",
        "request_kind",
        "sync_kind",
        "shutdown_phase",
        "outcome",
        "cache_result",
        "cold_start",
        "document_committed",
        "document_version",
        "diagnostic_generation",
        "diagnostic_count",
        "transport_count",
        "depth",
        "respawn_count",
        "error_type",
    }
)
_LSP_REQUEST_KINDS = {
    "textDocument/definition": "definition",
    "textDocument/references": "references",
    "textDocument/documentSymbol": "document_symbol",
}
TransportKey = tuple[LanguageId, Path]
AvailabilityCacheKey = tuple[TransportKey, str]
DiagnosticOwnerKey = tuple[str | None, int | None, str | None]


class LspTransportState(str, Enum):
    """Observable lifecycle of one language/workspace transport slot."""

    UNSTARTED = "unstarted"
    RESOLVING = "resolving"
    STARTING = "starting"
    INITIALIZING = "initializing"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class LspTransportStatus:
    """Immutable current state/transition record for one transport slot."""

    language: LanguageId
    workspace_root: Path
    state: LspTransportState
    generation: int
    sequence: int
    updated_at_monotonic: float
    launcher: str | None = None
    error_type: str | None = None
    error_phase: str | None = None
    protocol_error_code: int | None = None
    return_code: int | None = None
    stderr_ref: str | None = None
    retry_at_monotonic: float | None = None


@dataclass(frozen=True, slots=True)
class LspStderrReference:
    """Content-free metadata for one opaque in-memory stderr reference."""

    ref: str
    total_bytes: int
    truncated: bool
    tail_available: bool
    finalized: bool | None
    read_error_type: str | None = None
    cleanup_operation: str | None = None
    cleanup_error_type: str | None = None
    metadata_error_type: str | None = None


@dataclass(frozen=True, slots=True)
class LspTransportStatusView:
    """Default-deny transport projection safe for UI and model diagnostics."""

    language: str
    root_hash: str
    state: LspTransportState
    generation: int
    launcher: str | None = None
    error_phase: str | None = None
    error_type: str | None = None
    protocol_error_code: int | None = None
    return_code: int | None = None
    retry_at_monotonic: float | None = None
    stderr: LspStderrReference | None = None


@dataclass(frozen=True, slots=True)
class _LspStderrRecord:
    ref: str
    transport_key: TransportKey
    generation: int
    capture_id: int
    capture: LspStderrCapture


@dataclass
class LspHealthReport:
    """Result of startup health check."""

    total: int = 0
    available: int = 0
    languages: list[tuple[str, bool, str]] = field(default_factory=list)


@dataclass
class ToolRequest:
    """A synchronous active-tool request from the main thread."""

    file_path: Path
    language_id: LanguageId
    method: str
    params: dict[str, Any]
    future: concurrent.futures.Future[Any]
    timeout_seconds: float
    deadline_at: float
    needs_sync: bool = True  # Whether to sync file content before the query
    transport_key: TransportKey | None = None
    enqueue_sequence: int = 0
    enqueued_at: float = field(default_factory=time.monotonic)
    queue_depth: int = 0
    abandonment_status: str | None = None
    phase: str = "queue"


@dataclass(frozen=True, slots=True)
class _DocumentStamp:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, metadata: os.stat_result) -> _DocumentStamp:
        return cls(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            ctime_ns=metadata.st_ctime_ns,
        )


@dataclass(frozen=True, slots=True)
class _DocumentSnapshot:
    content: str = field(repr=False)
    stamp: _DocumentStamp


@dataclass(frozen=True, slots=True)
class DiagnosticRequest:
    """A routed diagnostics request owned by one edit operation."""

    batch_id: str
    route: DiagnosticRoute
    request_sequence: int
    document_committed: bool = False
    transport_key: TransportKey | None = None
    enqueue_sequence: int = 0
    enqueued_at: float = field(default_factory=time.monotonic)
    queue_depth: int = 0


@dataclass(frozen=True, slots=True)
class _ActiveWork:
    kind: str
    item: ToolRequest | DiagnosticRequest
    task: asyncio.Task[None]


class LspManager:
    """Workspace-scoped coordinator for LSP server interactions.

    All LSP I/O (subprocess stdin/stdout) passes through one background
    event loop. Work is serialized per transport while independent language
    and workspace transports may run concurrently.
    """

    def __init__(
        self,
        config: LspConfig,
        workspace_cwd: Path,
        *,
        ui_bus: Any = None,
        runtime_event_sink: Callable[[RuntimeEvent], None] | None = None,
    ) -> None:
        self._config = config
        self._workspace_cwd = workspace_cwd
        self.ui_bus = ui_bus
        self._runtime_event_sink = runtime_event_sink
        self.performance_monitor: RuntimePerformanceMonitor | None = None

        # Per-language/workspace state. One language server must never index
        # files from an unrelated workspace root.
        self._transports: dict[TransportKey, LspClient] = {}
        self._workspace_roots: dict[TransportKey, Path] = {}
        self._transport_statuses: dict[TransportKey, LspTransportStatus] = {}
        self._transport_state_history: deque[LspTransportStatus] = deque(
            maxlen=MAX_TRANSPORT_STATE_HISTORY
        )
        self._stderr_records: dict[str, _LspStderrRecord] = {}
        self._stderr_ref_by_owner: dict[tuple[TransportKey, int, int], str] = {}
        self._next_transport_state_sequence = 0
        self._transport_state_clock: Callable[[], float] = time.monotonic
        self._availability: dict[LanguageId, bool] = {}
        self._negative_availability_until: dict[AvailabilityCacheKey, float] = {}
        self._availability_clock: Callable[[], float] = time.monotonic
        self._command_lookup: Callable[[str], str | None] = shutil.which
        self._availability_metrics: dict[str, int] = {
            "lookups": 0,
            "available": 0,
            "unavailable": 0,
            "negative_cache_hits": 0,
        }
        self._re_spawn_counts: dict[TransportKey, int] = {}
        self._document_sync_stamps: dict[tuple[TransportKey, Path], _DocumentStamp] = {}

        # Queues
        self._diagnostics_queue: list[DiagnosticRequest] = []
        self._tool_queue: list[ToolRequest] = []
        self._next_work_sequence = 0

        # Routed results.  Clean publishes are retained as empty batches until
        # the owning consumer acknowledges them.  Non-published terminal
        # outcomes live separately: a timeout/failure must remain visible but
        # must never clear or replace the last published document state.
        self._diagnostic_batches: dict[str, DiagnosticBatch] = {}
        self._diagnostic_failure_outcomes: dict[str, DiagnosticOutcome] = {}
        self._pending_diagnostic_requests: set[str] = set()
        self._acknowledged_batches: dict[str, str] = {}
        self._latest_diagnostic_sequence: dict[
            tuple[str | None, int | None, str | None, Path], int
        ] = {}
        self._session_generations: dict[str, int] = {}
        self._next_diagnostic_sequence = 0
        self._diagnostic_clock: Callable[[], float] = time.time
        self._diagnostic_batch_metrics: dict[str, int] = {
            "carried_forward": 0,
            "expired": 0,
            "overwritten": 0,
            "capacity_evicted": 0,
            "stale_discarded": 0,
            **{f"outcome_{status.value}": 0 for status in DiagnosticOutcomeStatus},
        }

        # Lock (RLock for reentrancy in health_check)
        self._lock: threading.RLock = threading.RLock()
        self._shutdown_lock = threading.Lock()

        # Worker thread
        self._worker_thread: threading.Thread | None = None
        self._accepting_work = False
        self._closed = False
        self._stop_event = threading.Event()
        self._abort_current = False  # set during shutdown to skip in-flight work

        # Worker event loop reference (set once worker starts)
        self._worker_loop: asyncio.AbstractEventLoop | None = None
        self._worker_wakeup: asyncio.Event | None = None
        self._active_work: dict[TransportKey | None, _ActiveWork] = {}
        self._shutdown_deadline_at: float | None = None
        self._last_shutdown_completed = True

    # === Properties ===

    @property
    def config(self) -> LspConfig:
        return self._config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def describe_scopes(self) -> tuple[str, ...]:
        """Return secret-free transport and pending-batch ownership diagnostics."""
        with self._lock:
            self._prune_expired_diagnostic_batches_locked()
            statuses = tuple(self._transport_statuses.values())
            pending = tuple(
                sorted(
                    f"pending:g{batch.route.session_generation}:"
                    f"file={self._workspace_identifier(batch.route.file_path)}"
                    for batch in self._diagnostic_batches.values()
                )
            )
        transports = tuple(
            sorted(
                self._describe_transport_status(self._transport_status_view(status))
                for status in statuses
            )
        )
        return (*transports, *pending)

    def transport_statuses(self) -> tuple[LspTransportStatus, ...]:
        """Return privileged raw slots; UI/model code must use status views."""
        with self._lock:
            return tuple(
                sorted(
                    self._transport_statuses.values(),
                    key=lambda status: (
                        get_language_id_string(status.language),
                        str(status.workspace_root),
                    ),
                )
            )

    def transport_status_views(self) -> tuple[LspTransportStatusView, ...]:
        """Return a stable default-deny projection without paths or raw errors."""
        with self._lock:
            statuses = tuple(self._transport_statuses.values())
        views = tuple(self._transport_status_view(status) for status in statuses)
        return tuple(
            sorted(
                views,
                key=lambda view: (view.language, view.root_hash),
            )
        )

    def stderr_reference(self, ref: str) -> LspStderrReference | None:
        """Resolve content-free metadata; raw stderr is deliberately unavailable."""
        with self._lock:
            record = self._stderr_records.get(ref)
        if record is None:
            return None
        try:
            return self._stderr_reference_from_record(record)
        except Exception as error:
            logger.warning(
                "LSP stderr metadata snapshot failed: error_type=%s",
                type(error).__name__,
            )
            return LspStderrReference(
                ref=ref,
                total_bytes=0,
                truncated=False,
                tail_available=False,
                finalized=None,
                metadata_error_type=type(error).__name__,
            )

    def describe_failure_for_file(
        self,
        file_path: Path,
        *,
        phase: str,
        error_type: str,
        protocol_error_code: int | None = None,
    ) -> str:
        """Build a model-safe failure fact without external error text."""
        path = self._canonicalize_path(file_path)
        language = detect_language(path)
        key = self._transport_key(language, path) if language is not None else None
        facts = self._capture_failure_facts(
            key,
            phase=phase,
            error_type=error_type,
            protocol_error_code=protocol_error_code,
        )
        return render_lsp_failure(
            facts,
            fallback_phase=phase,
            fallback_error_type=error_type,
        )

    def _capture_failure_facts(
        self,
        key: TransportKey | None,
        *,
        phase: str,
        error_type: str,
        protocol_error_code: int | None = None,
    ) -> LspFailureFacts:
        """Freeze one causal failure without retaining paths or free-form text."""
        safe_phase = self._safe_fact(phase, "unknown")
        safe_error_type = self._safe_fact(error_type, "Error")
        try:
            status: LspTransportStatus | None = None
            client: LspClient | None = None
            if key is not None:
                with self._lock:
                    status = self._transport_statuses.get(key)
                    client = self._transports.get(key)
            view = self._transport_status_view(status) if status is not None else None
            stderr = view.stderr if view is not None else None
            status_failed = view is not None and view.state is LspTransportState.ERROR
            client_failure_reason = (
                client.transport_failure_reason if client is not None else None
            )
            client_failed = client_failure_reason is not None
            if (
                client_failed
                and not status_failed
                and key is not None
                and status is not None
            ):
                stderr_ref = self._retain_client_stderr(
                    key,
                    status.generation,
                    client,
                )
                stderr = (
                    self.stderr_reference(stderr_ref)
                    if stderr_ref is not None
                    else None
                )
            if protocol_error_code is None and status_failed:
                protocol_error_code = view.protocol_error_code
            transport_error_type = (
                view.error_type
                if status_failed
                else self._runtime_failure_type(
                    client_failure_reason or "transport closed",
                    client.transport_failure_returncode if client is not None else None,
                )
                if client_failed
                else None
            )
            return LspFailureFacts(
                phase=safe_phase,
                error_type=safe_error_type,
                language=(
                    view.language
                    if view is not None
                    else get_language_id_string(key[0])
                    if key is not None
                    else None
                ),
                root_hash=(
                    view.root_hash
                    if view is not None
                    else self._workspace_identifier(key[1])
                    if key is not None
                    else None
                ),
                state=(
                    LspTransportState.ERROR.value
                    if client_failed and not status_failed
                    else view.state.value
                    if view is not None
                    else None
                ),
                generation=view.generation if view is not None else None,
                launcher=view.launcher if view is not None else None,
                transport_error_phase=(
                    view.error_phase
                    if status_failed
                    else "runtime"
                    if client_failed
                    else None
                ),
                transport_error_type=transport_error_type,
                transport_observation_error_type=(
                    client.transport_failure_callback_error_type
                    if client_failed and client is not None
                    else None
                ),
                protocol_error_code=protocol_error_code,
                return_code=(
                    view.return_code
                    if status_failed
                    else client.transport_failure_returncode
                    if client_failed and client is not None
                    else None
                ),
                retry_scheduled=(status_failed and view.retry_at_monotonic is not None),
                stderr_ref=stderr.ref if stderr is not None else None,
                stderr_bytes=stderr.total_bytes if stderr is not None else None,
                stderr_truncated=stderr.truncated if stderr is not None else False,
                stderr_finalized=stderr.finalized if stderr is not None else None,
                stderr_read_error_type=(
                    stderr.read_error_type if stderr is not None else None
                ),
                stderr_cleanup_operation=(
                    stderr.cleanup_operation if stderr is not None else None
                ),
                stderr_cleanup_error_type=(
                    stderr.cleanup_error_type if stderr is not None else None
                ),
                stderr_metadata_error_type=(
                    stderr.metadata_error_type if stderr is not None else None
                ),
            )
        except Exception as projection_error:
            logger.warning(
                "LSP failure snapshot failed: error_type=%s",
                type(projection_error).__name__,
            )
            return LspFailureFacts(
                phase=safe_phase,
                error_type=safe_error_type,
                failure_projection_error_type=self._safe_fact(
                    type(projection_error).__name__,
                    "Error",
                ),
            )

    def _freeze_failure(
        self,
        error: Exception,
        key: TransportKey | None,
        *,
        phase: str,
    ) -> Exception:
        """Attach an immutable safe snapshot before crossing thread boundaries."""
        try:
            existing = getattr(error, "failure_facts", None)
        except Exception:
            existing = None
        if isinstance(existing, LspFailureFacts):
            return error
        try:
            code = getattr(error, "code", None)
        except Exception:
            code = None
        try:
            secondary_operation = getattr(error, "secondary_error_operation", None)
            secondary_error_type = getattr(error, "secondary_error_type", None)
        except Exception:
            secondary_operation = None
            secondary_error_type = None
        facts = self._capture_failure_facts(
            key,
            phase=phase,
            error_type=type(error).__name__,
            protocol_error_code=self._safe_protocol_error_code(code),
        )
        if isinstance(secondary_operation, str) or isinstance(
            secondary_error_type, str
        ):
            facts = replace(
                facts,
                secondary_error_operation=(
                    self._safe_fact(secondary_operation, "cleanup")
                    if isinstance(secondary_operation, str)
                    else None
                ),
                secondary_error_type=(
                    self._safe_fact(secondary_error_type, "Error")
                    if isinstance(secondary_error_type, str)
                    else None
                ),
            )
        try:
            error.failure_facts = facts  # type: ignore[attr-defined]
            return error
        except Exception:
            fallback = LspClientError(
                render_lsp_failure(
                    facts,
                    fallback_phase=phase,
                    fallback_error_type=type(error).__name__,
                )
            )
            fallback.failure_facts = facts
            return fallback

    def _diagnostic_failure_facts(
        self,
        error: Exception,
        key: TransportKey | None,
        *,
        phase: str,
    ) -> LspFailureFacts:
        """Freeze the same safe facts used by active tools for diagnostics."""
        frozen = self._freeze_failure(error, key, phase=phase)
        facts = getattr(frozen, "failure_facts", None)
        if isinstance(facts, LspFailureFacts):
            return facts
        return self._capture_failure_facts(
            key,
            phase=phase,
            error_type=type(error).__name__,
        )

    def transport_status_for_file(self, file_path: Path) -> LspTransportStatus | None:
        """Resolve a privileged raw slot, initially ``unstarted g0``."""
        path = self._canonicalize_path(file_path)
        language = detect_language(path)
        if not self._config.enabled or language is None:
            return None
        key = self._transport_key(language, path)
        return self._ensure_transport_status(key)

    def transport_state_history(self) -> tuple[LspTransportStatus, ...]:
        """Return privileged raw transition history, oldest first."""
        with self._lock:
            return tuple(self._transport_state_history)

    # === Lifecycle ===

    def health_check(self) -> LspHealthReport:
        """Explicitly probe PATH without changing lazy runtime availability."""
        from reuleauxcoder.extensions.lsp.registry import iter_supported_languages

        report = LspHealthReport()
        for lang in iter_supported_languages():
            cmd, args = self._resolve_command(lang)
            lookup_target = self._command_lookup_target(cmd, self._workspace_cwd)
            found = self._command_lookup(lookup_target) is not None

            lang_name = get_language_id_string(lang)
            details = f"launcher={self._launcher_name(cmd)} arg_count={len(args)}"
            report.languages.append((lang_name, found, details))
            report.total += 1
            if found:
                report.available += 1

        return report

    def configured_languages(self) -> tuple[str, ...]:
        """Return the declarative catalog without probing PATH or starting work."""
        from reuleauxcoder.extensions.lsp.registry import iter_supported_languages

        return tuple(
            get_language_id_string(language) for language in iter_supported_languages()
        )

    def availability_metrics(self) -> dict[str, int]:
        """Return content-free counters for lazy command resolution."""
        with self._lock:
            return dict(self._availability_metrics)

    async def _observe_lsp_phase(
        self,
        name: str,
        operation: Awaitable[Any],
        *,
        transport_key: TransportKey | None,
        attributes: Mapping[str, PerformanceValue] | None = None,
    ) -> Any:
        """Await one operation and retain a bounded, content-free timing."""
        started_at = time.monotonic()
        status = "ok"
        details = dict(attributes or {})
        try:
            return await operation
        except BaseException as error:
            status = self._performance_status(error)
            details["error_type"] = type(error).__name__
            raise
        finally:
            self._record_lsp_performance(
                name,
                started_at=started_at,
                status=status,
                transport_key=transport_key,
                attributes=details,
            )

    def _record_lsp_performance(
        self,
        name: str,
        *,
        started_at: float,
        status: str = "ok",
        transport_key: TransportKey | None = None,
        attributes: Mapping[str, PerformanceValue] | None = None,
    ) -> None:
        monitor = self.performance_monitor
        if monitor is None:
            return
        try:
            details: dict[str, PerformanceValue] = {}
            if transport_key is not None:
                language, root = transport_key
                with self._lock:
                    transport_status = self._transport_statuses.get(transport_key)
                    respawn_count = self._re_spawn_counts.get(transport_key, 0)
                details.update(
                    {
                        "language": get_language_id_string(language),
                        "root_hash": self._workspace_identifier(root),
                        "transport_generation": (
                            transport_status.generation
                            if transport_status is not None
                            else 0
                        ),
                        "launcher": (
                            transport_status.launcher
                            if transport_status is not None
                            else None
                        ),
                        "respawn_count": respawn_count,
                    }
                )
            details.update(
                (key, value)
                for key, value in (attributes or {}).items()
                if key in _LSP_PERFORMANCE_ATTRIBUTE_KEYS
            )
            error_type = details.get("error_type")
            if isinstance(error_type, str):
                details["error_type"] = error_type[:64]
            monitor.record(
                "lsp",
                name,
                (time.monotonic() - started_at) * 1000,
                status=status,
                attributes=details,
            )
        except Exception as error:
            logger.warning(
                "LSP performance sample failed: error_type=%s",
                type(error).__name__,
            )

    @staticmethod
    def _performance_status(error: BaseException) -> str:
        if isinstance(error, (asyncio.CancelledError, LspRequestCancelled)):
            return "cancelled"
        if isinstance(error, (asyncio.TimeoutError, LspRequestTimedOut)):
            return "timeout"
        return "error"

    @staticmethod
    def _workspace_identifier(root: Path) -> str:
        return hashlib.sha256(os.fsencode(root)).hexdigest()[:12]

    @staticmethod
    def _safe_fact(value: str, fallback: str) -> str:
        """Keep externally visible failure fields content-free and bounded."""
        safe = "".join(
            character
            for character in value
            if character.isascii()
            and (character.isalnum() or character in {"_", "-", "."})
        )[:64]
        return safe or fallback

    @staticmethod
    def _safe_protocol_error_code(value: object) -> int | None:
        if type(value) is int and -(2**31) <= value <= 2**31 - 1:
            return value
        return None

    def _retain_client_stderr(
        self,
        key: TransportKey,
        generation: int,
        client: LspClient,
    ) -> str | None:
        try:
            capture = getattr(client, "stderr_capture", None)
        except Exception as error:
            logger.warning(
                "LSP stderr capture retention failed: error_type=%s",
                type(error).__name__,
            )
            return None
        if not isinstance(capture, LspStderrCapture):
            return None
        owner = (key, generation, id(capture))
        with self._lock:
            ref = self._stderr_ref_by_owner.get(owner)
            if ref is not None and ref in self._stderr_records:
                return ref
            try:
                ref = f"lspstderr_{uuid.uuid4().hex}"
            except Exception as error:
                logger.warning(
                    "LSP stderr reference allocation failed: error_type=%s",
                    type(error).__name__,
                )
                return None
            self._stderr_records[ref] = _LspStderrRecord(
                ref=ref,
                transport_key=key,
                generation=generation,
                capture_id=id(capture),
                capture=capture,
            )
            self._stderr_ref_by_owner[owner] = ref
            while len(self._stderr_records) > MAX_LSP_STDERR_RECORDS:
                stale_ref = next(iter(self._stderr_records))
                stale = self._stderr_records.pop(stale_ref)
                stale_owner = (
                    stale.transport_key,
                    stale.generation,
                    stale.capture_id,
                )
                if self._stderr_ref_by_owner.get(stale_owner) == stale_ref:
                    self._stderr_ref_by_owner.pop(stale_owner, None)
            return ref

    @staticmethod
    def _record_client_cleanup_error(
        client: LspClient,
        operation: str,
        error: BaseException,
    ) -> None:
        """Retain a secondary cleanup fault without masking the primary failure."""
        try:
            capture = getattr(client, "stderr_capture", None)
            if isinstance(capture, LspStderrCapture):
                capture.record_cleanup_error(operation, error)
        except Exception as observation_error:
            logger.warning(
                "LSP cleanup-failure observation failed: error_type=%s",
                type(observation_error).__name__,
            )

    @staticmethod
    def _stderr_reference_from_record(
        record: _LspStderrRecord,
    ) -> LspStderrReference:
        metadata = record.capture.metadata()
        return LspStderrReference(
            ref=record.ref,
            total_bytes=metadata.total_bytes,
            truncated=metadata.truncated,
            tail_available=metadata.tail_available,
            finalized=metadata.finalized,
            read_error_type=metadata.read_error_type,
            cleanup_operation=metadata.cleanup_operation,
            cleanup_error_type=metadata.cleanup_error_type,
        )

    def _transport_status_view(
        self,
        status: LspTransportStatus,
    ) -> LspTransportStatusView:
        key = (status.language, status.workspace_root)
        with self._lock:
            client = self._transports.get(key)
        client_failure_reason = (
            client.transport_failure_reason if client is not None else None
        )
        client_failed = (
            client_failure_reason is not None
            and status.state is not LspTransportState.ERROR
        )
        stderr_ref = status.stderr_ref
        if client_failed and stderr_ref is None and client is not None:
            stderr_ref = self._retain_client_stderr(
                key,
                status.generation,
                client,
            )
        stderr = self.stderr_reference(stderr_ref) if stderr_ref is not None else None
        if stderr_ref is not None and stderr is None:
            stderr = LspStderrReference(
                ref=stderr_ref,
                total_bytes=0,
                truncated=False,
                tail_available=False,
                finalized=True,
                read_error_type="ReferenceExpired",
            )
        return LspTransportStatusView(
            language=get_language_id_string(status.language),
            root_hash=self._workspace_identifier(status.workspace_root),
            state=(LspTransportState.ERROR if client_failed else status.state),
            generation=status.generation,
            launcher=(
                self._safe_fact(status.launcher, "configured-launcher")
                if status.launcher is not None
                else None
            ),
            error_phase=(
                "runtime"
                if client_failed
                else self._safe_fact(status.error_phase, "unknown")
                if status.error_phase is not None
                else None
            ),
            error_type=(
                self._runtime_failure_type(
                    client_failure_reason or "transport closed",
                    client.transport_failure_returncode if client is not None else None,
                )
                if client_failed
                else self._safe_fact(status.error_type, "Error")
                if status.error_type is not None
                else None
            ),
            protocol_error_code=status.protocol_error_code,
            return_code=(
                client.transport_failure_returncode
                if client_failed and client is not None
                else status.return_code
            ),
            retry_at_monotonic=(None if client_failed else status.retry_at_monotonic),
            stderr=stderr,
        )

    @staticmethod
    def _request_kind(method: str) -> str:
        return _LSP_REQUEST_KINDS.get(method, "other")

    @staticmethod
    def _document_version(client: Any, file_path: Path) -> int | None:
        getter = getattr(client, "document_version", None)
        if not callable(getter):
            return None
        try:
            return int(getter(file_path))
        except Exception:
            return None

    def _record_queue_wait(
        self,
        work: ToolRequest | DiagnosticRequest,
        *,
        transport_key: TransportKey | None,
        status: str = "ok",
    ) -> None:
        attributes: dict[str, PerformanceValue] = {
            "work_kind": "tool" if isinstance(work, ToolRequest) else "diagnostics",
            "depth": work.queue_depth,
        }
        if isinstance(work, ToolRequest):
            attributes["request_kind"] = self._request_kind(work.method)
        else:
            attributes["document_committed"] = work.document_committed
        self._record_lsp_performance(
            "queue_wait",
            started_at=work.enqueued_at,
            status=status,
            transport_key=transport_key,
            attributes=attributes,
        )

    def start_worker(self) -> None:
        """Start the background worker thread (idempotent)."""
        if self._worker_thread is not None:
            return

        with self._lock:
            if self._worker_thread is not None or self._closed:
                return
            self._stop_event.clear()
            self._abort_current = False
            self._shutdown_deadline_at = None
            self._last_shutdown_completed = True
            self._accepting_work = True
            self._worker_thread = threading.Thread(
                target=self._worker_entry,
                name="lsp-worker",
                daemon=True,
            )
            self._worker_thread.start()
            logger.info("LSP worker thread started")

    def shutdown_all(self, *, timeout: float = WORKER_SHUTDOWN_TIMEOUT) -> bool:
        """Stop all LSP work within one deadline and report actual completion."""
        if timeout <= 0:
            raise ValueError("LSP shutdown timeout must be positive")
        deadline_at = time.monotonic() + timeout
        if not self._shutdown_lock.acquire(
            timeout=max(0.0, deadline_at - time.monotonic())
        ):
            return False
        try:
            return self._shutdown_all_locked(deadline_at=deadline_at)
        finally:
            self._shutdown_lock.release()

    def _shutdown_all_locked(self, *, deadline_at: float) -> bool:
        """Serialize shutdown ownership across callers."""
        logger.info("Shutting down LSP manager")
        had_worker = self._worker_thread is not None
        with self._lock:
            self._closed = True
            self._accepting_work = False
            self._shutdown_deadline_at = deadline_at
            self._abort_current = True
            self._stop_event.set()
            worker_loop = self._worker_loop
            active_work = tuple(self._active_work.values())
            for active in active_work:
                if active.kind == "tool":
                    request = active.item
                    assert isinstance(request, ToolRequest)
                    self._try_set_future_exception(
                        request.future,
                        self._freeze_failure(
                            LspClientError("LSP manager shutting down"),
                            request.transport_key,
                            phase=request.phase,
                        ),
                    )
            for request in self._tool_queue:
                self._try_set_future_exception(
                    request.future,
                    self._freeze_failure(
                        LspClientError("LSP manager shutting down"),
                        request.transport_key,
                        phase=request.phase,
                    ),
                )
            self._tool_queue.clear()
            for request in self._diagnostics_queue:
                self._complete_diagnostic_request_locked(
                    request,
                    status=DiagnosticOutcomeStatus.CANCELLED,
                    failure=self._diagnostic_failure_facts(
                        LspRequestCancelled("LSP manager shutting down"),
                        request.transport_key,
                        phase="shutdown",
                    ),
                )
            self._diagnostics_queue.clear()
            self._diagnostic_batches.clear()
            self._acknowledged_batches.clear()
            self._latest_diagnostic_sequence.clear()
            self._session_generations.clear()

        self._wake_worker()

        active_tasks = tuple(active.task for active in active_work)
        if worker_loop is not None and active_tasks:
            with suppress(RuntimeError):
                worker_loop.call_soon_threadsafe(
                    self._cancel_tasks_once,
                    active_tasks,
                )

        if self._worker_thread is not None:
            self._worker_thread.join(timeout=max(0.0, deadline_at - time.monotonic()))
            if self._worker_thread.is_alive():
                logger.warning("LSP worker thread did not join in time")
            else:
                self._worker_thread = None

        # Fallback for legacy/test-created clients when no worker is alive.
        # Runtime clients are created and closed by the worker event loop.
        if not had_worker and self._worker_thread is None and self._transports:
            try:
                self._last_shutdown_completed = asyncio.run(
                    self._shutdown_clients_async(deadline_at=deadline_at)
                )
            except Exception as error:
                self._last_shutdown_completed = False
                logger.warning(
                    "LSP fallback shutdown failed: error_type=%s",
                    type(error).__name__,
                )
        return (
            self._worker_thread is None
            and not self._transports
            and self._last_shutdown_completed
        )

    # === Diagnostics (fire-and-forget) ===

    def enqueue_diagnostics(
        self,
        file_path: Path,
        *,
        route: DiagnosticRoute | None = None,
        document_committed: bool = False,
    ) -> str | None:
        """Enqueue diagnostics and return the future batch identity.

        Stale rejection uses a manager-owned monotonic sequence because round
        numbers are not unique across multiple edits in one LLM response.
        ``document_committed`` keeps document sync, didSave, and diagnostics in
        one ordered worker item after a successful file mutation.
        """
        path = self._canonicalize_path(file_path)
        if not self._enabled_for_file(path):
            return None
        language = detect_language(path)
        if language is None:
            return None
        transport_key = self._transport_key(language, path)

        if route is None:
            route = DiagnosticRoute(file_path=path)
        elif self._canonicalize_path(route.file_path) != path:
            raise ValueError("diagnostic route file_path must match request path")
        elif route.file_path != path:
            route = replace(route, file_path=path)

        self.start_worker()
        with self._lock:
            if not self._accepting_work or self._stop_event.is_set():
                return None
            self._prune_expired_diagnostic_batches_locked()
            if route.agent_id is not None and route.session_generation is not None:
                current_generation = self._session_generations.get(route.agent_id)
                if (
                    current_generation is not None
                    and route.session_generation < current_generation
                ):
                    return None
                if (
                    current_generation is None
                    or route.session_generation > current_generation
                ):
                    self._advance_session_generation_locked(
                        route.agent_id, route.session_generation
                    )
            self._next_diagnostic_sequence += 1
            request_sequence = self._next_diagnostic_sequence
            self._next_work_sequence += 1
            enqueue_sequence = self._next_work_sequence
            key = (
                route.agent_id,
                route.session_generation,
                route.session_id,
                path,
            )
            self._latest_diagnostic_sequence[key] = request_sequence
            batch_id = uuid.uuid4().hex
            superseded = tuple(
                item
                for item in self._diagnostics_queue
                if (
                    item.route.agent_id,
                    item.route.session_generation,
                    item.route.session_id,
                    item.route.file_path,
                )
                == key
            )
            superseded_ids = {item.batch_id for item in superseded}
            self._diagnostics_queue = [
                item
                for item in self._diagnostics_queue
                if item.batch_id not in superseded_ids
            ]
            for item in superseded:
                self._complete_diagnostic_request_locked(
                    item,
                    status=DiagnosticOutcomeStatus.STALE_DISCARDED,
                )
            queue_depth = len(self._tool_queue) + len(self._diagnostics_queue) + 1
            self._diagnostics_queue.append(
                DiagnosticRequest(
                    batch_id=batch_id,
                    route=route,
                    request_sequence=request_sequence,
                    document_committed=document_committed,
                    transport_key=transport_key,
                    enqueue_sequence=enqueue_sequence,
                    queue_depth=queue_depth,
                )
            )
            self._pending_diagnostic_requests.add(batch_id)

        self._wake_worker()
        return batch_id

    def advance_session_generation(self, agent_id: str, generation: int) -> None:
        """Reject and evict diagnostics owned by an older Agent session."""
        if not agent_id:
            raise ValueError("agent_id must be non-empty")
        if generation < 0:
            raise ValueError("generation cannot be negative")
        with self._lock:
            current = self._session_generations.get(agent_id)
            if current is not None and generation <= current:
                return
            self._advance_session_generation_locked(agent_id, generation)

    def _advance_session_generation_locked(
        self, agent_id: str, generation: int
    ) -> None:
        self._prune_expired_diagnostic_batches_locked()
        self._session_generations[agent_id] = generation
        stale_requests = tuple(
            request
            for request in self._diagnostics_queue
            if self._is_older_generation(request.route, agent_id, generation)
        )
        stale_request_ids = {request.batch_id for request in stale_requests}
        self._diagnostics_queue = [
            request
            for request in self._diagnostics_queue
            if request.batch_id not in stale_request_ids
        ]
        for request in stale_requests:
            self._complete_diagnostic_request_locked(
                request,
                status=DiagnosticOutcomeStatus.STALE_DISCARDED,
            )
        previous_result_ids = set(self._diagnostic_batches) | set(
            self._diagnostic_failure_outcomes
        )
        self._diagnostic_batches = {
            batch_id: batch
            for batch_id, batch in self._diagnostic_batches.items()
            if not self._is_older_generation(batch.route, agent_id, generation)
        }
        self._diagnostic_failure_outcomes = {
            batch_id: outcome
            for batch_id, outcome in self._diagnostic_failure_outcomes.items()
            if not self._is_older_generation(outcome.route, agent_id, generation)
        }
        retained_result_ids = set(self._diagnostic_batches) | set(
            self._diagnostic_failure_outcomes
        )
        self._diagnostic_batch_metrics["stale_discarded"] += len(
            previous_result_ids - retained_result_ids
        )
        self._latest_diagnostic_sequence = {
            key: sequence
            for key, sequence in self._latest_diagnostic_sequence.items()
            if not (key[0] == agent_id and key[1] is not None and key[1] < generation)
        }

    @staticmethod
    def _is_older_generation(
        route: DiagnosticRoute, agent_id: str, generation: int
    ) -> bool:
        return (
            route.agent_id == agent_id
            and route.session_generation is not None
            and route.session_generation < generation
        )

    def pending_diagnostic_batches(
        self,
        *,
        route: DiagnosticRouteFilter | None = None,
        batch_id: str | None = None,
    ) -> tuple[DiagnosticBatch, ...]:
        """Return matching unacknowledged batches without consuming them."""
        with self._lock:
            self._prune_expired_diagnostic_batches_locked()
            return tuple(
                batch
                for current_id, batch in self._diagnostic_batches.items()
                if (batch_id is None or current_id == batch_id)
                and (route is None or self._route_matches(batch.route, route))
            )

    def diagnostic_request_result(
        self, batch_id: str
    ) -> tuple[DiagnosticBatch, ...] | None:
        """Return ``None`` while work is pending, otherwise its final batch set."""
        with self._lock:
            self._prune_expired_diagnostic_batches_locked()
            batch = self._diagnostic_batches.get(batch_id)
            if batch is not None:
                return (batch,)
            if batch_id in self._pending_diagnostic_requests:
                return None
            return ()

    def diagnostic_request_outcome(self, batch_id: str) -> DiagnosticOutcome | None:
        """Return a typed terminal outcome, or ``None`` while still pending.

        Published results are projected from the legacy batch store.  Failed,
        timed-out, stale and cancelled results are retained independently so
        they cannot be mistaken for a clean publish.
        """
        with self._lock:
            self._prune_expired_diagnostic_batches_locked()
            batch = self._diagnostic_batches.get(batch_id)
            if batch is not None:
                return DiagnosticOutcome.from_batch(batch)
            outcome = self._diagnostic_failure_outcomes.get(batch_id)
            if outcome is not None:
                return outcome
            return None

    def pending_diagnostic_batches_for_owner(
        self,
        *,
        agent_id: str | None,
        session_generation: int | None,
        session_id: str | None,
    ) -> tuple[DiagnosticBatch, ...]:
        """Return batches for one exact session owner, across origin turns."""
        owner = (agent_id, session_generation, session_id)
        with self._lock:
            self._prune_expired_diagnostic_batches_locked()
            return tuple(
                batch
                for batch in self._diagnostic_batches.values()
                if self._diagnostic_owner(batch.route) == owner
            )

    def pending_diagnostic_failure_outcomes_for_owner(
        self,
        *,
        agent_id: str | None,
        session_generation: int | None,
        session_id: str | None,
    ) -> tuple[DiagnosticOutcome, ...]:
        """Return unacknowledged non-published outcomes for one session owner."""
        owner = (agent_id, session_generation, session_id)
        with self._lock:
            self._prune_expired_diagnostic_batches_locked()
            return tuple(
                outcome
                for outcome in self._diagnostic_failure_outcomes.values()
                if self._diagnostic_owner(outcome.route) == owner
            )

    def acknowledge_diagnostic_batch(
        self,
        batch_id: str,
        *,
        consumer_id: str,
        carried_forward: bool = False,
    ) -> bool:
        """Acknowledge exactly one batch, preventing a second consumer."""
        with self._lock:
            batch = self._diagnostic_batches.pop(batch_id, None)
            outcome = self._diagnostic_failure_outcomes.pop(batch_id, None)
            if batch is None and outcome is None:
                return False
            self._record_acknowledgement(batch_id, consumer_id)
            if carried_forward:
                self._diagnostic_batch_metrics["carried_forward"] += 1
            return True

    def acknowledge_diagnostic_batches(
        self,
        batch_ids: tuple[str, ...],
        *,
        consumer_id: str,
        carried_forward_ids: set[str] | None = None,
    ) -> bool:
        """Atomically acknowledge an exact rendered terminal-result set."""
        carried = carried_forward_ids or set()
        with self._lock:
            available_ids = set(self._diagnostic_batches) | set(
                self._diagnostic_failure_outcomes
            )
            if len(set(batch_ids)) != len(batch_ids) or any(
                batch_id not in available_ids for batch_id in batch_ids
            ):
                return False
            for batch_id in batch_ids:
                self._diagnostic_batches.pop(batch_id, None)
                self._diagnostic_failure_outcomes.pop(batch_id, None)
                self._record_acknowledgement(batch_id, consumer_id)
                if batch_id in carried:
                    self._diagnostic_batch_metrics["carried_forward"] += 1
            return True

    def consume_diagnostic_batches(
        self,
        *,
        consumer_id: str,
        route: DiagnosticRouteFilter | None = None,
        batch_id: str | None = None,
    ) -> tuple[DiagnosticBatch, ...]:
        """Atomically select and acknowledge matching batches."""
        with self._lock:
            self._prune_expired_diagnostic_batches_locked()
            selected = tuple(
                batch
                for current_id, batch in self._diagnostic_batches.items()
                if (batch_id is None or current_id == batch_id)
                and (route is None or self._route_matches(batch.route, route))
            )
            for batch in selected:
                self._diagnostic_batches.pop(batch.batch_id, None)
                self._diagnostic_failure_outcomes.pop(batch.batch_id, None)
                self._record_acknowledgement(batch.batch_id, consumer_id)
            return selected

    def diagnostic_batch_acknowledgement(self, batch_id: str) -> str | None:
        """Return the consumer which acknowledged a batch, if any."""
        with self._lock:
            return self._acknowledged_batches.get(batch_id)

    def diagnostic_batch_metrics(self) -> dict[str, int]:
        """Return bounded-delivery counters for diagnostics batches."""
        with self._lock:
            self._prune_expired_diagnostic_batches_locked()
            return dict(self._diagnostic_batch_metrics)

    # === Active Tools (synchronous bridge) ===

    def send_request_sync(
        self,
        file_path: Path,
        method: str,
        params: dict[str, Any],
        timeout: float = 10.0,
        cancellation: CancellationSignal | None = None,
    ) -> Any:
        """Send a synchronous LSP request via the worker thread.

        Blocks the main thread until the worker resolves the future.
        The worker handles document sync (didOpen/didChange) before
        the actual LSP query.

        Raises LspClientError on timeout or server error.
        """
        if timeout <= 0:
            raise ValueError("LSP request timeout must be positive")
        path = self._canonicalize_path(file_path)
        lang = detect_language(path)
        if lang is None:
            raise LspClientError(f"No LSP support for file type: {path.suffix}")
        key = self._transport_key(lang, path)
        if cancellation is not None and cancellation.is_set():
            raise self._freeze_failure(
                LspRequestCancelled(f"LSP request '{method}' was cancelled"),
                key,
                phase="queue",
            )

        deadline_at = time.monotonic() + timeout
        # Start worker if not already running.  The worker owns LSP subprocesses,
        # so it also handles lazy spawn before executing the request.
        self.start_worker()

        # Enqueue the request — worker handles spawn + sync + query
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        req = ToolRequest(
            file_path=path,
            language_id=lang,
            method=method,
            params=params,
            future=future,
            timeout_seconds=timeout,
            deadline_at=deadline_at,
            needs_sync=True,
            transport_key=key,
        )

        with self._lock:
            if not self._accepting_work or self._stop_event.is_set():
                raise self._freeze_failure(
                    LspClientError("LSP manager shutting down"),
                    key,
                    phase="queue",
                )
            self._next_work_sequence += 1
            req.enqueue_sequence = self._next_work_sequence
            self._tool_queue.append(req)
            req.queue_depth = len(self._tool_queue) + len(self._diagnostics_queue)

        self._wake_worker()

        while True:
            remaining = deadline_at - time.monotonic()
            try:
                return future.result(
                    timeout=max(0.0, min(_TOOL_REQUEST_POLL_INTERVAL, remaining))
                )
            except concurrent.futures.TimeoutError:
                # A result settled at an interrupt boundary always wins.
                if future.done():
                    return future.result()

            if cancellation is not None and cancellation.is_set():
                if not self._abandon_tool_request(req, status="cancelled"):
                    return future.result()
                raise self._freeze_failure(
                    LspRequestCancelled(f"LSP request '{method}' was cancelled"),
                    key,
                    phase=req.phase,
                )
            if time.monotonic() >= deadline_at:
                if not self._abandon_tool_request(req, status="timeout"):
                    return future.result()
                raise self._timeout_error(req)

    # === Internal: Worker Thread ===

    def _worker_entry(self) -> None:
        """Entry point for the worker thread."""
        asyncio.run(self._async_worker_main())

    async def _async_worker_main(self) -> None:
        """Dispatch one active work item per transport on the worker loop."""
        worker_loop = asyncio.get_running_loop()
        wakeup = asyncio.Event()
        with self._lock:
            self._worker_loop = worker_loop
            self._worker_wakeup = wakeup

        try:
            while not self._stop_event.is_set():
                if self._dispatch_next_work():
                    continue
                wakeup.clear()
                if self._dispatch_next_work():
                    continue
                if self._stop_event.is_set():
                    break
                # ``call_soon_threadsafe`` normally wakes the selector
                # immediately.  Some restricted runtimes deny writes to
                # asyncio's internal socketpair, so keep a short bounded
                # fallback rather than leaving queued work asleep forever.
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        wakeup.wait(),
                        timeout=_WORKER_WAKEUP_FALLBACK_INTERVAL,
                    )
        finally:
            with self._lock:
                self._accepting_work = False
                active_work = tuple(self._active_work.values())
                for active in active_work:
                    if active.kind == "tool":
                        request = active.item
                        assert isinstance(request, ToolRequest)
                        self._try_set_future_exception(
                            request.future,
                            self._freeze_failure(
                                LspClientError("LSP worker stopped"),
                                request.transport_key,
                                phase=request.phase,
                            ),
                        )
                for request in self._tool_queue:
                    self._try_set_future_exception(
                        request.future,
                        self._freeze_failure(
                            LspClientError("LSP worker stopped"),
                            request.transport_key,
                            phase=request.phase,
                        ),
                    )
                self._tool_queue.clear()
                for request in self._diagnostics_queue:
                    self._complete_diagnostic_request_locked(
                        request,
                        status=DiagnosticOutcomeStatus.CANCELLED,
                        failure=self._diagnostic_failure_facts(
                            LspRequestCancelled("LSP worker stopped"),
                            request.transport_key,
                            phase="shutdown",
                        ),
                    )
                self._diagnostics_queue.clear()
            active_tasks = tuple(active.task for active in active_work)
            self._cancel_tasks_once(active_tasks)
            if active_tasks:
                await asyncio.gather(*active_tasks, return_exceptions=True)
            with self._lock:
                self._active_work.clear()
            try:
                self._last_shutdown_completed = await self._shutdown_clients_async(
                    deadline_at=self._shutdown_deadline_at
                    or time.monotonic() + WORKER_SHUTDOWN_TIMEOUT
                )
            except Exception as error:
                self._last_shutdown_completed = False
                logger.warning(
                    "LSP worker shutdown failed: error_type=%s",
                    type(error).__name__,
                )
            with self._lock:
                self._worker_loop = None
                self._worker_wakeup = None
            logger.info("LSP worker loop exited")

    def _dispatch_next_work(self) -> bool:
        """Atomically claim, create, and register one runnable work item."""
        with self._lock:
            if not self._accepting_work or self._stop_event.is_set():
                return False
            kind, work, transport_key = self._pop_next_work()
            if kind is None:
                return False
            if kind == "tool":
                work_coro = self._handle_tool_request(
                    work,
                    transport_key=transport_key,
                )
            else:
                work_coro = self._handle_diagnostics_request(
                    work,
                    transport_key=transport_key,
                )
            work_task = asyncio.create_task(work_coro)
            self._active_work[transport_key] = _ActiveWork(
                kind=kind,
                item=work,
                task=work_task,
            )
            work_task.add_done_callback(partial(self._work_task_done, transport_key))
            return True

    def _pop_next_work(
        self,
    ) -> tuple[str | None, Any, TransportKey | None]:
        """Pop the oldest runnable item across both ingress queues."""
        with self._lock:
            candidates: list[tuple[int, int, int, str, TransportKey | None]] = []
            for kind_rank, (kind, queue) in enumerate(
                (("tool", self._tool_queue), ("diagnostics", self._diagnostics_queue))
            ):
                for index, work in enumerate(queue):
                    transport_key = self._work_transport_key(kind, work)
                    if transport_key in self._active_work:
                        continue
                    candidates.append(
                        (
                            work.enqueue_sequence,
                            kind_rank,
                            index,
                            kind,
                            transport_key,
                        )
                    )
            if candidates:
                _, _, index, kind, transport_key = min(candidates)
                queue = self._tool_queue if kind == "tool" else self._diagnostics_queue
                return kind, queue.pop(index), transport_key
        return None, None, None

    def _work_transport_key(
        self,
        kind: str,
        work: ToolRequest | DiagnosticRequest,
    ) -> TransportKey | None:
        if work.transport_key is not None:
            return work.transport_key
        if kind == "tool":
            return self._transport_key(work.language_id, work.file_path)
        language = detect_language(work.route.file_path)
        if language is None:
            return None
        return self._transport_key(language, work.route.file_path)

    @staticmethod
    def _cancel_tasks_once(tasks: tuple[asyncio.Task[None], ...]) -> None:
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()

    def _work_task_done(
        self,
        transport_key: TransportKey | None,
        task: asyncio.Task[None],
    ) -> None:
        with self._lock:
            active = self._active_work.get(transport_key)
            if active is not None and active.task is task:
                self._active_work.pop(transport_key, None)
            wakeup = self._worker_wakeup
        if wakeup is not None:
            wakeup.set()
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "Unhandled LSP worker task error: error_type=%s",
                type(error).__name__,
            )

    def _wake_worker(self) -> None:
        with self._lock:
            worker_loop = self._worker_loop
            wakeup = self._worker_wakeup
        if worker_loop is not None and wakeup is not None:
            with suppress(RuntimeError):
                worker_loop.call_soon_threadsafe(wakeup.set)

    async def _handle_tool_request(
        self,
        req: ToolRequest,
        *,
        transport_key: TransportKey | None = None,
    ) -> None:
        """Process a synchronous active-tool request."""
        key = (
            transport_key
            or req.transport_key
            or self._transport_key(req.language_id, req.file_path)
        )
        total_status = "ok"
        total_attributes: dict[str, PerformanceValue] = {
            "work_kind": "tool",
            "request_kind": self._request_kind(req.method),
        }
        self._record_queue_wait(
            req,
            transport_key=key,
            status=req.abandonment_status
            or ("cancelled" if req.future.cancelled() else "ok"),
        )
        if req.future.cancelled():
            total_attributes["outcome"] = (
                "deadline_exhausted"
                if req.abandonment_status == "timeout"
                else "caller_abandoned"
            )
            self._record_lsp_performance(
                "total",
                started_at=req.enqueued_at,
                status=req.abandonment_status or "cancelled",
                transport_key=key,
                attributes=total_attributes,
            )
            return
        operation = asyncio.create_task(
            self._execute_tool_request(req, transport_key=transport_key)
        )
        try:
            while not operation.done():
                if req.future.cancelled():
                    total_status = req.abandonment_status or "cancelled"
                    total_attributes["outcome"] = (
                        "deadline_exhausted"
                        if req.abandonment_status == "timeout"
                        else "caller_abandoned"
                    )
                    return
                if self._abort_current:
                    raise self._freeze_failure(
                        LspClientError("LSP manager shutting down"),
                        key,
                        phase=req.phase,
                    )
                remaining = req.deadline_at - time.monotonic()
                if remaining <= 0:
                    raise self._timeout_error(req)
                await asyncio.wait(
                    {operation},
                    timeout=min(_TOOL_REQUEST_POLL_INTERVAL, remaining),
                )

            result = await operation
            if not self._try_set_future_result(req.future, result):
                total_status = req.abandonment_status or "cancelled"
                total_attributes["outcome"] = "late_completion"
        except asyncio.CancelledError:
            total_status = req.abandonment_status or "cancelled"
            if req.abandonment_status is not None:
                total_attributes["outcome"] = (
                    "deadline_exhausted"
                    if req.abandonment_status == "timeout"
                    else "caller_abandoned"
                )
            self._try_set_future_exception(
                req.future,
                self._freeze_failure(
                    LspRequestCancelled("LSP request cancelled"),
                    key,
                    phase=req.phase,
                ),
            )
        except Exception as e:
            total_status = self._performance_status(e)
            total_attributes["error_type"] = type(e).__name__
            if isinstance(e, LspRequestTimedOut):
                total_attributes["outcome"] = "deadline_exhausted"
            self._try_set_future_exception(
                req.future,
                self._freeze_failure(e, key, phase=req.phase),
            )
        finally:
            await self._cancel_and_wait_task(operation)
            self._record_lsp_performance(
                "total",
                started_at=req.enqueued_at,
                status=total_status,
                transport_key=key,
                attributes=total_attributes,
            )

    async def _execute_tool_request(
        self,
        req: ToolRequest,
        *,
        transport_key: TransportKey | None = None,
    ) -> Any:
        """Run start, document sync and query under one caller-owned deadline."""
        key = (
            transport_key
            or req.transport_key
            or self._transport_key(req.language_id, req.file_path)
        )
        req.phase = "availability"
        try:
            server = await self._get_or_create_server(
                req.language_id,
                req.file_path,
                transport_key=key,
            )
        except Exception as error:
            frozen = self._freeze_failure(error, key, phase=req.phase)
            if frozen is error:
                raise
            raise frozen from error
        if server is None:
            raise self._freeze_failure(
                LspServerUnavailable("LSP server unavailable"),
                key,
                phase=req.phase,
            )

        # Document sync before query (if needed)
        if req.needs_sync:
            req.phase = "document_sync"
            sync_started_at = time.monotonic()
            sync_status = "skipped"
            sync_attributes: dict[str, PerformanceValue] = {
                "work_kind": "tool",
                "sync_kind": "unchanged",
                "request_kind": self._request_kind(req.method),
            }
            try:
                document_key = (key, req.file_path)
                with self._lock:
                    last_stamp = self._document_sync_stamps.get(document_key)
                snapshot = self._load_document_for_sync(
                    req.file_path,
                    last_stamp=last_stamp,
                    force=False,
                )
                if snapshot is not None:
                    sync_kind = "open" if last_stamp is None else "change"
                    sync_attributes["sync_kind"] = sync_kind
                    if last_stamp is None:
                        await server.did_open(req.file_path, snapshot.content)
                    else:
                        await server.did_change(req.file_path, snapshot.content)
                    with self._lock:
                        self._document_sync_stamps[document_key] = snapshot.stamp
                    sync_attributes["document_version"] = self._document_version(
                        server,
                        req.file_path,
                    )
                    sync_status = "ok"
            except Exception as e:
                sync_status = self._performance_status(e)
                sync_attributes["error_type"] = type(e).__name__
                logger.warning(
                    "LSP tool document sync failed: language=%s error_type=%s",
                    get_language_id_string(req.language_id),
                    type(e).__name__,
                )
                frozen = self._freeze_failure(e, key, phase=req.phase)
                if frozen is e:
                    raise
                raise frozen from e
            except BaseException as error:
                sync_status = self._performance_status(error)
                sync_attributes["error_type"] = type(error).__name__
                raise
            finally:
                self._record_lsp_performance(
                    "document_sync",
                    started_at=sync_started_at,
                    status=sync_status,
                    transport_key=key,
                    attributes=sync_attributes,
                )

        req.phase = "request"
        if self._abort_current:
            raise self._freeze_failure(
                LspClientError("LSP manager shutting down"),
                key,
                phase=req.phase,
            )
        remaining = req.deadline_at - time.monotonic()
        if remaining <= 0:
            raise self._timeout_error(req)
        try:
            return await self._observe_lsp_phase(
                "request",
                server.send_request(req.method, req.params, timeout=remaining),
                transport_key=key,
                attributes={
                    "request_kind": self._request_kind(req.method),
                    "document_version": self._document_version(server, req.file_path),
                },
            )
        except Exception as error:
            frozen = self._freeze_failure(error, key, phase=req.phase)
            if frozen is error:
                raise
            raise frozen from error

    def _timeout_error(self, req: ToolRequest) -> Exception:
        error = LspRequestTimedOut(
            f"LSP request '{req.method}' timed out after {req.timeout_seconds:g}s total"
        )
        key = req.transport_key or self._transport_key(
            req.language_id,
            req.file_path,
        )
        return self._freeze_failure(error, key, phase=req.phase)

    @staticmethod
    def _try_set_future_result(
        future: concurrent.futures.Future[Any], result: Any
    ) -> bool:
        try:
            future.set_result(result)
        except concurrent.futures.InvalidStateError:
            return False
        return True

    @staticmethod
    def _try_set_future_exception(
        future: concurrent.futures.Future[Any], error: BaseException
    ) -> bool:
        try:
            future.set_exception(error)
        except concurrent.futures.InvalidStateError:
            return False
        return True

    @staticmethod
    async def _cancel_and_wait_task(task: asyncio.Task[Any]) -> None:
        """Cancel one child operation and shield its cleanup from outer cancel."""
        if not task.done() and not task.cancelling():
            task.cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    break
            except Exception:
                break
        if task.done():
            with suppress(asyncio.CancelledError, Exception):
                task.result()

    def _abandon_tool_request(self, req: ToolRequest, *, status: str) -> bool:
        with self._lock:
            req.abandonment_status = status
            if not req.future.cancel():
                req.abandonment_status = None
                return False
            queued = any(item is req for item in self._tool_queue)
            self._tool_queue = [item for item in self._tool_queue if item is not req]
        if queued:
            key = req.transport_key or self._transport_key(
                req.language_id,
                req.file_path,
            )
            self._record_queue_wait(req, transport_key=key, status=status)
            self._record_lsp_performance(
                "total",
                started_at=req.enqueued_at,
                status=status,
                transport_key=key,
                attributes={
                    "work_kind": "tool",
                    "request_kind": self._request_kind(req.method),
                    "outcome": (
                        "deadline_exhausted"
                        if status == "timeout"
                        else "caller_abandoned"
                    ),
                },
            )
        return True

    async def _handle_diagnostics_request(
        self,
        request: DiagnosticRequest,
        *,
        transport_key: TransportKey | None = None,
    ) -> None:
        """Process diagnostics and retain exactly one typed terminal outcome."""
        file_path = request.route.file_path
        lang = detect_language(file_path)
        resolved_transport_key = (
            transport_key
            or request.transport_key
            or (self._transport_key(lang, file_path) if lang is not None else None)
        )
        total_status = "ok"
        total_attributes: dict[str, PerformanceValue] = {
            "work_kind": "diagnostics",
            "document_committed": request.document_committed,
        }
        failure_phase = "availability"
        terminal_status: DiagnosticOutcomeStatus | None = None
        terminal_batch: DiagnosticBatch | None = None
        terminal_failure: LspFailureFacts | None = None
        self._record_queue_wait(request, transport_key=resolved_transport_key)

        try:
            if lang is None:
                raise LspClientError("Unsupported diagnostics language")
            server = await self._get_or_create_server(
                lang,
                file_path,
                transport_key=resolved_transport_key,
            )
            if server is None:
                total_status = "unavailable"
                total_attributes["outcome"] = "server_unavailable"
                terminal_status = DiagnosticOutcomeStatus.SERVER_UNAVAILABLE
                terminal_failure = self._diagnostic_failure_facts(
                    LspServerUnavailable("LSP server unavailable"),
                    resolved_transport_key,
                    phase=failure_phase,
                )
                return

            baseline_generation = server.diagnostics_generation(file_path)

            # Sync file content
            # A successful mutation is authoritative even when the filesystem
            # timestamp has not advanced (coarse or preserved mtimes). Always
            # send the committed on-disk content before didSave.
            failure_phase = "document_sync"
            sync_started_at = time.monotonic()
            sync_status = "skipped"
            sync_attributes: dict[str, PerformanceValue] = {
                "work_kind": "diagnostics",
                "sync_kind": "unchanged",
                "document_committed": request.document_committed,
            }
            try:
                document_key = (resolved_transport_key, file_path)
                with self._lock:
                    last_stamp = self._document_sync_stamps.get(document_key)
                snapshot = self._load_document_for_sync(
                    file_path,
                    last_stamp=last_stamp,
                    force=request.document_committed,
                )
                if snapshot is not None:
                    sync_attributes["sync_kind"] = (
                        "open" if last_stamp is None else "change"
                    )
                    try:
                        if last_stamp is None:
                            await server.did_open(file_path, snapshot.content)
                        else:
                            await server.did_change(file_path, snapshot.content)
                        with self._lock:
                            self._document_sync_stamps[document_key] = snapshot.stamp
                        sync_status = "ok"
                    except Exception as error:
                        sync_status = self._performance_status(error)
                        sync_attributes["error_type"] = type(error).__name__
                        logger.warning(
                            "LSP diagnostics document sync failed: "
                            "language=%s error_type=%s",
                            get_language_id_string(lang),
                            type(error).__name__,
                        )
                        raise

                # A successful file mutation is one ordered operation:
                # synchronize the committed content, then notify save, then
                # observe diagnostics. Keeping these steps in this request
                # prevents queue priority from moving diagnostics ahead of
                # didSave.
                if request.document_committed:
                    await server.did_save(file_path)
                    sync_attributes["document_version"] = self._document_version(
                        server,
                        file_path,
                    )
                    if "error_type" not in sync_attributes:
                        sync_status = "ok"
                await server.refresh_diagnostics(file_path)
            except Exception as e:
                sync_status = self._performance_status(e)
                sync_attributes["error_type"] = type(e).__name__
                logger.warning(
                    "LSP diagnostics sync phase failed: language=%s error_type=%s",
                    get_language_id_string(lang),
                    type(e).__name__,
                )
                raise
            except BaseException as error:
                sync_status = self._performance_status(error)
                sync_attributes["error_type"] = type(error).__name__
                raise
            finally:
                self._record_lsp_performance(
                    "document_sync",
                    started_at=sync_started_at,
                    status=sync_status,
                    transport_key=resolved_transport_key,
                    attributes=sync_attributes,
                )

            # Wait for diagnostics
            failure_phase = "diagnostics_wait"
            wait_started_at = time.monotonic()
            wait_status = "ok"
            wait_attributes: dict[str, PerformanceValue] = {
                "document_committed": request.document_committed,
                "document_version": self._document_version(server, file_path),
            }
            try:
                diagnostics = await server.wait_for_diagnostics(
                    file_path,
                    timeout=self._config.poll_timeout_ms / 1000,
                    after_generation=baseline_generation,
                )
                diagnostic_generation = server.diagnostics_generation(file_path)
                wait_attributes["diagnostic_generation"] = diagnostic_generation
                wait_attributes["diagnostic_count"] = len(diagnostics)
                if diagnostic_generation <= baseline_generation:
                    wait_status = "timeout"
                    wait_attributes["outcome"] = "timeout"
                else:
                    wait_attributes["outcome"] = "published"
            except BaseException as error:
                wait_status = self._performance_status(error)
                wait_attributes["error_type"] = type(error).__name__
                raise
            finally:
                self._record_lsp_performance(
                    "diagnostics_wait",
                    started_at=wait_started_at,
                    status=wait_status,
                    transport_key=resolved_transport_key,
                    attributes=wait_attributes,
                )
            if diagnostic_generation <= baseline_generation:
                # Timeout is not an explicit clean publish.  Retaining no batch
                # is safer than clearing diagnostics from a previous version.
                total_status = "timeout"
                total_attributes["outcome"] = "timeout"
                terminal_status = DiagnosticOutcomeStatus.TIMED_OUT
                terminal_failure = self._diagnostic_failure_facts(
                    LspRequestTimedOut("LSP diagnostics publish timed out"),
                    resolved_transport_key,
                    phase=failure_phase,
                )
                return

            block = DiagnosticBlock(
                file_path=self._relativize_path(file_path),
                items=diagnostics,
            )
            terminal_batch = DiagnosticBatch(
                batch_id=request.batch_id,
                route=request.route,
                request_sequence=request.request_sequence,
                document_version=server.diagnostic_document_version(file_path),
                diagnostic_generation=diagnostic_generation,
                block=block,
                created_at=self._diagnostic_clock(),
            )
            terminal_status = (
                DiagnosticOutcomeStatus.PUBLISHED_NONEMPTY
                if diagnostics
                else DiagnosticOutcomeStatus.PUBLISHED_CLEAN
            )
            total_attributes["outcome"] = terminal_status.value
            total_attributes["diagnostic_count"] = len(diagnostics)
            total_attributes["diagnostic_generation"] = diagnostic_generation

        except asyncio.CancelledError as error:
            total_status = "cancelled"
            total_attributes["error_type"] = type(error).__name__
            total_attributes["outcome"] = "cancelled"
            terminal_status = DiagnosticOutcomeStatus.CANCELLED
            terminal_failure = self._diagnostic_failure_facts(
                LspRequestCancelled("LSP diagnostics request cancelled"),
                resolved_transport_key,
                phase=failure_phase,
            )
            raise
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            total_status = self._performance_status(e)
            total_attributes["error_type"] = type(e).__name__
            logger.warning(
                "LSP diagnostics transport error: language=%s error_type=%s",
                lang.name if lang is not None else "unknown",
                type(e).__name__,
            )
            if lang is not None:
                self._on_transport_error(
                    lang,
                    file_path,
                    type(e).__name__,
                    transport_key=resolved_transport_key,
                )
            terminal_status = DiagnosticOutcomeStatus.ERROR
            total_attributes["outcome"] = terminal_status.value
            terminal_failure = self._diagnostic_failure_facts(
                e,
                resolved_transport_key,
                phase=failure_phase,
            )
        except Exception as e:
            total_status = self._performance_status(e)
            total_attributes["error_type"] = type(e).__name__
            logger.warning(
                "LSP diagnostics request failed: language=%s error_type=%s",
                lang.name if lang is not None else "unknown",
                type(e).__name__,
            )
            if isinstance(e, (LspRequestTimedOut, asyncio.TimeoutError)):
                terminal_status = DiagnosticOutcomeStatus.TIMED_OUT
            elif isinstance(e, LspServerUnavailable):
                terminal_status = DiagnosticOutcomeStatus.SERVER_UNAVAILABLE
            else:
                terminal_status = DiagnosticOutcomeStatus.ERROR
            total_attributes["outcome"] = terminal_status.value
            terminal_failure = self._diagnostic_failure_facts(
                e,
                resolved_transport_key,
                phase=failure_phase,
            )
        finally:
            if terminal_status is None:
                terminal_status = DiagnosticOutcomeStatus.ERROR
                total_status = "error"
                total_attributes["outcome"] = terminal_status.value
                terminal_failure = self._diagnostic_failure_facts(
                    LspClientError("Diagnostics request ended without a result"),
                    resolved_transport_key,
                    phase=failure_phase,
                )

            completed = False
            published_batch: DiagnosticBatch | None = None
            with self._lock:
                if terminal_status in {
                    DiagnosticOutcomeStatus.PUBLISHED_NONEMPTY,
                    DiagnosticOutcomeStatus.PUBLISHED_CLEAN,
                }:
                    route_key = (
                        request.route.agent_id,
                        request.route.session_generation,
                        request.route.session_id,
                        file_path,
                    )
                    if (
                        self._abort_current
                        or self._latest_diagnostic_sequence.get(route_key)
                        != request.request_sequence
                        or not self._route_generation_is_current(request.route)
                    ):
                        terminal_status = DiagnosticOutcomeStatus.STALE_DISCARDED
                        terminal_batch = None
                        total_status = "cancelled"
                        total_attributes["outcome"] = terminal_status.value
                        self._diagnostic_batch_metrics["stale_discarded"] += 1
                completed = self._complete_diagnostic_request_locked(
                    request,
                    status=terminal_status,
                    batch=terminal_batch,
                    failure=terminal_failure,
                )
                if completed and terminal_status in {
                    DiagnosticOutcomeStatus.PUBLISHED_NONEMPTY,
                    DiagnosticOutcomeStatus.PUBLISHED_CLEAN,
                }:
                    published_batch = terminal_batch

            if published_batch is not None:
                # Runtime event delivery is an observer.  Its own failure is
                # isolated and cannot rewrite the already committed primary
                # diagnostics outcome.
                try:
                    self._publish_diagnostic_event(published_batch)
                except Exception as error:
                    logger.warning(
                        "Runtime diagnostics observer failed: error_type=%s",
                        type(error).__name__,
                    )
            self._record_lsp_performance(
                "total",
                started_at=request.enqueued_at,
                status=total_status,
                transport_key=resolved_transport_key,
                attributes=total_attributes,
            )

    def _route_generation_is_current(self, route: DiagnosticRoute) -> bool:
        if route.agent_id is None or route.session_generation is None:
            return True
        current = self._session_generations.get(route.agent_id)
        return current is None or route.session_generation >= current

    @staticmethod
    def _route_matches(actual: DiagnosticRoute, query: DiagnosticRouteFilter) -> bool:
        if query.file_path is not None and actual.file_path != query.file_path:
            return False
        for name in (
            "agent_id",
            "session_generation",
            "session_id",
            "turn_id",
            "tool_call_id",
        ):
            expected = getattr(query, name)
            if expected is not None and getattr(actual, name) != expected:
                return False
        return True

    @staticmethod
    def _diagnostic_owner(route: DiagnosticRoute) -> DiagnosticOwnerKey:
        return route.agent_id, route.session_generation, route.session_id

    def _complete_diagnostic_request_locked(
        self,
        request: DiagnosticRequest,
        *,
        status: DiagnosticOutcomeStatus,
        batch: DiagnosticBatch | None = None,
        failure: LspFailureFacts | None = None,
    ) -> bool:
        """Atomically move one accepted request from pending to one terminal."""
        if request.batch_id not in self._pending_diagnostic_requests:
            return False

        if status in {
            DiagnosticOutcomeStatus.PUBLISHED_NONEMPTY,
            DiagnosticOutcomeStatus.PUBLISHED_CLEAN,
        }:
            if batch is None:
                raise ValueError("published diagnostic outcome requires a batch")
            self._store_diagnostic_batch_locked(batch)
        else:
            if batch is not None:
                raise ValueError("failed diagnostic outcome cannot carry a batch")
            self._store_diagnostic_failure_outcome_locked(
                DiagnosticOutcome(
                    batch_id=request.batch_id,
                    route=request.route,
                    request_sequence=request.request_sequence,
                    status=status,
                    created_at=self._diagnostic_clock(),
                    failure=failure,
                )
            )

        self._pending_diagnostic_requests.remove(request.batch_id)
        self._diagnostic_batch_metrics[f"outcome_{status.value}"] += 1
        return True

    def _store_diagnostic_failure_outcome_locked(
        self, outcome: DiagnosticOutcome
    ) -> None:
        """Retain a real failure without changing published document state."""
        if outcome.is_published:
            raise ValueError("published outcomes belong in the diagnostic batch store")
        self._prune_expired_diagnostic_batches_locked()
        self._diagnostic_failure_outcomes[outcome.batch_id] = outcome
        self._bound_diagnostic_results_for_owner_locked(
            self._diagnostic_owner(outcome.route)
        )

    def _store_diagnostic_batch_locked(self, batch: DiagnosticBatch) -> None:
        """Store the newest document state and bound one owner's backlog."""
        self._prune_expired_diagnostic_batches_locked()
        owner = self._diagnostic_owner(batch.route)

        overwritten = [
            batch_id
            for batch_id, pending in self._diagnostic_batches.items()
            if self._diagnostic_owner(pending.route) == owner
            and pending.route.file_path == batch.route.file_path
        ]
        for batch_id in overwritten:
            self._diagnostic_batches.pop(batch_id, None)
        self._diagnostic_batch_metrics["overwritten"] += len(overwritten)

        self._diagnostic_batches[batch.batch_id] = batch
        self._bound_diagnostic_results_for_owner_locked(owner)

    def _bound_diagnostic_results_for_owner_locked(
        self, owner: DiagnosticOwnerKey
    ) -> None:
        published = sorted(
            (
                batch.created_at,
                batch.request_sequence,
                batch.batch_id,
            )
            for batch in self._diagnostic_batches.values()
            if self._diagnostic_owner(batch.route) == owner
        )
        failed = sorted(
            (
                outcome.created_at,
                outcome.request_sequence,
                outcome.batch_id,
            )
            for outcome in self._diagnostic_failure_outcomes.values()
            if self._diagnostic_owner(outcome.route) == owner
        )
        overflow = (
            len(published) + len(failed) - MAX_PENDING_DIAGNOSTIC_BATCHES_PER_OWNER
        )
        # A failure is observation, not a new document state.  Prefer evicting
        # old failure observations so their arrival cannot clear the last
        # successfully published diagnostics for a document.
        evicted = (failed + published)[: max(0, overflow)]
        for _, _, batch_id in evicted:
            self._diagnostic_batches.pop(batch_id, None)
            self._diagnostic_failure_outcomes.pop(batch_id, None)
        self._diagnostic_batch_metrics["capacity_evicted"] += len(evicted)

    def _prune_expired_diagnostic_batches_locked(self) -> None:
        cutoff = self._diagnostic_clock() - DIAGNOSTIC_BATCH_TTL_SECONDS
        expired_batches = [
            batch_id
            for batch_id, batch in self._diagnostic_batches.items()
            if batch.created_at <= cutoff
        ]
        expired_outcomes = [
            batch_id
            for batch_id, outcome in self._diagnostic_failure_outcomes.items()
            if outcome.created_at <= cutoff
        ]
        for batch_id in expired_batches:
            self._diagnostic_batches.pop(batch_id, None)
        for batch_id in expired_outcomes:
            self._diagnostic_failure_outcomes.pop(batch_id, None)
        self._diagnostic_batch_metrics["expired"] += len(expired_batches) + len(
            expired_outcomes
        )

    def _record_acknowledgement(self, batch_id: str, consumer_id: str) -> None:
        self._acknowledged_batches[batch_id] = consumer_id
        while len(self._acknowledged_batches) > 1024:
            self._acknowledged_batches.pop(next(iter(self._acknowledged_batches)))

    def _publish_diagnostic_event(self, batch: DiagnosticBatch) -> None:
        sink = self._runtime_event_sink
        if sink is None:
            return
        if batch.block.items:
            payload = DiagnosticsPublished(
                batch_id=batch.batch_id,
                file_path=batch.block.file_path,
                document_version=batch.document_version,
                diagnostic_generation=batch.diagnostic_generation,
                diagnostics=tuple(
                    RuntimeDiagnostic(
                        line=item.line,
                        character=item.character,
                        message=item.message,
                        severity=item.severity_label.lower(),
                        code=item.code,
                    )
                    for item in batch.block.items
                ),
            )
        else:
            payload = DiagnosticsCleared(
                batch_id=batch.batch_id,
                file_path=batch.block.file_path,
                document_version=batch.document_version,
                diagnostic_generation=batch.diagnostic_generation,
            )
        try:
            sink(
                RuntimeEvent(
                    payload=payload,
                    agent_id=batch.route.agent_id,
                    session_generation=batch.route.session_generation,
                    session_id=batch.route.session_id,
                    turn_id=batch.route.turn_id,
                    correlation_id=batch.route.tool_call_id or batch.batch_id,
                )
            )
        except Exception as error:
            logger.warning(
                "Runtime diagnostics event sink failed: error_type=%s",
                type(error).__name__,
            )

    # === Internal: Server Lifecycle ===

    async def _get_or_create_server(
        self,
        lang: LanguageId,
        file_path: Path,
        *,
        transport_key: TransportKey | None = None,
    ) -> LspClient | None:
        """Get or create an LSP server (called from worker thread)."""
        key = transport_key or self._transport_key(lang, file_path)
        self._ensure_transport_status(key)
        server = self._transports.get(key)
        if server is not None and server.is_usable:
            return server

        count = self._re_spawn_counts.get(key, 0)
        if count >= MAX_RESPWANS:
            logger.error(
                "LSP respawn limit reached: language=%s root_hash=%s limit=%d",
                lang.name,
                self._workspace_identifier(key[1]),
                MAX_RESPWANS,
            )
            status = self._ensure_transport_status(key)
            self._transition_transport(
                key,
                status.generation,
                LspTransportState.ERROR,
                error_type="RespawnLimitReached",
                error_phase="restart",
            )
            return None

        if server is not None:
            await self._discard_transport_async(key, server)
            with self._lock:
                self._re_spawn_counts[key] = count + 1
            if count + 1 >= MAX_RESPWANS:
                status = self._ensure_transport_status(key)
                self._transition_transport(
                    key,
                    status.generation,
                    LspTransportState.ERROR,
                    error_type="RespawnLimitReached",
                    error_phase="restart",
                )
                return None

        return await self._spawn_async(
            lang,
            file_path,
            transport_key=key,
        )

    async def _spawn_async(
        self,
        lang: LanguageId,
        file_path: Path,
        *,
        transport_key: TransportKey | None = None,
    ) -> LspClient | None:
        """Spawn + initialize from the worker thread (inline await)."""
        key = transport_key or self._transport_key(lang, file_path)
        root = key[1]
        self._ensure_transport_status(key)
        launch = self._resolve_launch(lang, root)
        cmd, args = launch.command, list(launch.args)
        init_opts = launch.initialization_options

        cached_retry_at = self._negative_command_retry_at(key, cmd)
        if cached_retry_at is not None:
            status = self._ensure_transport_status(key)
            if status.state is not LspTransportState.ERROR:
                generation = (
                    self._begin_transport_attempt(key, cmd)
                    if status.generation == 0
                    else status.generation
                )
                self._transition_transport(
                    key,
                    generation,
                    LspTransportState.ERROR,
                    command=cmd,
                    error_type="LauncherNotFound",
                    error_phase="availability",
                    retry_at=cached_retry_at,
                )
            return None

        generation = self._begin_transport_attempt(key, cmd)
        found, retry_at = self._lookup_command_availability(key, cmd)
        if not found:
            logger.info(
                "LSP command unavailable: language=%s launcher=%s",
                get_language_id_string(lang),
                self._launcher_name(cmd),
            )
            self._transition_transport(
                key,
                generation,
                LspTransportState.ERROR,
                command=cmd,
                error_type="LauncherNotFound",
                error_phase="availability",
                retry_at=retry_at,
            )
            return None

        self._transition_transport(
            key,
            generation,
            LspTransportState.STARTING,
            command=cmd,
        )
        client = LspClient(
            language_id=lang,
            workspace_root=root,
            on_unexpected_exit=lambda current, reason, returncode: self._on_client_exit(
                key,
                current,
                generation,
                reason,
                returncode,
            ),
        )

        failure_phase = "spawn"
        try:
            cold_start = generation == 1
            await self._observe_lsp_phase(
                "spawn",
                client.spawn(cmd, args),
                transport_key=key,
                attributes={"cold_start": cold_start},
            )
            if not self._transition_transport(
                key,
                generation,
                LspTransportState.INITIALIZING,
                command=cmd,
            ):
                await self._close_transport_observed(key, client, graceful=False)
                return None
            failure_phase = "initialize"
            await self._observe_lsp_phase(
                "initialize",
                client.initialize(init_opts),
                transport_key=key,
                attributes={"cold_start": cold_start},
            )
            await asyncio.sleep(0)
            if not client.is_usable:
                raise LspClientError("LSP server exited during initialization")
        except asyncio.CancelledError:
            if self._stop_event.is_set():
                self._transition_transport(
                    key,
                    generation,
                    LspTransportState.STOPPING,
                    command=cmd,
                )
                await self._close_transport_observed(
                    key,
                    client,
                    deadline_at=self._shutdown_deadline_at,
                    graceful=False,
                )
                self._transition_transport(
                    key,
                    generation,
                    LspTransportState.STOPPED,
                    command=cmd,
                )
            else:
                await self._close_transport_observed(key, client, graceful=False)
                stderr_ref = self._retain_client_stderr(key, generation, client)
                self._transition_transport(
                    key,
                    generation,
                    LspTransportState.ERROR,
                    command=cmd,
                    error_type="StartCancelled",
                    error_phase=failure_phase,
                    stderr_ref=stderr_ref,
                )
            raise
        except Exception as e:
            try:
                await self._close_transport_observed(key, client, graceful=False)
            except Exception as cleanup_error:
                self._record_client_cleanup_error(
                    client,
                    "manager_start_cleanup_failed",
                    cleanup_error,
                )
                logger.warning(
                    "LSP failed-start cleanup failed: language=%s error_type=%s",
                    lang.name,
                    type(cleanup_error).__name__,
                )
            stderr_ref = self._retain_client_stderr(key, generation, client)
            logger.warning(
                "Failed to start LSP server: language=%s launcher=%s "
                "arg_count=%d error_type=%s",
                lang.name,
                self._launcher_name(cmd),
                len(args),
                type(e).__name__,
            )
            with self._lock:
                self._re_spawn_counts[key] = self._re_spawn_counts.get(key, 0) + 1
            self._transition_transport(
                key,
                generation,
                LspTransportState.ERROR,
                command=cmd,
                error_type=type(e).__name__,
                error_phase=failure_phase,
                protocol_error_code=(e.code if isinstance(e, LspServerError) else None),
                stderr_ref=stderr_ref,
            )
            return None

        with self._lock:
            status = self._ensure_transport_status_locked(key)
            if (
                status.generation != generation
                or status.state is not LspTransportState.INITIALIZING
            ):
                stale = True
            else:
                stale = False
                self._transports[key] = client
                self._re_spawn_counts[key] = 0
                self._record_transport_status_locked(
                    key,
                    state=LspTransportState.READY,
                    generation=generation,
                    launcher=self._launcher_name(cmd),
                )
        if stale:
            await self._close_transport_observed(key, client, graceful=False)
            return None

        logger.info(
            "LSP server ready: language=%s root_hash=%s generation=%d",
            get_language_id_string(lang),
            self._workspace_identifier(root),
            generation,
        )
        return client

    async def _close_transport_observed(
        self,
        key: TransportKey,
        client: LspClient,
        *,
        deadline_at: float | None = None,
        graceful: bool | None = None,
    ) -> None:
        """Close one client and retain the actual stopped/unreaped outcome."""
        started_at = time.monotonic()
        graceful_attempt = client.is_usable if graceful is None else graceful
        shutdown_phase = "graceful_attempt" if graceful_attempt else "abort_only"
        status = "ok"
        attributes: dict[str, PerformanceValue] = {
            "shutdown_phase": shutdown_phase,
        }
        try:
            if graceful_attempt:
                if deadline_at is None:
                    await client.shutdown()
                else:
                    await client.shutdown(deadline_at=deadline_at)
            else:
                if deadline_at is None:
                    await client.abort()
                else:
                    await client.abort(deadline_at=deadline_at)
        except BaseException as error:
            status = self._performance_status(error)
            attributes["error_type"] = type(error).__name__
            raise
        finally:
            if client.is_alive:
                status = "incomplete"
                attributes["outcome"] = "unreaped"
            else:
                attributes["outcome"] = "stopped"
            self._record_lsp_performance(
                "shutdown",
                started_at=started_at,
                status=status,
                transport_key=key,
                attributes=attributes,
            )

    async def _discard_transport_async(
        self,
        key: TransportKey,
        client: LspClient | None,
    ) -> None:
        """Remove and shut down a transport on the worker event loop."""
        if client is None:
            return
        generation: int | None = None
        with self._lock:
            if self._transports.get(key) is client:
                self._transports.pop(key, None)
                status = self._ensure_transport_status_locked(key)
                generation = status.generation
                self._record_transport_status_locked(
                    key,
                    state=LspTransportState.STOPPING,
                    generation=generation,
                    launcher=status.launcher,
                )
        try:
            await self._close_transport_observed(key, client)
        except Exception as error:
            self._record_client_cleanup_error(
                client,
                "manager_discard_failed",
                error,
            )
            logger.warning(
                "LSP transport discard failed: error_type=%s",
                type(error).__name__,
            )
        finally:
            if generation is not None:
                if client.is_alive:
                    stderr_ref = self._retain_client_stderr(
                        key,
                        generation,
                        client,
                    )
                    self._transition_transport(
                        key,
                        generation,
                        LspTransportState.ERROR,
                        error_type="ShutdownIncomplete",
                        error_phase="shutdown",
                        stderr_ref=stderr_ref,
                    )
                else:
                    self._transition_transport(
                        key,
                        generation,
                        LspTransportState.STOPPED,
                    )

    async def _shutdown_clients_async(self, *, deadline_at: float) -> bool:
        """Shut down all transports concurrently under one manager deadline."""
        with self._lock:
            clients = dict(self._transports)
            self._transports.clear()
            self._document_sync_stamps.clear()
            generations: dict[TransportKey, int] = {}
            for key in clients:
                status = self._ensure_transport_status_locked(key)
                generations[key] = status.generation
                self._record_transport_status_locked(
                    key,
                    state=LspTransportState.STOPPING,
                    generation=status.generation,
                    launcher=status.launcher,
                )
            for key, status in tuple(self._transport_statuses.items()):
                if key not in clients and status.state is not LspTransportState.STOPPED:
                    self._record_transport_status_locked(
                        key,
                        state=LspTransportState.STOPPED,
                        generation=status.generation,
                        launcher=status.launcher,
                    )

        if not clients:
            return True
        remaining = max(0.0, deadline_at - time.monotonic())
        if remaining <= 0:
            await asyncio.gather(
                *(
                    self._close_transport_observed(
                        key,
                        client,
                        deadline_at=deadline_at,
                    )
                    for key, client in clients.items()
                ),
                return_exceptions=True,
            )
            completed = all(not client.is_alive for client in clients.values())
            self._finalize_transport_shutdown_states(clients, generations)
            return completed
        shutdowns = asyncio.gather(
            *(
                self._close_transport_observed(
                    key,
                    client,
                    deadline_at=deadline_at,
                )
                for key, client in clients.items()
            ),
            return_exceptions=True,
        )
        timed_out = False
        try:
            await asyncio.wait_for(shutdowns, timeout=remaining)
        except asyncio.TimeoutError:
            timed_out = True
        self._finalize_transport_shutdown_states(clients, generations)
        return not timed_out and all(not client.is_alive for client in clients.values())

    def _finalize_transport_shutdown_states(
        self,
        clients: dict[TransportKey, LspClient],
        generations: dict[TransportKey, int],
    ) -> None:
        for key, client in clients.items():
            if client.is_alive:
                stderr_ref = self._retain_client_stderr(
                    key,
                    generations[key],
                    client,
                )
                self._transition_transport(
                    key,
                    generations[key],
                    LspTransportState.ERROR,
                    error_type="ShutdownIncomplete",
                    error_phase="shutdown",
                    stderr_ref=stderr_ref,
                )
            else:
                self._transition_transport(
                    key,
                    generations[key],
                    LspTransportState.STOPPED,
                )

    def _on_client_exit(
        self,
        key: TransportKey,
        client: LspClient,
        generation: int,
        _reason: str,
        returncode: int | None,
    ) -> None:
        """Accept an unexpected exit only from the current ready generation."""
        failure_type = self._runtime_failure_type(_reason, returncode)
        changed = False
        refined = False
        with self._lock:
            status = self._transport_statuses.get(key)
            current_generation = (
                self._transports.get(key) is client
                and status is not None
                and status.generation == generation
            )
            if current_generation and status.state is LspTransportState.READY:
                stderr_ref = self._retain_client_stderr(
                    key,
                    generation,
                    client,
                )
                self._record_transport_status_locked(
                    key,
                    state=LspTransportState.ERROR,
                    generation=generation,
                    launcher=status.launcher,
                    error_type=failure_type,
                    error_phase="runtime",
                    return_code=returncode,
                    stderr_ref=stderr_ref,
                )
                changed = True
            elif (
                current_generation
                and status.state is LspTransportState.ERROR
                and status.return_code is None
                and returncode is not None
            ):
                self._record_transport_status_locked(
                    key,
                    state=LspTransportState.ERROR,
                    generation=generation,
                    launcher=status.launcher,
                    error_type=status.error_type,
                    error_phase=status.error_phase,
                    protocol_error_code=status.protocol_error_code,
                    return_code=returncode,
                    stderr_ref=status.stderr_ref,
                    retry_at=status.retry_at_monotonic,
                )
                changed = True
                refined = True
        if changed:
            logger.warning(
                "LSP transport exited: language=%s root_hash=%s "
                "generation=%d error_type=%s return_code=%s refined=%s",
                get_language_id_string(key[0]),
                self._workspace_identifier(key[1]),
                generation,
                status.error_type if refined else failure_type,
                returncode,
                refined,
            )

    @classmethod
    def _runtime_failure_type(
        cls,
        reason: str,
        returncode: int | None,
    ) -> str:
        """Classify client-owned reason strings without projecting their text."""
        prefixes = (
            ("stderr reader failed:", "StderrReader"),
            ("response reader error:", "ResponseReader"),
            ("server response write failed:", "ServerResponseWrite"),
        )
        for prefix, label in prefixes:
            if reason.startswith(prefix):
                cause = cls._safe_fact(reason.removeprefix(prefix), "Error")
                return f"{label}{cause}"[:64]
        if returncode is not None:
            return "ProcessExited"
        return "TransportClosed"

    def _on_transport_error(
        self,
        lang: LanguageId,
        file_path: Path,
        error_type: str,
        *,
        transport_key: TransportKey | None = None,
    ) -> None:
        """Mark the current file transport as errored after an I/O failure."""
        key = transport_key or self._transport_key(lang, file_path)
        status = self._ensure_transport_status(key)
        with self._lock:
            client = self._transports.get(key)
        stderr_ref = (
            self._retain_client_stderr(key, status.generation, client)
            if client is not None
            else None
        )
        preserve_cause = status.state is LspTransportState.ERROR
        self._transition_transport(
            key,
            status.generation,
            LspTransportState.ERROR,
            error_type=(
                status.error_type
                if preserve_cause
                else self._safe_fact(error_type, "TransportIOError")
            ),
            error_phase=(status.error_phase if preserve_cause else "runtime"),
            protocol_error_code=(
                status.protocol_error_code if preserve_cause else None
            ),
            return_code=status.return_code if preserve_cause else None,
            stderr_ref=stderr_ref,
            retry_at=status.retry_at_monotonic if preserve_cause else None,
        )
        logger.warning(
            "LSP transport marked dead: language=%s error_type=%s",
            lang.name,
            self._safe_fact(error_type, "TransportIOError"),
        )

    # === Internal: Document Sync ===

    def _canonicalize_path(self, file_path: Path) -> Path:
        path = Path(file_path)
        if not path.is_absolute():
            path = self._workspace_cwd / path
        return path.resolve()

    @classmethod
    def _read_file_content(cls, file_path: Path) -> str:
        """Read one bounded UTF-8 document or raise a typed safe failure."""
        snapshot = cls._load_document_for_sync(
            file_path,
            last_stamp=None,
            force=True,
        )
        assert snapshot is not None
        return snapshot.content

    @classmethod
    def _load_document_for_sync(
        cls,
        file_path: Path,
        *,
        last_stamp: _DocumentStamp | None,
        force: bool,
    ) -> _DocumentSnapshot | None:
        """Load a stable bounded snapshot, retrying one concurrent mutation."""
        for attempt in range(2):
            try:
                handle = file_path.open("rb")
            except OSError as error:
                raise LspDocumentReadError("LSP document read failed") from error

            changed_during_read = False
            snapshot: _DocumentSnapshot | None = None
            try:
                before = cls._document_stamp(handle)
                if before.size > MAX_LSP_FILE_SIZE_BYTES:
                    raise LspDocumentTooLarge("LSP document exceeds the size limit")
                if force or last_stamp is None or before != last_stamp:
                    try:
                        raw = handle.read(MAX_LSP_FILE_SIZE_BYTES + 1)
                    except OSError as error:
                        raise LspDocumentReadError(
                            "LSP document read failed"
                        ) from error
                    if len(raw) > MAX_LSP_FILE_SIZE_BYTES:
                        raise LspDocumentTooLarge("LSP document exceeds the size limit")
                    after = cls._document_stamp(handle)
                    if before != after:
                        changed_during_read = True
                    else:
                        try:
                            content = raw.decode("utf-8")
                        except UnicodeDecodeError as error:
                            raise LspDocumentDecodeError(
                                "LSP document is not valid UTF-8"
                            ) from error
                        snapshot = _DocumentSnapshot(content=content, stamp=after)
            except BaseException as primary_error:
                cls._close_document_handle(
                    handle,
                    primary_error=primary_error,
                )
                raise
            cls._close_document_handle(handle)
            if not changed_during_read:
                return snapshot
            if attempt == 0:
                continue
            raise LspDocumentChangedDuringRead(
                "LSP document changed repeatedly while being read"
            )
        raise AssertionError("document read retry loop exhausted")

    @staticmethod
    def _document_stamp(handle: Any) -> _DocumentStamp:
        try:
            return _DocumentStamp.from_stat(os.fstat(handle.fileno()))
        except (OSError, ValueError) as error:
            raise LspDocumentStatError("LSP document stat failed") from error

    @staticmethod
    def _close_document_handle(
        handle: Any,
        *,
        primary_error: BaseException | None = None,
    ) -> None:
        try:
            handle.close()
        except Exception as close_error:
            if primary_error is None:
                raise LspDocumentCloseError(
                    "LSP document close failed"
                ) from close_error
            try:
                primary_error.secondary_error_operation = "document_close"  # type: ignore[attr-defined]
                primary_error.secondary_error_type = type(close_error).__name__  # type: ignore[attr-defined]
            except Exception as observation_error:
                logger.warning(
                    "LSP document close-failure observation failed: error_type=%s",
                    type(observation_error).__name__,
                )
            logger.warning(
                "LSP document close failed while preserving primary failure: "
                "error_type=%s",
                type(close_error).__name__,
            )

    # === Internal: Helpers ===

    def _enabled_for_file(self, file_path: Path) -> bool:
        """Check cheap configuration/file gates without probing PATH."""
        if not self._config.enabled:
            return False
        lang = detect_language(file_path)
        if lang is None:
            return False
        with self._lock:
            forced_availability = self._availability.get(lang)
        return forced_availability is not False

    def _ensure_transport_status(self, key: TransportKey) -> LspTransportStatus:
        with self._lock:
            return self._ensure_transport_status_locked(key)

    def _ensure_transport_status_locked(self, key: TransportKey) -> LspTransportStatus:
        status = self._transport_statuses.get(key)
        if status is not None:
            return status
        return self._record_transport_status_locked(
            key,
            state=LspTransportState.UNSTARTED,
            generation=0,
        )

    def _begin_transport_attempt(self, key: TransportKey, command: str) -> int:
        """Advance one slot generation and enter resolving atomically."""
        with self._lock:
            current = self._ensure_transport_status_locked(key)
            generation = current.generation + 1
            self._document_sync_stamps = {
                document_key: stamp
                for document_key, stamp in self._document_sync_stamps.items()
                if document_key[0] != key
            }
            self._record_transport_status_locked(
                key,
                state=LspTransportState.RESOLVING,
                generation=generation,
                launcher=self._launcher_name(command),
            )
            return generation

    def _transition_transport(
        self,
        key: TransportKey,
        generation: int,
        state: LspTransportState,
        *,
        command: str | None = None,
        error_type: str | None = None,
        error_phase: str | None = None,
        protocol_error_code: int | None = None,
        return_code: int | None = None,
        stderr_ref: str | None = None,
        retry_at: float | None = None,
    ) -> bool:
        """CAS one transition; stale generation completions are rejected."""
        protocol_error_code = self._safe_protocol_error_code(protocol_error_code)
        with self._lock:
            current = self._ensure_transport_status_locked(key)
            if current.generation != generation:
                return False
            launcher = (
                self._launcher_name(command)
                if command is not None
                else current.launcher
            )
            if state is not LspTransportState.ERROR:
                error_type = None
                error_phase = None
                protocol_error_code = None
                return_code = None
                stderr_ref = None
                retry_at = None
            elif stderr_ref is None:
                # Reclassification within one failed generation must not lose
                # the only correlation to that process's bounded stderr.
                stderr_ref = current.stderr_ref
            if (
                current.state is state
                and current.launcher == launcher
                and current.error_type == error_type
                and current.error_phase == error_phase
                and current.protocol_error_code == protocol_error_code
                and current.return_code == return_code
                and current.stderr_ref == stderr_ref
                and current.retry_at_monotonic == retry_at
            ):
                return True
            self._record_transport_status_locked(
                key,
                state=state,
                generation=generation,
                launcher=launcher,
                error_type=error_type,
                error_phase=error_phase,
                protocol_error_code=protocol_error_code,
                return_code=return_code,
                stderr_ref=stderr_ref,
                retry_at=retry_at,
            )
            return True

    def _record_transport_status_locked(
        self,
        key: TransportKey,
        *,
        state: LspTransportState,
        generation: int,
        launcher: str | None = None,
        error_type: str | None = None,
        error_phase: str | None = None,
        protocol_error_code: int | None = None,
        return_code: int | None = None,
        stderr_ref: str | None = None,
        retry_at: float | None = None,
    ) -> LspTransportStatus:
        self._next_transport_state_sequence += 1
        status = LspTransportStatus(
            language=key[0],
            workspace_root=key[1],
            state=state,
            generation=generation,
            sequence=self._next_transport_state_sequence,
            updated_at_monotonic=self._transport_state_clock(),
            launcher=launcher,
            error_type=error_type,
            error_phase=error_phase,
            protocol_error_code=protocol_error_code,
            return_code=return_code,
            stderr_ref=stderr_ref,
            retry_at_monotonic=retry_at,
        )
        self._transport_statuses[key] = status
        self._transport_state_history.append(status)
        return status

    @classmethod
    def _launcher_name(cls, command: str) -> str:
        return cls._safe_fact(
            Path(command).name or "configured-launcher",
            "configured-launcher",
        )

    @staticmethod
    def _describe_transport_status(status: LspTransportStatusView) -> str:
        description = (
            f"{status.language}:root={status.root_hash}:"
            f"g{status.generation}:{status.state.value}"
        )
        if status.launcher:
            description += f":launcher={status.launcher}"
        if status.error_phase:
            description += f":phase={status.error_phase}"
        if status.error_type:
            description += f":error={status.error_type}"
        if status.protocol_error_code is not None:
            description += f":protocol_code={status.protocol_error_code}"
        if status.return_code is not None:
            description += f":return_code={status.return_code}"
        if status.retry_at_monotonic is not None:
            description += ":retry_scheduled=true"
        if status.stderr is not None:
            description += (
                f":stderr={status.stderr.ref}"
                f":stderr_bytes={status.stderr.total_bytes}"
                f":stderr_truncated={str(status.stderr.truncated).lower()}"
            )
            if status.stderr.finalized is False:
                description += ":stderr_pending=true"
            elif status.stderr.finalized is None:
                description += ":stderr_finalized=unknown"
            if status.stderr.read_error_type is not None:
                description += f":stderr_read_error={status.stderr.read_error_type}"
            if status.stderr.cleanup_operation is not None:
                description += f":stderr_cleanup={status.stderr.cleanup_operation}"
            if status.stderr.cleanup_error_type is not None:
                description += (
                    f":stderr_cleanup_error={status.stderr.cleanup_error_type}"
                )
            if status.stderr.metadata_error_type is not None:
                description += (
                    f":stderr_metadata_error={status.stderr.metadata_error_type}"
                )
        return description

    def _command_available(self, key: TransportKey, command: str) -> bool:
        """Resolve one launcher lazily, retrying missing commands after a TTL."""
        if self._negative_command_retry_at(key, command) is not None:
            return False
        found, _ = self._lookup_command_availability(key, command)
        return found

    def _negative_command_retry_at(
        self, key: TransportKey, command: str
    ) -> float | None:
        """Return an active negative-cache deadline without touching PATH."""
        started_at = time.monotonic()
        lang, root = key
        cache_key = (key, self._command_lookup_target(command, root))
        retry_at: float | None = None
        with self._lock:
            forced = self._availability.get(lang)
            if forced is not None:
                return None
            now = self._availability_clock()
            self._negative_availability_until = {
                cached_key: expires_at
                for cached_key, expires_at in self._negative_availability_until.items()
                if expires_at > now
            }
            unavailable_until = self._negative_availability_until.get(cache_key, 0)
            if unavailable_until > now:
                self._availability_metrics["negative_cache_hits"] += 1
                retry_at = unavailable_until
        if retry_at is not None:
            self._record_lsp_performance(
                "availability_lookup",
                started_at=started_at,
                status="unavailable",
                transport_key=key,
                attributes={
                    "cache_result": "negative_hit",
                    "outcome": "unavailable",
                },
            )
        return retry_at

    def _lookup_command_availability(
        self, key: TransportKey, command: str
    ) -> tuple[bool, float | None]:
        """Perform one launcher lookup and update the negative cache."""
        started_at = time.monotonic()
        lang, root = key
        lookup_target = self._command_lookup_target(command, root)
        cache_key = (key, lookup_target)
        with self._lock:
            forced = self._availability.get(lang)
        if forced is not None:
            self._record_lsp_performance(
                "availability_lookup",
                started_at=started_at,
                status="ok" if forced else "unavailable",
                transport_key=key,
                attributes={
                    "cache_result": "forced",
                    "outcome": "available" if forced else "unavailable",
                },
            )
            return forced, None
        try:
            found = self._command_lookup(lookup_target) is not None
        except BaseException as error:
            self._record_lsp_performance(
                "availability_lookup",
                started_at=started_at,
                status=self._performance_status(error),
                transport_key=key,
                attributes={
                    "cache_result": "miss",
                    "error_type": type(error).__name__,
                },
            )
            raise
        with self._lock:
            self._availability_metrics["lookups"] += 1
            if found:
                self._availability_metrics["available"] += 1
                self._negative_availability_until.pop(cache_key, None)
                retry_at = None
            else:
                self._availability_metrics["unavailable"] += 1
                retry_at = self._availability_clock() + MISSING_COMMAND_TTL_SECONDS
                self._negative_availability_until[cache_key] = retry_at
        self._record_lsp_performance(
            "availability_lookup",
            started_at=started_at,
            status="ok" if found else "unavailable",
            transport_key=key,
            attributes={
                "cache_result": "miss",
                "outcome": "available" if found else "unavailable",
            },
        )
        return found, retry_at

    @staticmethod
    def _command_lookup_target(command: str, root: Path) -> str:
        """Resolve path-like launchers as the subprocess cwd would resolve them."""
        path = Path(command)
        if path.is_absolute() or path.name == command:
            return command
        return str((root / path).resolve())

    def _resolve_root(self, lang: LanguageId, file_path: Path) -> Path:
        """Resolve and cache workspace root for a language."""
        override = self._get_workspace_root_override(lang)
        root = resolve_workspace_root(
            file_path, lang, cwd=self._workspace_cwd, override=override
        )
        resolved = root.resolve()
        with self._lock:
            self._workspace_roots[(lang, resolved)] = resolved
        return resolved

    def _transport_key(self, lang: LanguageId, file_path: Path) -> TransportKey:
        return lang, self._resolve_root(lang, file_path)

    def _resolve_command(self, lang: LanguageId) -> tuple[str, list[str]]:
        """Get server command with config overrides applied."""
        cmd, args = get_server_command(lang)
        cfg_override = self._config.get_override(lang.name.lower())
        if cfg_override:
            if cfg_override.cmd is not None:
                cmd = cfg_override.cmd
            if cfg_override.args is not None:
                args = cfg_override.args
        return cmd, args

    def _resolve_launch(self, lang: LanguageId, root: Path) -> LspServerLaunch:
        """Resolve the exact root-aware launch consumed by availability and spawn."""
        launch = resolve_server_launch(
            lang,
            root,
            typescript_mode=self._config.typescript_mode,
        )
        override = self._config.get_override(lang.name.lower())
        if override is None:
            return launch
        return LspServerLaunch(
            command=override.cmd if override.cmd is not None else launch.command,
            args=(tuple(override.args) if override.args is not None else launch.args),
            initialization_options=(
                override.init_opts
                if override.init_opts is not None
                else launch.initialization_options
            ),
            implementation=launch.implementation,
        )

    def _resolve_init_opts(self, lang: LanguageId) -> dict[str, Any] | None:
        """Get initialization options from config override."""
        cfg_override = self._config.get_override(lang.name.lower())
        if cfg_override and cfg_override.init_opts is not None:
            return cfg_override.init_opts
        return None

    def _get_workspace_root_override(self, lang: LanguageId) -> str | None:
        """Get config-level workspace_root override for a language."""
        lang_key = lang.name.lower()
        override = self._config.get_override(lang_key)
        if override and override.workspace_root:
            return override.workspace_root
        return None

    def _relativize_path(self, file_path: Path) -> str:
        """Convert absolute path to workspace-relative, or basename.

        Normalises to forward slashes for cross-platform consistency
        (LSP URIs and diagnostics use /).
        """
        try:
            return file_path.relative_to(self._workspace_cwd).as_posix()
        except ValueError:
            return file_path.name
