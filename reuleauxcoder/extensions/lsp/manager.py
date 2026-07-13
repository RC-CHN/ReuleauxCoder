"""LSP Manager — workspace-scoped coordinator for LSP server lifecycle.

Ownership:
- All LSP subprocess communication (sole writer to stdin, via worker thread)
- Lazy per-language LspClient map
- Dual-queue worker thread (diagnostics fire-and-forget + active tool sync bridge)
- Crash detection with re-spawn limit
- Startup health check
- Session-scoped document sync tracking
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import shutil
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Any

from reuleauxcoder.domain.runtime.events import (
    DiagnosticsCleared,
    DiagnosticsPublished,
    RuntimeDiagnostic,
    RuntimeEvent,
)

from reuleauxcoder.extensions.lsp.client import (
    LspClient,
    LspClientError,
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
    15.0  # must allow in-flight request to time out (10s) + cleanup
)
_WORKER_POLL_INTERVAL = 0.1
SPAWN_TIMEOUT = 30.0
TransportKey = tuple[LanguageId, Path]


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
    timeout: float
    needs_sync: bool = True  # Whether to sync file content before the query


@dataclass(frozen=True, slots=True)
class DiagnosticRequest:
    """A routed diagnostics request owned by one edit operation."""

    batch_id: str
    route: DiagnosticRoute
    request_sequence: int


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
        self._availability: dict[LanguageId, bool] = {}
        self._re_spawn_counts: dict[TransportKey, int] = {}
        self._last_sync_time: dict[tuple[TransportKey, Path], float] = {}

        # Queues
        self._diagnostics_queue: list[DiagnosticRequest] = []
        self._tool_queue: list[ToolRequest] = []
        self._notification_queue: list[tuple[str, Path]] = []
        # ("did_save", file_path)

        # Routed results.  Clean publishes are retained as empty batches until
        # the owning consumer acknowledges them.
        self._diagnostic_batches: dict[str, DiagnosticBatch] = {}
        self._acknowledged_batches: dict[str, str] = {}
        self._latest_diagnostic_sequence: dict[
            tuple[str | None, int | None, Path], int
        ] = {}
        self._session_generations: dict[str, int] = {}
        self._next_diagnostic_sequence = 0

        # Lock (RLock for reentrancy in health_check)
        self._lock: threading.RLock = threading.RLock()

        # Worker thread
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._abort_current = False  # set during shutdown to skip in-flight work
        self._request_condition = threading.Condition()

        # Worker event loop reference (set once worker starts)
        self._worker_loop: asyncio.AbstractEventLoop | None = None

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
            transports = tuple(
                sorted(
                    f"{get_language_id_string(language)}:{root}"
                    for language, root in self._transports
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

    # === Lifecycle ===

    def health_check(self) -> LspHealthReport:
        """Scan PATH for available LSP servers.

        Called once at startup.  Availability is cached for the session.
        """
        from reuleauxcoder.extensions.lsp.registry import iter_supported_languages

        report = LspHealthReport()
        for lang in iter_supported_languages():
            cmd, args = self._resolve_command(lang)
            found = shutil.which(cmd) is not None

            with self._lock:
                self._availability[lang] = found

            lang_name = get_language_id_string(lang)
            full_cmd = f"{cmd} {' '.join(args)}".strip()
            details = full_cmd
            report.languages.append((lang_name, found, details))
            report.total += 1
            if found:
                report.available += 1

        return report

    def start_worker(self) -> None:
        """Start the background worker thread (idempotent)."""
        if self._worker_thread is not None:
            return

        with self._lock:
            if self._worker_thread is not None:
                return
            self._stop_event.clear()
            self._abort_current = False
            self._worker_thread = threading.Thread(
                target=self._worker_entry,
                name="lsp-worker",
                daemon=True,
            )
            self._worker_thread.start()
            logger.info("LSP worker thread started")

    def shutdown_all(self) -> None:
        """Gracefully shutdown all LSP servers and stop the worker thread."""
        logger.info("Shutting down LSP manager")
        self._abort_current = True  # tells worker to skip in-flight work
        self._stop_event.set()

        with self._request_condition:
            self._request_condition.notify_all()

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
            self._notification_queue.clear()
            self._diagnostic_batches.clear()
            self._acknowledged_batches.clear()
            self._latest_diagnostic_sequence.clear()
            self._session_generations.clear()

        if self._worker_thread is not None:
            self._worker_thread.join(timeout=WORKER_SHUTDOWN_TIMEOUT)
            if self._worker_thread.is_alive():
                logger.warning("LSP worker thread did not join in time")
            else:
                self._worker_thread = None

        # Fallback for legacy/test-created clients when no worker is alive.
        # Runtime clients are created and closed by the worker event loop.
        if self._worker_thread is None:
            clients: dict[TransportKey, LspClient]
            with self._lock:
                clients = dict(self._transports)
                self._transports.clear()
            for client in clients.values():
                with suppress(Exception):
                    asyncio.run(client.shutdown())

    # === Diagnostics (fire-and-forget) ===

    def enqueue_diagnostics(
        self,
        file_path: Path,
        *,
        route: DiagnosticRoute | None = None,
    ) -> str | None:
        """Enqueue diagnostics and return the future batch identity.

        Stale rejection uses a manager-owned monotonic sequence because round
        numbers are not unique across multiple edits in one LLM response.
        """
        if not self._enabled_for_file(file_path):
            return None

        path = Path(file_path)
        if route is None:
            route = DiagnosticRoute(file_path=path)
        elif route.file_path != path:
            raise ValueError("diagnostic route file_path must match request path")

        with self._lock:
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
            key = (route.agent_id, route.session_generation, path)
            self._latest_diagnostic_sequence[key] = request_sequence
            batch_id = uuid.uuid4().hex
            self._diagnostics_queue = [
                item
                for item in self._diagnostics_queue
                if (
                    item.route.agent_id,
                    item.route.session_generation,
                    item.route.file_path,
                )
                != key
            ]
            self._diagnostics_queue.append(
                DiagnosticRequest(
                    batch_id=batch_id,
                    route=route,
                    request_sequence=request_sequence,
                )
            )

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
        self._session_generations[agent_id] = generation
        self._diagnostics_queue = [
            request
            for request in self._diagnostics_queue
            if not self._is_older_generation(request.route, agent_id, generation)
        ]
        self._diagnostic_batches = {
            batch_id: batch
            for batch_id, batch in self._diagnostic_batches.items()
            if not self._is_older_generation(batch.route, agent_id, generation)
        }
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
            return tuple(
                batch
                for current_id, batch in self._diagnostic_batches.items()
                if (batch_id is None or current_id == batch_id)
                and (route is None or self._route_matches(batch.route, route))
            )

    def acknowledge_diagnostic_batch(self, batch_id: str, *, consumer_id: str) -> bool:
        """Acknowledge exactly one batch, preventing a second consumer."""
        with self._lock:
            if self._diagnostic_batches.pop(batch_id, None) is None:
                return False
            self._record_acknowledgement(batch_id, consumer_id)
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

    # === Active Tools (synchronous bridge) ===

    def send_request_sync(
        self,
        file_path: Path,
        method: str,
        params: dict[str, Any],
        timeout: float = 10.0,
    ) -> Any:
        """Send a synchronous LSP request via the worker thread.

        Blocks the main thread until the worker resolves the future.
        The worker handles document sync (didOpen/didChange) before
        the actual LSP query.

        Raises LspClientError on timeout or server error.
        """
        lang = detect_language(file_path)
        if lang is None:
            raise LspClientError(f"No LSP support for file type: {file_path.suffix}")

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
            timeout=timeout,
            needs_sync=True,
        )

        with self._lock:
            self._tool_queue.append(req)

        with self._request_condition:
            self._request_condition.notify()

        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise LspClientError(f"LSP request '{method}' timed out after {timeout}s")

    # === Notifications (fire-and-forget) ===

    def notify_did_save(self, file_path: Path) -> None:
        """Enqueue a didSave notification.  Returns immediately."""
        if not self._enabled_for_file(file_path):
            return

        with self._lock:
            self._notification_queue.append(("did_save", file_path))

        with self._request_condition:
            self._request_condition.notify()

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
                    await self._handle_tool_request(work)
                elif kind == "diagnostics":
                    await self._handle_diagnostics_request(work)
                elif kind == "notification":
                    await self._handle_notification(*work)
                else:
                    # No work — poll briefly, then check again.
                    # Using asyncio.sleep avoids blocking the event loop
                    # (unlike threading.Condition.wait which would stall it).
                    # The main thread's enqueue + condition.notify() reduces
                    # wakeup latency, but the poll interval is the worst case.
                    await asyncio.sleep(_WORKER_POLL_INTERVAL)
        finally:
            await self._shutdown_clients_async()
            self._worker_loop = None
            logger.info("LSP worker loop exited")

    def _pop_next_work(self) -> tuple[str | None, Any]:
        """Pop exactly one queued item without discarding lower-priority work."""
        with self._lock:
            if self._tool_queue:
                return "tool", self._tool_queue.pop(0)
            if self._diagnostics_queue:
                return "diagnostics", self._diagnostics_queue.pop(0)
            if self._notification_queue:
                return "notification", self._notification_queue.pop(0)
        return None, None

    async def _handle_tool_request(self, req: ToolRequest) -> None:
        """Process a synchronous active-tool request."""
        try:
            server = await self._get_or_create_server(req.language_id, req.file_path)
            if server is None:
                req.future.set_exception(
                    LspClientError(
                        f"No LSP server available for {get_language_id_string(req.language_id)}"
                    )
                )
                return

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
                                self._last_sync_time[key] = (
                                    req.file_path.stat().st_mtime
                                )
                        except Exception as e:
                            logger.debug("LSP sync error (swallowed): %s", e)

            # Execute the actual LSP request
            if self._abort_current:
                req.future.set_exception(LspClientError("LSP manager shutting down"))
                return

            result = await asyncio.wait_for(
                server.send_request(req.method, req.params),
                timeout=req.timeout,
            )
            req.future.set_result(result)

        except asyncio.TimeoutError:
            req.future.set_exception(
                LspClientError(
                    f"LSP request '{req.method}' timed out after {req.timeout}s"
                )
            )
        except Exception as e:
            req.future.set_exception(e)

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
            stale = self._check_stale(lang, file_path)
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
            )
            accepted = False
            with self._lock:
                # A slower obsolete request must not overwrite a newer batch.
                key = (
                    request.route.agent_id,
                    request.route.session_generation,
                    file_path,
                )
                if self._latest_diagnostic_sequence.get(
                    key
                ) == request.request_sequence and self._route_generation_is_current(
                    request.route
                ):
                    self._diagnostic_batches[batch.batch_id] = batch
                    accepted = True
            if accepted:
                self._publish_diagnostic_event(batch)

        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.warning("LSP transport error for %s: %s", lang.name, e)
            self._on_transport_error(lang, str(e))
        except Exception as e:
            logger.debug("LSP diagnostics error (swallowed): %s", e)

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

    async def _handle_notification(
        self,
        kind: str,
        file_path: Path,
    ) -> None:
        """Process a fire-and-forget notification (didSave, etc.)."""
        lang = detect_language(file_path)
        if lang is None:
            return

        try:
            server = self._transports.get(self._transport_key(lang, file_path))
            if server and server.is_alive and server.is_initialized:
                if kind == "did_save":
                    await server.did_save(file_path)
        except Exception as e:
            logger.debug("LSP notification error (swallowed): %s", e)

    # === Internal: Server Lifecycle ===

    async def _get_or_create_server(
        self,
        lang: LanguageId,
        file_path: Path,
    ) -> LspClient | None:
        """Get or create an LSP server (called from worker thread)."""
        key = self._transport_key(lang, file_path)
        server = self._transports.get(key)
        if server is not None and server.is_alive:
            return server

        count = self._re_spawn_counts.get(key, 0)
        if count >= MAX_RESPWANS:
            logger.error(
                "LSP server for %s at %s failed %d times — disabled for this workspace",
                lang.name,
                key[1],
                MAX_RESPWANS,
            )
            return None

        if server is not None:
            await self._discard_transport_async(key, server)
            with self._lock:
                self._re_spawn_counts[key] = count + 1
            if count + 1 >= MAX_RESPWANS:
                return None

        return await self._spawn_async(lang, file_path)

    async def _spawn_async(
        self,
        lang: LanguageId,
        file_path: Path,
    ) -> LspClient | None:
        """Spawn + initialize from the worker thread (inline await)."""
        if lang not in self._availability or not self._availability[lang]:
            return None

        root = self._resolve_root(lang, file_path)
        key = (lang, root)
        override = self._config.get_override(lang.name.lower())
        if override is not None and any(
            value is not None
            for value in (override.cmd, override.args, override.init_opts)
        ):
            cmd, args = self._resolve_command(lang)
            init_opts = self._resolve_init_opts(lang)
        else:
            launch = resolve_server_launch(
                lang,
                root,
                typescript_mode=self._config.typescript_mode,
            )
            cmd, args = launch.command, list(launch.args)
            init_opts = launch.initialization_options

        client = LspClient(language_id=lang, workspace_root=root)

        try:
            await self._do_spawn(client, cmd, args, init_opts)
        except Exception as e:
            logger.warning(
                "Failed to spawn LSP server (async) for %s (%s %s): %s",
                lang.name,
                cmd,
                " ".join(args),
                e,
            )
            with self._lock:
                self._re_spawn_counts[key] = self._re_spawn_counts.get(key, 0) + 1
            return None

        with self._lock:
            self._transports[key] = client
            self._re_spawn_counts[key] = 0

        logger.info(
            "LSP server ready (async): lang=%s, root=%s",
            get_language_id_string(lang),
            root,
        )
        return client

    async def _do_spawn(
        self,
        client: LspClient,
        cmd: str,
        args: list[str],
        init_opts: dict[str, Any] | None,
    ) -> None:
        """Spawn and initialize a client (shared by sync and async paths)."""
        await client.spawn(cmd, args)
        await client.initialize(init_opts)

    async def _discard_transport_async(
        self,
        key: TransportKey,
        client: LspClient | None,
    ) -> None:
        """Remove and shut down a transport on the worker event loop."""
        if client is None:
            return
        with self._lock:
            if self._transports.get(key) is client:
                self._transports.pop(key, None)
        with suppress(Exception):
            await client.shutdown()

    async def _shutdown_clients_async(self) -> None:
        """Shut down all transports on the worker event loop."""
        with self._lock:
            clients = dict(self._transports)
            self._transports.clear()
            self._last_sync_time.clear()

        for client in clients.values():
            with suppress(Exception):
                await client.shutdown()

    def _on_transport_error(self, lang: LanguageId, reason: str) -> None:
        """Mark a transport as dead after a worker-thread error."""
        logger.warning("LSP transport for %s marked dead: %s", lang.name, reason)

    # === Internal: Document Sync ===

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
        """Check if LSP is enabled and supports this file type."""
        if not self._config.enabled:
            return False
        lang = detect_language(file_path)
        if lang is None:
            return False
        with self._lock:
            available = self._availability.get(lang, False)
        return available

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
            if cfg_override.cmd:
                cmd = cfg_override.cmd
            if cfg_override.args:
                args = cfg_override.args
        return cmd, args

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
