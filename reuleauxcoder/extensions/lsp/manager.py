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
import logging
import shutil
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

from reuleauxcoder.domain.cancellation import CancellationSignal
from reuleauxcoder.domain.runtime.events import (
    DiagnosticsCleared,
    DiagnosticsPublished,
    RuntimeDiagnostic,
    RuntimeEvent,
)

from reuleauxcoder.extensions.lsp.client import (
    LspClient,
    LspClientError,
    LspRequestCancelled,
    LspRequestTimedOut,
    MAX_LSP_FILE_SIZE_BYTES,
)
from reuleauxcoder.extensions.lsp.config import LspConfig
from reuleauxcoder.extensions.lsp.diagnostics import (
    DiagnosticBatch,
    DiagnosticBlock,
    DiagnosticRoute,
    DiagnosticRouteFilter,
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
_WORKER_POLL_INTERVAL = 0.1
_TOOL_REQUEST_POLL_INTERVAL = 0.05
SPAWN_TIMEOUT = 30.0
MISSING_COMMAND_TTL_SECONDS = 30.0
DIAGNOSTIC_BATCH_TTL_SECONDS = 300.0
MAX_PENDING_DIAGNOSTIC_BATCHES_PER_OWNER = 32
MAX_TRANSPORT_STATE_HISTORY = 256
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
    error_message: str | None = None
    retry_at_monotonic: float | None = None


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


@dataclass(frozen=True, slots=True)
class DiagnosticRequest:
    """A routed diagnostics request owned by one edit operation."""

    batch_id: str
    route: DiagnosticRoute
    request_sequence: int
    document_committed: bool = False


class LspManager:
    """Workspace-scoped coordinator for LSP server interactions.

    All LSP I/O (subprocess stdin/stdout) passes through a single
    background worker thread.  This avoids locks — serialisation is
    natural because only one writer exists.
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

        # Per-language/workspace state. One language server must never index
        # files from an unrelated workspace root.
        self._transports: dict[TransportKey, LspClient] = {}
        self._workspace_roots: dict[TransportKey, Path] = {}
        self._transport_statuses: dict[TransportKey, LspTransportStatus] = {}
        self._transport_state_history: deque[LspTransportStatus] = deque(
            maxlen=MAX_TRANSPORT_STATE_HISTORY
        )
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
        self._last_sync_time: dict[tuple[TransportKey, Path], float] = {}

        # Queues
        self._diagnostics_queue: list[DiagnosticRequest] = []
        self._tool_queue: list[ToolRequest] = []

        # Routed results.  Clean publishes are retained as empty batches until
        # the owning consumer acknowledges them.
        self._diagnostic_batches: dict[str, DiagnosticBatch] = {}
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
        }

        # Lock (RLock for reentrancy in health_check)
        self._lock: threading.RLock = threading.RLock()

        # Worker thread
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._abort_current = False  # set during shutdown to skip in-flight work
        self._request_condition = threading.Condition()

        # Worker event loop reference (set once worker starts)
        self._worker_loop: asyncio.AbstractEventLoop | None = None
        self._current_work_task: asyncio.Task[None] | None = None
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
            transports = tuple(
                sorted(
                    self._describe_transport_status(status)
                    for status in self._transport_statuses.values()
                )
            )
            pending = tuple(
                sorted(
                    f"pending:{batch.route.agent_id or '-'}:"
                    f"g{batch.route.session_generation}:"
                    f"{batch.route.file_path}"
                    for batch in self._diagnostic_batches.values()
                )
            )
        return (*transports, *pending)

    def transport_statuses(self) -> tuple[LspTransportStatus, ...]:
        """Return an immutable, stable-order snapshot of all resolved slots."""
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

    def transport_status_for_file(
        self, file_path: Path
    ) -> LspTransportStatus | None:
        """Resolve a supported file to its slot, initially ``unstarted g0``."""
        path = self._canonicalize_path(file_path)
        language = detect_language(path)
        if not self._config.enabled or language is None:
            return None
        key = self._transport_key(language, path)
        return self._ensure_transport_status(key)

    def transport_state_history(self) -> tuple[LspTransportStatus, ...]:
        """Return the bounded transition history, oldest first."""
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
            full_cmd = f"{cmd} {' '.join(args)}".strip()
            details = full_cmd
            report.languages.append((lang_name, found, details))
            report.total += 1
            if found:
                report.available += 1

        return report

    def configured_languages(self) -> tuple[str, ...]:
        """Return the declarative catalog without probing PATH or starting work."""
        from reuleauxcoder.extensions.lsp.registry import iter_supported_languages

        return tuple(
            get_language_id_string(language)
            for language in iter_supported_languages()
        )

    def availability_metrics(self) -> dict[str, int]:
        """Return content-free counters for lazy command resolution."""
        with self._lock:
            return dict(self._availability_metrics)

    def start_worker(self) -> None:
        """Start the background worker thread (idempotent)."""
        if self._worker_thread is not None:
            return

        with self._lock:
            if self._worker_thread is not None:
                return
            self._stop_event.clear()
            self._abort_current = False
            self._shutdown_deadline_at = None
            self._last_shutdown_completed = True
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
        logger.info("Shutting down LSP manager")
        had_worker = self._worker_thread is not None
        deadline_at = time.monotonic() + timeout
        self._shutdown_deadline_at = deadline_at
        self._abort_current = True  # tells worker to skip in-flight work
        self._stop_event.set()

        with self._request_condition:
            self._request_condition.notify_all()

        with self._lock:
            worker_loop = self._worker_loop
            current_work = self._current_work_task
        if worker_loop is not None and current_work is not None:
            with suppress(RuntimeError):
                worker_loop.call_soon_threadsafe(current_work.cancel)

        # Fail queued synchronous requests immediately.  The worker owns any
        # in-flight request and will fail/finish it before shutting clients down.
        with self._lock:
            for req in self._tool_queue:
                if not req.future.done():
                    req.future.set_exception(
                        LspClientError("LSP manager shutting down")
                    )
            self._tool_queue.clear()
            self._diagnostics_queue.clear()
            self._pending_diagnostic_requests.clear()
            self._diagnostic_batches.clear()
            self._acknowledged_batches.clear()
            self._latest_diagnostic_sequence.clear()
            self._session_generations.clear()

        if self._worker_thread is not None:
            self._worker_thread.join(
                timeout=max(0.0, deadline_at - time.monotonic())
            )
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
            except Exception:
                self._last_shutdown_completed = False
                logger.exception("LSP fallback shutdown failed")
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

        if route is None:
            route = DiagnosticRoute(file_path=path)
        elif self._canonicalize_path(route.file_path) != path:
            raise ValueError("diagnostic route file_path must match request path")
        elif route.file_path != path:
            route = replace(route, file_path=path)

        with self._lock:
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
            owner = self._diagnostic_owner(route)
            overwritten = [
                current_id
                for current_id, pending in self._diagnostic_batches.items()
                if self._diagnostic_owner(pending.route) == owner
                and pending.route.file_path == path
            ]
            for current_id in overwritten:
                self._diagnostic_batches.pop(current_id, None)
            self._diagnostic_batch_metrics["overwritten"] += len(overwritten)
            self._next_diagnostic_sequence += 1
            request_sequence = self._next_diagnostic_sequence
            key = (
                route.agent_id,
                route.session_generation,
                route.session_id,
                path,
            )
            self._latest_diagnostic_sequence[key] = request_sequence
            batch_id = uuid.uuid4().hex
            superseded_ids = {
                item.batch_id
                for item in self._diagnostics_queue
                if (
                    item.route.agent_id,
                    item.route.session_generation,
                    item.route.session_id,
                    item.route.file_path,
                )
                == key
            }
            self._diagnostics_queue = [
                item
                for item in self._diagnostics_queue
                if item.batch_id not in superseded_ids
            ]
            self._pending_diagnostic_requests.difference_update(superseded_ids)
            self._diagnostics_queue.append(
                DiagnosticRequest(
                    batch_id=batch_id,
                    route=route,
                    request_sequence=request_sequence,
                    document_committed=document_committed,
                )
            )
            self._pending_diagnostic_requests.add(batch_id)

        self.start_worker()
        with self._request_condition:
            self._request_condition.notify()
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
        stale_request_ids = {
            request.batch_id
            for request in self._diagnostics_queue
            if self._is_older_generation(request.route, agent_id, generation)
        }
        self._diagnostics_queue = [
            request
            for request in self._diagnostics_queue
            if request.batch_id not in stale_request_ids
        ]
        self._pending_diagnostic_requests.difference_update(stale_request_ids)
        previous_batch_count = len(self._diagnostic_batches)
        self._diagnostic_batches = {
            batch_id: batch
            for batch_id, batch in self._diagnostic_batches.items()
            if not self._is_older_generation(batch.route, agent_id, generation)
        }
        self._diagnostic_batch_metrics["stale_discarded"] += (
            previous_batch_count - len(self._diagnostic_batches)
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

    def acknowledge_diagnostic_batch(
        self,
        batch_id: str,
        *,
        consumer_id: str,
        carried_forward: bool = False,
    ) -> bool:
        """Acknowledge exactly one batch, preventing a second consumer."""
        with self._lock:
            if self._diagnostic_batches.pop(batch_id, None) is None:
                return False
            self._record_acknowledgement(batch_id, consumer_id)
            if carried_forward:
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
        lang = detect_language(file_path)
        if lang is None:
            raise LspClientError(f"No LSP support for file type: {file_path.suffix}")
        if cancellation is not None and cancellation.is_set():
            raise LspRequestCancelled(f"LSP request '{method}' was cancelled")

        deadline_at = time.monotonic() + timeout
        # Start worker if not already running.  The worker owns LSP subprocesses,
        # so it also handles lazy spawn before executing the request.
        self.start_worker()

        # Enqueue the request — worker handles spawn + sync + query
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        req = ToolRequest(
            file_path=file_path,
            language_id=lang,
            method=method,
            params=params,
            future=future,
            timeout_seconds=timeout,
            deadline_at=deadline_at,
            needs_sync=True,
        )

        with self._lock:
            self._tool_queue.append(req)

        with self._request_condition:
            self._request_condition.notify()

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
                if not self._abandon_tool_request(req):
                    return future.result()
                raise LspRequestCancelled(f"LSP request '{method}' was cancelled")
            if time.monotonic() >= deadline_at:
                if not self._abandon_tool_request(req):
                    return future.result()
                raise self._timeout_error(req)

    # === Internal: Worker Thread ===

    def _worker_entry(self) -> None:
        """Entry point for the worker thread."""
        asyncio.run(self._async_worker_main())

    async def _async_worker_main(self) -> None:
        """Main worker loop — sole owner of LSP subprocesses."""
        self._worker_loop = asyncio.get_event_loop()

        try:
            while not self._stop_event.is_set():
                kind, work = self._pop_next_work()
                if kind == "tool":
                    work_coro = self._handle_tool_request(work)
                elif kind == "diagnostics":
                    work_coro = self._handle_diagnostics_request(work)
                else:
                    # No work — poll briefly, then check again.
                    # Using asyncio.sleep avoids blocking the event loop
                    # (unlike threading.Condition.wait which would stall it).
                    # The main thread's enqueue + condition.notify() reduces
                    # wakeup latency, but the poll interval is the worst case.
                    await asyncio.sleep(_WORKER_POLL_INTERVAL)
                    continue

                current_work = asyncio.create_task(work_coro)
                with self._lock:
                    self._current_work_task = current_work
                if self._stop_event.is_set():
                    current_work.cancel()
                try:
                    await current_work
                except asyncio.CancelledError:
                    pass
                finally:
                    with self._lock:
                        if self._current_work_task is current_work:
                            self._current_work_task = None
        finally:
            try:
                self._last_shutdown_completed = await self._shutdown_clients_async(
                    deadline_at=self._shutdown_deadline_at
                    or time.monotonic() + WORKER_SHUTDOWN_TIMEOUT
                )
            except Exception:
                self._last_shutdown_completed = False
                logger.exception("LSP worker shutdown failed")
            self._worker_loop = None
            logger.info("LSP worker loop exited")

    def _pop_next_work(self) -> tuple[str | None, Any]:
        """Pop exactly one queued item without discarding lower-priority work."""
        with self._lock:
            if self._tool_queue:
                return "tool", self._tool_queue.pop(0)
            if self._diagnostics_queue:
                return "diagnostics", self._diagnostics_queue.pop(0)
        return None, None

    async def _handle_tool_request(self, req: ToolRequest) -> None:
        """Process a synchronous active-tool request."""
        if req.future.cancelled():
            return
        operation = asyncio.create_task(self._execute_tool_request(req))
        try:
            while not operation.done():
                if req.future.cancelled():
                    return
                if self._abort_current:
                    raise LspClientError("LSP manager shutting down")
                remaining = req.deadline_at - time.monotonic()
                if remaining <= 0:
                    raise self._timeout_error(req)
                await asyncio.wait(
                    {operation},
                    timeout=min(_TOOL_REQUEST_POLL_INTERVAL, remaining),
                )

            result = await operation
            if not req.future.done():
                req.future.set_result(result)
        except asyncio.CancelledError:
            if not req.future.done():
                req.future.set_exception(LspClientError("LSP request cancelled"))
        except Exception as e:
            if not req.future.done():
                req.future.set_exception(e)
        finally:
            if not operation.done():
                operation.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await operation

    async def _execute_tool_request(self, req: ToolRequest) -> Any:
        """Run start, document sync and query under one caller-owned deadline."""
        server = await self._get_or_create_server(req.language_id, req.file_path)
        if server is None:
            raise LspClientError(
                f"No LSP server available for {get_language_id_string(req.language_id)}"
            )

        # Document sync before query (if needed)
        if req.needs_sync:
            stale = self._check_stale(req.language_id, req.file_path)
            if stale:
                content = self._read_file_content(req.file_path)
                if content is not None:
                    key = (
                        self._transport_key(req.language_id, req.file_path),
                        req.file_path,
                    )
                    last_sync = self._last_sync_time.get(key, 0)
                    try:
                        if last_sync == 0:
                            await server.did_open(req.file_path, content)
                        else:
                            await server.did_change(req.file_path, content)
                        with self._lock:
                            self._last_sync_time[key] = req.file_path.stat().st_mtime
                    except Exception as e:
                        logger.debug("LSP sync error (swallowed): %s", e)

        if self._abort_current:
            raise LspClientError("LSP manager shutting down")
        remaining = req.deadline_at - time.monotonic()
        if remaining <= 0:
            raise self._timeout_error(req)
        return await server.send_request(req.method, req.params, timeout=remaining)

    @staticmethod
    def _timeout_error(req: ToolRequest) -> LspRequestTimedOut:
        return LspRequestTimedOut(
            f"LSP request '{req.method}' timed out after "
            f"{req.timeout_seconds:g}s total"
        )

    def _abandon_tool_request(self, req: ToolRequest) -> bool:
        if not req.future.cancel():
            return False
        with self._lock:
            self._tool_queue = [item for item in self._tool_queue if item is not req]
        return True

    async def _handle_diagnostics_request(self, request: DiagnosticRequest) -> None:
        """Process a fire-and-forget diagnostics request."""
        file_path = request.route.file_path
        lang = detect_language(file_path)
        if lang is None:
            return

        try:
            server = await self._get_or_create_server(lang, file_path)
            if server is None:
                return

            baseline_generation = server.diagnostics_generation(file_path)

            # Sync file content
            # A successful mutation is authoritative even when the filesystem
            # timestamp has not advanced (coarse or preserved mtimes). Always
            # send the committed on-disk content before didSave.
            stale = request.document_committed or self._check_stale(lang, file_path)
            if stale:
                content = self._read_file_content(file_path)
                if content is not None:
                    key = (self._transport_key(lang, file_path), file_path)
                    last_sync = self._last_sync_time.get(key, 0)
                    try:
                        if last_sync == 0:
                            await server.did_open(file_path, content)
                        else:
                            await server.did_change(file_path, content)
                        with self._lock:
                            self._last_sync_time[key] = file_path.stat().st_mtime
                    except Exception as e:
                        logger.debug("LSP sync error (swallowed): %s", e)

            # A successful file mutation is one ordered operation: synchronize
            # the committed content, then notify save, then observe diagnostics.
            # Keeping these steps in this request prevents queue priority from
            # moving diagnostics ahead of didSave.
            if request.document_committed:
                await server.did_save(file_path)

            # Wait for diagnostics
            diagnostics = await server.wait_for_diagnostics(
                file_path,
                timeout=self._config.poll_timeout_ms / 1000,
                after_generation=baseline_generation,
            )
            diagnostic_generation = server.diagnostics_generation(file_path)
            if diagnostic_generation <= baseline_generation:
                # Timeout is not an explicit clean publish.  Retaining no batch
                # is safer than clearing diagnostics from a previous version.
                return

            block = DiagnosticBlock(
                file_path=self._relativize_path(file_path),
                items=diagnostics,
            )
            batch = DiagnosticBatch(
                batch_id=request.batch_id,
                route=request.route,
                request_sequence=request.request_sequence,
                document_version=server.diagnostic_document_version(file_path),
                diagnostic_generation=diagnostic_generation,
                block=block,
                created_at=self._diagnostic_clock(),
            )
            accepted = False
            with self._lock:
                # A slower obsolete request must not overwrite a newer batch.
                key = (
                    request.route.agent_id,
                    request.route.session_generation,
                    request.route.session_id,
                    file_path,
                )
                if self._latest_diagnostic_sequence.get(
                    key
                ) == request.request_sequence and self._route_generation_is_current(
                    request.route
                ):
                    self._store_diagnostic_batch_locked(batch)
                    accepted = True
                else:
                    self._diagnostic_batch_metrics["stale_discarded"] += 1
            if accepted:
                self._publish_diagnostic_event(batch)

        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.warning("LSP transport error for %s: %s", lang.name, e)
            self._on_transport_error(lang, file_path, str(e))
        except Exception as e:
            logger.debug("LSP diagnostics error (swallowed): %s", e)
        finally:
            with self._lock:
                self._pending_diagnostic_requests.discard(request.batch_id)

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
        owned = sorted(
            (
                pending
                for pending in self._diagnostic_batches.values()
                if self._diagnostic_owner(pending.route) == owner
            ),
            key=lambda pending: (
                pending.created_at,
                pending.request_sequence,
                pending.batch_id,
            ),
        )
        overflow = len(owned) - MAX_PENDING_DIAGNOSTIC_BATCHES_PER_OWNER
        for pending in owned[: max(0, overflow)]:
            self._diagnostic_batches.pop(pending.batch_id, None)
        self._diagnostic_batch_metrics["capacity_evicted"] += max(0, overflow)

    def _prune_expired_diagnostic_batches_locked(self) -> None:
        cutoff = self._diagnostic_clock() - DIAGNOSTIC_BATCH_TTL_SECONDS
        expired = [
            batch_id
            for batch_id, batch in self._diagnostic_batches.items()
            if batch.created_at <= cutoff
        ]
        for batch_id in expired:
            self._diagnostic_batches.pop(batch_id, None)
        self._diagnostic_batch_metrics["expired"] += len(expired)

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
        except Exception:
            logger.exception("Runtime diagnostics event sink failed")

    # === Internal: Server Lifecycle ===

    async def _get_or_create_server(
        self,
        lang: LanguageId,
        file_path: Path,
    ) -> LspClient | None:
        """Get or create an LSP server (called from worker thread)."""
        key = self._transport_key(lang, file_path)
        self._ensure_transport_status(key)
        server = self._transports.get(key)
        if server is not None and server.is_usable:
            return server

        count = self._re_spawn_counts.get(key, 0)
        if count >= MAX_RESPWANS:
            logger.error(
                "LSP server for %s at %s failed %d times — disabled for this workspace",
                lang.name,
                key[1],
                MAX_RESPWANS,
            )
            status = self._ensure_transport_status(key)
            self._transition_transport(
                key,
                status.generation,
                LspTransportState.ERROR,
                error_type="RespawnLimitReached",
                error_message=f"transport failed {MAX_RESPWANS} times",
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
                    error_message=f"transport failed {MAX_RESPWANS} times",
                )
                return None

        return await self._spawn_async(lang, file_path)

    async def _spawn_async(
        self,
        lang: LanguageId,
        file_path: Path,
    ) -> LspClient | None:
        """Spawn + initialize from the worker thread (inline await)."""
        root = self._resolve_root(lang, file_path)
        key = (lang, root)
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
                    error_message=f"launcher not found: {self._launcher_name(cmd)}",
                    retry_at=cached_retry_at,
                )
            return None

        generation = self._begin_transport_attempt(key, cmd)
        found, retry_at = self._lookup_command_availability(key, cmd)
        if not found:
            logger.info(
                "LSP command unavailable for %s: %s",
                get_language_id_string(lang),
                cmd,
            )
            self._transition_transport(
                key,
                generation,
                LspTransportState.ERROR,
                command=cmd,
                error_type="LauncherNotFound",
                error_message=f"launcher not found: {self._launcher_name(cmd)}",
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
            on_unexpected_exit=lambda current, reason, returncode: (
                self._on_client_exit(
                    key,
                    current,
                    generation,
                    reason,
                    returncode,
                )
            ),
        )

        try:
            await client.spawn(cmd, args)
            if not self._transition_transport(
                key,
                generation,
                LspTransportState.INITIALIZING,
                command=cmd,
            ):
                await client.abort()
                return None
            await client.initialize(init_opts)
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
                await client.abort()
                self._transition_transport(
                    key,
                    generation,
                    LspTransportState.STOPPED,
                    command=cmd,
                )
            else:
                await client.abort()
                self._transition_transport(
                    key,
                    generation,
                    LspTransportState.ERROR,
                    command=cmd,
                    error_type="StartCancelled",
                    error_message="transport start cancelled",
                )
            raise
        except Exception as e:
            await client.abort()
            logger.warning(
                "Failed to spawn LSP server (async) for %s (%s %s): %s",
                lang.name,
                cmd,
                " ".join(args),
                e,
            )
            with self._lock:
                self._re_spawn_counts[key] = self._re_spawn_counts.get(key, 0) + 1
            self._transition_transport(
                key,
                generation,
                LspTransportState.ERROR,
                command=cmd,
                error_type=type(e).__name__,
                error_message=str(e),
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
            await client.abort()
            return None

        logger.info(
            "LSP server ready (async): lang=%s, root=%s",
            get_language_id_string(lang),
            root,
        )
        return client

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
            if client.is_usable:
                await client.shutdown()
            else:
                await client.abort()
        except Exception:
            logger.exception("LSP transport discard failed")
        if generation is not None:
            if client.is_alive:
                self._transition_transport(
                    key,
                    generation,
                    LspTransportState.ERROR,
                    error_type="ShutdownIncomplete",
                    error_message="transport remained alive after discard",
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
            self._last_sync_time.clear()
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
                *(client.abort(deadline_at=deadline_at) for client in clients.values()),
                return_exceptions=True,
            )
            completed = all(not client.is_alive for client in clients.values())
            self._finalize_transport_shutdown_states(clients, generations)
            return completed
        shutdowns = asyncio.gather(
            *(
                (
                    client.shutdown(deadline_at=deadline_at)
                    if client.is_usable
                    else client.abort(deadline_at=deadline_at)
                )
                for client in clients.values()
            ),
            return_exceptions=True,
        )
        timed_out = False
        try:
            await asyncio.wait_for(shutdowns, timeout=remaining)
        except asyncio.TimeoutError:
            timed_out = True
        self._finalize_transport_shutdown_states(clients, generations)
        return not timed_out and all(
            not client.is_alive for client in clients.values()
        )

    def _finalize_transport_shutdown_states(
        self,
        clients: dict[TransportKey, LspClient],
        generations: dict[TransportKey, int],
    ) -> None:
        for key, client in clients.items():
            if client.is_alive:
                self._transition_transport(
                    key,
                    generations[key],
                    LspTransportState.ERROR,
                    error_type="ShutdownIncomplete",
                    error_message="transport remained alive after shutdown deadline",
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
        reason: str,
        returncode: int | None,
    ) -> None:
        """Accept an unexpected exit only from the current ready generation."""
        changed = False
        with self._lock:
            status = self._transport_statuses.get(key)
            if (
                self._transports.get(key) is client
                and status is not None
                and status.generation == generation
                and status.state is LspTransportState.READY
            ):
                message = reason
                if returncode is not None:
                    message += f" (return code {returncode})"
                self._record_transport_status_locked(
                    key,
                    state=LspTransportState.ERROR,
                    generation=generation,
                    launcher=status.launcher,
                    error_type=(
                        "ProcessExited"
                        if returncode is not None
                        else "TransportClosed"
                    ),
                    error_message=" ".join(message.split())[:512],
                )
                changed = True
        if changed:
            logger.warning(
                "LSP transport exited: language=%s root=%s generation=%d reason=%s",
                get_language_id_string(key[0]),
                key[1],
                generation,
                reason,
            )

    def _on_transport_error(
        self, lang: LanguageId, file_path: Path, reason: str
    ) -> None:
        """Mark the current file transport as errored after an I/O failure."""
        key = self._transport_key(lang, file_path)
        status = self._ensure_transport_status(key)
        self._transition_transport(
            key,
            status.generation,
            LspTransportState.ERROR,
            error_type="TransportIOError",
            error_message=reason,
        )
        logger.warning("LSP transport for %s marked dead: %s", lang.name, reason)

    # === Internal: Document Sync ===

    def _canonicalize_path(self, file_path: Path) -> Path:
        path = Path(file_path)
        if not path.is_absolute():
            path = self._workspace_cwd / path
        return path.resolve()

    def _check_stale(self, lang: LanguageId, file_path: Path) -> bool:
        """Check if a file's content is stale in the LSP server."""
        try:
            mtime = file_path.stat().st_mtime
        except OSError:
            return False

        key = (self._transport_key(lang, file_path), file_path)
        last_sync = self._last_sync_time.get(key, 0)
        return mtime > last_sync

    @staticmethod
    def _read_file_content(file_path: Path) -> str | None:
        """Read file content, returning None if unreadable or too large."""
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception:
            return None

        if len(content.encode("utf-8")) > MAX_LSP_FILE_SIZE_BYTES:
            return None

        return content

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

    def _ensure_transport_status_locked(
        self, key: TransportKey
    ) -> LspTransportStatus:
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
            self._last_sync_time = {
                document_key: synced_at
                for document_key, synced_at in self._last_sync_time.items()
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
        error_message: str | None = None,
        retry_at: float | None = None,
    ) -> bool:
        """CAS one transition; stale generation completions are rejected."""
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
                error_message = None
                retry_at = None
            bounded_message = (
                " ".join(error_message.split())[:512]
                if error_message is not None
                else None
            )
            if (
                current.state is state
                and current.launcher == launcher
                and current.error_type == error_type
                and current.error_message == bounded_message
                and current.retry_at_monotonic == retry_at
            ):
                return True
            self._record_transport_status_locked(
                key,
                state=state,
                generation=generation,
                launcher=launcher,
                error_type=error_type,
                error_message=bounded_message,
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
        error_message: str | None = None,
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
            error_message=error_message,
            retry_at_monotonic=retry_at,
        )
        self._transport_statuses[key] = status
        self._transport_state_history.append(status)
        return status

    @staticmethod
    def _launcher_name(command: str) -> str:
        return (Path(command).name or command)[:128]

    @staticmethod
    def _describe_transport_status(status: LspTransportStatus) -> str:
        description = (
            f"{get_language_id_string(status.language)}:"
            f"{status.workspace_root}:g{status.generation}:{status.state.value}"
        )
        if status.launcher:
            description += f":launcher={status.launcher}"
        if status.error_type:
            description += f":error={status.error_type}"
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
        lang, root = key
        cache_key = (key, self._command_lookup_target(command, root))
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
                return unavailable_until
        return None

    def _lookup_command_availability(
        self, key: TransportKey, command: str
    ) -> tuple[bool, float | None]:
        """Perform one launcher lookup and update the negative cache."""
        lang, root = key
        lookup_target = self._command_lookup_target(command, root)
        cache_key = (key, lookup_target)
        with self._lock:
            forced = self._availability.get(lang)
            if forced is not None:
                return forced, None
        found = self._command_lookup(lookup_target) is not None
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
