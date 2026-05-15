"""LSP Manager — singleton coordinator for LSP server lifecycle.

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
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reuleauxcoder.extensions.lsp.client import (
    LspClient,
    LspClientError,
    MAX_LSP_FILE_SIZE_BYTES,
)
from reuleauxcoder.extensions.lsp.config import LspConfig, LspServerOverride
from reuleauxcoder.extensions.lsp.diagnostics import DiagnosticBlock
from reuleauxcoder.extensions.lsp.registry import (
    LanguageId,
    detect_language,
    get_language_id_string,
    get_server_command,
    resolve_workspace_root,
)

logger = logging.getLogger(__name__)

# === Constants ===

MAX_RESPWANS = 3
WORKER_SHUTDOWN_TIMEOUT = 5.0
_WORKER_POLL_INTERVAL = 0.1
SPAWN_TIMEOUT = 30.0


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


class LspManager:
    """Singleton coordinator for all LSP server interactions.

    All LSP I/O (subprocess stdin/stdout) passes through a single
    background worker thread.  This avoids locks — serialisation is
    natural because only one writer exists.
    """

    def __init__(
        self,
        config: LspConfig,
        workspace_cwd: Path,
    ) -> None:
        self._config = config
        self._workspace_cwd = workspace_cwd

        # Per-language state
        self._transports: dict[LanguageId, LspClient] = {}
        self._workspace_roots: dict[LanguageId, Path] = {}
        self._availability: dict[LanguageId, bool] = {}
        self._re_spawn_counts: dict[LanguageId, int] = {}
        self._last_sync_time: dict[tuple[LanguageId, Path], float] = {}

        # Queues
        self._diagnostics_queue: list[tuple[Path, int]] = []
        self._tool_queue: list[ToolRequest] = []
        self._notification_queue: list[tuple[str, Path]] = []
        # ("did_save", file_path)

        # Results
        self._results: dict[Path, DiagnosticBlock] = {}

        # Lock (RLock for reentrancy in health_check)
        self._lock: threading.RLock = threading.RLock()

        # Worker thread
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
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

    # === Lifecycle ===

    def health_check(self) -> LspHealthReport:
        """Scan PATH for available LSP servers.

        Called once at startup.  Availability is cached for the session.
        """
        from reuleauxcoder.extensions.lsp.registry import iter_supported_languages

        report = LspHealthReport()
        for lang in iter_supported_languages():
            cmd, _args = get_server_command(lang)
            found = shutil.which(cmd) is not None

            with self._lock:
                self._availability[lang] = found

            lang_name = get_language_id_string(lang)
            details = f"✓ {cmd}" if found else f"✗ {cmd} not found on PATH"
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

        if self._worker_thread is not None:
            self._worker_thread.join(timeout=WORKER_SHUTDOWN_TIMEOUT)
            if self._worker_thread.is_alive():
                logger.warning("LSP worker thread did not join in time")
            else:
                self._worker_thread = None

        # Fallback for legacy/test-created clients when no worker is alive.
        # Runtime clients are created and closed by the worker event loop.
        if self._worker_thread is None:
            clients: dict[LanguageId, LspClient]
            with self._lock:
                clients = dict(self._transports)
                self._transports.clear()
            for client in clients.values():
                with suppress(Exception):
                    asyncio.run(client.shutdown())

    # === Diagnostics (fire-and-forget) ===

    def enqueue_diagnostics(self, file_path: Path, seq: int) -> None:
        """Enqueue a diagnostics request.  Returns immediately."""
        if not self._enabled_for_file(file_path):
            return

        with self._lock:
            self._diagnostics_queue.append((file_path, seq))

        with self._request_condition:
            self._request_condition.notify()

    def drain_diagnostics(self) -> list[DiagnosticBlock]:
        """Drain accumulated diagnostics results."""
        with self._lock:
            blocks = list(self._results.values())
            self._results.clear()
        return blocks

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
                # Collect work
                with self._lock:
                    tool = self._tool_queue.pop(0) if self._tool_queue else None
                    diag = (
                        self._diagnostics_queue.pop(0)
                        if self._diagnostics_queue
                        else None
                    )
                    notif = (
                        self._notification_queue.pop(0)
                        if self._notification_queue
                        else None
                    )

                if tool is not None:
                    await self._handle_tool_request(tool)
                elif diag is not None:
                    await self._handle_diagnostics_request(*diag)
                elif notif is not None:
                    await self._handle_notification(*notif)
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
                        key = (req.language_id, req.file_path)
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

    async def _handle_diagnostics_request(
        self,
        file_path: Path,
        seq: int,
    ) -> None:
        """Process a fire-and-forget diagnostics request."""
        lang = detect_language(file_path)
        if lang is None:
            return

        try:
            server = await self._get_or_create_server(lang, file_path)
            if server is None:
                return

            # Sync file content
            stale = self._check_stale(lang, file_path)
            if stale:
                content = self._read_file_content(file_path)
                if content is not None:
                    key = (lang, file_path)
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
            )

            if diagnostics:
                block = DiagnosticBlock(
                    file_path=self._relativize_path(file_path),
                    items=diagnostics,
                )
                with self._lock:
                    self._results[file_path] = block

        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.warning("LSP transport error for %s: %s", lang.name, e)
            self._on_transport_error(lang, str(e))
        except Exception as e:
            logger.debug("LSP diagnostics error (swallowed): %s", e)

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
            server = self._transports.get(lang)
            if server and server.is_alive and server.is_initialized:
                if kind == "did_save":
                    await server.did_save(file_path)
        except Exception as e:
            logger.debug("LSP notification error (swallowed): %s", e)

    # === Internal: Server Lifecycle ===

    def _ensure_server_ready(
        self,
        lang: LanguageId,
        file_path: Path,
    ) -> LspClient | None:
        """Get or create an LSP server.  May block on initial spawn.

        Called synchronously from the main thread.
        """
        with self._lock:
            server = self._transports.get(lang)

        if server is not None:
            if server.is_alive:
                return server
            return self._re_spawn(lang, file_path)

        return self._spawn_blocking(lang, file_path)

    async def _get_or_create_server(
        self,
        lang: LanguageId,
        file_path: Path,
    ) -> LspClient | None:
        """Get or create an LSP server (called from worker thread)."""
        server = self._transports.get(lang)
        if server is not None and server.is_alive:
            return server

        if server is not None:
            await self._discard_transport_async(lang, server)
            count = self._re_spawn_counts.get(lang, 0)
            if count >= MAX_RESPWANS:
                logger.error(
                    "LSP server for %s failed %d times — disabled for this session",
                    lang.name,
                    MAX_RESPWANS,
                )
                with self._lock:
                    self._availability[lang] = False
                return None
            with self._lock:
                self._re_spawn_counts[lang] = count + 1

        return await self._spawn_async(lang, file_path)

    def _spawn_blocking(
        self,
        lang: LanguageId,
        file_path: Path,
    ) -> LspClient | None:
        """Spawn + initialize from the main thread.

        Creates a temporary asyncio event loop for the one-shot
        spawn + initialize handshake.  After initialization, all
        further communication goes through the worker thread.
        """
        if lang not in self._availability or not self._availability[lang]:
            return None

        root = self._resolve_root(lang, file_path)
        cmd, args = self._resolve_command(lang)
        init_opts = self._resolve_init_opts(lang)

        client = LspClient(language_id=lang, workspace_root=root)

        try:
            # Use a temp event loop for spawn + initialize only
            asyncio.run(self._do_spawn(client, cmd, args, init_opts))
        except Exception as e:
            logger.warning(
                "Failed to spawn LSP server for %s (%s %s): %s",
                lang.name,
                cmd,
                " ".join(args),
                e,
            )
            with self._lock:
                self._availability[lang] = False
            return None

        with self._lock:
            self._transports[lang] = client
            self._re_spawn_counts[lang] = 0

        logger.info(
            "LSP server ready: lang=%s, root=%s",
            get_language_id_string(lang),
            root,
        )
        return client

    async def _spawn_async(
        self,
        lang: LanguageId,
        file_path: Path,
    ) -> LspClient | None:
        """Spawn + initialize from the worker thread (inline await)."""
        if lang not in self._availability or not self._availability[lang]:
            return None

        root = self._resolve_root(lang, file_path)
        cmd, args = self._resolve_command(lang)
        init_opts = self._resolve_init_opts(lang)

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
                self._availability[lang] = False
            return None

        with self._lock:
            self._transports[lang] = client
            self._re_spawn_counts[lang] = 0

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
        lang: LanguageId,
        client: LspClient | None,
    ) -> None:
        """Remove and shut down a transport on the worker event loop."""
        if client is None:
            return
        with self._lock:
            if self._transports.get(lang) is client:
                self._transports.pop(lang, None)
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

    def _re_spawn(self, lang: LanguageId, file_path: Path) -> LspClient | None:
        """Attempt to re-spawn a crashed LSP server."""
        count = self._re_spawn_counts.get(lang, 0)

        if count >= MAX_RESPWANS:
            logger.error(
                "LSP server for %s failed %d times — disabled for this session",
                lang.name,
                MAX_RESPWANS,
            )
            with self._lock:
                self._availability[lang] = False
            return None

        logger.warning(
            "Re-spawning LSP server for %s (attempt %d/%d)",
            lang.name,
            count + 1,
            MAX_RESPWANS,
        )

        with self._lock:
            self._transports.pop(lang, None)
            self._re_spawn_counts[lang] = count + 1

        return self._spawn_blocking(lang, file_path)

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

        key = (lang, file_path)
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
        with self._lock:
            self._workspace_roots[lang] = root
        return root

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
        if cfg_override:
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
        """Convert absolute path to workspace-relative, or basename."""
        try:
            return str(file_path.relative_to(self._workspace_cwd))
        except ValueError:
            return file_path.name
