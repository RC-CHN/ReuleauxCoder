"""Content-free LSP performance observations over deterministic stdio."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from reuleauxcoder.domain.runtime.performance import RuntimePerformanceMonitor
from reuleauxcoder.extensions.lsp.client import LspClient, LspRequestTimedOut
from reuleauxcoder.extensions.lsp.config import LspConfig, LspServerOverride
from reuleauxcoder.extensions.lsp.diagnostics import DiagnosticRoute
from reuleauxcoder.extensions.lsp.manager import LspManager
from reuleauxcoder.extensions.lsp.registry import LanguageId

FAKE_SERVER = Path(__file__).with_name("fake_stdio_server.py")
CORE_PHASES = {
    "availability_lookup",
    "queue_wait",
    "spawn",
    "initialize",
    "document_sync",
    "request",
    "diagnostics_wait",
    "shutdown",
    "total",
}
SAFE_ATTRIBUTE_KEYS = {
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


def _manager(
    tmp_path: Path,
    *,
    mode: str,
    poll_timeout_ms: int = 200,
    extra_args: tuple[str, ...] = (),
) -> tuple[LspManager, RuntimePerformanceMonitor]:
    log_path = tmp_path / f"{mode}-trace.jsonl"
    manager = LspManager(
        LspConfig(
            poll_timeout_ms=poll_timeout_ms,
            server_overrides={
                "python": LspServerOverride(
                    language="python",
                    cmd=sys.executable,
                    args=[
                        "-u",
                        str(FAKE_SERVER),
                        "--mode",
                        mode,
                        "--log",
                        str(log_path),
                        *extra_args,
                    ],
                )
            },
        ),
        workspace_cwd=tmp_path,
    )
    monitor = RuntimePerformanceMonitor()
    manager.performance_monitor = monitor
    manager._availability[LanguageId.PYTHON] = True
    return manager, monitor


def _wait_for_diagnostic_result(
    manager: LspManager,
    batch_id: str,
    *,
    timeout: float = 3.0,
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = manager.diagnostic_request_result(batch_id)
        if result is not None:
            return result
        time.sleep(0.01)
    raise AssertionError("timed out waiting for deterministic diagnostics result")


def test_lsp_performance_records_core_phases_without_request_payloads(
    tmp_path: Path,
) -> None:
    source_secret = "SOURCE-CONTENT-MUST-NOT-BE-OBSERVED"
    method_secret = "private/raw-custom-method"
    parameter_secret = "PRIVATE-REQUEST-PARAMETER"
    route_secrets = (
        "private-agent-id",
        "private-session-id",
        "private-turn-id",
        "private-tool-call-id",
    )
    path = tmp_path / "private-source-name.py"
    path.write_text(f"value = {source_secret!r}\n", encoding="utf-8")
    manager, monitor = _manager(tmp_path, mode="pull")

    try:
        assert (
            manager.send_request_sync(
                path,
                method_secret,
                {"private_argument": parameter_secret},
                timeout=2.0,
            )
            is None
        )
        path.write_text(
            f"value = {source_secret!r}\nchanged = True\n", encoding="utf-8"
        )
        batch_id = manager.enqueue_diagnostics(
            path,
            route=DiagnosticRoute(
                file_path=path,
                agent_id=route_secrets[0],
                session_generation=7,
                session_id=route_secrets[1],
                turn_id=route_secrets[2],
                tool_call_id=route_secrets[3],
            ),
            document_committed=True,
        )
        assert batch_id is not None
        assert _wait_for_diagnostic_result(manager, batch_id)
    finally:
        assert manager.shutdown_all(timeout=2.0)

    samples = monitor.snapshot(category="lsp")
    assert CORE_PHASES <= {sample.name for sample in samples}
    assert all(sample.category == "lsp" for sample in samples)
    assert all(sample.elapsed_ms >= 0 for sample in samples)
    assert all(set(sample.attribute_map()) <= SAFE_ATTRIBUTE_KEYS for sample in samples)
    for sample in samples:
        attributes = sample.attribute_map()
        assert {
            "language",
            "root_hash",
            "transport_generation",
            "launcher",
        } <= set(attributes)
        assert len(str(attributes["root_hash"])) == 12

    request_attributes = [
        sample.attribute_map() for sample in samples if sample.name == "request"
    ]
    assert request_attributes
    assert all(
        attributes.get("request_kind") == "other" for attributes in request_attributes
    )

    queue_attributes = [
        sample.attribute_map() for sample in samples if sample.name == "queue_wait"
    ]
    assert {attributes.get("work_kind") for attributes in queue_attributes} >= {
        "tool",
        "diagnostics",
    }
    assert all(
        isinstance(attributes.get("depth"), int) for attributes in queue_attributes
    )

    observed = repr(
        [(sample.name, sample.status, sample.attribute_map()) for sample in samples]
    )
    forbidden = (
        str(tmp_path),
        str(path),
        str(FAKE_SERVER),
        source_secret,
        method_secret,
        parameter_secret,
        *route_secrets,
    )
    assert all(secret not in observed for secret in forbidden)


def test_diagnostics_wait_is_timeout_when_generation_does_not_advance(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unchanged.py"
    path.write_text("value = 1\n", encoding="utf-8")
    manager, monitor = _manager(tmp_path, mode="push", poll_timeout_ms=100)

    try:
        # didOpen produces the baseline push generation.  The unchanged follow-up
        # does not sync again, so no later publish can advance that generation.
        assert manager.send_request_sync(path, "test/warm", {}, timeout=2.0) is None
        monitor.clear()

        batch_id = manager.enqueue_diagnostics(path)
        assert batch_id is not None
        assert _wait_for_diagnostic_result(manager, batch_id) == ()
    finally:
        assert manager.shutdown_all(timeout=2.0)

    waits = [
        sample
        for sample in monitor.snapshot(category="lsp")
        if sample.name == "diagnostics_wait"
    ]
    assert len(waits) == 1
    assert waits[0].status == "timeout"


def test_cancelled_phase_and_unreaped_shutdown_have_truthful_statuses(
    tmp_path: Path,
) -> None:
    manager, monitor = _manager(tmp_path, mode="pull")
    path = tmp_path / "status.py"
    key = manager._transport_key(LanguageId.PYTHON, path)

    async def cancel_request() -> None:
        raise asyncio.CancelledError

    async def observe_cancel() -> None:
        try:
            await manager._observe_lsp_phase(
                "request",
                cancel_request(),
                transport_key=key,
            )
        except asyncio.CancelledError:
            pass

    class UnreapedClient:
        is_usable = False
        is_alive = True

        async def abort(self) -> None:
            return None

    async def observe_shutdown() -> None:
        await manager._close_transport_observed(key, UnreapedClient())  # type: ignore[arg-type]

    asyncio.run(observe_cancel())
    asyncio.run(observe_shutdown())

    request = next(
        sample
        for sample in monitor.snapshot(category="lsp")
        if sample.name == "request"
    )
    shutdown = next(
        sample
        for sample in monitor.snapshot(category="lsp")
        if sample.name == "shutdown"
    )
    assert request.status == "cancelled"
    assert request.attribute_map()["error_type"] == "CancelledError"
    assert shutdown.status == "incomplete"
    assert shutdown.attribute_map()["outcome"] == "unreaped"


def test_active_caller_timeout_is_not_reported_as_cancellation(tmp_path: Path) -> None:
    path = tmp_path / "timeout.py"
    path.write_text("value = 1\n", encoding="utf-8")
    release = tmp_path / "release-blocked-request"
    manager, monitor = _manager(
        tmp_path,
        mode="push",
        extra_args=(
            "--block-method",
            "test/blocked",
            "--block-until",
            str(release),
        ),
    )

    try:
        assert manager.send_request_sync(path, "test/warm", {}, timeout=2.0) is None
        monitor.clear()
        with pytest.raises(LspRequestTimedOut):
            manager.send_request_sync(path, "test/blocked", {}, timeout=0.2)
    finally:
        release.touch()
        assert manager.shutdown_all(timeout=2.0)

    totals = [
        sample
        for sample in monitor.snapshot(category="lsp")
        if sample.name == "total" and sample.attribute_map().get("work_kind") == "tool"
    ]
    assert len(totals) == 1
    assert totals[0].status == "timeout"
    assert totals[0].attribute_map()["outcome"] == "deadline_exhausted"


def test_observation_metadata_failure_never_changes_operation_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, monitor = _manager(tmp_path, mode="pull")
    non_utf8_root = Path(os.fsdecode(b"/tmp/lsp-root-\xff"))
    key = (LanguageId.PYTHON, non_utf8_root)
    assert len(manager._workspace_identifier(non_utf8_root)) == 12

    def fail_metadata(_root: Path) -> str:
        raise UnicodeError("deterministic metadata failure")

    monkeypatch.setattr(manager, "_workspace_identifier", fail_metadata)

    async def observed() -> int:
        return await manager._observe_lsp_phase(
            "request",
            asyncio.sleep(0, result=42),
            transport_key=key,
        )

    assert asyncio.run(observed()) == 42
    assert monitor.snapshot(category="lsp") == ()


def test_client_request_timeout_is_observed_as_timeout(tmp_path: Path) -> None:
    manager, monitor = _manager(tmp_path, mode="pull")
    key = manager._transport_key(LanguageId.PYTHON, tmp_path / "timeout.py")
    client = LspClient(LanguageId.PYTHON, tmp_path)
    client._process = SimpleNamespace(stdin=object())  # type: ignore[assignment]
    client._write_message = AsyncMock()  # type: ignore[method-assign]

    async def observed() -> None:
        await manager._observe_lsp_phase(
            "request",
            client._send_request("test/timeout", {}, timeout=0.001),
            transport_key=key,
        )

    with pytest.raises(LspRequestTimedOut):
        asyncio.run(observed())

    sample = monitor.snapshot(category="lsp")[-1]
    assert sample.name == "request"
    assert sample.status == "timeout"
    assert sample.attribute_map()["error_type"] == "LspRequestTimedOut"


def test_availability_samples_distinguish_lookup_and_negative_cache(
    tmp_path: Path,
) -> None:
    manager, monitor = _manager(tmp_path, mode="pull")
    manager._availability.clear()
    manager._command_lookup = lambda _command: None
    key = manager._transport_key(LanguageId.PYTHON, tmp_path / "missing.py")

    assert not manager._command_available(key, "missing-lsp")
    assert not manager._command_available(key, "missing-lsp")

    samples = [
        sample
        for sample in monitor.snapshot(category="lsp")
        if sample.name == "availability_lookup"
    ]
    assert [sample.status for sample in samples] == ["unavailable", "unavailable"]
    assert [sample.attribute_map()["cache_result"] for sample in samples] == [
        "miss",
        "negative_hit",
    ]
