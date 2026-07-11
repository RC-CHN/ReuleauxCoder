"""Tests for LspManager — synchronous surface and helper methods.

Tests that require actual LSP subprocess communication (async worker,
spawn/initialize) are deferred to integration tests.

This module tests:
- Health check caching
- Server lifecycle state transitions (re-spawn limit)
- File staleness detection
- Config override resolution
- Relativize path
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from reuleauxcoder.extensions.lsp.client import LspClientError
from reuleauxcoder.extensions.lsp.config import LspConfig, LspServerOverride
from reuleauxcoder.extensions.lsp.manager import (
    MAX_RESPWANS,
    LspManager,
)
from reuleauxcoder.extensions.lsp.registry import LanguageId


def _make_mock_client(*, alive: bool = True) -> MagicMock:
    """Build a mock LspClient for lifecycle tests."""
    from unittest.mock import MagicMock

    c = MagicMock()
    c.is_alive = alive
    c.is_initialized = True
    c.shutdown = MagicMock()
    return c


@pytest.fixture
def manager() -> LspManager:
    """Create a manager with all languages marked unavailable."""
    config = LspConfig(enabled=True)
    mgr = LspManager(config, workspace_cwd=Path("/tmp"))
    # Mark all languages unavailable to avoid accidental spawn attempts
    for lang in LanguageId:
        with mgr._lock:
            mgr._availability[lang] = False
    return mgr


class TestHealthCheck:
    def test_health_check_caches_availability(self, manager: LspManager) -> None:
        report = manager.health_check()
        assert report.total == 9  # 9 supported languages
        assert isinstance(report.available, int)
        # Health check should populate _availability
        with manager._lock:
            assert len(manager._availability) == 9

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
                result = await manager._get_or_create_server(
                    LanguageId.PYTHON, path
                )
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
            return await manager._get_or_create_server(
                LanguageId.PYTHON, path
            )

        result = asyncio.run(run())
        assert result is None
        with manager._lock:
            assert manager._availability[LanguageId.PYTHON] is True


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
                await manager._get_or_create_server(
                    LanguageId.PYTHON, first_path
                ),
                await manager._get_or_create_server(
                    LanguageId.PYTHON, second_path
                ),
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

    def test_raises_when_server_unavailable(self, manager: LspManager) -> None:
        with pytest.raises(LspClientError, match="No LSP server available"):
            manager.send_request_sync(
                Path("/tmp/test.py"),
                "textDocument/definition",
                {},
                timeout=1.0,
            )
        manager.shutdown_all()


class TestEnqueueDiagnostics:
    def test_enqueue_when_enabled(self, manager: LspManager) -> None:
        manager._config.enabled = True
        with manager._lock:
            manager._availability[LanguageId.PYTHON] = True

        assert len(manager._diagnostics_queue) == 0
        manager.enqueue_diagnostics(Path("/tmp/test.py"), seq=1)
        assert len(manager._diagnostics_queue) == 1

    def test_no_enqueue_when_disabled(self, manager: LspManager) -> None:
        manager._config.enabled = False
        manager.enqueue_diagnostics(Path("/tmp/test.py"), seq=1)
        assert len(manager._diagnostics_queue) == 0


class TestDrainDiagnostics:
    def test_drain_clears_results(self, manager: LspManager) -> None:
        from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic, DiagnosticBlock

        block = DiagnosticBlock(
            file_path="test.py",
            items=[Diagnostic(line=1, character=1, message="err")],
        )
        with manager._lock:
            manager._results[Path("/tmp/test.py")] = block

        drained = manager.drain_diagnostics()
        assert len(drained) == 1
        assert drained[0].file_path == "test.py"
        # Should be empty after drain
        drained2 = manager.drain_diagnostics()
        assert len(drained2) == 0


class TestNotifyDidSave:
    def test_enqueues_notification(self, manager: LspManager) -> None:
        manager._config.enabled = True
        with manager._lock:
            manager._availability[LanguageId.PYTHON] = True

        assert len(manager._notification_queue) == 0
        manager.notify_did_save(Path("/tmp/test.py"))
        assert len(manager._notification_queue) == 1
        kind, path = manager._notification_queue[0]
        assert kind == "did_save"
        assert path == Path("/tmp/test.py")


class TestWorkerQueueOrdering:
    def test_pop_removes_exactly_one_queue_item(self, manager: LspManager) -> None:
        tool = object()
        manager._tool_queue.append(tool)
        manager._diagnostics_queue.append((Path("/tmp/a.py"), 1))
        manager._notification_queue.append(("did_save", Path("/tmp/a.py")))

        assert manager._pop_next_work() == ("tool", tool)
        assert len(manager._diagnostics_queue) == 1
        assert len(manager._notification_queue) == 1
        assert manager._pop_next_work()[0] == "diagnostics"
        assert len(manager._notification_queue) == 1
        assert manager._pop_next_work()[0] == "notification"
        assert manager._pop_next_work() == (None, None)

    def test_enqueue_replaces_older_pending_seq_for_same_file(
        self, manager: LspManager
    ) -> None:
        manager._config.enabled = True
        manager._availability[LanguageId.PYTHON] = True
        path = Path("/tmp/test.py")

        manager.enqueue_diagnostics(path, seq=1)
        manager.enqueue_diagnostics(path, seq=2)
        manager.enqueue_diagnostics(path, seq=1)

        assert manager._diagnostics_queue == [(path, 2)]


class TestDiagnosticReplacement:
    def test_clean_batch_replaces_old_result(
        self, manager: LspManager, tmp_path: Path
    ) -> None:
        import asyncio

        from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic, DiagnosticBlock

        path = tmp_path / "main.py"
        path.write_text("x = 1")
        server = MagicMock()
        server.diagnostics_generation.return_value = 1
        server.wait_for_diagnostics = AsyncMock(return_value=[])
        manager._get_or_create_server = AsyncMock(return_value=server)
        manager._latest_diagnostic_seq[path] = 2
        manager._results[path] = DiagnosticBlock(
            file_path="main.py",
            items=[Diagnostic(line=1, character=1, message="old")],
        )

        asyncio.run(manager._handle_diagnostics_request(path, 2))

        assert manager._results[path].items == []
        server.wait_for_diagnostics.assert_awaited_once_with(
            path,
            timeout=manager.config.poll_timeout_ms / 1000,
            after_generation=1,
        )

    def test_stale_seq_cannot_overwrite_newer_result(
        self, manager: LspManager, tmp_path: Path
    ) -> None:
        import asyncio

        from reuleauxcoder.extensions.lsp.diagnostics import Diagnostic, DiagnosticBlock

        path = tmp_path / "main.py"
        path.write_text("x = 1")
        server = MagicMock()
        server.diagnostics_generation.return_value = 4
        server.wait_for_diagnostics = AsyncMock(
            return_value=[Diagnostic(line=1, character=1, message="stale")]
        )
        manager._get_or_create_server = AsyncMock(return_value=server)
        manager._latest_diagnostic_seq[path] = 2
        current = DiagnosticBlock(
            file_path="main.py",
            items=[Diagnostic(line=1, character=1, message="new")],
        )
        manager._results[path] = current

        asyncio.run(manager._handle_diagnostics_request(path, 1))

        assert manager._results[path] is current
