"""Tests for LspManager — synchronous surface and helper methods.

Tests that require actual LSP subprocess communication (async worker,
spawn/initialize) are deferred to integration tests.

This module tests:
- Explicit health checks and lazy command availability
- Server lifecycle state transitions (re-spawn limit)
- File staleness detection
- Config override resolution
- Relativize path
"""

from __future__ import annotations

import concurrent.futures
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from reuleauxcoder.domain.runtime.events import (
    DiagnosticsCleared,
    DiagnosticsPublished,
)
from reuleauxcoder.extensions.lsp.client import LspClientError
from reuleauxcoder.extensions.lsp.config import LspConfig, LspServerOverride
from reuleauxcoder.extensions.lsp.manager import (
    DIAGNOSTIC_BATCH_TTL_SECONDS,
    MAX_RESPWANS,
    MAX_PENDING_DIAGNOSTIC_BATCHES_PER_OWNER,
    MISSING_COMMAND_TTL_SECONDS,
    DiagnosticRequest,
    LspManager,
    LspTransportState,
    ToolRequest,
)
from reuleauxcoder.extensions.lsp.diagnostics import (
    Diagnostic,
    DiagnosticBatch,
    DiagnosticBlock,
    DiagnosticRoute,
    DiagnosticRouteFilter,
)
from reuleauxcoder.extensions.lsp.registry import LanguageId


def _make_mock_client(*, alive: bool = True) -> MagicMock:
    """Build a mock LspClient for lifecycle tests."""
    from unittest.mock import MagicMock

    c = MagicMock()
    c.is_alive = alive
    c.is_usable = alive
    c.is_initialized = True
    c.shutdown = AsyncMock()
    c.abort = AsyncMock()
    return c


@pytest.fixture
def manager() -> LspManager:
    """Create a manager with all languages marked unavailable."""
    config = LspConfig(enabled=True)
    mgr = LspManager(config, workspace_cwd=Path("/tmp"))
    # Queue-focused unit tests inspect requests synchronously.  Keep their
    # worker deterministic; the dedicated enqueue test below exercises the
    # real thread lifecycle.
    mgr.start_worker = MagicMock()  # type: ignore[method-assign]
    mgr._accepting_work = True
    # Mark all languages unavailable to avoid accidental spawn attempts
    for lang in LanguageId:
        with mgr._lock:
            mgr._availability[lang] = False
    return mgr


class TestHealthCheck:
    def test_health_check_does_not_populate_lazy_runtime_cache(self) -> None:
        manager = LspManager(LspConfig(enabled=True), workspace_cwd=Path("/tmp"))
        lookup = MagicMock(return_value=None)
        manager._command_lookup = lookup

        report = manager.health_check()

        assert report.total == 9  # 9 supported languages
        assert report.available == 0
        assert lookup.call_count == report.total
        with manager._lock:
            assert manager._availability == {}
            assert manager._negative_availability_until == {}
        assert manager.availability_metrics() == {
            "lookups": 0,
            "available": 0,
            "unavailable": 0,
            "negative_cache_hits": 0,
        }

    def test_health_report_has_language_entries(self, manager: LspManager) -> None:
        report = manager.health_check()
        for lang_name, available, details in report.languages:
            assert isinstance(lang_name, str)
            assert isinstance(available, bool)
            assert isinstance(details, str)


class TestReSpawnLimit:
    def test_re_spawn_increments_counter(self, manager: LspManager) -> None:
        """When a dead server is found, _get_or_create_server increments the count."""
        import asyncio

        manager._availability[LanguageId.PYTHON] = True
        path = Path("/tmp/test.py")
        key = manager._transport_key(LanguageId.PYTHON, path)
        manager._re_spawn_counts[key] = 0
        dead = _make_mock_client(alive=False)
        manager._transports[key] = dead

        async def run():
            with patch.object(manager, "_spawn_async", return_value=dead) as spawn:
                result = await manager._get_or_create_server(LanguageId.PYTHON, path)
            return result, spawn

        result, spawn = asyncio.run(run())
        assert result is dead
        spawn.assert_called_once()
        assert manager._re_spawn_counts.get(key, 0) == 1

    def test_re_spawn_limit_is_scoped_to_workspace(self, manager: LspManager) -> None:
        """A failed workspace must not disable that language for other roots."""
        import asyncio

        manager._availability[LanguageId.PYTHON] = True
        path = Path("/tmp/test.py")
        key = manager._transport_key(LanguageId.PYTHON, path)
        manager._re_spawn_counts[key] = MAX_RESPWANS
        dead = _make_mock_client(alive=False)
        manager._transports[key] = dead

        async def run():
            return await manager._get_or_create_server(LanguageId.PYTHON, path)

        result = asyncio.run(run())
        assert result is None
        with manager._lock:
            assert manager._availability[LanguageId.PYTHON] is True

    def test_dead_transport_reaching_limit_reports_error(
        self, manager: LspManager
    ) -> None:
        import asyncio

        path = Path("/tmp/test.py")
        key = manager._transport_key(LanguageId.PYTHON, path)
        manager._re_spawn_counts[key] = MAX_RESPWANS - 1
        manager._transports[key] = _make_mock_client(alive=False)

        with patch.object(manager, "_spawn_async") as spawn:
            result = asyncio.run(manager._get_or_create_server(LanguageId.PYTHON, path))

        assert result is None
        spawn.assert_not_called()
        status = manager.transport_status_for_file(path)
        assert status is not None
        assert status.state is LspTransportState.ERROR
        assert status.error_type == "RespawnLimitReached"


class TestWorkspaceTransportIsolation:
    def test_same_language_uses_distinct_servers_per_root(
        self, manager: LspManager, tmp_path: Path
    ) -> None:
        import asyncio

        first_root = tmp_path / "first"
        second_root = tmp_path / "second"
        for root in (first_root, second_root):
            root.mkdir()
            (root / "pyproject.toml").write_text("[project]\nname='demo'\n")
        first_path = first_root / "main.py"
        second_path = second_root / "main.py"
        first_path.write_text("x = 1\n")
        second_path.write_text("x = 2\n")

        first = _make_mock_client()
        second = _make_mock_client()
        first_key = manager._transport_key(LanguageId.PYTHON, first_path)
        second_key = manager._transport_key(LanguageId.PYTHON, second_path)
        manager._transports[first_key] = first
        manager._transports[second_key] = second

        async def run():
            return (
                await manager._get_or_create_server(LanguageId.PYTHON, first_path),
                await manager._get_or_create_server(LanguageId.PYTHON, second_path),
            )

        resolved_first, resolved_second = asyncio.run(run())

        assert first_key != second_key
        assert resolved_first is first
        assert resolved_second is second


class TestFileStaleness:
    def test_file_not_stale_when_no_last_sync(
        self, manager: LspManager, tmp_path: Path
    ) -> None:
        f = tmp_path / "test.py"
        f.write_text("hello")
        # No last_sync_time entry → technically stale (mtime > 0)
        assert manager._check_stale(LanguageId.PYTHON, f) is True

    def test_file_not_stale_when_up_to_date(
        self, manager: LspManager, tmp_path: Path
    ) -> None:
        f = tmp_path / "test.py"
        f.write_text("hello")
        future_mtime = f.stat().st_mtime + 100
        key = manager._transport_key(LanguageId.PYTHON, f)
        manager._last_sync_time[(key, f)] = future_mtime
        assert manager._check_stale(LanguageId.PYTHON, f) is False

    def test_file_stale_after_edit(self, manager: LspManager, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("old")
        key = manager._transport_key(LanguageId.PYTHON, f)
        manager._last_sync_time[(key, f)] = f.stat().st_mtime
        time.sleep(0.01)  # ensure mtime changes
        f.write_text("new")
        assert manager._check_stale(LanguageId.PYTHON, f) is True

    def test_missing_file_not_stale(self, manager: LspManager) -> None:
        assert manager._check_stale(LanguageId.PYTHON, Path("/nonexistent.py")) is False


class TestReadFileContent:
    def test_read_normal_file(self, manager: LspManager, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("print(1)")
        assert manager._read_file_content(f) == "print(1)"

    def test_read_missing_file(self, manager: LspManager) -> None:
        assert manager._read_file_content(Path("/nonexistent.py")) is None


class TestConfigOverrides:
    def test_resolve_command_with_override(self, manager: LspManager) -> None:
        manager._config.server_overrides["python"] = LspServerOverride(
            language="python",
            cmd="/custom/pyright",
            args=["--custom"],
        )
        cmd, args = manager._resolve_command(LanguageId.PYTHON)
        assert cmd == "/custom/pyright"
        assert args == ["--custom"]

    def test_resolve_command_no_override(self, manager: LspManager) -> None:
        cmd, args = manager._resolve_command(LanguageId.RUST)
        assert cmd == "rust-analyzer"

    def test_resolve_launch_accepts_explicit_empty_args(
        self, manager: LspManager
    ) -> None:
        manager._config.server_overrides["python"] = LspServerOverride(
            language="python",
            args=[],
        )

        launch = manager._resolve_launch(LanguageId.PYTHON, Path("/tmp"))

        assert launch.args == ()

    def test_init_override_preserves_root_aware_typescript_launch(
        self, manager: LspManager, tmp_path: Path
    ) -> None:
        package_root = tmp_path / "node_modules" / "typescript"
        package_root.mkdir(parents=True)
        (package_root / "package.json").write_text(
            '{"version": "7.0.0"}', encoding="utf-8"
        )
        manager._config.server_overrides["typescript"] = LspServerOverride(
            language="typescript",
            init_opts={"typescript": {"custom": True}},
        )

        launch = manager._resolve_launch(LanguageId.TYPESCRIPT, tmp_path)

        assert launch.args == (
            str(package_root / "bin" / "tsc"),
            "--lsp",
            "--stdio",
        )
        assert launch.initialization_options == {"typescript": {"custom": True}}

    def test_resolve_init_opts(self, manager: LspManager) -> None:
        manager._config.server_overrides["python"] = LspServerOverride(
            language="python",
            init_opts={"python.analysis.extraPaths": ["./lib"]},
        )
        opts = manager._resolve_init_opts(LanguageId.PYTHON)
        assert opts == {"python.analysis.extraPaths": ["./lib"]}

    def test_resolve_init_opts_none(self, manager: LspManager) -> None:
        assert manager._resolve_init_opts(LanguageId.RUST) is None

    def test_get_workspace_root_override(self, manager: LspManager) -> None:
        manager._config.server_overrides["rust"] = LspServerOverride(
            language="rust",
            workspace_root="/custom/crate",
        )
        assert manager._get_workspace_root_override(LanguageId.RUST) == "/custom/crate"

    def test_get_workspace_root_override_none(self, manager: LspManager) -> None:
        assert manager._get_workspace_root_override(LanguageId.RUST) is None


class TestEnabledForFile:
    def test_disabled_when_config_disabled(self, manager: LspManager) -> None:
        manager._config.enabled = False
        assert manager._enabled_for_file(Path("/tmp/test.py")) is False

    def test_disabled_when_unsupported_extension(self, manager: LspManager) -> None:
        manager._config.enabled = True
        assert manager._enabled_for_file(Path("/tmp/notes.txt")) is False

    def test_disabled_when_language_unavailable(self, manager: LspManager) -> None:
        manager._config.enabled = True
        with manager._lock:
            manager._availability[LanguageId.PYTHON] = False
        assert manager._enabled_for_file(Path("/tmp/test.py")) is False

    def test_enabled_when_all_conditions_met(self, manager: LspManager) -> None:
        manager._config.enabled = True
        with manager._lock:
            manager._availability[LanguageId.PYTHON] = True
        assert manager._enabled_for_file(Path("/tmp/test.py")) is True

    def test_supported_unknown_does_not_probe_path(self, manager: LspManager) -> None:
        lookup = MagicMock(side_effect=AssertionError("PATH must stay lazy"))
        manager._command_lookup = lookup
        with manager._lock:
            manager._availability.pop(LanguageId.PYTHON)

        assert manager._enabled_for_file(Path("/tmp/test.py")) is True
        lookup.assert_not_called()


class TestLazyCommandAvailability:
    def test_missing_command_cache_expires_and_is_keyed_by_final_command(
        self, manager: LspManager
    ) -> None:
        assert MISSING_COMMAND_TTL_SECONDS == 30.0
        now = [100.0]
        launch_command = "node_modules/.bin/lsp"
        first_key = (LanguageId.PYTHON, Path("/workspace/first").resolve())
        second_key = (LanguageId.PYTHON, Path("/workspace/second").resolve())
        first_command = "/workspace/first/node_modules/.bin/lsp"
        second_command = "/workspace/second/node_modules/.bin/lsp"
        resolved = {
            first_command: None,
            second_command: second_command,
        }
        lookup = MagicMock(side_effect=lambda command: resolved[command])
        manager._availability_clock = lambda: now[0]
        manager._command_lookup = lookup
        with manager._lock:
            manager._availability.pop(LanguageId.PYTHON)

        assert not manager._command_available(first_key, launch_command)

        resolved[first_command] = first_command
        now[0] += MISSING_COMMAND_TTL_SECONDS - 0.001
        assert not manager._command_available(first_key, launch_command)
        assert manager._command_available(second_key, launch_command)

        now[0] += 0.001
        assert manager._command_available(first_key, launch_command)
        assert [call.args[0] for call in lookup.call_args_list] == [
            first_command,
            second_command,
            first_command,
        ]
        assert manager.availability_metrics() == {
            "lookups": 3,
            "available": 2,
            "unavailable": 1,
            "negative_cache_hits": 1,
        }
        with manager._lock:
            assert manager._negative_availability_until == {}

    def test_dot_relative_command_is_checked_from_transport_root(
        self, manager: LspManager, tmp_path: Path, monkeypatch
    ) -> None:
        app_cwd = tmp_path / "app"
        available_root = tmp_path / "available"
        missing_root = tmp_path / "missing"
        for directory in (app_cwd, available_root, missing_root):
            directory.mkdir()
        for directory in (app_cwd, available_root):
            executable = directory / "fake-lsp"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
        monkeypatch.chdir(app_cwd)
        with manager._lock:
            manager._availability.pop(LanguageId.PYTHON)

        assert manager._command_available(
            (LanguageId.PYTHON, available_root), "./fake-lsp"
        )
        assert not manager._command_available(
            (LanguageId.PYTHON, missing_root), "./fake-lsp"
        )


class TestRelativizePath:
    def test_within_workspace(self, manager: LspManager) -> None:
        mgr = LspManager(LspConfig(), workspace_cwd=Path("/home/user/proj"))
        assert (
            mgr._relativize_path(Path("/home/user/proj/src/main.py")) == "src/main.py"
        )

    def test_outside_workspace(self, manager: LspManager) -> None:
        mgr = LspManager(LspConfig(), workspace_cwd=Path("/home/user/proj"))
        path = mgr._relativize_path(Path("/etc/passwd"))
        assert path == "passwd"


class TestSendRequestSyncValidation:
    def test_raises_for_unsupported_file(self, manager: LspManager) -> None:
        with pytest.raises(LspClientError, match="No LSP support"):
            manager.send_request_sync(
                Path("/tmp/notes.txt"),
                "textDocument/definition",
                {},
            )

    def test_raises_when_server_unavailable(self) -> None:
        manager = LspManager(LspConfig(enabled=True), workspace_cwd=Path("/tmp"))
        manager._availability[LanguageId.PYTHON] = False
        try:
            with pytest.raises(LspClientError, match="No LSP server available"):
                manager.send_request_sync(
                    Path("/tmp/test.py"),
                    "textDocument/definition",
                    {},
                    timeout=1.0,
                )
        finally:
            assert manager.shutdown_all(timeout=1.0)


class TestEnqueueDiagnostics:
    def test_enqueue_when_enabled(self) -> None:
        manager = LspManager(LspConfig(enabled=True), workspace_cwd=Path("/tmp"))
        manager._config.enabled = True
        with manager._lock:
            manager._availability[LanguageId.PYTHON] = True

        with patch.object(
            manager,
            "_worker_entry",
            side_effect=lambda: manager._stop_event.wait(),
        ) as worker_entry:
            assert manager._worker_thread is None
            try:
                batch_id = manager.enqueue_diagnostics(Path("/tmp/test.py"))
                worker = manager._worker_thread
                assert batch_id is not None
                assert worker is not None
                assert worker.is_alive()
                assert len(manager._diagnostics_queue) == 1
            finally:
                assert manager.shutdown_all(timeout=1.0)

        worker_entry.assert_called_once_with()
        assert manager._worker_thread is None

    def test_no_enqueue_when_disabled(self, manager: LspManager) -> None:
        manager._config.enabled = False
        manager.enqueue_diagnostics(Path("/tmp/test.py"))
        assert len(manager._diagnostics_queue) == 0
        assert manager._worker_thread is None


class TestDiagnosticBatchConsumption:
    def test_consume_acknowledges_exact_batch(self, manager: LspManager) -> None:
        route = DiagnosticRoute(file_path=Path("/tmp/test.py"), agent_id="agent-1")
        block = DiagnosticBlock(
            file_path="test.py",
            items=[Diagnostic(line=1, character=1, message="err")],
        )
        batch = DiagnosticBatch(
            batch_id="batch-1",
            route=route,
            request_sequence=1,
            document_version=2,
            diagnostic_generation=3,
            block=block,
        )
        with manager._lock:
            manager._diagnostic_batches[batch.batch_id] = batch

        consumed = manager.consume_diagnostic_batches(consumer_id="consumer-1")
        assert consumed == (batch,)
        assert manager.pending_diagnostic_batches() == ()
        assert manager.diagnostic_batch_acknowledgement("batch-1") == "consumer-1"
        assert not manager.acknowledge_diagnostic_batch(
            "batch-1", consumer_id="consumer-2"
        )

    def test_route_filter_isolates_agent_generation_turn_and_two_files(
        self, manager: LspManager
    ) -> None:
        def publish(batch_id: str, route: DiagnosticRoute) -> DiagnosticBatch:
            batch = DiagnosticBatch(
                batch_id=batch_id,
                route=route,
                request_sequence=len(manager._diagnostic_batches) + 1,
                document_version=1,
                diagnostic_generation=1,
                block=DiagnosticBlock(file_path=str(route.file_path), items=[]),
            )
            manager._diagnostic_batches[batch_id] = batch
            return batch

        parent_a = publish(
            "parent-a",
            DiagnosticRoute(
                file_path=Path("/tmp/a.py"),
                agent_id="parent",
                session_generation=2,
                session_id="session",
                turn_id="turn",
                tool_call_id="tool-a",
            ),
        )
        parent_b = publish(
            "parent-b",
            DiagnosticRoute(
                file_path=Path("/tmp/b.py"),
                agent_id="parent",
                session_generation=2,
                session_id="session",
                turn_id="turn",
                tool_call_id="tool-b",
            ),
        )
        publish(
            "old-generation",
            DiagnosticRoute(
                file_path=Path("/tmp/a.py"),
                agent_id="parent",
                session_generation=1,
                session_id="session",
                turn_id="turn",
            ),
        )
        publish(
            "subagent",
            DiagnosticRoute(
                file_path=Path("/tmp/a.py"),
                agent_id="child",
                session_generation=2,
                session_id="session",
                turn_id="turn",
            ),
        )

        consumed = manager.consume_diagnostic_batches(
            consumer_id="parent-injector",
            route=DiagnosticRouteFilter(
                agent_id="parent",
                session_generation=2,
                session_id="session",
                turn_id="turn",
            ),
        )

        assert consumed == (parent_a, parent_b)
        assert {batch.batch_id for batch in manager.pending_diagnostic_batches()} == {
            "old-generation",
            "subagent",
        }

    def test_exact_owner_selection_crosses_turn_but_not_session(
        self, manager: LspManager
    ) -> None:
        def publish(batch_id: str, *, session_id: str, turn_id: str) -> None:
            route = DiagnosticRoute(
                file_path=Path(f"/tmp/{batch_id}.py"),
                agent_id="parent",
                session_generation=2,
                session_id=session_id,
                turn_id=turn_id,
            )
            manager._diagnostic_batches[batch_id] = DiagnosticBatch(
                batch_id=batch_id,
                route=route,
                request_sequence=1,
                document_version=1,
                diagnostic_generation=1,
                block=DiagnosticBlock(file_path=str(route.file_path), items=[]),
            )

        publish("same-owner", session_id="session-a", turn_id="turn-1")
        publish("other-session", session_id="session-b", turn_id="turn-2")

        selected = manager.pending_diagnostic_batches_for_owner(
            agent_id="parent",
            session_generation=2,
            session_id="session-a",
        )

        assert [batch.batch_id for batch in selected] == ["same-owner"]


class TestDiagnosticBatchBounds:
    @staticmethod
    def _batch(
        batch_id: str,
        *,
        path: str,
        sequence: int,
        created_at: float,
        session_id: str = "session",
    ) -> DiagnosticBatch:
        route = DiagnosticRoute(
            file_path=Path(path),
            agent_id="parent",
            session_generation=2,
            session_id=session_id,
            turn_id="origin",
        )
        return DiagnosticBatch(
            batch_id=batch_id,
            route=route,
            request_sequence=sequence,
            document_version=sequence,
            diagnostic_generation=sequence,
            block=DiagnosticBlock(file_path=path, items=[]),
            created_at=created_at,
        )

    def test_ttl_prunes_once_at_the_boundary(self, manager: LspManager) -> None:
        manager._diagnostic_clock = lambda: 1000.0
        expired = self._batch(
            "expired",
            path="/tmp/expired.py",
            sequence=1,
            created_at=1000.0 - DIAGNOSTIC_BATCH_TTL_SECONDS,
        )
        current = self._batch(
            "current",
            path="/tmp/current.py",
            sequence=2,
            created_at=1000.0 - DIAGNOSTIC_BATCH_TTL_SECONDS + 0.001,
        )
        manager._diagnostic_batches = {
            expired.batch_id: expired,
            current.batch_id: current,
        }

        assert manager.pending_diagnostic_batches() == (current,)
        assert manager.pending_diagnostic_batches() == (current,)
        assert manager.diagnostic_batch_metrics()["expired"] == 1

    def test_ack_succeeds_after_peek_crosses_ttl_boundary(
        self, manager: LspManager
    ) -> None:
        now = [1000.0]
        manager._diagnostic_clock = lambda: now[0]
        batch = self._batch(
            "injected",
            path="/tmp/injected.py",
            sequence=1,
            created_at=701.0,
        )
        manager._diagnostic_batches[batch.batch_id] = batch

        assert manager.pending_diagnostic_batches() == (batch,)
        now[0] = 1002.0

        assert manager.acknowledge_diagnostic_batch(
            batch.batch_id,
            consumer_id="successful-injector",
        )
        assert manager.diagnostic_batch_metrics()["expired"] == 0

    def test_new_document_state_overwrites_pending_batch(
        self, manager: LspManager
    ) -> None:
        manager._diagnostic_clock = lambda: 1000.0
        old = self._batch("old", path="/tmp/main.py", sequence=1, created_at=900.0)
        new = self._batch("new", path="/tmp/main.py", sequence=2, created_at=901.0)

        with manager._lock:
            manager._store_diagnostic_batch_locked(old)
            manager._store_diagnostic_batch_locked(new)

        assert manager.pending_diagnostic_batches() == (new,)
        assert manager.diagnostic_batch_metrics()["overwritten"] == 1

    def test_enqueue_discards_pending_state_for_same_document(
        self, manager: LspManager
    ) -> None:
        manager._diagnostic_clock = lambda: 1000.0
        manager._availability[LanguageId.PYTHON] = True
        old = self._batch("old", path="/tmp/main.py", sequence=1, created_at=900.0)
        manager._diagnostic_batches[old.batch_id] = old

        queued = manager.enqueue_diagnostics(
            Path("/tmp/main.py"),
            route=old.route,
        )

        assert queued is not None
        assert manager.pending_diagnostic_batches() == ()
        assert manager.diagnostic_batch_metrics()["overwritten"] == 1

    def test_capacity_evicts_only_oldest_batch_for_same_owner(
        self, manager: LspManager
    ) -> None:
        manager._diagnostic_clock = lambda: 1000.0
        other = self._batch(
            "other-owner",
            path="/tmp/other.py",
            sequence=1,
            created_at=900.0,
            session_id="other-session",
        )
        with manager._lock:
            manager._store_diagnostic_batch_locked(other)
            for index in range(MAX_PENDING_DIAGNOSTIC_BATCHES_PER_OWNER + 1):
                manager._store_diagnostic_batch_locked(
                    self._batch(
                        f"batch-{index}",
                        path=f"/tmp/file-{index}.py",
                        sequence=index + 1,
                        created_at=900.0 + index,
                    )
                )

        ids = {batch.batch_id for batch in manager.pending_diagnostic_batches()}
        assert "batch-0" not in ids
        assert "batch-1" in ids
        assert "other-owner" in ids
        assert len(ids) == MAX_PENDING_DIAGNOSTIC_BATCHES_PER_OWNER + 1
        assert manager.diagnostic_batch_metrics()["capacity_evicted"] == 1


class TestWorkerQueueOrdering:
    def test_pop_removes_exactly_one_queue_item(self, manager: LspManager) -> None:
        tool = ToolRequest(
            file_path=Path("/tmp/tool.py"),
            language_id=LanguageId.PYTHON,
            method="test/tool",
            params={},
            future=concurrent.futures.Future(),
            timeout_seconds=1.0,
            deadline_at=time.monotonic() + 1.0,
            enqueue_sequence=1,
        )
        manager._tool_queue.append(tool)
        request = DiagnosticRequest(
            batch_id="batch-a",
            route=DiagnosticRoute(file_path=Path("/tmp/a.py")),
            request_sequence=1,
            enqueue_sequence=2,
        )
        manager._diagnostics_queue.append(request)

        assert manager._pop_next_work()[:2] == ("tool", tool)
        assert len(manager._diagnostics_queue) == 1
        assert manager._pop_next_work()[0] == "diagnostics"
        assert manager._pop_next_work() == (None, None, None)

    def test_cross_queue_dispatch_is_global_fifo(self, manager: LspManager) -> None:
        for index, sequence in enumerate((1, 3)):
            manager._tool_queue.append(
                ToolRequest(
                    file_path=Path(f"/tmp/tool-{index}.py"),
                    language_id=LanguageId.PYTHON,
                    method=f"test/tool-{index}",
                    params={},
                    future=concurrent.futures.Future(),
                    timeout_seconds=1.0,
                    deadline_at=time.monotonic() + 1.0,
                    transport_key=(
                        LanguageId.PYTHON,
                        Path(f"/tmp/root-{index}"),
                    ),
                    enqueue_sequence=sequence,
                )
            )
        diagnostics = DiagnosticRequest(
            batch_id="batch-fair",
            route=DiagnosticRoute(file_path=Path("/tmp/fair.py")),
            request_sequence=1,
            transport_key=(LanguageId.PYTHON, Path("/tmp/diagnostics")),
            enqueue_sequence=2,
        )
        manager._diagnostics_queue.append(diagnostics)

        assert manager._pop_next_work()[0] == "tool"
        assert manager._pop_next_work()[:2] == ("diagnostics", diagnostics)
        assert manager._pop_next_work()[0] == "tool"

    def test_same_transport_preserves_cross_queue_order(
        self, manager: LspManager
    ) -> None:
        transport_key = (LanguageId.PYTHON, Path("/tmp/shared"))
        diagnostics = DiagnosticRequest(
            batch_id="batch-first",
            route=DiagnosticRoute(file_path=Path("/tmp/shared/main.py")),
            request_sequence=1,
            transport_key=transport_key,
            enqueue_sequence=1,
        )
        tool = ToolRequest(
            file_path=Path("/tmp/shared/main.py"),
            language_id=LanguageId.PYTHON,
            method="test/second",
            params={},
            future=concurrent.futures.Future(),
            timeout_seconds=1.0,
            deadline_at=time.monotonic() + 1.0,
            transport_key=transport_key,
            enqueue_sequence=2,
        )
        manager._diagnostics_queue.append(diagnostics)
        manager._tool_queue.append(tool)

        assert manager._pop_next_work()[:2] == ("diagnostics", diagnostics)
        assert manager._pop_next_work()[:2] == ("tool", tool)

    def test_document_commit_syncs_then_saves_then_waits(
        self, manager: LspManager, tmp_path: Path
    ) -> None:
        import asyncio

        path = tmp_path / "main.py"
        path.write_text("x = 1", encoding="utf-8")
        manager._availability[LanguageId.PYTHON] = True
        calls: list[str] = []
        server = MagicMock()
        server.diagnostics_generation.side_effect = [0, 1]
        server.diagnostic_document_version.return_value = 1

        async def did_open(_path, _content):
            calls.append("did_open")

        async def did_save(_path):
            calls.append("did_save")

        async def wait_for_diagnostics(*_args, **_kwargs):
            calls.append("wait_for_diagnostics")
            return [Diagnostic(line=1, character=1, message="saved")]

        server.did_open.side_effect = did_open
        server.did_save.side_effect = did_save
        server.wait_for_diagnostics.side_effect = wait_for_diagnostics
        manager._get_or_create_server = AsyncMock(return_value=server)

        batch_id = manager.enqueue_diagnostics(path, document_committed=True)
        request = manager._diagnostics_queue.pop()
        asyncio.run(manager._handle_diagnostics_request(request))

        assert calls == ["did_open", "did_save", "wait_for_diagnostics"]
        assert manager.pending_diagnostic_batches(batch_id=batch_id)

    def test_document_commit_forces_sync_when_mtime_is_unchanged(
        self, manager: LspManager, tmp_path: Path
    ) -> None:
        import asyncio

        path = tmp_path / "main.py"
        path.write_text("value = 2", encoding="utf-8")
        manager._availability[LanguageId.PYTHON] = True
        key = (manager._transport_key(LanguageId.PYTHON, path), path)
        manager._last_sync_time[key] = path.stat().st_mtime
        calls: list[str] = []
        server = MagicMock()
        server.diagnostics_generation.side_effect = [0, 1]
        server.diagnostic_document_version.return_value = 2

        async def did_change(_path, content):
            assert content == "value = 2"
            calls.append("did_change")

        async def did_save(_path):
            calls.append("did_save")

        async def wait_for_diagnostics(*_args, **_kwargs):
            calls.append("wait_for_diagnostics")
            return []

        server.did_change.side_effect = did_change
        server.did_save.side_effect = did_save
        server.wait_for_diagnostics.side_effect = wait_for_diagnostics
        manager._get_or_create_server = AsyncMock(return_value=server)

        batch_id = manager.enqueue_diagnostics(path, document_committed=True)
        request = manager._diagnostics_queue.pop()
        asyncio.run(manager._handle_diagnostics_request(request))

        assert calls == ["did_change", "did_save", "wait_for_diagnostics"]
        assert manager.pending_diagnostic_batches(batch_id=batch_id)

    def test_enqueue_replaces_older_pending_request_for_same_owner_and_file(
        self, manager: LspManager
    ) -> None:
        manager._config.enabled = True
        manager._availability[LanguageId.PYTHON] = True
        path = Path("/tmp/test.py")

        manager.enqueue_diagnostics(path)
        manager.enqueue_diagnostics(path)
        manager.enqueue_diagnostics(path)

        assert len(manager._diagnostics_queue) == 1
        assert manager._diagnostics_queue[0].route.file_path == path
        assert manager._diagnostics_queue[0].request_sequence == 3

    def test_same_agent_generation_and_file_keeps_sessions_independent(
        self, manager: LspManager
    ) -> None:
        manager._availability[LanguageId.PYTHON] = True
        path = Path("/tmp/session.py")
        for session_id in ("session-a", "session-b"):
            manager.enqueue_diagnostics(
                path,
                route=DiagnosticRoute(
                    file_path=path,
                    agent_id="parent",
                    session_generation=2,
                    session_id=session_id,
                ),
            )

        assert len(manager._diagnostics_queue) == 2
        assert {request.route.session_id for request in manager._diagnostics_queue} == {
            "session-a",
            "session-b",
        }


class TestSessionGenerationWatermark:
    def test_advance_evicts_old_queue_and_batches_and_rejects_old_enqueue(
        self, manager: LspManager
    ) -> None:
        manager._availability[LanguageId.PYTHON] = True
        path = Path("/tmp/session.py")
        old_route = DiagnosticRoute(
            file_path=path,
            agent_id="agent-1",
            session_generation=1,
        )
        batch_id = manager.enqueue_diagnostics(path, route=old_route)
        assert batch_id is not None
        manager._diagnostic_batches["completed-old"] = DiagnosticBatch(
            batch_id="completed-old",
            route=old_route,
            request_sequence=1,
            document_version=1,
            diagnostic_generation=1,
            block=DiagnosticBlock(file_path=str(path), items=[]),
        )

        manager.advance_session_generation("agent-1", 2)

        assert manager._diagnostics_queue == []
        assert manager.pending_diagnostic_batches() == ()
        assert manager.enqueue_diagnostics(path, route=old_route) is None
        assert manager.diagnostic_batch_metrics()["stale_discarded"] == 1

    def test_inflight_old_generation_cannot_publish_after_reset(
        self, manager: LspManager, tmp_path: Path
    ) -> None:
        import asyncio

        path = tmp_path / "main.py"
        path.write_text("x = 1")
        manager._availability[LanguageId.PYTHON] = True
        events = []
        manager._runtime_event_sink = events.append
        server = MagicMock()
        server.diagnostics_generation.side_effect = [1, 2]
        server.diagnostic_document_version.return_value = 1
        server.wait_for_diagnostics = AsyncMock(
            return_value=[Diagnostic(line=1, character=1, message="late")]
        )
        manager._get_or_create_server = AsyncMock(return_value=server)
        route = DiagnosticRoute(
            file_path=path,
            agent_id="agent-1",
            session_generation=1,
        )
        manager.enqueue_diagnostics(path, route=route)
        inflight = manager._diagnostics_queue.pop()

        manager.advance_session_generation("agent-1", 2)
        asyncio.run(manager._handle_diagnostics_request(inflight))

        assert manager.pending_diagnostic_batches() == ()
        assert events == []


class TestDiagnosticReplacement:
    def test_clean_publish_is_retained_as_explicit_batch(
        self, manager: LspManager, tmp_path: Path
    ) -> None:
        import asyncio

        path = tmp_path / "main.py"
        path.write_text("x = 1")
        server = MagicMock()
        server.diagnostics_generation.side_effect = [1, 2]
        server.diagnostic_document_version.return_value = 2
        server.wait_for_diagnostics = AsyncMock(return_value=[])
        manager._get_or_create_server = AsyncMock(return_value=server)
        runtime_events = []
        manager._runtime_event_sink = runtime_events.append
        manager._availability[LanguageId.PYTHON] = True
        batch_id = manager.enqueue_diagnostics(
            path,
            route=DiagnosticRoute(
                file_path=path,
                agent_id="agent-1",
                session_generation=4,
                turn_id="turn-1",
                tool_call_id="tool-1",
            ),
        )
        request = manager._diagnostics_queue.pop()

        asyncio.run(manager._handle_diagnostics_request(request))

        batches = manager.pending_diagnostic_batches(batch_id=batch_id)
        assert len(batches) == 1
        assert batches[0].block.items == []
        assert batches[0].document_version == 2
        assert batches[0].diagnostic_generation == 2
        assert len(runtime_events) == 1
        assert isinstance(runtime_events[0].payload, DiagnosticsCleared)
        assert runtime_events[0].agent_id == "agent-1"
        assert runtime_events[0].correlation_id == "tool-1"
        server.wait_for_diagnostics.assert_awaited_once_with(
            path,
            timeout=manager.config.poll_timeout_ms / 1000,
            after_generation=1,
        )

    def test_non_empty_batch_publishes_typed_runtime_diagnostics(
        self, manager: LspManager
    ) -> None:
        events = []
        manager._runtime_event_sink = events.append
        batch = DiagnosticBatch(
            batch_id="batch-errors",
            route=DiagnosticRoute(
                file_path=Path("/tmp/main.py"),
                agent_id="agent-1",
                session_generation=2,
                session_id="session-1",
                turn_id="turn-1",
                tool_call_id="tool-1",
            ),
            request_sequence=1,
            document_version=3,
            diagnostic_generation=5,
            block=DiagnosticBlock(
                file_path="main.py",
                items=[Diagnostic(line=2, character=4, message="broken")],
            ),
        )

        manager._publish_diagnostic_event(batch)

        assert len(events) == 1
        event = events[0]
        assert isinstance(event.payload, DiagnosticsPublished)
        assert event.payload.batch_id == "batch-errors"
        assert event.payload.diagnostics[0].message == "broken"
        assert event.session_generation == 2

    def test_stale_request_cannot_publish_after_newer_request(
        self, manager: LspManager, tmp_path: Path
    ) -> None:
        import asyncio

        path = tmp_path / "main.py"
        path.write_text("x = 1")
        server = MagicMock()
        server.diagnostics_generation.side_effect = [4, 5]
        server.diagnostic_document_version.return_value = 7
        server.wait_for_diagnostics = AsyncMock(
            return_value=[Diagnostic(line=1, character=1, message="stale")]
        )
        manager._get_or_create_server = AsyncMock(return_value=server)
        manager._availability[LanguageId.PYTHON] = True
        route = DiagnosticRoute(
            file_path=path,
            agent_id="agent-1",
            session_generation=1,
        )
        stale_id = manager.enqueue_diagnostics(path, route=route)
        stale_request = manager._diagnostics_queue.pop()
        current_id = manager.enqueue_diagnostics(path, route=route)

        asyncio.run(manager._handle_diagnostics_request(stale_request))

        assert manager.pending_diagnostic_batches(batch_id=stale_id) == ()
        assert current_id is not None

    def test_timeout_does_not_publish_false_clean_batch(
        self, manager: LspManager, tmp_path: Path
    ) -> None:
        import asyncio

        path = tmp_path / "main.py"
        path.write_text("x = 1")
        server = MagicMock()
        server.diagnostics_generation.return_value = 8
        server.wait_for_diagnostics = AsyncMock(return_value=[])
        manager._get_or_create_server = AsyncMock(return_value=server)
        manager._availability[LanguageId.PYTHON] = True
        batch_id = manager.enqueue_diagnostics(path)
        request = manager._diagnostics_queue.pop()

        asyncio.run(manager._handle_diagnostics_request(request))

        assert manager.pending_diagnostic_batches(batch_id=batch_id) == ()
