"""Tests for lazy LSP initialization in the application runner."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from reuleauxcoder.domain.config.models import Config
from reuleauxcoder.domain.hooks.registry import HookRegistry
from reuleauxcoder.extensions.lsp.manager import LspManager
from reuleauxcoder.interfaces.entrypoint.runner import AppRunner
from reuleauxcoder.interfaces.events import UIEventBus


def test_init_lsp_registers_without_path_probe_or_worker() -> None:
    runner = AppRunner()
    hook_registry = MagicMock(spec=HookRegistry)
    bind_lsp_manager = MagicMock()
    agent = SimpleNamespace(
        hook_registry=hook_registry,
        lsp_manager=None,
        tools=[
            SimpleNamespace(
                backend_id="local",
                bind_lsp_manager=bind_lsp_manager,
            )
        ],
    )
    ui_bus = UIEventBus()

    with (
        patch(
            "reuleauxcoder.extensions.lsp.manager.shutil.which",
            side_effect=AssertionError("startup must not probe PATH"),
        ) as which,
        patch.object(
            LspManager,
            "health_check",
            side_effect=AssertionError("startup must not run a health check"),
        ) as health_check,
    ):
        runner._init_lsp(Config(lsp={"enabled": True}), agent, ui_bus)

    manager = runner._lsp_manager
    assert manager is not None
    assert agent.lsp_manager is manager
    assert manager._worker_thread is None
    which.assert_not_called()
    health_check.assert_not_called()
    hook_registry.bind_runtime_service.assert_called_once_with(
        "lsp_manager", manager
    )
    bind_lsp_manager.assert_called_once_with(manager)

    messages = tuple(event.message for event in ui_bus.history_snapshot())
    assert messages == (
        "LSP: 9 language integrations configured; "
        "servers start on first supported file or LSP query.",
    )
    assert all("ready" not in message.lower() for message in messages)
