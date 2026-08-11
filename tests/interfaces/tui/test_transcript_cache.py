from __future__ import annotations

import threading

from reuleauxcoder.domain.runtime.performance import RuntimePerformanceMonitor
from reuleauxcoder.interfaces.tui import event_adapter as event_adapter_module
from reuleauxcoder.interfaces.tui import transcript_cache as cache_module
from reuleauxcoder.interfaces.tui.event_adapter import MiniTUIEventAdapter


def _populated_adapter(
    *,
    cell_count: int = 64,
    monitor: RuntimePerformanceMonitor | None = None,
    incident_sink=None,
    batch_size: int = 8,
) -> MiniTUIEventAdapter:
    adapter = MiniTUIEventAdapter(
        performance_monitor=monitor,
        incident_sink=incident_sink,
        transcript_prewarm_batch_size=batch_size,
    )
    adapter.append_restored_conversation(
        [
            {
                "role": "assistant" if index % 2 else "user",
                "content": f"row {index} · 中文 🚀 **markdown**",
            }
            for index in range(cell_count)
        ]
    )
    adapter.transcript_layout(100)
    return adapter


def test_resize_prewarm_is_single_flight_and_rejects_stale_generation(
    monkeypatch,
) -> None:
    monitor = RuntimePerformanceMonitor()
    adapter = _populated_adapter(monitor=monitor)
    initial = adapter.transcript_layout(100)
    original = cache_module.cell_fragments
    first_resize_started = threading.Event()
    release_first_resize = threading.Event()
    counter_lock = threading.Lock()
    active = 0
    maximum_active = 0

    def controlled_fragments(cell, *, width, markdown_renderer):
        nonlocal active, maximum_active
        with counter_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            if width == 70 and not first_resize_started.is_set():
                first_resize_started.set()
                assert release_first_resize.wait(2.0)
            return original(
                cell,
                width=width,
                markdown_renderer=markdown_renderer,
            )
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(cache_module, "cell_fragments", controlled_fragments)

    pending, scroll = adapter.transcript_layout_rebased(70, 5)
    assert pending is initial
    assert scroll == 5
    assert first_resize_started.wait(1.0)

    superseded, _ = adapter.transcript_layout_rebased(60, 5)
    assert superseded is initial
    release_first_resize.set()
    assert adapter.wait_for_transcript_prewarm(3.0)

    current = adapter.transcript_layout(60)
    assert current is not initial
    assert all(cell.key[2] == 60 for cell in current.cells)
    stats = adapter.transcript_cache_stats()
    assert stats.submitted == 2
    assert stats.completed == 1
    assert stats.stale_rejected == 1
    assert stats.workers_started == 1
    assert stats.batches >= 8
    assert stats.cache_misses >= 64
    assert stats.in_flight is False
    assert maximum_active == 1

    samples = monitor.snapshot(category="tui_cache")
    assert [sample.status for sample in samples] == ["stale", "ok"]
    assert samples[-1].attribute_map()["generation"] == stats.generation
    assert samples[-1].attribute_map()["render_rows"] >= 64


def test_resize_prewarm_reuses_cached_width_and_reports_hits() -> None:
    monitor = RuntimePerformanceMonitor()
    adapter = _populated_adapter(cell_count=32, monitor=monitor)

    adapter.transcript_layout_rebased(70, 0)
    assert adapter.wait_for_transcript_prewarm(2.0)
    adapter.transcript_layout_rebased(100, 0)
    assert adapter.wait_for_transcript_prewarm(2.0)

    layout = adapter.transcript_layout(100)
    assert all(cell.key[2] == 100 for cell in layout.cells)
    latest = monitor.snapshot(category="tui_cache")[-1]
    attributes = latest.attribute_map()
    assert attributes["cache_hits"] == 32
    assert attributes["cache_misses"] == 0


def test_resize_prewarm_failure_is_nonfatal_and_visible_to_agent(
    monkeypatch,
) -> None:
    facts = []

    def incident_sink(phase, error_type, ref, count=1, **_route):
        facts.append((phase, error_type, ref, count))
        return True

    monitor = RuntimePerformanceMonitor()
    adapter = _populated_adapter(
        cell_count=8,
        monitor=monitor,
        incident_sink=incident_sink,
        batch_size=2,
    )

    def broken_fragments(*_args, **_kwargs):
        raise RuntimeError("render-content-secret")

    monkeypatch.setattr(cache_module, "cell_fragments", broken_fragments)
    old_layout, _ = adapter.transcript_layout_rebased(70, 0)
    assert old_layout is adapter._transcript_layout
    assert adapter.wait_for_transcript_prewarm(2.0)

    stats = adapter.transcript_cache_stats()
    assert stats.failures == 1
    assert stats.completed == 0
    assert facts == [("ui_projection", "RuntimeError", "layout_prewarm", 1)]
    sample = monitor.snapshot(category="tui_cache")[-1]
    assert sample.status == "error"
    assert sample.attribute_map()["error_type"] == "RuntimeError"
    assert "render-content-secret" not in repr(sample)

    monkeypatch.setattr(cache_module, "cell_fragments", event_adapter_module._cell_fragments)
    recovered = adapter.transcript_layout(70)
    assert all(cell.key[2] == 70 for cell in recovered.cells)


def test_session_clear_rejects_late_resize_result(monkeypatch) -> None:
    adapter = _populated_adapter(cell_count=16, batch_size=4)
    original = cache_module.cell_fragments
    started = threading.Event()
    release = threading.Event()

    def blocked_fragments(cell, *, width, markdown_renderer):
        if not started.is_set():
            started.set()
            assert release.wait(2.0)
        return original(
            cell,
            width=width,
            markdown_renderer=markdown_renderer,
        )

    monkeypatch.setattr(cache_module, "cell_fragments", blocked_fragments)
    adapter.transcript_layout_rebased(70, 0)
    assert started.wait(1.0)

    adapter.clear_transcript()
    release.set()
    assert adapter.wait_for_transcript_prewarm(2.0)

    assert adapter.transcript.state.transcript.cells == ()
    assert adapter.transcript_layout(70).cells == ()
    stats = adapter.transcript_cache_stats()
    assert stats.completed == 0
    assert stats.stale_rejected == 1
