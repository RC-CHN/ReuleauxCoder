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
from unittest.mock import MagicMock, patch

import pytest

from reuleauxcoder.extensions.lsp.client import LspClientError
from reuleauxcoder.extensions.lsp.config import LspConfig, LspServerOverride
from reuleauxcoder.extensions.lsp.manager import (
    MAX_RESPWANS,
    LspHealthReport,
    LspManager,
    ToolRequest,
)
from reuleauxcoder.extensions.lsp.registry import LanguageId


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
    def test_health_check_caches_availability(
        self, manager: LspManager
    ) -> None:
        report = manager.health_check()
        assert report.total == 9  # 9 supported languages
        assert isinstance(report.available, int)
        # Health check should populate _availability
        with manager._lock:
            assert len(manager._availability) == 9

    def test_health_report_has_language_entries(
        self, manager: LspManager
    ) -> None:
        report = manager.health_check()
        for lang_name, available, details in report.languages:
            assert isinstance(lang_name, str)
            assert isinstance(available, bool)
            assert isinstance(details, str)


class TestReSpawnLimit:
    def test_re_spawn_increments_counter(
        self, manager: LspManager
    ) -> None:
        manager._availability[LanguageId.PYTHON] = True
        manager._re_spawn_counts[LanguageId.PYTHON] = 0

        # First re-spawn: should succeed when count < MAX_RESPWANS
        # (but actual spawn will fail since we have no real server)
        result = manager._re_spawn(LanguageId.PYTHON, Path("/tmp/test.py"))
        # re-spawn calls _spawn_blocking which fails due to unavailable server
        # But the counter should be incremented
        assert manager._re_spawn_counts.get(LanguageId.PYTHON, 0) >= 1

    def test_re_spawn_limit_disables_language(
        self, manager: LspManager
    ) -> None:
        manager._availability[LanguageId.PYTHON] = True
        manager._re_spawn_counts[LanguageId.PYTHON] = MAX_RESPWANS  # at limit

        result = manager._re_spawn(LanguageId.PYTHON, Path("/tmp/test.py"))
        assert result is None
        with manager._lock:
            assert manager._availability[LanguageId.PYTHON] is False


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
        manager._last_sync_time[(LanguageId.PYTHON, f)] = future_mtime
        assert manager._check_stale(LanguageId.PYTHON, f) is False

    def test_file_stale_after_edit(
        self, manager: LspManager, tmp_path: Path
    ) -> None:
        f = tmp_path / "test.py"
        f.write_text("old")
        manager._last_sync_time[(LanguageId.PYTHON, f)] = f.stat().st_mtime
        time.sleep(0.01)  # ensure mtime changes
        f.write_text("new")
        assert manager._check_stale(LanguageId.PYTHON, f) is True

    def test_missing_file_not_stale(
        self, manager: LspManager
    ) -> None:
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
    def test_disabled_when_config_disabled(
        self, manager: LspManager
    ) -> None:
        manager._config.enabled = False
        assert manager._enabled_for_file(Path("/tmp/test.py")) is False

    def test_disabled_when_unsupported_extension(
        self, manager: LspManager
    ) -> None:
        manager._config.enabled = True
        assert manager._enabled_for_file(Path("/tmp/notes.txt")) is False

    def test_disabled_when_language_unavailable(
        self, manager: LspManager
    ) -> None:
        manager._config.enabled = True
        with manager._lock:
            manager._availability[LanguageId.PYTHON] = False
        assert manager._enabled_for_file(Path("/tmp/test.py")) is False

    def test_enabled_when_all_conditions_met(
        self, manager: LspManager
    ) -> None:
        manager._config.enabled = True
        with manager._lock:
            manager._availability[LanguageId.PYTHON] = True
        assert manager._enabled_for_file(Path("/tmp/test.py")) is True


class TestRelativizePath:
    def test_within_workspace(self, manager: LspManager) -> None:
        mgr = LspManager(LspConfig(), workspace_cwd=Path("/home/user/proj"))
        assert mgr._relativize_path(Path("/home/user/proj/src/main.py")) == "src/main.py"

    def test_outside_workspace(self, manager: LspManager) -> None:
        mgr = LspManager(LspConfig(), workspace_cwd=Path("/home/user/proj"))
        path = mgr._relativize_path(Path("/etc/passwd"))
        assert path == "passwd"


class TestSendRequestSyncValidation:
    def test_raises_for_unsupported_file(
        self, manager: LspManager
    ) -> None:
        with pytest.raises(LspClientError, match="No LSP support"):
            manager.send_request_sync(
                Path("/tmp/notes.txt"),
                "textDocument/definition",
                {},
            )

    def test_raises_when_server_unavailable(
        self, manager: LspManager
    ) -> None:
        with manager._lock:
            manager._availability[LanguageId.PYTHON] = True
        # Server not spawned → _ensure_server_ready returns None
        with pytest.raises(LspClientError, match="No LSP server available"):
            manager.send_request_sync(
                Path("/tmp/test.py"),
                "textDocument/definition",
                {},
            )


class TestEnqueueDiagnostics:
    def test_enqueue_when_enabled(
        self, manager: LspManager
    ) -> None:
        manager._config.enabled = True
        with manager._lock:
            manager._availability[LanguageId.PYTHON] = True

        assert len(manager._diagnostics_queue) == 0
        manager.enqueue_diagnostics(Path("/tmp/test.py"), seq=1)
        assert len(manager._diagnostics_queue) == 1

    def test_no_enqueue_when_disabled(
        self, manager: LspManager
    ) -> None:
        manager._config.enabled = False
        manager.enqueue_diagnostics(Path("/tmp/test.py"), seq=1)
        assert len(manager._diagnostics_queue) == 0


class TestDrainDiagnostics:
    def test_drain_clears_results(
        self, manager: LspManager
    ) -> None:
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
    def test_enqueues_notification(
        self, manager: LspManager
    ) -> None:
        manager._config.enabled = True
        with manager._lock:
            manager._availability[LanguageId.PYTHON] = True

        assert len(manager._notification_queue) == 0
        manager.notify_did_save(Path("/tmp/test.py"))
        assert len(manager._notification_queue) == 1
        kind, path = manager._notification_queue[0]
        assert kind == "did_save"
        assert path == Path("/tmp/test.py")
