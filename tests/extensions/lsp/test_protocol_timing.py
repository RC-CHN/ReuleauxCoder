"""Deterministic end-to-end LSP protocol timing tests over real stdio."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from reuleauxcoder.extensions.lsp.config import LspConfig, LspServerOverride
from reuleauxcoder.extensions.lsp.diagnostics import DiagnosticRoute
from reuleauxcoder.extensions.lsp.manager import LspManager
from reuleauxcoder.extensions.lsp.registry import LanguageId

FAKE_SERVER = Path(__file__).with_name("fake_stdio_server.py")


def _server_args(
    mode: str,
    log_path: Path,
    *,
    first_save_gate: Path | None = None,
) -> list[str]:
    args = [
        "-u",
        str(FAKE_SERVER),
        "--mode",
        mode,
        "--log",
        str(log_path),
    ]
    if first_save_gate is not None:
        args.extend(("--block-first-save-until", str(first_save_gate)))
    return args


def _events(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for deterministic LSP event")


def _manager(
    tmp_path: Path,
    log_path: Path,
    *,
    mode: str,
    first_save_gate: Path | None = None,
) -> LspManager:
    config = LspConfig(
        enabled=True,
        poll_timeout_ms=3_000,
        server_overrides={
            "python": LspServerOverride(
                language="python",
                cmd=sys.executable,
                args=_server_args(mode, log_path, first_save_gate=first_save_gate),
            )
        },
    )
    manager = LspManager(config, workspace_cwd=tmp_path)
    manager._availability[LanguageId.PYTHON] = True
    manager.start_worker()
    return manager


def _route(path: Path, *, turn: str, tool: str) -> DiagnosticRoute:
    return DiagnosticRoute(
        file_path=path,
        agent_id="parent",
        session_generation=1,
        session_id="session",
        turn_id=turn,
        tool_call_id=tool,
    )


def _wait_for_batch(manager: LspManager, batch_id: str):
    batches = ()

    def ready() -> bool:
        nonlocal batches
        batches = manager.pending_diagnostic_batches(batch_id=batch_id)
        return bool(batches)

    _wait_until(ready)
    return batches[0]


def test_save_only_server_publishes_after_sync_then_save(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    path.write_text("# FAKE_LSP_ERROR: save-only\n", encoding="utf-8")
    log_path = tmp_path / "save-only.jsonl"
    manager = _manager(tmp_path, log_path, mode="save-only")
    try:
        batch_id = manager.enqueue_diagnostics(
            path,
            route=_route(path, turn="turn-1", tool="edit-1"),
            document_committed=True,
        )
        assert batch_id is not None

        batch = _wait_for_batch(manager, batch_id)

        assert [item.message for item in batch.block.items] == ["save-only"]
        protocol = [
            event["method"]
            for event in _events(log_path)
            if event["method"]
            in {
                "textDocument/didOpen",
                "textDocument/didChange",
                "textDocument/didSave",
                "textDocument/publishDiagnostics",
            }
        ]
        assert protocol == [
            "textDocument/didOpen",
            "textDocument/didSave",
            "textDocument/publishDiagnostics",
        ]
    finally:
        manager.shutdown_all()
    assert manager._worker_thread is None
    assert manager._transports == {}


def test_push_on_change_is_observed_after_document_commit(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    path.write_text("# FAKE_LSP_ERROR: first push\n", encoding="utf-8")
    log_path = tmp_path / "push.jsonl"
    manager = _manager(tmp_path, log_path, mode="push")
    try:
        batch_id = manager.enqueue_diagnostics(
            path,
            route=_route(path, turn="turn-1", tool="edit-1"),
            document_committed=True,
        )
        assert batch_id is not None

        batch = _wait_for_batch(manager, batch_id)

        assert [item.message for item in batch.block.items] == ["first push"]
        assert batch.diagnostic_generation == 1
        assert manager.acknowledge_diagnostic_batch(
            batch_id,
            consumer_id="test",
        )

        path.write_text("# FAKE_LSP_ERROR: second push\n", encoding="utf-8")
        second_id = manager.enqueue_diagnostics(
            path,
            route=_route(path, turn="turn-2", tool="edit-2"),
            document_committed=True,
        )
        assert second_id is not None
        second = _wait_for_batch(manager, second_id)

        assert [item.message for item in second.block.items] == ["second push"]
        assert second.diagnostic_generation == 2
        assert second.document_version == 2
        protocol = [
            event["method"]
            for event in _events(log_path)
            if event["method"]
            in {
                "textDocument/didOpen",
                "textDocument/didChange",
                "textDocument/didSave",
                "textDocument/publishDiagnostics",
            }
        ]
        assert protocol == [
            "textDocument/didOpen",
            "textDocument/publishDiagnostics",
            "textDocument/didSave",
            "textDocument/didChange",
            "textDocument/publishDiagnostics",
            "textDocument/didSave",
        ]
    finally:
        manager.shutdown_all()
    assert manager._worker_thread is None
    assert manager._transports == {}


def test_pull_server_handles_full_unchanged_and_clean(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    path.write_text("# FAKE_LSP_ERROR: pull error\n", encoding="utf-8")
    log_path = tmp_path / "pull.jsonl"
    manager = _manager(tmp_path, log_path, mode="pull")
    try:
        first_id = manager.enqueue_diagnostics(
            path,
            route=_route(path, turn="turn-1", tool="edit-1"),
            document_committed=True,
        )
        assert first_id is not None
        first = _wait_for_batch(manager, first_id)
        assert [item.message for item in first.block.items] == ["pull error"]
        assert first.diagnostic_generation == 2
        assert manager.acknowledge_diagnostic_batch(first_id, consumer_id="test")

        path.write_text("clean = True\n", encoding="utf-8")
        clean_id = manager.enqueue_diagnostics(
            path,
            route=_route(path, turn="turn-2", tool="edit-2"),
            document_committed=True,
        )
        assert clean_id is not None
        clean = _wait_for_batch(manager, clean_id)
        assert clean.block.items == []
        assert clean.diagnostic_generation == 4
        assert clean.document_version == 2
    finally:
        manager.shutdown_all()
    assert manager._worker_thread is None
    assert manager._transports == {}

    diagnostic_responses = [
        event
        for event in _events(log_path)
        if event["direction"] == "send"
        and event["method"] == "response:textDocument/diagnostic"
    ]
    assert [event["kind"] for event in diagnostic_responses] == [
        "full",
        "unchanged",
        "full",
        "unchanged",
    ]
    assert [event["result_id"] for event in diagnostic_responses] == [
        "result-1",
        "result-1",
        "result-2",
        "result-2",
    ]
    assert [event["item_count"] for event in diagnostic_responses] == [1, 0, 0, 0]
    diagnostic_requests = [
        event
        for event in _events(log_path)
        if event["direction"] == "recv"
        and event["method"] == "textDocument/diagnostic"
    ]
    assert [event.get("previous_result_id") for event in diagnostic_requests] == [
        None,
        "result-1",
        "result-1",
        "result-2",
    ]


def test_rapid_commits_never_publish_obsolete_batch(tmp_path: Path) -> None:
    path = tmp_path / "main.py"
    path.write_text("# FAKE_LSP_ERROR: first\n", encoding="utf-8")
    log_path = tmp_path / "rapid.jsonl"
    release_gate = tmp_path / "release-first-save"
    manager = _manager(
        tmp_path,
        log_path,
        mode="save-only",
        first_save_gate=release_gate,
    )
    try:
        first_id = manager.enqueue_diagnostics(
            path,
            route=_route(path, turn="turn-1", tool="edit-1"),
            document_committed=True,
        )
        assert first_id is not None
        _wait_until(
            lambda: any(
                event["direction"] == "state"
                and event["method"] == "first_save_blocked"
                for event in _events(log_path)
            )
        )

        path.write_text("# FAKE_LSP_ERROR: second\n", encoding="utf-8")
        second_id = manager.enqueue_diagnostics(
            path,
            route=_route(path, turn="turn-2", tool="edit-2"),
            document_committed=True,
        )
        assert second_id is not None
        release_gate.touch()

        batch = _wait_for_batch(manager, second_id)

        assert batch.batch_id == second_id
        assert batch.route.tool_call_id == "edit-2"
        assert [item.message for item in batch.block.items] == ["second"]
        assert manager.pending_diagnostic_batches(batch_id=first_id) == ()
        assert manager.diagnostic_batch_metrics()["stale_discarded"] == 1
        saves = [
            event
            for event in _events(log_path)
            if event["direction"] == "recv"
            and event["method"] == "textDocument/didSave"
        ]
        assert len(saves) == 2
    finally:
        release_gate.touch(exist_ok=True)
        manager.shutdown_all()
    assert manager._worker_thread is None
    assert manager._transports == {}
